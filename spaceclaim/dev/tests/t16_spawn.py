# -*- coding: utf-8 -*-
"""t16: 从 asyncio 里 spawn 一个 python 子进程去 import numpy/ansys.geometry,
看是不是"asyncio 父进程 + 管道 stdio"这个组合让 C 扩展导入卡死。"""
import asyncio, os, sys, time

PY = sys.executable

CASES = {
    "numpy": "import numpy; print('numpy', numpy.__version__, flush=True)",
    "agc": "import ansys.geometry.core as a; print('agc', a.__version__, flush=True)",
}


async def run_case(name, code, redirect):
    kwargs = {}
    if redirect:
        kwargs = dict(stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                      stderr=asyncio.subprocess.PIPE)
    else:
        kwargs = dict(stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                      stderr=asyncio.subprocess.DEVNULL)
    t0 = time.time()
    proc = await asyncio.create_subprocess_exec(PY, "-c", code, **kwargs)
    try:
        if redirect:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=90)
            print("  %-6s redirect=%-5s  %.1fs  rc=%s out=%r" % (
                name, redirect, time.time() - t0, proc.returncode,
                out.decode("utf-8", "replace").strip()[:80]))
        else:
            await asyncio.wait_for(proc.wait(), timeout=90)
            print("  %-6s redirect=%-5s  %.1fs  rc=%s (no output captured)" % (
                name, redirect, time.time() - t0, proc.returncode))
    except asyncio.TimeoutError:
        print("  %-6s redirect=%-5s  TIMEOUT after 90 s" % (name, redirect))
        proc.kill()
        await proc.wait()


async def main():
    print("=== t16: child import under asyncio ===")
    for redirect in (False, True):
        for name, code in CASES.items():
            await run_case(name, code, redirect)
    print("=== t16 end ===")


asyncio.run(main())
