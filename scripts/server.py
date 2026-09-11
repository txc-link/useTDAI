# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.0,<2"]
# ///
"""Local stdio MCP bridge for a remote TDAI MemoryCore REST service."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP


DEFAULT_SERVICE_ID = "default"
DATA_DIR = Path(
    os.environ.get("TDAI_LOCAL_DATA_DIR", "").strip()
    or Path.home() / ".codex" / "tdai-memory"
).expanduser()
STATE_PATH = DATA_DIR / "checkpoints.json"
PENDING_DIR = DATA_DIR / "pending"
WORKER_DIR = DATA_DIR / "workers"
HOOK_LOG_PATH = DATA_DIR / "hook-errors.log"
DEFAULT_BATCH_TURNS = 5
DEFAULT_IDLE_SECONDS = 180

_AMBIENT_BLOCK_RE = re.compile(
    r"<(?:in-app-browser-context|environment_context|recommended_plugins)\b[^>]*>.*?"
    r"</(?:in-app-browser-context|environment_context|recommended_plugins)>",
    re.IGNORECASE | re.DOTALL,
)
_API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{12,}")
_CREDENTIAL_RE = re.compile(
    r"(?i)(\b(?:password|passwd|pwd|api[_ -]?key|token|secret|userKey)\b|密码)"
    r"(\s*(?:is|是|[:=：])?\s*)([^\s,，;；]+)"
)
_LOW_VALUE_RE = re.compile(
    r"(?i)^(?:ok(?:ay)?|yes|yep|thanks?|done|好(?:的)?|可以|行|继续|收到|谢谢|明白|知道了|嗯|是|对(?:的)?)"
    r"[\s.!！?？。~～]*$"
)


def _redact(text: str) -> str:
    text = _AMBIENT_BLOCK_RE.sub("", text)
    text = _API_KEY_RE.sub("[REDACTED_API_KEY]", text)
    text = _BEARER_RE.sub(r"\1[REDACTED_TOKEN]", text)
    text = _CREDENTIAL_RE.sub(r"\1\2[REDACTED_CREDENTIAL]", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            # Keep only human-visible text. Claude/Pi/DSH may place tool results,
            # tool calls, or reasoning blocks in the same content array.
            if item.get("type") not in {None, "text", "input_text", "output_text"}:
                continue
            value = item.get("text")
            if not isinstance(value, str):
                value = item.get("content")
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def _load_state() -> dict[str, list[str]]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                str(k): [str(v) for v in values]
                for k, values in data.items()
                if isinstance(values, list)
            }
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


def _save_state(state: dict[str, list[str]]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix="checkpoints-", suffix=".json", dir=STATE_PATH.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
        os.replace(temp_name, STATE_PATH)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _message_id(role: str, text: str) -> str:
    return hashlib.sha256(f"{role}\0{text}".encode("utf-8")).hexdigest()


def _is_low_value(text: str) -> bool:
    return bool(_LOW_VALUE_RE.fullmatch(text.strip()))


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f"{path.stem}-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def _file_lock(path: Path, timeout: float = 5.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, str(os.getpid()).encode("ascii"))
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > 120:
                    path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for local TDAI state lock: {path.name}")
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(descriptor)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _pending_path(checkpoint_key: str) -> Path:
    return PENDING_DIR / f"{checkpoint_key}.json"


def _queue_lock_path(checkpoint_key: str) -> Path:
    return PENDING_DIR / f"{checkpoint_key}.lock"


def _worker_path(checkpoint_key: str) -> Path:
    return WORKER_DIR / f"{checkpoint_key}.json"


def _checkpoint_key(platform: str, session_id: str) -> str:
    identity = session_id if platform == "codex" else f"{platform}:{session_id}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _load_pending(checkpoint_key: str) -> dict[str, Any] | None:
    try:
        value = json.loads(_pending_path(checkpoint_key).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("messages"), list):
        return None
    return value


def _log_hook_error(message: str) -> None:
    HOOK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    safe = _redact(message).replace(_load_user_key(), "[REDACTED]") if _load_user_key() else _redact(message)
    with HOOK_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"time": time.time(), "error": safe[:2000]}, ensure_ascii=False) + "\n")


def _record_checkpoint(checkpoint_key: str, message_ids: list[str]) -> None:
    with _file_lock(DATA_DIR / "checkpoints.lock"):
        state = _load_state()
        merged = list(dict.fromkeys([*state.get(checkpoint_key, []), *message_ids]))
        state[checkpoint_key] = merged[-1000:]
        _save_state(state)


def _load_user_key() -> str:
    """Read the user-scoped environment variable, tolerating stale parent processes."""
    key = os.environ.get("TDAI_USER_KEY", "").strip()
    if os.name != "nt":
        return key
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            stored, _ = winreg.QueryValueEx(handle, "TDAI_USER_KEY")
        if isinstance(stored, str) and stored.strip():
            return stored.strip()
    except (FileNotFoundError, OSError):
        pass
    return key


def _parse_transcript(path: str, max_messages: int = 300) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                # Claude Code stores the conversational message under `message`
                # and marks the outer record as `user` or `assistant`.
                candidate = entry.get("message")
                payload = candidate if isinstance(candidate, dict) else entry
            if not isinstance(payload, dict):
                continue
            payload_type = payload.get("type")
            role = payload.get("role") or (
                entry.get("type") if entry.get("type") in {"user", "assistant"} else None
            )
            if payload_type not in {None, "message", "user", "assistant"}:
                continue
            if role not in {"user", "assistant"}:
                continue
            if role == "assistant" and payload.get("phase") not in {None, "final_answer"}:
                continue
            text = _redact(_extract_text(payload.get("content")))
            if text and not _is_low_value(text):
                messages.append({"role": role, "content": text})
    return messages[-max(1, min(int(max_messages), 1000)) :]


class TDAIClient:
    def __init__(self) -> None:
        self.endpoint = os.environ.get("TDAI_MEMORY_ENDPOINT", "").strip().rstrip("/")
        self.user_key = _load_user_key()
        self.service_id = os.environ.get("TDAI_SERVICE_ID", DEFAULT_SERVICE_ID)
        self.team_id = os.environ.get("TDAI_TEAM_ID", "").strip()
        self.agent_id = os.environ.get("TDAI_AGENT_ID", "").strip()
        self.user_id = os.environ.get("TDAI_USER_ID", "").strip()
        self.task_id = os.environ.get("TDAI_TASK_ID", "").strip()
        if not self.endpoint:
            raise RuntimeError("TDAI_MEMORY_ENDPOINT is not set")
        if not self.user_key:
            raise RuntimeError("TDAI_USER_KEY is not set")
        missing_scope = [
            name
            for name, value in (
                ("TDAI_TEAM_ID", self.team_id),
                ("TDAI_AGENT_ID", self.agent_id),
                ("TDAI_USER_ID", self.user_id),
            )
            if not value
        ]
        if missing_scope:
            raise RuntimeError("Missing TDAI isolation scope: " + ", ".join(missing_scope))

    def scoped(self, body: dict[str, Any], task_id: str | None = None) -> dict[str, Any]:
        """Attach required v3 isolation fields to a data-plane request body."""
        result = {
            **body,
            "team_id": self.team_id,
            "agent_id": self.agent_id,
            "user_id": self.user_id,
        }
        effective_task_id = (task_id or self.task_id).strip()
        if effective_task_id:
            result["task_id"] = effective_task_id
        return result

    def post(self, path: str, body: dict[str, Any], timeout: float = 30.0) -> Any:
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self.user_key}",
            "Content-Type": "application/json",
            "x-tdai-user-key": self.user_key,
            "x-tdai-service-id": self.service_id,
        }
        for name, value in (
            ("x-tdai-team-id", self.team_id),
            ("x-tdai-agent-id", self.agent_id),
            ("x-tdai-user-id", self.user_id),
        ):
            if value:
                headers[name] = value
        request = urllib.request.Request(
            f"{self.endpoint}{path}",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            detail = detail.replace(self.user_key, "[REDACTED]")
            raise RuntimeError(f"TDAI HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"TDAI connection failed: {exc.reason}") from exc
        if not isinstance(payload, dict) or payload.get("code") != 0:
            safe = json.dumps(payload, ensure_ascii=False)[:1000].replace(
                self.user_key, "[REDACTED]"
            )
            raise RuntimeError(f"TDAI request failed: {safe}")
        return payload.get("data")

    def verify(self) -> dict[str, Any]:
        data = self.post("/v3/meta/auth/verify", {"user_key": self.user_key})
        if not isinstance(data, dict) or data.get("valid") is not True:
            raise RuntimeError("TDAI rejected the configured user key")
        return data


def _client() -> TDAIClient:
    return TDAIClient()


def _total(data: Any) -> int | None:
    if not isinstance(data, dict):
        return None
    value = data.get("total")
    return value if isinstance(value, int) else None


def _first_record(data: Any, *keys: str) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    for key in keys:
        records = data.get(key)
        if isinstance(records, list) and records and isinstance(records[0], dict):
            return records[0]
    return None


def _chat_memory_agent_id(asset_id: str, team_id: str) -> str | None:
    """Resolve the owning agent from a system-minted chat_memory asset id."""
    prefix = f"chat_memory-{team_id}-"
    if not asset_id.startswith(prefix):
        return None
    agent_id = asset_id[len(prefix) :].strip()
    return agent_id or None


def _shared_chat_memories(client: TDAIClient) -> list[dict[str, Any]]:
    """Return only chat memories visibly bound to the configured, caller-owned agent."""
    data = client.post(
        "/v3/meta/agent-fixed-asset/list-with-detail",
        {
            "agent_id": client.agent_id,
            "asset_types": ["chat_memory"],
            "apply_visibility_filter": True,
            "touch_usage": False,
            "limit": 100,
            "offset": 0,
        },
    )
    if not isinstance(data, dict):
        raise RuntimeError("TDAI returned an invalid fixed-asset response")
    agent = data.get("agent")
    if not isinstance(agent, dict):
        raise RuntimeError("TDAI fixed-asset response has no agent")
    if agent.get("agent_id") != client.agent_id:
        raise RuntimeError("TDAI fixed-asset response does not match the configured agent")
    if agent.get("team_id") != client.team_id:
        raise RuntimeError("Configured agent does not belong to the configured team")
    if agent.get("owner_user_id") != client.user_id:
        raise RuntimeError("Configured agent is not owned by the authenticated TDAI user")

    result: list[dict[str, Any]] = []
    items = data.get("items")
    if not isinstance(items, list):
        return result
    for binding in items:
        if not isinstance(binding, dict) or binding.get("asset_type") != "chat_memory":
            continue
        asset_id = binding.get("asset_id")
        if not isinstance(asset_id, str):
            continue
        target_agent_id = _chat_memory_agent_id(asset_id, client.team_id)
        if not target_agent_id or target_agent_id == client.agent_id:
            continue

        asset = client.post("/v3/meta/asset/get", {"asset_id": asset_id})
        if not isinstance(asset, dict):
            continue
        if asset.get("asset_type") != "chat_memory":
            continue
        if asset.get("team_id") != client.team_id or asset.get("status") != "active":
            continue
        owner_user_id = asset.get("owner_user_id")
        visibility = asset.get("visibility")
        if not isinstance(owner_user_id, str) or not owner_user_id:
            continue
        # Mirror TDAI's binding visibility rules instead of trusting a caller-supplied id.
        if visibility == "team":
            pass
        elif visibility == "private" and owner_user_id == client.user_id:
            pass
        else:
            continue
        result.append(
            {
                "asset_id": asset_id,
                "name": binding.get("name") or asset.get("name") or asset_id,
                "target_agent_id": target_agent_id,
                "owner_user_id": owner_user_id,
                "visibility": visibility,
                "injection_mode": binding.get("injection_mode"),
                "priority": binding.get("priority"),
            }
        )
    return result


def _shared_chat_memory(client: TDAIClient, asset_id: str) -> dict[str, Any]:
    clean_asset_id = asset_id.strip()
    if not clean_asset_id:
        raise ValueError("A shared Chat Memory asset_id is required")
    for item in _shared_chat_memories(client):
        if item["asset_id"] == clean_asset_id:
            return item
    raise PermissionError(
        "The requested Chat Memory is not an accessible fixed binding of the configured agent"
    )


def _shared_scope(
    client: TDAIClient,
    memory: dict[str, Any],
    body: dict[str, Any],
    task_id: str | None,
) -> dict[str, Any]:
    result = {
        **body,
        "team_id": client.team_id,
        "agent_id": memory["target_agent_id"],
        "user_id": memory["owner_user_id"],
    }
    effective_task_id = (task_id or client.task_id).strip()
    if effective_task_id:
        result["task_id"] = effective_task_id
    return result


def _public_shared_memory(memory: dict[str, Any]) -> dict[str, Any]:
    return {
        key: memory.get(key)
        for key in (
            "asset_id",
            "name",
            "target_agent_id",
            "visibility",
            "injection_mode",
            "priority",
        )
    }


def _memory_status(client: TDAIClient, task_id: str | None = None) -> dict[str, Any]:
    """Build a compact data-plane status without returning remembered content."""
    l0_count = client.post("/v3/conversation/count", client.scoped({}, task_id))
    l1_count = client.post("/v3/atomic/count", client.scoped({}, task_id))
    l2_count = client.post("/v3/scenario/count", client.scoped({}, task_id))
    l3_count = client.post("/v3/core/count", client.scoped({}, task_id))
    l0_page = client.post(
        "/v3/conversation/query",
        client.scoped({"limit": 1, "offset": 0}, task_id),
    )
    l1_page = client.post(
        "/v3/atomic/query",
        client.scoped({"limit": 1, "offset": 0}, task_id),
    )
    l3 = client.post("/v3/core/read", client.scoped({}, task_id))

    newest_l0 = _first_record(l0_page, "messages", "items", "records") or {}
    newest_l1 = _first_record(l1_page, "memories", "items", "records") or {}
    effective_task_id = (task_id or client.task_id).strip() or None
    return {
        "data_plane": "ok",
        "scope": {
            "service_id": client.service_id,
            "team_id": client.team_id,
            "agent_id": client.agent_id,
            "user_id": client.user_id,
            "task_id": effective_task_id,
        },
        "counts": {
            "l0_conversations": _total(l0_count),
            "l1_atomic_memories": _total(l1_count),
            "l2_scenarios": _total(l2_count),
            "l3_core_memories": _total(l3_count),
        },
        "latest": {
            "l0_recorded_at": newest_l0.get("recorded_at")
            or newest_l0.get("timestamp")
            or newest_l0.get("created_at"),
            "l1_updated_at": newest_l1.get("updated_at")
            or newest_l1.get("created_at"),
            "l3_updated_at": l3.get("updated_at") if isinstance(l3, dict) else None,
        },
        "note": (
            "This checks authenticated L0-L3 data APIs. Inspect generation logs in "
            "the TDAI Workbench for asynchronous extraction/model fallback details."
        ),
    }


mcp = FastMCP("tdai_memory")


@mcp.tool()
def memory_status(task_id: str | None = None) -> dict[str, Any]:
    """Check authenticated L0-L3 availability, counts, and latest timestamps."""
    return _memory_status(_client(), task_id)


@mcp.tool()
def core_memory_read(task_id: str | None = None) -> Any:
    """Read the compact L3 core/persona memory for broad context restoration."""
    client = _client()
    return client.post("/v3/core/read", client.scoped({}, task_id))


@mcp.tool()
def scenario_list(
    path_prefix: str | None = None,
    limit: int = 20,
    task_id: str | None = None,
) -> Any:
    """List L2 scenario summaries before reading one relevant scenario in detail."""
    body: dict[str, Any] = {}
    if path_prefix:
        body["path_prefix"] = path_prefix
    client = _client()
    data = client.post("/v3/scenario/ls", client.scoped(body, task_id))
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return data
    entries = data["entries"]
    bounded = max(1, min(int(limit), 100))
    return {
        **data,
        "entries": entries[:bounded],
        "returned": min(len(entries), bounded),
    }


@mcp.tool()
def scenario_read(
    path: str,
    version: int | None = None,
    task_id: str | None = None,
) -> Any:
    """Read one L2 scenario selected from scenario_list."""
    clean_path = path.strip()
    if not clean_path:
        raise ValueError("Scenario path is required")
    body: dict[str, Any] = {"path": clean_path}
    if version is not None:
        body["version"] = version
    client = _client()
    return client.post("/v3/scenario/read", client.scoped(body, task_id))


@mcp.tool()
def memory_search(
    query: str,
    limit: int = 5,
    memory_type: str | None = None,
    task_id: str | None = None,
) -> Any:
    """Search distilled TDAI memories for durable facts, preferences, and decisions."""
    body: dict[str, Any] = {"query": query, "limit": max(1, min(limit, 20))}
    if memory_type:
        body["type"] = memory_type
    client = _client()
    return client.post("/v3/atomic/search", client.scoped(body, task_id))


@mcp.tool()
def conversation_search(
    query: str,
    limit: int = 5,
    session_id: str | None = None,
    task_id: str | None = None,
) -> Any:
    """Search raw remembered conversation when exact wording or context is needed."""
    body: dict[str, Any] = {"query": query, "limit": max(1, min(limit, 20))}
    if session_id:
        body["session_id"] = session_id
    client = _client()
    return client.post("/v3/conversation/search", client.scoped(body, task_id))


@mcp.tool()
def shared_memory_list() -> dict[str, Any]:
    """List other agents' Chat Memory blocks explicitly bound to this Agent."""
    client = _client()
    items = [_public_shared_memory(item) for item in _shared_chat_memories(client)]
    return {"source_agent_id": client.agent_id, "items": items, "total": len(items)}


