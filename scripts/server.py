# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.0,<2"]
# ///
"""Local stdio MCP bridge for a remote TDAI MemoryCore REST service."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP


DEFAULT_SERVICE_ID = "default"
STATE_PATH = Path.home() / ".codex" / "tdai-memory" / "checkpoints.json"

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
            payload = entry.get("payload", entry)
            if not isinstance(payload, dict) or payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in {"user", "assistant"}:
                continue
            if role == "assistant" and payload.get("phase") not in {None, "final_answer"}:
                continue
            text = _redact(_extract_text(payload.get("content")))
            if text:
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
) -> dict[str, Any]:
    """Capture new safe user/final-assistant messages before Codex compacts context."""
    parsed = _parse_transcript(transcript_path, max_messages=max_messages)
    state = _load_state()
    checkpoint_key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    already = set(state.get(checkpoint_key, []))
    pending = [m for m in parsed if _message_id(m["role"], m["content"]) not in already]
    if not pending:
        return {"captured": 0, "skipped": len(parsed)}

    remote_session = f"codex-{checkpoint_key[:24]}"
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
    state[checkpoint_key] = ids[-1000:]
    _save_state(state)
    return {"captured": len(pending), "skipped": len(parsed) - len(pending)}


@mcp.tool()
def capture_transcript(
    transcript_path: str,
    session_id: str,
    max_messages: int = 300,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Capture new safe user/final-assistant messages before Codex compacts context."""
    return _capture_transcript(transcript_path, session_id, max_messages, task_id)


def _capture_hook() -> int:
    event = json.load(sys.stdin)
    if not isinstance(event, dict):
        raise ValueError("Hook input must be a JSON object")
    transcript_path = event.get("transcript_path")
    session_id = event.get("session_id")
    if not isinstance(transcript_path, str) or not transcript_path.strip():
        raise ValueError("Hook input has no transcript_path")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Hook input has no session_id")
    _capture_transcript(transcript_path, session_id)
    # Exit 0 with no stdout so the hook adds nothing to the model context.
    return 0


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
    print("TDAI parser OK system/tool/ambient excluded; credentials redacted")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--parser-test", action="store_true")
    parser.add_argument("--capture-hook", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return _self_test()
    if args.parser_test:
        return _parser_test()
    if args.capture_hook:
        return _capture_hook()
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"tdai-memory: {exc}", file=sys.stderr)
        raise SystemExit(1)
