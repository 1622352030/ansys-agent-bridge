# -*- coding: utf-8 -*-
"""t26: 通过 MCP stdio 端到端验证 scdm_inspect_geometry。"""
import asyncio
import json
import os
import sys
import time
import traceback

SRC = r"C:\english_path\github_fork\ansys-agent-bridge\python\src"
MODEL = r"C:\english_path\fluent\9_1_youcang\scdoc\zhuangpeiti_fix_9_10_1.scdoc"
ARCH = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\assembly31.scdoc"
PY = sys.executable
LOG = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\t26_mcp_inspect.log"

open(LOG, "w", encoding="utf-8").close()


def W(m=""):
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
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            W("工具数: %d" % len(names))
            W("含 scdm_inspect_geometry: %s" % ("scdm_inspect_geometry" in names))
            W("只读标注: %s" % [
                (t.name, t.annotations.readOnlyHint) for t in tools.tools
                if t.name == "scdm_inspect_geometry"])

            await session.call_tool("scdm_session_start", {"hidden": True})

            # ── 失败模型 ──────────────────────────────────────────────
            W()
            W("=== 体检: zhuangpeiti_fix_9_10_1.scdoc (建网失败的那个) ===")
            await session.call_tool("scdm_open_file", {"path": MODEL})
            t0 = time.time()
            r = await session.call_tool("scdm_inspect_geometry", {})
            d = json.loads(r.content[0].text)
            W("  用时 %.1fs  ok=%s  clean=%s" % (time.time() - t0, d.get("ok"), d.get("clean")))
            W("  problems_found = %s" % json.dumps(d.get("problems_found"), ensure_ascii=False))

            dup = d["checks"]["duplicate_faces"]
            W("  duplicate_faces: %d 组 / %d 个面" % (dup["group_count"], dup["object_count"]))
            for g in dup["groups"]:
                W("      %s" % "  |  ".join(
                    "%s %s area=%.10g" % (f.get("body"), f.get("id"), f.get("area_m2", 0))
                    for f in g.get("faces", [])))

            sh = d["checks"]["short_edges"]
            W("  short_edges: %d 条, 阈值 %s" % (sh["object_count"], sh["threshold_used"]))
            W("      by_body = %s" % json.dumps(sh.get("by_body", {}), ensure_ascii=False))
            W("      span = %s" % json.dumps(sh.get("span", {}), ensure_ascii=False))
            W("      返回体大小 = %d 字节" % len(r.content[0].text))

            # ── 指定检查项 ────────────────────────────────────────────
            W()
            W("=== 只查 duplicate_faces ===")
            r = await session.call_tool("scdm_inspect_geometry", {"checks": ["duplicate_faces"]})
            d2 = json.loads(r.content[0].text)
            W("  keys=%s  groups=%s" % (list(d2["checks"].keys()),
                                        d2["checks"]["duplicate_faces"]["group_count"]))

            # ── 显式阈值 ──────────────────────────────────────────────
            W()
            W("=== short_edges 指定阈值 0.005 ===")
            r = await session.call_tool(
                "scdm_inspect_geometry",
                {"checks": ["short_edges"], "short_edge_length": 0.005})
            d3 = json.loads(r.content[0].text)
            sh3 = d3["checks"]["short_edges"]
            W("  scan=%s" % json.dumps(sh3["scan"], ensure_ascii=False))
            W("  object_count=%s" % sh3["object_count"])

            # ── 不可靠项显式请求 ──────────────────────────────────────
            W()
            W("=== 显式请求 inexact_edges (应带 unreliable 标注) ===")
            r = await session.call_tool("scdm_inspect_geometry", {"checks": ["inexact_edges"]})
            d4 = json.loads(r.content[0].text)
            W("  %s" % json.dumps(d4["checks"]["inexact_edges"], ensure_ascii=False)[:220])

            # ── 另一个模型(干净的 846 面装配体) ────────────────────────
            W()
            W("=== 体检: assembly31.scdoc (对照) ===")
            await session.call_tool("scdm_open_file", {"path": ARCH})
            r = await session.call_tool("scdm_inspect_geometry",
                                        {"checks": ["duplicate_faces", "short_edges"]})
            d5 = json.loads(r.content[0].text)
            W("  clean=%s  problems=%s" % (d5.get("clean"),
                                           json.dumps(d5.get("problems_found"), ensure_ascii=False)))

            # ── 错误路径 ──────────────────────────────────────────────
            W()
            W("=== 错误路径: 不存在的检查项 ===")
            r = await session.call_tool("scdm_inspect_geometry", {"checks": ["nope"]})
            d6 = json.loads(r.content[0].text)
            W("  ok=%s error=%s" % (d6.get("ok"), str(d6.get("message"))[:100]))

            await session.call_tool("scdm_session_close", {})


try:
    asyncio.run(main())
    W()
    W("=== t26 end ===")
except Exception:
    W("FAIL:\n" + traceback.format_exc())
