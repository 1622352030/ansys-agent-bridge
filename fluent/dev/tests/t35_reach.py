# -*- coding: utf-8 -*-
"""t35: 读入真实 case 后, 判断所谓缺口是"能力不可达"还是"未封装但可达"。

判定原则: 能通过 run_code 调 Fluent 自身 API 完成的, 就不是能力缺失。
"""
import asyncio
import json
import os
import sys
import time
import traceback

VENV = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\.venv-pyfluent"
EXE = os.path.join(VENV, "Scripts", "ansys-fluent-mcp.exe")
CASE = r"C:\english_path\fluent\9_1_youcang\case\deliver_oil\motor_cht_oil.cas.h5"
LOG = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\t35_reach.log"
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
                    return {"_raw": txt[:300]}

            async def code(label, snippet, timeout=400):
                d = await call("run_code", {"code": snippet}, timeout=timeout)
                out = d.get("stdout")
                err = d.get("stderr") or d.get("message") or ""
                st = d.get("status")
                detail = str(out).strip() if out else ""
                if not detail and err:
                    detail = str(err).strip().splitlines()[-1][:130] if str(err).strip() else ""
                W("  %-34s status=%-6s %s" % (label, st, detail[:150]))
                return d

            W("=== 连接 8 核 no_gui solver ===")
            d = await call("connect", {"connect_kwargs": {
                "processor_count": 8, "ui_mode": "no_gui", "mode": "solver"}})
            W("  connect: %s" % d.get("status"))

            # ── 1. 案例读写可达性 ─────────────────────────────────────
            W()
            W("=== 1. 案例读写: run_code 能否调 Fluent 的 file API ===")
            await code("solver.file 方法",
                       "print([a for a in dir(solver.file) if not a.startswith('_')])")
            W("  现在读 case: %s" % os.path.basename(CASE))
            t0 = time.time()
            d = await code("read_case(...)",
                           "solver.file.read_case(file_name=r'%s')\nprint('CASE_READ_OK')" % CASE,
                           timeout=900)
            W("  读 case 用时 %.1fs  状态=%s" % (time.time() - t0, d.get("status")))

            # ── 2. 读入后 setup 树可达性 ──────────────────────────────
            W()
            W("=== 2. 读入 case 后各域可达性 ===")
            await code("setup.models 子项",
                       "print([a for a in dir(solver.setup.models) if not a.startswith('_')][:20])")
            await code("energy.enabled 读值",
                       "print(solver.setup.models.energy.enabled.get_state())")
            await code("boundary_conditions 列表",
                       "print(list(solver.setup.boundary_conditions.keys())[:15])")
            await code("cell_zone_conditions 列表",
                       "print(list(solver.setup.cell_zone_conditions.keys())[:15])")
            await code("materials 列表",
                       "print(list(solver.setup.materials.keys())[:10])")
            await code("solution 方法",
                       "print([a for a in dir(solver.solution) if not a.startswith('_')][:20])")
            await code("mesh 方法",
                       "print([a for a in dir(solver.mesh) if not a.startswith('_')][:20])")

            # ── 3. 写设置 + 读回验证闭环 ──────────────────────────────
            W()
            W("=== 3. 写后验证能否闭环 ===")
            await code("写前读 enable",
                       "print('BEFORE=', solver.setup.models.energy.enabled.get_state())")
            await code("写 energy.enabled = True",
                       "solver.setup.models.energy.enabled = True")
            await code("写后读回",
                       "print('AFTER=', solver.setup.models.energy.enabled.get_state())")
            d = await call("get_state", {"paths": ["setup/models/energy/enabled"]})
            W("  get_state 工具读回 -> %s" % json.dumps(d, ensure_ascii=False)[:250])

            # ── 4. 体积热源路径是否可达 ───────────────────────────────
            W()
            W("=== 4. 体积热源与监视器路径 ===")
            await code("cell_zone_conditions 子项",
                       "cz = list(solver.setup.cell_zone_conditions.keys())[:3]\nprint(cz)\n"
                       "print([a for a in dir(solver.setup.cell_zone_conditions[cz[0]]) "
                       "if not a.startswith('_')][:18] if cz else 'none')")
            await code("report_definitions 方法",
                       "print([a for a in dir(solver.solution.report_definitions) "
                       "if not a.startswith('_')][:15])")

            await call("disconnect", {}, timeout=300)


try:
    asyncio.run(main())
    W()
    W("=== t35 end ===")
except Exception:
    W("FAIL:\n" + traceback.format_exc())