@mcp.tool()
def shared_memory_search(
    asset_id: str,
    query: str,
    limit: int = 5,
    memory_type: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Search L1 memories in one explicitly bound Chat Memory from another Agent."""
    clean_query = query.strip()
    if not clean_query:
        raise ValueError("A non-empty shared-memory query is required")
    client = _client()
    memory = _shared_chat_memory(client, asset_id)
    body: dict[str, Any] = {
        "query": clean_query,
        "limit": max(1, min(int(limit), 20)),
    }
    if memory_type:
        body["type"] = memory_type
    data = client.post(
        "/v3/atomic/search",
        _shared_scope(client, memory, body, task_id),
    )
    return {"source": _public_shared_memory(memory), "results": data}


@mcp.tool()
def shared_conversation_search(
    asset_id: str,
    query: str,
    limit: int = 5,
    session_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Search L0 conversations in one explicitly bound Chat Memory from another Agent."""
    clean_query = query.strip()
    if not clean_query:
        raise ValueError("A non-empty shared-conversation query is required")
    client = _client()
    memory = _shared_chat_memory(client, asset_id)
    body: dict[str, Any] = {
        "query": clean_query,
        "limit": max(1, min(int(limit), 20)),
    }
    if session_id:
        body["session_id"] = session_id
    data = client.post(
        "/v3/conversation/search",
        _shared_scope(client, memory, body, task_id),
    )
    return {"source": _public_shared_memory(memory), "results": data}


@mcp.tool()
def remember(
    note: str, session_id: str | None = None, task_id: str | None = None
) -> Any:
    """Store one durable, non-secret note in TDAI for reuse across agents."""
    clean = _redact(note)
    if not clean:
        raise ValueError("Nothing safe to remember after redaction")
    stable_session = session_id or f"codex-note-{hashlib.sha256(clean.encode()).hexdigest()[:24]}"
    client = _client()
    return client.post(
        "/v3/conversation/add",
        client.scoped({
            "session_id": stable_session,
            "messages": [{"role": "user", "content": clean}],
        }, task_id),
        timeout=60.0,
    )


def _capture_transcript(
    transcript_path: str,
    session_id: str,
    max_messages: int = 300,
    task_id: str | None = None,
    platform: str = "codex",
) -> dict[str, Any]:
    """Capture new safe user/final-assistant messages from a supported harness."""
    parsed = _parse_transcript(transcript_path, max_messages=max_messages)
    state = _load_state()
    checkpoint_key = _checkpoint_key(platform, session_id)
    already = set(state.get(checkpoint_key, []))
    pending = [m for m in parsed if _message_id(m["role"], m["content"]) not in already]
    if not pending:
        return {"captured": 0, "skipped": len(parsed)}

    remote_session = f"{platform}-{checkpoint_key[:24]}"
    client = _client()
    for offset in range(0, len(pending), 50):
        client.post(
            "/v3/conversation/add",
            client.scoped(
                {"session_id": remote_session, "messages": pending[offset : offset + 50]},
                task_id,
            ),
            timeout=60.0,
        )

    ids = [_message_id(m["role"], m["content"]) for m in parsed]
    _record_checkpoint(checkpoint_key, ids)
    _remove_pending_ids(checkpoint_key, set(ids))
    return {"captured": len(pending), "skipped": len(parsed) - len(pending)}


def _remove_pending_ids(checkpoint_key: str, sent_ids: set[str]) -> None:
    path = _pending_path(checkpoint_key)
    with _file_lock(_queue_lock_path(checkpoint_key)):
        pending = _load_pending(checkpoint_key)
        if not pending:
            return
        remaining = [
            message
            for message in pending["messages"]
            if _message_id(message["role"], message["content"]) not in sent_ids
        ]
        if remaining:
            pending["messages"] = remaining
            pending["updated_at"] = time.time()
            _atomic_write_json(path, pending)
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _flush_pending(checkpoint_key: str) -> dict[str, int]:
    with _file_lock(_queue_lock_path(checkpoint_key)):
        pending = _load_pending(checkpoint_key)
        if not pending:
            return {"captured": 0, "remaining": 0}
        messages = [
            message
            for message in pending["messages"]
            if isinstance(message, dict)
            and message.get("role") in {"user", "assistant"}
            and isinstance(message.get("content"), str)
        ]
        session_id = str(pending.get("session_id", ""))
        task_id = pending.get("task_id")
    if not messages or not session_id:
        _remove_pending_ids(
            checkpoint_key,
            {_message_id(m["role"], m["content"]) for m in messages},
        )
        return {"captured": 0, "remaining": 0}

    client = _client()
    remote_session = str(pending.get("remote_session") or f"codex-{checkpoint_key[:24]}")
    for offset in range(0, len(messages), 50):
        client.post(
            "/v3/conversation/add",
            client.scoped(
                {"session_id": remote_session, "messages": messages[offset : offset + 50]},
                task_id if isinstance(task_id, str) else None,
            ),
            timeout=60.0,
        )
    sent_ids = [_message_id(m["role"], m["content"]) for m in messages]
    _record_checkpoint(checkpoint_key, sent_ids)
    _remove_pending_ids(checkpoint_key, set(sent_ids))
    remaining = _load_pending(checkpoint_key)
    return {
        "captured": len(messages),
        "remaining": len(remaining["messages"]) if remaining else 0,
    }


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _ensure_idle_worker(
    checkpoint_key: str,
    idle_seconds: int,
    batch_turns: int,
) -> None:
    marker = _worker_path(checkpoint_key)
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = json.loads(marker.read_text(encoding="utf-8"))
        pid = int(current.get("pid", 0)) if isinstance(current, dict) else 0
        if _pid_alive(pid):
            return
        marker.unlink()
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        try:
            marker.unlink()
        except FileNotFoundError:
            pass

    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--idle-worker",
        "--checkpoint-key",
        checkpoint_key,
        "--idle-seconds",
        str(idle_seconds),
        "--batch-turns",
        str(batch_turns),
    ]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": {**os.environ, "TDAI_LOCAL_DATA_DIR": str(DATA_DIR)},
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)
    _atomic_write_json(marker, {"pid": process.pid, "started_at": time.time()})


