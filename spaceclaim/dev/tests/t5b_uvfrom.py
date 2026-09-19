# -*- coding: utf-8 -*-
"""smoke: 直接以 MCP stdio 方式启动 uv run --from git+ 的服务器并调用 doctor。"""
import asyncio, json, os, sys, time, traceback

UV = r"C:\Users\16223\.local\bin\uv.exe"
GIT = "git+https://github.com/1622352030/ansys-agent-bridge#subdirectory=python"

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

params = StdioServerParameters(
    command=UV,
    args=["tool", "run", "--quiet", "--from", GIT, "ansys-bridge-mcp"],
    env={**os.environ, "PYTHONIOENCODING": "utf-8", "ANSYS_BRIDGE_LOG_LEVEL": "WARNING"},
)


async def main():
    t0 = time.time()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("conectado en %.1f s" % (time.time() - t0), flush=True)
            tools = await session.list_tools()
            print("tools=%d" % len(tools.tools), flush=True)
            r = await session.call_tool("ansys_bridge_doctor", {})
            d = json.loads(r.content[0].text)
            print("ok=%s packages=%s" % (d.get("ok"), json.dumps(d["packages"])), flush=True)


try:
    asyncio.run(main())
    print("=== uv --from smoke OK ===")
except Exception:
    print("FALLO:\n" + traceback.format_exc())
