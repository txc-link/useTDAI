# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.0,<2"]
# ///
"""Read-only end-to-end smoke test for the local TDAI MCP bridge."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


SERVER = Path(__file__).with_name("server.py")


def result_json(result: object) -> dict:
    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if not isinstance(text, str):
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise RuntimeError("MCP tool returned no JSON object")


def find_uv() -> str:
    configured = os.environ.get("TDAI_UV_PATH", "").strip()
    discovered = shutil.which("uv")
    uv = configured or discovered
    if not uv:
        raise RuntimeError("uv was not found; install it or set TDAI_UV_PATH")
    return uv


async def main() -> None:
    params = StdioServerParameters(
        command=find_uv(),
        args=["run", "--script", str(SERVER)],
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            expected = {
                "capture_transcript",
                "conversation_search",
                "core_memory_read",
                "memory_status",
                "memory_search",
                "remember",
                "scenario_list",
                "scenario_read",
                "shared_conversation_search",
                "shared_memory_list",
                "shared_memory_search",
            }
            if set(names) != expected:
                raise RuntimeError(f"Unexpected MCP tools: {names}")
            result = await session.call_tool(
                "memory_search", {"query": "TDAI connectivity test", "limit": 1}
            )
            if result.isError:
                raise RuntimeError(f"memory_search failed: {result.content}")
            status = await session.call_tool("memory_status", {})
            if status.isError:
                raise RuntimeError(f"memory_status failed: {status.content}")
            shared = await session.call_tool("shared_memory_list", {})
            if shared.isError:
                raise RuntimeError(f"shared_memory_list failed: {shared.content}")
            shared_data = result_json(shared)
            items = shared_data.get("items", [])
            if isinstance(items, list) and items and isinstance(items[0], dict):
                asset_id = items[0].get("asset_id")
                if isinstance(asset_id, str) and asset_id:
                    for tool in ("shared_memory_search", "shared_conversation_search"):
                        result = await session.call_tool(
                            tool,
                            {
                                "asset_id": asset_id,
                                "query": "TDAI connectivity test",
                                "limit": 1,
                            },
                        )
                        if result.isError:
                            raise RuntimeError(f"{tool} failed: {result.content}")
            denied = await session.call_tool(
                "shared_memory_search",
                {
                    "asset_id": "chat_memory-not-a-bound-asset",
                    "query": "must not be queried",
                    "limit": 1,
                },
            )
            if not denied.isError:
                raise RuntimeError("unbound shared Chat Memory was not rejected")
            print(
                "TDAI MCP OK tools="
                + ",".join(names)
                + "; read-only search/status/shared-bindings passed"
            )


if __name__ == "__main__":
    asyncio.run(main())
