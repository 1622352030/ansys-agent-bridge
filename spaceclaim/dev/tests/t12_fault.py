# -*- coding: utf-8 -*-
"""t12: 用 faulthandler 每 20 秒 dump 一次主线程 C 栈, 找出真卡点。

通过环境变量 ANSYS_BRIDGE_FAULTHANDLER=1 开启。
"""
import asyncio, json, os, sys, time

SRC = r"C:\english_path\github_fork\ansys-agent-bridge\python\src"
PY = sys.executable
LOG = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\t12_fault.log"

open(LOG, "w", encoding="utf-8").close()


def W(m):
    s = str(m)
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


async def main():
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONIOENCODING": "utf-8"}
    proc = await asyncio.create_subprocess_exec(
        PY, "-X", "faulthandler", "-m", "ansys_bridge_mcp.server",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env,
    )

    async def drain():
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                return
            W("[err] " + raw.decode("utf-8", "replace").rstrip())

    asyncio.create_task(drain())
    n = [0]

    async def call(method, params=None, timeout=200):
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
                             "clientInfo": {"name": "raw", "version": "0"}}, timeout=60)
    proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode())
    await proc.stdin.drain()

    # 后台每 20 秒给服务器发一个 SIGABRT 风格的 dump: Windows 上用 py-spy 更实际,
    # 这里改用 py-spy 在外部采样。先只发请求。
    W("--- scdm_session_start ---")
    try:
        r = await call("tools/call", {"name": "scdm_session_start", "arguments": {"hidden": True}}, timeout=200)
        W("returned: %s" % (json.dumps(r)[:300] if r else None))
    except asyncio.TimeoutError:
        W("!! TIMEOUT 200 s")

    # 卡住时 dump
    spy = r"C:\Users\16223\AppData\Local\Programs\Python\Python313\Scripts\py-spy.exe"
    if os.path.exists(spy):
        p = await asyncio.create_subprocess_exec(
            spy, "dump", "--pid", str(proc.pid),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await p.communicate()
        W("=== py-spy dump of pid %d ===" % proc.pid)
        W(out.decode("utf-8", "replace"))

    proc.kill()
    await proc.wait()


asyncio.run(main())
W("=== t12 end ===")
