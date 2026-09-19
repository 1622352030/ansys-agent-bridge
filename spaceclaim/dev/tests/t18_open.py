# -*- coding: utf-8 -*-
"""t18: 通过 MCP 打开 assembly31.scdoc, 列出全部实体, 并与原始 PyAnsys 路径对比。

不打布尔, 只看 open_file 到底给了我们哪个 design。
"""
import asyncio, json, os, sys, traceback

SRC = r"C:\english_path\github_fork\ansys-agent-bridge\python\src"
GEOM = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\assembly31.scdoc"
PY = sys.executable
LOG = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\t18_open.log"

open(LOG, "w", encoding="utf-8").close()


def W(m):
    s = str(m)
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

params = StdioServerParameters(
    command=PY, args=["-m", "ansys_bridge_mcp.server"],
    env={**os.environ, "PYTHONPATH": SRC, "PYTHONIOENCODING": "utf-8"},
)


async def main():
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.call_tool("scdm_session_start", {"hidden": True})

            r = await session.call_tool("scdm_open_file", {"path": GEOM})
            d = json.loads(r.content[0].text)
            W("open_file: design=%r is_active=%r body_count=%s" % (
                d.get("design"), d.get("is_active"), d.get("body_count")))
            total = 0
            for b in d["bodies"]:
                total += b["faces"] or 0
                W("   %-14s faces=%-5s vol=%.10g" % (b["name"], b["faces"], b["volume_m3"] or 0))
            W("   TOTAL faces = %d" % total)

            r = await session.call_tool("scdm_session_status", {})
            W("status: " + json.dumps(json.loads(r.content[0].text), ensure_ascii=False))

            await session.call_tool("scdm_session_close", {})


try:
    asyncio.run(main())
    W("=== t18 end ===")
except Exception:
    W("FAIL:\n" + traceback.format_exc())
