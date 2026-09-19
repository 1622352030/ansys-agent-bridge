# -*- coding: utf-8 -*-
"""t8: 在工作线程里启动 SpaceClaim —— 复现 MCP 工具的执行环境。"""
import concurrent.futures as cf
import time, traceback

LOG = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\t8_thread.log"
open(LOG, "w", encoding="utf-8").close()


def W(m):
    s = str(m)
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


def launch():
    W("  [thread] importing ...")
    from ansys.geometry.core import launch_modeler_with_spaceclaim
    W("  [thread] launching ...")
    t = time.time()
    m = launch_modeler_with_spaceclaim(version=242, hidden=True, timeout=120)
    W("  [thread] OK %.1fs %s" % (time.time() - t, m.client.backend_version))
    m.close()
    return "done"


W("=== t8: launch inside a worker thread ===")
with cf.ThreadPoolExecutor(max_workers=1) as ex:
    fut = ex.submit(launch)
    try:
        W("result: " + fut.result(timeout=240))
    except cf.TimeoutError:
        W("TIMEOUT: launch did not finish in 240 s")
    except Exception:
        W("EXC:\n" + traceback.format_exc())
W("=== t8 end ===")
