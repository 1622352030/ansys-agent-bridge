# -*- coding: utf-8 -*-
"""t34: 区分"能力不可达"与"未封装但可达"。

对每一项所谓缺口, 探测它能否通过 run_code 调用 Fluent 自身 API 完成。
只做属性探测与一次读写回环, 不实际读文件、不真跑求解。
"""
import asyncio
import json
import os
import sys
import time
import traceback

VENV = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\.venv-pyfluent"
EXE = os.path.join(VENV, "Scripts", "ansys-fluent-mcp.exe")
LOG = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\t34_reachability.log"
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

            async def call(name, args=None, timeout=900):
                r = await asyncio.wait_for(
                    session.call_tool(name, args or {}), timeout=timeout)
                txt = r.content[0].text if r.content else ""
                try:
                    return json.loads(txt)
                except Exception:
                    return {"_raw": txt[:300]}

            async def probe(label, code):
                d = await call("run_code", {"code": code}, timeout=300)
                out = d.get("stdout") or d.get("result") or ""
                if isinstance(out, dict):
                    out = out.get("stdout") or json.dumps(out, ensure_ascii=False)
                status = d.get("status")
                W("  %-30s status=%-6s %s" % (label, status, str(out).strip()[:150]))
                return d

            W("=== 连接 (24核 / no_gui / solver) ===")
            d = await call("connect", {"connect_kwargs": {
                "processor_count": 24, "ui_mode": "no_gui",
                "precision": "double", "mode": "solver"}})
            W("  %s" % d.get("status"))

            W()
            W("=== A. 案例与网格读写是否可达 ===")
            await probe("solver.file 可用方法",
                        "print([a for a in dir(solver.file) if not a.startswith('_')])")

            W()
            W("=== B. 初始化与迭代是否可达 ===")
            await probe("solver.solution 可用方法",
                        "print([a for a in dir(solver.solution) if not a.startswith('_')])")

            W()
            W("=== C. 网格相关是否可达 ===")
            await probe("solver.mesh 可用方法",
                        "print([a for a in dir(solver.mesh) if not a.startswith('_')])")

            W()
            W("=== D. 写设置 + 读回验证是否形成闭环 ===")
            await probe("写前读 energy.enabled",
                        "print(solver.setup.models.energy.enabled.get_state())")
            await probe("写 energy.enabled = True",
                        "solver.setup.models.energy.enabled = True")
            await probe("写后读回",
                        "print(solver.setup.models.energy.enabled.get_state())")

            W()
            W("=== E. 边界条件命名对象是否可达 ===")
            await probe("列出 BC",
                        "print(list(solver.setup.boundary_conditions.keys())[:12])")

            W()
            W("=== F. TUI 命令通道 ===")
            await probe("execute_tui 存在性",
                        "print(hasattr(session, 'execute_tui'))")

            W()
            W("=== G. get_state 工具读回设置 ===")
            d = await call("get_state", {"paths": ["setup/models/energy/enabled"]})
            W("  get_state -> %s" % json.dumps(d, ensure_ascii=False)[:300])

            await call("disconnect", {}, timeout=300)


try:
    asyncio.run(main())
    W()
    W("=== t34 end ===")
except Exception:
    W("FAIL:\n" + traceback.format_exc())
