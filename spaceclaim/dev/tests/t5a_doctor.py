# -*- coding: utf-8 -*-
"""最小 MCP stdio 回归：确认 doctor 工具不再污染 stdout。"""
import asyncio, json, os, sys, traceback

SRC = r"C:\english_path\github_fork\ansys-agent-bridge\python\src"
PY = sys.executable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

params = StdioServerParameters(
    command=PY, args=["-m", "ansys_bridge_mcp.server"],
    env={**os.environ, "PYTHONPATH": SRC},
)


async def main():
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools=%d" % len(tools.tools), flush=True)
            r = await session.call_tool("ansys_bridge_doctor", {})
            d = json.loads(r.content[0].text)
            print("isError=%s" % r.isError, flush=True)
            print("ok=%s" % d.get("ok"), flush=True)
            print("packages=%s" % json.dumps(d["packages"]), flush=True)
            # 第二次调用，确认会话没有被第一次调用打断
            r2 = await session.call_tool("scdm_session_status", {})
            print("2nd call ok, running=%s" % json.loads(r2.content[0].text).get("running"), flush=True)


try:
    asyncio.run(main())
    print("=== T5a 通过 ===")
except Exception:
    print("异常:\n" + traceback.format_exc())
