# -*- coding: utf-8 -*-
"""t37: 追查 max_ortho_skew 为什么是 null, 以及能否用别的方式拿到偏斜度。"""
import asyncio
import json
import os
import sys
import traceback

VENV = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\.venv-pyfluent"
EXE = os.path.join(VENV, "Scripts", "ansys-fluent-mcp.exe")
CASE = r"C:\english_path\fluent\9_1_youcang\case\deliver_oil\motor_cht_oil.cas.h5"
LOG = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\t37_skew.log"
open(LOG, "w", encoding="utf-8").close()


def W(m=""):
    s = str(m)
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

env = {**os.environ, "PYFLUENT_FLUENT_ROOT": r"C:\Program Files\ANSYS Inc\v242\fluent"}


async def main():
    params = StdioServerParameters(command=EXE, args=[], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(name, args=None, timeout=1200):
                r = await asyncio.wait_for(
                    session.call_tool(name, args or {}), timeout=timeout)
                txt = r.content[0].text if r.content else ""
                try:
                    return json.loads(txt)
                except Exception:
                    return {"_raw": txt[:400]}

            async def code(label, snippet, timeout=600):
                d = await call("run_code", {"code": snippet}, timeout=timeout)
                out = d.get("stdout")
                err = str(d.get("stderr") or d.get("message") or "")
                W("  %-32s status=%s" % (label, d.get("status")))
                if out:
                    W("      out: %s" % str(out).strip()[:500])
                if err and d.get("status") != "ok":
                    W("      err: %s" % err.strip().splitlines()[-1][:180])
                return d

            await call("connect", {"connect_kwargs": {
                "processor_count": 8, "ui_mode": "no_gui", "mode": "solver"}})
            await code("read_case", "solver.file.read_case(file_name=r'%s')\nprint('ok')" % CASE,
                       timeout=900)

            W()
            W("=== 1. mesh.quality() 原始返回 ===")
            await code("quality()", "print(repr(session.settings.mesh.quality()))")

            W()
            W("=== 2. PyFluent 是否有别的质量入口 ===")
            await code("mesh 下与 quality 相关的名字",
                       "print([a for a in dir(session.settings.mesh) if 'qual' in a.lower() or 'skew' in a.lower()])")
            await code("settings 顶层相关名字",
                       "print([a for a in dir(session.settings) if 'qual' in a.lower() or 'skew' in a.lower()])")

            W()
            W("=== 3. TUI 通道取偏斜度 ===")
            for cmd in (
                "session.execute_tui(r'/mesh/quality')",
                "session.execute_tui(r'/mesh/check')",
                "session.execute_tui(r'(inquire-quality)')",
            ):
                await code(cmd[:46], "print(%s)" % cmd)

            W()
            W("=== 4. 用 field data 取最大偏斜度 ===")
            await code("field_info 可用字段",
                       "print([a for a in dir(session.field_info) if not a.startswith('_')][:25])")
            await code("是否有 skewness 场",
                       "print([f for f in dir(session.field_info) if 'skew' in f.lower()])")

            W()
            W("=== 5. 逐 zone 的 quality 是否可行 ===")
            await code("mesh.quality 的参数",
                       "import inspect\nprint(inspect.signature(session.settings.mesh.quality))")

            await call("disconnect", {}, timeout=300)


try:
    asyncio.run(main())
    W()
    W("=== t37 end ===")
except Exception:
    W("FAIL:\n" + traceback.format_exc())
