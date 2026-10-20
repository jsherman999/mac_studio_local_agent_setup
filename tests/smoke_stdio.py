#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.3,<3"]
# ///
"""Live smoke test: start the server over stdio exactly as Claude Code would, list tools, and
call local_status (and optionally local_ask) against the real local stack.

    uv run --script tests/smoke_stdio.py            # status only
    uv run --script tests/smoke_stdio.py --ask      # also a tiny generation
"""
import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SERVER = Path(__file__).resolve().parents[1] / "mcp_server" / "local_agent_mcp.py"


async def main() -> None:
    cmd = os.environ.get("SMOKE_CMD")  # e.g. "python" in CI; default uses uv
    params = (StdioServerParameters(command=cmd, args=[str(SERVER)], env=dict(os.environ)) if cmd
              else StdioServerParameters(command="uv", args=["run", "--script", str(SERVER)], env=dict(os.environ)))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("tools:", ", ".join(t.name for t in tools.tools))
            res = await s.call_tool("local_status", {})
            print(res.content[0].text)
            if "--ask" in sys.argv:
                res = await s.call_tool("local_ask", {"prompt": "Reply with the single word: ready",
                                                      "model_role": "fast", "max_tokens": 20})
                print("local_ask ->", res.content[0].text)


asyncio.run(main())
