# -*- coding: utf-8 -*-
"""t36: 拉取真实 case 的网格质量数据, 为"合格判定"提供实测依据。"""
import asyncio
import json
import os
import sys
import time
import traceback

VENV = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\.venv-pyfluent"
EXE = os.path.join(VENV, "Scripts", "ansys-fluent-mcp.exe")
CASES = [
    r"C:\english_path\fluent\9_1_youcang\case\deliver_oil\motor_cht_oil.cas.h5",
]
LOG = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\t36_meshq.log"
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

            await call("connect", {"connect_kwargs": {
                "processor_count": 8, "ui_mode": "no_gui", "mode": "solver"}})

            for case in CASES:
                W("=== %s ===" % os.path.basename(case))
                await call("run_code", {"code":
                    "solver.file.read_case(file_name=r'%s')\nprint('ok')" % case},
                    timeout=900)
                W()
                W("--- mesh_quality (含 mesh.check) ---")
                d = await call("mesh_quality", {"include_check": True}, timeout=900)
                W(json.dumps(d, ensure_ascii=False, indent=1)[:2500])

                W()
                W("--- 区域内网格质量(逐 zone) ---")
                d2 = await call("run_code", {"code": """
import json
rows = []
for name in list(solver.setup.cell_zone_conditions.keys()):
    try:
        c = solver.setup.cell_zone_conditions[name]
        rows.append(name)
    except Exception:
        pass
print('ZONE_COUNT=', len(rows))
print('ZONES=', json.dumps(rows[:40]))
"""}, timeout=600)
                W("  %s" % str(d2.get("stdout") or d2.get("message"))[:600])

            await call("disconnect", {}, timeout=300)


try:
    asyncio.run(main())
    W()
    W("=== t36 end ===")
except Exception:
    W("FAIL:\n" + traceback.format_exc())
