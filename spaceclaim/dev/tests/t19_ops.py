# -*- coding: utf-8 -*-
"""t19: 逐个算子单独跑, 每次重新 open_file, 打印 before/after 快照。"""
import asyncio, json, os, sys, traceback

SRC = r"C:\english_path\github_fork\ansys-agent-bridge\python\src"
GEOM = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\assembly31.scdoc"
PY = sys.executable

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

            async def reopen():
                r = await session.call_tool("scdm_open_file", {"path": GEOM})
                d = json.loads(r.content[0].text)
                print("  opened: design=%r bodies=%s faces_total=%s" % (
                    d.get("design"), d.get("body_count"),
                    sum(b["faces"] or 0 for b in d["bodies"])), flush=True)

            for op in ("unite", "subtract", "intersect"):
                print("=== %s ===" % op, flush=True)
                await reopen()
                r = await session.call_tool("scdm_boolean", {
                    "operation": op, "target": "stator", "tools": ["winding 1"]})
                d = json.loads(r.content[0].text)
                print("  ok=%s error=%s" % (d.get("ok"), d.get("error")), flush=True)
                print("  faces %s->%s  bodies %s->%s  vol %s->%s" % (
                    d.get("faces_before"), d.get("faces_after"),
                    d.get("body_count_before"), d.get("body_count_after"),
                    d.get("volume_before"), d.get("volume_after")), flush=True)
                if d.get("bodies_changed"):
                    for c in d["bodies_changed"][:4]:
                        print("    %s" % (c,), flush=True)
                if d.get("message"):
                    print("  msg: %s" % str(d["message"])[:160], flush=True)

            await session.call_tool("scdm_session_close", {})


try:
    asyncio.run(main())
    print("=== t19 end ===")
except Exception:
    print("FAIL:\n" + traceback.format_exc())
