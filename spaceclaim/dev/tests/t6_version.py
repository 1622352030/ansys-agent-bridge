# -*- coding: utf-8 -*-
"""t6: 用字符串版本号 "242" 启动, 与整数 242 对比。带硬超时。"""
import sys, time, traceback

LOG = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\t6_version.log"
open(LOG, "w", encoding="utf-8").close()


def W(m):
    s = str(m)
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


which = sys.argv[1] if len(sys.argv) > 1 else "int"
version = 242 if which == "int" else "242"
W("=== t6 version=%r ===" % (version,))

from ansys.geometry.core import launch_modeler_with_spaceclaim

t0 = time.time()
try:
    m = launch_modeler_with_spaceclaim(version=version, hidden=True, timeout=120)
    W("OK %.1f s  backend=%s %s" % (time.time() - t0, m.client.backend_type, m.client.backend_version))
    m.close()
except Exception:
    W("FAIL %.1f s" % (time.time() - t0))
    W(traceback.format_exc())
W("=== t6 end ===")