def _queue_transcript(
    transcript_path: str,
    session_id: str,
    max_messages: int = 300,
    task_id: str | None = None,
    batch_turns: int = DEFAULT_BATCH_TURNS,
    idle_seconds: int = DEFAULT_IDLE_SECONDS,
    start_worker: bool = True,
    platform: str = "codex",
) -> dict[str, int]:
    parsed = _parse_transcript(transcript_path, max_messages=max_messages)
    checkpoint_key = _checkpoint_key(platform, session_id)
    state = _load_state()
    captured_ids = set(state.get(checkpoint_key, []))
    path = _pending_path(checkpoint_key)
    with _file_lock(_queue_lock_path(checkpoint_key)):
        pending = _load_pending(checkpoint_key) or {
            "session_id": session_id,
            "platform": platform,
            "remote_session": f"{platform}-{checkpoint_key[:24]}",
            "task_id": task_id,
            "created_at": time.time(),
            "updated_at": time.time(),
            "messages": [],
        }
        queued_ids = {
            _message_id(message["role"], message["content"])
            for message in pending["messages"]
            if isinstance(message, dict)
            and message.get("role") in {"user", "assistant"}
            and isinstance(message.get("content"), str)
        }
        additions = [
            message
            for message in parsed
            if _message_id(message["role"], message["content"])
            not in captured_ids | queued_ids
        ]
        if additions:
            pending["messages"].extend(additions)
            pending["updated_at"] = time.time()
            if task_id:
                pending["task_id"] = task_id
            _atomic_write_json(path, pending)
        queued = len(pending["messages"])
        turns = sum(1 for message in pending["messages"] if message.get("role") == "assistant")
    if queued and start_worker:
        _ensure_idle_worker(
            checkpoint_key,
            max(10, int(idle_seconds)),
            max(1, int(batch_turns)),
        )
    return {"queued": queued, "added": len(additions), "turns": turns}


