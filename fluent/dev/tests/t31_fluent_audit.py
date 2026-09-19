# -*- coding: utf-8 -*-
"""t31: 实测 ansys-fluent-mcp 的关键缺陷。

验证项:
  1. connect() 不传参数时, 实际以什么参数启动 (processor_count / ui_mode)
  2. run_code 沙箱: import numpy / open / subprocess 是否被拒
  3. run_code 能否正常写设置
  4. mesh_quality 的返回结构 (有无合格判定)
"""
import asyncio
import json
import os
import sys
import time
import traceback

VENV = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\.venv-pyfluent"
EXE = os.path.join(VENV, "Scripts", "ansys-fluent-mcp.exe")
LOG = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\t31_fluent_audit.log"
open(LOG, "w", encoding="utf-8").close()


def W(m=""):
    s = str(m)
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# 让它无头运行, 便于观察 processor_count 默认值的后果
env = {**os.environ, "PYFLUENT_FLUENT_ROOT": r"C:\Program Files\ANSYS Inc\v242\fluent"}


async def main():
    params = StdioServerParameters(command=EXE, args=[], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            W("工具数: %d" % len(tools.tools))

            async def call(name, args=None, timeout=600):
                r = await asyncio.wait_for(
                    session.call_tool(name, args or {}), timeout=timeout)
                txt = r.content[0].text if r.content else ""
                try:
                    return json.loads(txt)
                except Exception:
                    return {"_raw": txt[:400]}

            # ── 1. 默认参数连接 ───────────────────────────────────────
            W()
            W("=== 1. connect()  不传任何参数（用默认值）===")
            t0 = time.time()
            d = await call("connect", {})
            W("  用时 %.1fs" % (time.time() - t0))
            W("  返回: %s" % json.dumps(d, ensure_ascii=False)[:700])

            W()
            W("=== 1b. session_status ===")
            W("  %s" % json.dumps(await call("session_status", {}), ensure_ascii=False)[:500])

            # ── 2. 沙箱限制 ───────────────────────────────────────────
            W()
            W("=== 2. run_code 沙箱限制 ===")
            cases = [
                ("import numpy", "import numpy"),
                ("import pandas", "import pandas"),
                ("import math (应允许)", "import math\nresult = math.pi"),
                ("open()", "f = open(r'C:\\temp\\x.txt', 'w')"),
                ("subprocess", "import subprocess\nsubprocess.run(['cmd'])"),
                ("os.system", "import os\nos.system('dir')"),
                ("setattr 反射写", "setattr(solver, 'x', 1)"),
                ("eval", "eval('1+1')"),
            ]
            for label, code in cases:
                d = await call("run_code", {"code": code}, timeout=180)
                status = d.get("status") or d.get("_raw", "")[:60]
                err = d.get("error_code") or d.get("message", "")
                W("  %-22s status=%-8s %s" % (label, status, str(err)[:110]))

            await call("disconnect", {}, timeout=120)


try:
    asyncio.run(main())
    W()
    W("=== t31 end ===")
except Exception:
    W("FAIL:\n" + traceback.format_exc())
