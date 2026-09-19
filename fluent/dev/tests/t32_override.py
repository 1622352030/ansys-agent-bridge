# -*- coding: utf-8 -*-
"""t32: 实测 connect_kwargs 能否覆盖处理器数 / 界面模式 / 精度。

这是判断"默认参数"是否算缺陷的决定性测试:
能改 -> 只是默认值, 不是缺陷; 改不了 -> 才是缺陷。
用 24 核 + no_gui 验证, 正是实际工作要用的配置。
"""
import asyncio
import json
import os
import sys
import time
import traceback

VENV = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\.venv-pyfluent"
EXE = os.path.join(VENV, "Scripts", "ansys-fluent-mcp.exe")
LOG = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\t32_override.log"
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
                    return {"_raw": txt[:400]}

            # ── 覆盖全部三个参数 ──────────────────────────────────────
            W("=== connect(connect_kwargs = 24 核 / no_gui / double) ===")
            kwargs = {
                "processor_count": 24,
                "ui_mode": "no_gui",
                "precision": "double",
                "dimension": 3,
                "mode": "solver",
            }
            W("  传入: %s" % json.dumps(kwargs))
            t0 = time.time()
            d = await call("connect", {"connect_kwargs": kwargs})
            W("  用时 %.1fs" % (time.time() - t0))
            W("  返回: %s" % json.dumps(d, ensure_ascii=False)[:500])

            W()
            W("=== 连接后: 用 run_code 读回实际核数 ===")
            for label, code in (
                ("读回 processor 相关", "print(solver)"),
                ("session 属性", "print([a for a in dir(session) if not a.startswith('_')][:40])"),
            ):
                d = await call("run_code", {"code": code}, timeout=300)
                W("  %-22s status=%s out=%s" % (
                    label, d.get("status"), str(d.get("stdout") or d.get("result") or d)[:200]))

            W()
            W("=== 求解器状态(能看出实际配置) ===")
            d = await call("solver_status", {}, timeout=300)
            W("  %s" % json.dumps(d, ensure_ascii=False)[:700])

            await call("disconnect", {}, timeout=300)


try:
    asyncio.run(main())
    W()
    W("=== t32 end ===")
except Exception:
    W("FAIL:\n" + traceback.format_exc())