def _idle_worker(checkpoint_key: str, idle_seconds: int, batch_turns: int) -> int:
    marker = _worker_path(checkpoint_key)
    failures = 0
    try:
        # The parent writes the marker immediately after spawning. Waiting briefly
        # avoids leaving a stale marker if a forced batch flush finishes instantly.
        for _ in range(20):
            if marker.exists():
                break
            time.sleep(0.05)
        while True:
            pending = _load_pending(checkpoint_key)
            if not pending or not pending.get("messages"):
                return 0
            messages = pending["messages"]
            turns = sum(1 for message in messages if message.get("role") == "assistant")
            idle_for = max(0.0, time.time() - float(pending.get("updated_at", 0)))
            if turns < batch_turns and idle_for < idle_seconds:
                time.sleep(min(15.0, max(1.0, idle_seconds - idle_for)))
                continue
            try:
                result = _flush_pending(checkpoint_key)
                failures = 0
                if result["remaining"] == 0:
                    return 0
            except Exception as exc:
                failures += 1
                _log_hook_error(f"background flush attempt {failures} failed: {exc}")
                if failures >= 3:
                    return 1
                time.sleep(30 * failures)
    finally:
        try:
            current = json.loads(marker.read_text(encoding="utf-8"))
            if isinstance(current, dict) and int(current.get("pid", 0)) == os.getpid():
                marker.unlink()
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            pass


