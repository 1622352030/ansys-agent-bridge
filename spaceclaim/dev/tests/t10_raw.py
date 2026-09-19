# -*- coding: utf-8 -*-
"""t10: 自己起 MCP server 进程, 原始 JSON-RPC, 完整捕获 stderr。

绕过 mcp SDK 的 stdio_client, 这样 stderr 不会被吞掉。
"""
import asyncio, json, os, sys, time

SRC = r"C:\english_path\github_fork\ansys-agent-bridge\python\src"
PY = sys.executable
LOG = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\t10_raw.log"

lines = []


def W(m):
    s = str(m)
    lines.append(s)
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


open(LOG, "w", encoding="utf-8").close()


async def drain_stderr(stream):
    while True:
        raw = await stream.readline()
        if not raw:
            return
        W("[stderr] " + raw.decode("utf-8", "replace").rstrip())


async def main():
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONIOENCODING": "utf-8",
           "ANSYS_BRIDGE_LOG_LEVEL": "INFO"}
    proc = await asyncio.create_subprocess_exec(
        PY, "-m", "ansys_bridge_mcp.server",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env,
    )
    asyncio.create_task(drain_stderr(proc.stderr))
    n = [0]

    async def call(method, params=None, timeout=240):
        n[0] += 1
        req = {"jsonrpc": "2.0", "id": n[0], "method": method}
        if params is not None:
            req["params"] = params
        proc.stdin.write((json.dumps(req) + "\n").encode())
        await proc.stdin.drain()
        t0 = time.time()
        while True:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
            if not raw:
                W("!! stdout closed while waiting for %s" % method)
                return None
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                W("!! non-JSON on stdout (PROTOCOL VIOLATION): %r" % text[:300])
                continue
            if msg.get("id") == n[0]:
                W("%s -> %.1fs" % (method, time.time() - t0))
                return msg

    init = await call("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "raw", "version": "0"},
    }, timeout=60)
    W("initialize ok: %s" % (init is not None))
    proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode())
    await proc.stdin.drain()

    r = await call("tools/list", {}, timeout=60)
    W("tools: %d" % len(r["result"]["tools"]))

    W("--- doctor ---")
    r = await call("tools/call", {"name": "ansys_bridge_doctor", "arguments": {}}, timeout=120)
    W(json.dumps(r["result"].get("structuredContent") or r["result"], ensure_ascii=False)[:300])

    W("--- scdm_session_start (hidden) ---")
    try:
        r = await call("tools/call", {"name": "scdm_session_start", "arguments": {"hidden": True}}, timeout=240)
        if r is not None:
            W(json.dumps(r["result"].get("structuredContent") or r["result"], ensure_ascii=False)[:600])
    except asyncio.TimeoutError:
        W("!! TIMEOUT after 240 s waiting for scdm_session_start")

    W("--- cleanup ---")
    try:
        proc.kill()
    except Exception:
        pass
    await proc.wait()


try:
    asyncio.run(main())
    W("=== t10 end ===")
except Exception:
    import traceback
    W("FAIL:\n" + traceback.format_exc())
