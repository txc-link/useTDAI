# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.0,<2"]
# ///
"""Offline end-to-end smoke test for the TDAI Knowledge MCP bridge."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


SERVER = Path(__file__).with_name("knowledge_server.py")


class MockTDAIHandler(BaseHTTPRequestHandler):
    failures: list[str] = []

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _reply(self, data: Any) -> None:
        encoded = json.dumps({"code": 0, "message": "ok", "data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        if self.headers.get("x-tdai-service-id") != "mock-service":
            self.failures.append(f"missing service header on {self.path}")
        if self.path.startswith("/v3/skill/"):
            if not self.headers.get("Authorization", "").startswith("Bearer "):
                self.failures.append(f"missing user auth on {self.path}")
            for name in ("team_id", "agent_id", "user_id"):
                if not body.get(name):
                    self.failures.append(f"missing {name} on {self.path}")

        if self.path == "/v3/wiki/list":
            self._reply({"items": [{"id": "wiki-demo", "status": "ready"}], "total": 1})
        elif self.path == "/v3/code-graph/list":
            self._reply({"items": [{"id": "cg-demo", "status": "ready"}], "total": 1})
        elif self.path == "/v3/tools/list":
            self._reply(
                {
                    "knowledge_id": body.get("knowledge_id"),
                    "tools": [{"name": "search", "read_only": True}],
                }
            )
        elif self.path == "/v3/tools/call":
            self._reply({"content": [{"type": "text", "text": "mock hit"}], "isError": False})
        elif self.path == "/v3/skill/search":
            self._reply({"items": [{"skill_id": "skill-demo", "score": 1.0}]})
        elif self.path == "/v3/skill/get":
            self._reply(
                {
                    "skill_id": body.get("skill_id"),
                    "content": "Reference only.",
                    "storage_dir": "/internal/path/not-for-clients",
                }
            )
        else:
            self.send_error(404)


def find_uv() -> str:
    configured = os.environ.get("TDAI_UV_PATH", "").strip()
    uv = configured or shutil.which("uv")
    if not uv:
        raise RuntimeError("uv was not found; install it or set TDAI_UV_PATH")
    return uv


async def run_mcp(endpoint: str) -> None:
    env = {
        **os.environ,
        "TDAI_KNOWLEDGE_ENDPOINT": endpoint,
        "TDAI_MEMORY_ENDPOINT": endpoint,
        "TDAI_SERVICE_ID": "mock-service",
        "TDAI_TEAM_ID": "team-demo",
        "TDAI_AGENT_ID": "agent-demo",
        "TDAI_USER_ID": "user-demo",
        "TDAI_USER_KEY": "test-user-key",
    }
    params = StdioServerParameters(
        command=find_uv(), args=["run", "--script", str(SERVER)], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            expected = {
                "knowledge_assets_list",
                "knowledge_tool_call",
                "knowledge_tools_list",
                "skill_read",
                "skill_search",
            }
            if set(names) != expected:
                raise RuntimeError(f"Unexpected MCP tools: {names}")
            calls = [
                ("knowledge_assets_list", {}),
                ("knowledge_tools_list", {"knowledge_id": "wiki-demo"}),
                (
                    "knowledge_tool_call",
                    {
                        "knowledge_id": "wiki-demo",
                        "tool_name": "search",
                        "params": {"query": "demo"},
                    },
                ),
                ("skill_search", {"query": "demo"}),
                ("skill_read", {"skill_id": "skill-demo"}),
            ]
            for name, arguments in calls:
                result = await session.call_tool(name, arguments)
                if result.isError:
                    raise RuntimeError(f"{name} failed: {result.content}")
            print(
                "TDAI Knowledge MCP OK tools="
                + ",".join(names)
                + "; mocked read-only calls passed"
            )


def main() -> None:
    MockTDAIHandler.failures = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockTDAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        asyncio.run(run_mcp(endpoint))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    if MockTDAIHandler.failures:
        raise RuntimeError("; ".join(MockTDAIHandler.failures))


if __name__ == "__main__":
    main()