@mcp.tool()
def capture_transcript(
    transcript_path: str,
    session_id: str,
    max_messages: int = 300,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Capture new safe user/final-assistant messages before context is discarded."""
    return _capture_transcript(transcript_path, session_id, max_messages, task_id)


def _capture_hook(platform: str) -> int:
    event = json.load(sys.stdin)
    if not isinstance(event, dict):
        raise ValueError("Hook input must be a JSON object")
    transcript_path = event.get("transcript_path")
    session_id = event.get("session_id")
    if not isinstance(transcript_path, str) or not transcript_path.strip():
        raise ValueError("Hook input has no transcript_path")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Hook input has no session_id")
    _capture_transcript(transcript_path, session_id, platform=platform)
    # Exit 0 with no stdout so the hook adds nothing to the model context.
    return 0


def _buffer_hook(platform: str, batch_turns: int, idle_seconds: int) -> int:
    event = json.load(sys.stdin)
    if not isinstance(event, dict):
        raise ValueError("Hook input must be a JSON object")
    transcript_path = event.get("transcript_path")
    session_id = event.get("session_id")
    if not isinstance(transcript_path, str) or not transcript_path.strip():
        raise ValueError("Hook input has no transcript_path")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Hook input has no session_id")
    task_id = event.get("task_id")
    _queue_transcript(
        transcript_path,
        session_id,
        task_id=task_id if isinstance(task_id, str) else None,
        batch_turns=batch_turns,
        idle_seconds=idle_seconds,
        platform=platform,
    )
    # Queueing is local and fast; the detached worker performs network I/O.
    return 0


def _canonical_messages(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    messages: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in {"user", "assistant"}:
            continue
        text = _redact(_extract_text(item.get("content")))
        if text and not _is_low_value(text):
            messages.append({"role": role, "content": text})
    return messages


def _canonical_hook(platform: str, batch_turns: int, idle_seconds: int) -> int:
    event = json.load(sys.stdin)
    if not isinstance(event, dict):
        raise ValueError("Canonical hook input must be a JSON object")
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Canonical hook input has no session_id")

    raw_messages = event.get("messages")
    if platform == "hermes" and not isinstance(raw_messages, list):
        extra = event.get("extra") if isinstance(event.get("extra"), dict) else {}
        raw_messages = [
            {"role": "user", "content": extra.get("user_message", "")},
            {"role": "assistant", "content": extra.get("assistant_response", "")},
        ]
    messages = _canonical_messages(raw_messages)
    task_id = event.get("task_id")
    if not isinstance(task_id, str):
        extra = event.get("extra") if isinstance(event.get("extra"), dict) else {}
        task_id = extra.get("task_id") if isinstance(extra.get("task_id"), str) else None

    event_name = str(event.get("hook_event_name", ""))
    force = bool(event.get("force")) or event_name in {
        "PreCompact",
        "SessionEnd",
        "on_session_finalize",
        "session_shutdown",
        "session.compacted",
        "session.deleted",
    }

    fd, transcript_path = tempfile.mkstemp(prefix="tdai-canonical-", suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for message in messages:
                handle.write(
                    json.dumps(
                        {"payload": {"type": "message", **message}},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        result = _queue_transcript(
            transcript_path,
            session_id,
            task_id=task_id,
            batch_turns=batch_turns,
            idle_seconds=idle_seconds,
            start_worker=not force,
            platform=platform,
        )
    finally:
        try:
            os.unlink(transcript_path)
        except FileNotFoundError:
            pass

    if force and result["queued"]:
        _flush_pending(_checkpoint_key(platform, session_id))
    return 0


def _apply_cli_scope(args: argparse.Namespace) -> None:
    """Apply non-secret scope passed by a standalone command hook."""
    values = {
        "TDAI_MEMORY_ENDPOINT": args.endpoint,
        "TDAI_SERVICE_ID": args.service_id,
        "TDAI_TEAM_ID": args.team_id,
        "TDAI_AGENT_ID": args.agent_id,
        "TDAI_USER_ID": args.user_id,
        "TDAI_TASK_ID": args.task_id,
    }
    for name, value in values.items():
        if isinstance(value, str) and value.strip():
            os.environ[name] = value.strip()


def _apply_local_data_dir(args: argparse.Namespace) -> None:
    global DATA_DIR, STATE_PATH, PENDING_DIR, WORKER_DIR, HOOK_LOG_PATH
    if not isinstance(args.local_data_dir, str) or not args.local_data_dir.strip():
        return
    DATA_DIR = Path(args.local_data_dir).expanduser()
    STATE_PATH = DATA_DIR / "checkpoints.json"
    PENDING_DIR = DATA_DIR / "pending"
    WORKER_DIR = DATA_DIR / "workers"
    HOOK_LOG_PATH = DATA_DIR / "hook-errors.log"


def _self_test() -> int:
    client = _client()
    auth = client.verify()
    user = auth.get("user") if isinstance(auth, dict) else None
    user_id = user.get("user_id") if isinstance(user, dict) else None
    if client.user_id and user_id != client.user_id:
        raise RuntimeError(f"Unexpected TDAI user id: {user_id!r}")
    client.post(
        "/v3/core/read",
        client.scoped({}),
    )
    client.post(
        "/v3/scenario/ls",
        client.scoped({}),
    )
    client.post(
        "/v3/atomic/search",
        client.scoped({"query": "TDAI connectivity test", "limit": 1}),
    )
    client.post(
        "/v3/conversation/search",
        client.scoped({"query": "TDAI connectivity test", "limit": 1}),
    )
    status = _memory_status(client)
    if status.get("data_plane") != "ok":
        raise RuntimeError(f"Unexpected TDAI status: {status!r}")
    _shared_chat_memories(client)
    print(
        f"TDAI sidecar OK user_id={user_id}; auth, status, L0-L3, and shared bindings passed"
    )
    return 0


def _parser_test() -> int:
    sample = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": "hidden system prompt"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            '<in-app-browser-context source="ambient-ui-state">hidden UI</in-app-browser-context>'
                            "Remember this decision. 密码: unsafe-value sk-test_1234567890123456"
                        ),
                    }
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "hidden commentary"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Durable final answer."}],
            },
        },
    ]
    fd, name = tempfile.mkstemp(prefix="tdai-parser-test-", suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for item in sample:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        parsed = _parse_transcript(name)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
    serialized = json.dumps(parsed, ensure_ascii=False)
    assert len(parsed) == 2, parsed
    assert "hidden system prompt" not in serialized
    assert "hidden UI" not in serialized
    assert "hidden commentary" not in serialized
    assert "unsafe-value" not in serialized
    assert "sk-test_" not in serialized
    assert "[REDACTED_CREDENTIAL]" in serialized
    assert "[REDACTED_API_KEY]" in serialized

    claude_sample = [
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Keep this cross-platform decision."}],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": "private tool output"}],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "The decision is durable."}],
            },
        },
    ]
    fd, name = tempfile.mkstemp(prefix="tdai-claude-parser-test-", suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for item in claude_sample:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        claude_parsed = _parse_transcript(name)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
    assert claude_parsed == [
        {"role": "user", "content": "Keep this cross-platform decision."},
        {"role": "assistant", "content": "The decision is durable."},
    ], claude_parsed
    assert "private tool output" not in json.dumps(claude_parsed)
    canonical = _canonical_messages(
        [
            {"role": "system", "content": "do not store"},
            {"role": "user", "content": "ok"},
            {"role": "user", "content": "A meaningful portable fact."},
            {"role": "assistant", "content": [{"type": "text", "text": "Stored safely."}]},
        ]
    )
    assert [message["role"] for message in canonical] == ["user", "assistant"], canonical
    print("TDAI parser OK Codex/Claude/canonical formats filtered and redacted")
    return 0


def _buffer_test() -> int:
    global DATA_DIR, STATE_PATH, PENDING_DIR, WORKER_DIR, HOOK_LOG_PATH
    original_paths = (DATA_DIR, STATE_PATH, PENDING_DIR, WORKER_DIR, HOOK_LOG_PATH)
    original_client = globals()["_client"]
    posts: list[dict[str, Any]] = []

    class FakeClient:
        def scoped(self, body: dict[str, Any], task_id: str | None = None) -> dict[str, Any]:
            return {**body, "task_id": task_id}

        def post(self, path: str, body: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
            posts.append({"path": path, "body": body, "timeout": timeout})
            return {}

    try:
        with tempfile.TemporaryDirectory(prefix="tdai-buffer-test-") as directory:
            DATA_DIR = Path(directory)
            STATE_PATH = DATA_DIR / "checkpoints.json"
            PENDING_DIR = DATA_DIR / "pending"
            WORKER_DIR = DATA_DIR / "workers"
            HOOK_LOG_PATH = DATA_DIR / "hook-errors.log"
            transcript = DATA_DIR / "transcript.jsonl"
            sample = [
                {
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"text": "可以"}],
                    }
                },
                {
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"text": "Use a buffered TDAI capture policy."}],
                    }
                },
            ]
            transcript.write_text(
                "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in sample),
                encoding="utf-8",
            )
            first = _queue_transcript(
                str(transcript), "buffer-test", start_worker=False
            )
            second = _queue_transcript(
                str(transcript), "buffer-test", start_worker=False
            )
            assert first == {"queued": 1, "added": 1, "turns": 1}, first
            assert second == {"queued": 1, "added": 0, "turns": 1}, second
            globals()["_client"] = lambda: FakeClient()
            key = hashlib.sha256(b"buffer-test").hexdigest()
            pending = _load_pending(key)
            assert pending is not None
            pending["updated_at"] = 0
            _atomic_write_json(_pending_path(key), pending)
            _atomic_write_json(
                _worker_path(key), {"pid": os.getpid(), "started_at": time.time()}
            )
            assert _idle_worker(key, idle_seconds=1, batch_turns=5) == 0
            assert len(posts) == 1 and posts[0]["path"] == "/v3/conversation/add", posts
            assert not _pending_path(key).exists()
            assert not _worker_path(key).exists()
            assert len(_load_state().get(key, [])) == 1
            assert _checkpoint_key("codex", "same") != _checkpoint_key("hermes", "same")
            assert _checkpoint_key("hermes", "same") != _checkpoint_key("pi", "same")
    finally:
        globals()["_client"] = original_client
        DATA_DIR, STATE_PATH, PENDING_DIR, WORKER_DIR, HOOK_LOG_PATH = original_paths
    print("TDAI buffer OK local queue, filtering, dedup, flush, checkpoint passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--parser-test", action="store_true")
    parser.add_argument("--buffer-test", action="store_true")
    parser.add_argument("--capture-hook", action="store_true")
    parser.add_argument("--buffer-hook", action="store_true")
    parser.add_argument("--canonical-hook", action="store_true")
    parser.add_argument("--idle-worker", action="store_true")
    parser.add_argument("--checkpoint-key")
    parser.add_argument("--batch-turns", type=int, default=DEFAULT_BATCH_TURNS)
    parser.add_argument("--idle-seconds", type=int, default=DEFAULT_IDLE_SECONDS)
    parser.add_argument("--platform", default="codex")
    parser.add_argument("--local-data-dir")
    parser.add_argument("--endpoint")
    parser.add_argument("--service-id")
    parser.add_argument("--team-id")
    parser.add_argument("--agent-id")
    parser.add_argument("--user-id")
    parser.add_argument("--task-id")
    args = parser.parse_args()
    _apply_local_data_dir(args)
    _apply_cli_scope(args)
    if args.self_test:
        return _self_test()
    if args.parser_test:
        return _parser_test()
    if args.buffer_test:
        return _buffer_test()
    if args.capture_hook:
        return _capture_hook(args.platform)
    if args.buffer_hook:
        return _buffer_hook(
            args.platform,
            max(1, args.batch_turns),
            max(10, args.idle_seconds),
        )
    if args.canonical_hook:
        return _canonical_hook(
            args.platform,
            max(1, args.batch_turns),
            max(10, args.idle_seconds),
        )
    if args.idle_worker:
        if not args.checkpoint_key or not re.fullmatch(r"[0-9a-f]{64}", args.checkpoint_key):
            raise ValueError("--idle-worker requires a SHA-256 --checkpoint-key")
        return _idle_worker(
            args.checkpoint_key,
            max(10, args.idle_seconds),
            max(1, args.batch_turns),
        )
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"tdai-memory: {exc}", file=sys.stderr)
        raise SystemExit(1)
