# -*- coding: utf-8 -*-
"""t17: 先在全新建的 python 进程里 import ansys.geometry.core, 再调用 start。
用的就是 server.py 里的同一个函数, 但导入提前到这个新进程的顶层。"""
import sys, time

sys.path.insert(0, r"C:\english_path\github_fork\ansys-agent-bridge\python\src")

print("top-level import ...", flush=True)
t = time.time()
import ansys.geometry.core  # noqa: F401
print("imported in %.1fs" % (time.time() - t), flush=True)

from ansys_bridge_mcp.scdm import Session

s = Session()
t = time.time()
try:
    r = s.start(hidden=True, timeout=120)
    print("start ok %.1fs" % (time.time() - t), flush=True)
    print(r, flush=True)
    s.close()
except Exception as e:
    print("EXC %.1fs %r" % (time.time() - t, e), flush=True)
