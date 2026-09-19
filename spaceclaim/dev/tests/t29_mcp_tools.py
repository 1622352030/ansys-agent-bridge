# -*- coding: utf-8 -*-
"""t29: 端到端实测 scdm_min_distance / scdm_insert_file / scdm_transform。"""
import asyncio
import hashlib
import json
import os
import sys
import time
import traceback

SRC = r"C:\english_path\github_fork\ansys-agent-bridge\python\src"
MODEL = r"C:\english_path\fluent\9_1_youcang\scdoc\zhuangpeiti_fix_9_10_1.scdoc"
OTHER = r"C:\english_path\fluent\9_1_youcang\scdoc\youcang_new_9_11.scdoc"
PY = sys.executable
LOG = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\t29_mcp_tools.log"
open(LOG, "w", encoding="utf-8").close()


def W(m=""):
    s = str(m)
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


h0 = sha256(MODEL)

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
            W("工具数: %d  (新增 3 个)" % len(tools.tools))
            await session.call_tool("scdm_session_start", {"hidden": True})

            async def call(name, args=None):
                r = await session.call_tool(name, args or {})
                return json.loads(r.content[0].text)

            async def reopen():
                await session.call_tool("scdm_open_file", {"path": MODEL})

            # ── 1. 最小间距 ───────────────────────────────────────────
            W()
            W("=== scdm_min_distance ===")
            await reopen()
            for a, b in (("stator", "winding 1"), ("stator", "pip"), ("stator", "inlet"),
                         ("winding 1", "winding 2"), ("stator", "outlet")):
                d = await call("scdm_min_distance", {"left": a, "right": b})
                W("  %-11s <-> %-11s  ok=%s  %s m  接触=%s" % (
                    a, b, d.get("ok"), d.get("distance_m"), d.get("touching")))
            d = await call("scdm_min_distance", {"left": "stator", "right": "nope"})
            W("  错误路径: ok=%s error=%s" % (d.get("ok"), d.get("error")))

            # ── 2. 变换 scale ─────────────────────────────────────────
            W()
            W("=== scdm_transform: scale 1.5 ===")
            await reopen()
            d = await call("scdm_transform", {
                "operation": "scale", "bodies": ["stator"], "scale_factor": 1.5})
            vb = (d.get("volume_before_m3") or {}).get("stator")
            va = (d.get("volume_after_m3") or {}).get("stator")
            W("  ok=%s verified=%s moved=%s" % (d.get("ok"), d.get("verified"), d.get("moved")))
            W("  体积 %s -> %s   比值=%s  (期望 1.5^3=3.375)" % (
                vb, va, round(va / vb, 6) if vb and va else None))

            # ── 3. 变换 rotate ────────────────────────────────────────
            W()
            W("=== scdm_transform: rotate z 90deg ===")
            await reopen()
            d = await call("scdm_transform", {
                "operation": "rotate", "bodies": ["stator"], "angle_deg": 90, "axis": "z"})
            fb = (d.get("fingerprint_before") or {}).get("stator")
            fa = (d.get("fingerprint_after") or {}).get("stator")
            W("  ok=%s verified=%s" % (d.get("ok"), d.get("verified")))
            W("  before min=%s max=%s" % (fb and fb.get("min"), fb and fb.get("max")))
            W("  after  min=%s max=%s" % (fa and fa.get("min"), fa and fa.get("max")))
            if fb and fa:
                W("  z 是否不变: %s (绕 z 旋转应不变)" % (
                    fb["min"][2] == fa["min"][2] and fb["max"][2] == fa["max"][2]))

            # ── 4. 变换 mirror ────────────────────────────────────────
            W()
            W("=== scdm_transform: mirror (法向 z) ===")
            await reopen()
            d = await call("scdm_transform", {"operation": "mirror", "bodies": ["stator"], "axis": "z"})
            W("  ok=%s verified=%s" % (d.get("ok"), d.get("verified")))

            # ── 5. 变换错误路径 ───────────────────────────────────────
            W()
            W("=== 变换参数错误 ===")
            for label, args in (
                ("未知操作", {"operation": "warble", "bodies": ["stator"]}),
                ("scale 缺参数", {"operation": "scale", "bodies": ["stator"]}),
                ("负因子", {"operation": "scale", "bodies": ["stator"], "scale_factor": -1}),
                ("实体不存在", {"operation": "scale", "bodies": ["nope"], "scale_factor": 2}),
                ("非法轴", {"operation": "rotate", "bodies": ["stator"], "angle_deg": 10, "axis": "w"}),
            ):
                d = await call("scdm_transform", args)
                msg = str(d.get("message") or "")[:70]
                W("  %-12s ok=%s error=%s  %s" % (label, d.get("ok"), d.get("error"), msg))

            # ── 6. 插入文件 ───────────────────────────────────────────
            W()
            W("=== scdm_insert_file ===")
            await reopen()
            before = (await call("scdm_list_bodies")).get("body_count")
            t0 = time.time()
            d = await call("scdm_insert_file", {"path": OTHER})
            W("  ok=%s 用时 %.1fs" % (d.get("ok"), time.time() - t0))
            W("  component=%r  body %s -> %s" % (
                d.get("component"), d.get("bodies_before"), d.get("bodies_after")))
            after = (await call("scdm_list_bodies")).get("body_count")
            W("  list_bodies 复核: %s -> %s" % (before, after))
            d = await call("scdm_insert_file", {"path": r"C:\nope\missing.scdoc"})
            W("  错误路径: ok=%s error=%s" % (d.get("ok"), d.get("error")))

            await session.call_tool("scdm_session_close", {})


try:
    asyncio.run(main())
    W()
    W("源文件 sha256 未变: %s" % (sha256(MODEL) == h0))
    W("=== t29 end ===")
except Exception:
    W("FAIL:\n" + traceback.format_exc())
