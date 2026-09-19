# -*- coding: utf-8 -*-
"""端到端测试 ansys-agent-bridge MCP server(stdio)"""
import asyncio, json, os, sys, traceback
from pathlib import Path

SRC = r"C:\english_path\github_fork\ansys-agent-bridge\python\src"
GEOM = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\assembly31.scdoc"
PY = sys.executable
LOG = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\t5_mcp.log"


def W(m):
    print(m, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(str(m) + "\n")


open(LOG, "w", encoding="utf-8").close()

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

params = StdioServerParameters(
    command=PY,
    args=["-m", "ansys_bridge_mcp.server"],
    env={**os.environ, "PYTHONPATH": SRC},
)


async def main():
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            W("=== 已连接 MCP server ===")

            tools = await session.list_tools()
            W("工具 %d 个:" % len(tools.tools))
            for t in tools.tools:
                ro = t.annotations.readOnlyHint if t.annotations else None
                W("   %-26s readOnly=%s" % (t.name, ro))

            W("\n--- ansys_bridge_doctor ---")
            r = await session.call_tool("ansys_bridge_doctor", {})
            d = json.loads(r.content[0].text)
            W("   ok=%s  newest=%s  sc=%s" % (
                d.get("ok"), d["ansys"]["newest"],
                list(d["ansys"]["spaceclaim"])))
            W("   packages=%s" % d["packages"])

            W("\n--- scdm_session_start (version 自动探测) ---")
            r = await session.call_tool("scdm_session_start", {"hidden": True})
            d = json.loads(r.content[0].text)
            W("   " + json.dumps(d, ensure_ascii=False)[:300])

            W("\n--- scdm_open_file(assembly31.scdoc) ---")
            r = await session.call_tool("scdm_open_file", {"path": GEOM})
            d = json.loads(r.content[0].text)
            W("   ok=%s body_count=%s ns=%s" % (
                d.get("ok"), d.get("body_count"), d.get("named_selections")))
            if d.get("bodies"):
                for b in d["bodies"][:3]:
                    W("      %-14s faces=%-5s vol=%.10g" % (b["name"], b["faces"], b["volume_m3"]))

            W("\n--- scdm_collisions(all_pairs) ---")
            r = await session.call_tool("scdm_collisions", {"all_pairs": True})
            d = json.loads(r.content[0].text)
            W("   ok=%s pairs=%s counts=%s" % (d.get("ok"), d.get("pair_count"), d.get("counts")))

            W("\n--- scdm_boolean(unite stator + winding 1) — 应真生效 ---")
            r = await session.call_tool("scdm_boolean", {
                "operation": "unite", "target": "stator", "tools": ["winding 1"]})
            d = json.loads(r.content[0].text)
            W("   ok=%s faces %s->%s  bodies %s->%s  changed=%d" % (
                d.get("ok"), d.get("faces_before"), d.get("faces_after"),
                d.get("body_count_before"), d.get("body_count_after"),
                len(d.get("bodies_changed", []))))

            W("\n--- 重开, 测 subtract — 预期 no_geometry_change ---")
            await session.call_tool("scdm_open_file", {"path": GEOM})
            r = await session.call_tool("scdm_boolean", {
                "operation": "subtract", "target": "stator", "tools": ["winding 1"]})
            d = json.loads(r.content[0].text)
            W("   ok=%s error=%s" % (d.get("ok"), d.get("error")))
            W("   before=%s after=%s" % (d.get("before"), d.get("after")))
            W("   guidance=%s" % str(d.get("guidance"))[:120])

            W("\n--- 重开, 测 share_topology — 预期被 guard 拦下 ---")
            await session.call_tool("scdm_open_file", {"path": GEOM})
            r = await session.call_tool("scdm_share_topology", {})
            d = json.loads(r.content[0].text)
            W("   ok=%s error=%s" % (d.get("ok"), d.get("error")))
            W("   message=%s" % str(d.get("message"))[:200])

            W("\n--- scdm_export(step) ---")
            out = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\out_mcp"
            r = await session.call_tool("scdm_export", {"format": "step", "location": out})
            d = json.loads(r.content[0].text)
            W("   ok=%s path=%s exists=%s bytes=%s" % (
                d.get("ok"), d.get("path"), d.get("exists"), d.get("bytes")))

            W("\n--- scdm_session_close ---")
            r = await session.call_tool("scdm_session_close", {})
            W("   " + r.content[0].text[:120])


try:
    asyncio.run(main())
    W("\n=== T5 结束 ===")
except Exception:
    W("异常:\n" + traceback.format_exc())
