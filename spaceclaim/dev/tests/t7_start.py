# -*- coding: utf-8 -*-
"""t7: 只测 scdm_session_start, 通过 MCP stdio, 从本地源码运行。"""
import asyncio, json, os, sys, time, traceback

SRC = r"C:\english_path\github_fork\ansys-agent-bridge\python\src"
PY = sys.executable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

params = StdioServerParameters(
    command=PY, args=["-m", "ansys_bridge_mcp.server"],
    env={**os.environ, "PYTHONPATH": SRC, "PYTHONIOENCODING": "utf-8"},
)


async def main():
    t0 = time.time()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("connected %.1fs" % (time.time() - t0), flush=True)
            t1 = time.time()
            print("calling scdm_session_start ...", flush=True)
            r = await session.call_tool("scdm_session_start", {"hidden": True})
            print("returned after %.1fs isError=%s" % (time.time() - t1, r.isError), flush=True)
            print(r.content[0].text[:600], flush=True)
            r = await session.call_tool("scdm_session_close", {})
            print("closed:", r.content[0].text[:200], flush=True)


try:
    asyncio.run(main())
    print("=== t7 ok ===")
except Exception:
    print("FAIL:\n" + traceback.format_exc())
