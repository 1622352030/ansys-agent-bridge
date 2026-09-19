# -*- coding: utf-8 -*-
"""t33: 复测 stdout 是否会被非 JSON 字节污染。

t32 末尾客户端报 "Failed to parse JSONRPC message from server",
input_value='\\r'。用最小流程(只 connect + disconnect)复测。
"""
import asyncio
import json
import os
import sys
import traceback

VENV = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\.venv-pyfluent"
EXE = os.path.join(VENV, "Scripts", "ansys-fluent-mcp.exe")
LOG = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\t33_stdout.log"
open(LOG, "w", encoding="utf-8").close()


def W(m=""):
    s = str(m)
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

env = {**os.environ, "PYFLUENT_FLUENT_ROOT": r"C:\Program Files\ANSYS Inc\v242\fluent"}

# 记录原始 stdout 的每一行, 看有没有非 JSON 内容
raw_lines = []


async def main():
    params = StdioServerParameters(command=EXE, args=[], env=env)
    parse_failures = []

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(name, args=None, timeout=600):
                r = await asyncio.wait_for(
                    session.call_tool(name, args or {}), timeout=timeout)
                txt = r.content[0].text if r.content else ""
                try:
                    return json.loads(txt)
                except Exception:
                    return {"_raw": txt[:300]}

            W("=== 最小流程: connect(默认) -> session_status -> disconnect ===")
            W("  connect     : %s" % json.dumps(await call("connect", {}), ensure_ascii=False)[:200])
            W("  status      : %s" % json.dumps(await call("session_status", {}), ensure_ascii=False)[:200])
            W("  disconnect  : %s" % json.dumps(await call("disconnect", {}), ensure_ascii=False)[:200])
            W("  以上三步都拿到了合法 JSON 响应")
            W()
            W("=== 现在再调一次, 检验断开后是否还能正常通信 ===")
            try:
                d = await call("session_status", {}, timeout=120)
                W("  session_status 再调: %s" % json.dumps(d, ensure_ascii=False)[:200])
            except Exception as exc:
                W("  再调失败: %s: %s" % (type(exc).__name__, str(exc)[:300]))


try:
    asyncio.run(main())
    W()
    W("=== t33 end (未出现协议错误) ===")
except Exception:
    W("FAIL (可能就是协议污染):\n" + traceback.format_exc())
