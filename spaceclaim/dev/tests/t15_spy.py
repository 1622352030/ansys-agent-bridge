# -*- coding: utf-8 -*-
"""t15: 起服务器 -> 调 scdm_session_start -> 卡住后每 20 秒完整 py-spy dump。"""
import asyncio, json, os, sys, time

SRC = r"C:\english_path\github_fork\ansys-agent-bridge\python\src"
PY = sys.executable
SPY = r"C:\Users\16223\AppData\Local\Programs\Python\Python313\Scripts\py-spy.exe"
LOG = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\t15_spy.log"

open(LOG, "w", encoding="utf-8").close()


def W(m):
    s = str(m)
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


async def spy_full(pid, tag):
    p = await asyncio.create_subprocess_exec(
        SPY, "dump", "--pid", str(pid),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await p.communicate()
    W("=== py-spy [%s] pid=%d ===" % (tag, pid))
    W(out.decode("utf-8", "replace"))


async def main():
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONIOENCODING": "utf-8"}
    proc = await asyncio.create_subprocess_exec(
        PY, "-m", "ansys_bridge_mcp.server",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env,
    )
    W("server pid = %d" % proc.pid)
    W("--- baseline dump (before any tool call) ---")
    await spy_full(proc.pid, "baseline")

    async def drain():
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                return
            W("[err] " + raw.decode("utf-8", "replace").rstrip())

    asyncio.create_task(drain())
    n = [0]

    async def call(method, params=None, timeout=30):
        n[0] += 1
        req = {"jsonrpc": "2.0", "id": n[0], "method": method}
        if params is not None:
            req["params"] = params
        proc.stdin.write((json.dumps(req) + "\n").encode())
        await proc.stdin.drain()
        while True:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
            if not raw:
                return None
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            msg = json.loads(text)
            if msg.get("id") == n[0]:
                return msg

    await call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                             "clientInfo": {"name": "raw", "version": "0"}})
    proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode())
    await proc.stdin.drain()

    W("--- calling scdm_session_start ---")
    n[0] += 1
    proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": n[0], "method": "tools/call",
                                  "params": {"name": "scdm_session_start",
                                             "arguments": {"hidden": True}}}) + "\n").encode())
    await proc.stdin.drain()

    reader = asyncio.create_task(_read_reply(proc, n[0]))
    for i in range(3):
        await asyncio.sleep(25)
        if reader.done():
            break
        await spy_full(proc.pid, "hang+%ds" % (25 * (i + 1)))
    if not reader.done():
        await spy_full(proc.pid, "final")
        reader.cancel()
    else:
        W("REPLY: " + str(reader.result())[:500])

    try:
        proc.kill()
    except Exception:
        pass
    await proc.wait()


async def _read_reply(proc, want_id):
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            return "stdout closed"
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            continue
        msg = json.loads(text)
        if msg.get("id") == want_id:
            return msg


asyncio.run(main())
W("=== t15 end ===")
