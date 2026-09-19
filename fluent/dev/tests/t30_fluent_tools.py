# -*- coding: utf-8 -*-
"""列出 ansys-fluent-mcp 已注册的工具, 作为 Fluent 侧"已实现"基准。

用该 venv 自己的 mcp/fastmcp 版本运行。
"""
import asyncio
import json
import os
import sys
import traceback

VENV = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\.venv-pyfluent"
EXE = os.path.join(VENV, "Scripts", "ansys-fluent-mcp.exe")
OUT = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\fluent_mcp_tools.json"


async def main():
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        # mcp 2.x may have moved these
        import mcp
        print("mcp version:", getattr(mcp, "__version__", "?"))
        print("mcp attrs:", [a for a in dir(mcp) if "lient" in a or "Stdio" in a])
        import mcp.client as c
        print("mcp.client attrs:", [a for a in dir(c) if not a.startswith("_")])
        raise

    params = StdioServerParameters(command=EXE, args=[], env={**os.environ})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            rows = []
            for t in tools.tools:
                desc = (t.description or "").strip().split("\n")[0][:150]
                # mcp 2.x renamed inputSchema -> input_schema
                schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}
                props = list((schema.get("properties") or {}).keys())
                rows.append({"name": t.name, "description": desc, "params": props,
                             "required": schema.get("required") or []})
            with open(OUT, "w", encoding="utf-8") as f:
                json.dump(rows, f, indent=1, ensure_ascii=False)
            print("工具数: %d" % len(rows))
            for r in rows:
                print("  %-34s %s" % (r["name"], r["description"][:80]))
            print("written:", OUT)


try:
    asyncio.run(main())
except Exception:
    print("FAIL:\n" + traceback.format_exc())
