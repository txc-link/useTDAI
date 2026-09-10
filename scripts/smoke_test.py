# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.0,<2"]
# ///
"""Read-only end-to-end smoke test for the local TDAI MCP bridge."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


SERVER = Path(__file__).with_name("server.py")


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
            print(
                "TDAI MCP OK tools="
                + ",".join(names)
                + "; read-only search/status passed"
            )


if __name__ == "__main__":
    asyncio.run(main())
