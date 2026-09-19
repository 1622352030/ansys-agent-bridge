# -*- coding: utf-8 -*-
"""t14: 起服务器 -> 调 scdm_session_start -> 每 15 秒 py-spy dump 主线程 C 栈。"""
import asyncio, json, os, sys, time

SRC = r"C:\english_path\github_fork\ansys-agent-bridge\python\src"
PY = sys.executable
SPY = r"C:\Users\16223\AppData\Local\Programs\Python\Python313\Scripts\py-spy.exe"
LOG = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\t14_spy.log"

open(LOG, "w", encoding="utf-8").close()


def W(m):
    s = str(m)
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


async def spy_dump(pid, tag):
    p = await asyncio.create_subprocess_exec(
        SPY, "dump", "--pid", str(pid), "--locals",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await p.communicate()
    text = out.decode("utf-8", "replace")
    W("=== py-spy [%s] pid=%d ===" % (tag, pid))
    # 只保留最有信息量的尾部
    W("\n".join(text.splitlines()[-25:]))


async def main():
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONIOENCODING": "utf-8"}
    proc = await asyncio.create_subprocess_exec(
        PY, "-m", "ansys_bridge_mcp.server",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env,
    )
    W("server pid = %d" % proc.pid)

    async def drain():
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                return
            W("[err] " + raw.decode("utf-8", "replace").rstrip())

    asyncio.create_task(drain())
    n = [0]

    async def call(method, params=None, timeout=45):
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
                return None
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            msg = json.loads(text)
            if msg.get("id") == n[0]:
                W("%s -> %.1fs" % (method, time.time() - t0))
                return msg

    await call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                             "clientInfo": {"name": "raw", "version": "0"}}, timeout=30)
    proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode())
    await proc.stdin.drain()
    await call("tools/list", {}, timeout=30)

    async def dumper():
        for i in range(4):
            await asyncio.sleep(15)
            try:
                await spy_dump(proc.pid, "t=%ds" % (15 * (i + 1)))
            except Exception as exc:
                W("spy failed: %r" % (exc,))

    task = asyncio.create_task(dumper())
    W("--- calling scdm_session_start ---")
    n[0] += 1
    proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": n[0], "method": "tools/call",
                                  "params": {"name": "scdm_session_start",
                                             "arguments": {"hidden": True}}}) + "\n").encode())
    await proc.stdin.drain()
    try:
        while True:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
            if not raw:
                W("stdout closed")
                break
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            msg = json.loads(text)
            if msg.get("id") == n[0]:
                W("RETURNED: " + json.dumps(msg)[:400])
                break
    except asyncio.TimeoutError:
        W("!! no response in 60 s (still polling)")
    task.cancel()
    try:
        proc.kill()
    except Exception:
        pass
    await proc.wait()


asyncio.run(main())
W("=== t14 end ===")
