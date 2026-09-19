# -*- coding: utf-8 -*-
"""测试1: launch_modeler_with_spaceclaim(version=242, hidden=True)"""
import sys, time, traceback

LOG = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\t1_launch.log"
lines = []


def W(m):
    s = str(m)
    lines.append(s)
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


open(LOG, "w", encoding="utf-8").close()
W("=== T1: launch_modeler_with_spaceclaim ===")
W("python: " + sys.version.replace("\n", " "))

W("[1] 导入 ...")
from ansys.geometry.core import launch_modeler_with_spaceclaim
from ansys.geometry.core.connection.backend import BackendType
import ansys.geometry.core as agc
W("    ansys-geometry-core " + agc.__version__)

modeler = None
t0 = time.time()
try:
    W("[2] launch(version=242, hidden=True, timeout=150) ...")
    modeler = launch_modeler_with_spaceclaim(version=242, hidden=True, timeout=150)
    dt = time.time() - t0
    W("    ✅ 启动成功, 用时 %.1f s" % dt)
    W("[3] backend_type = %s" % modeler.client.backend_type)
    W("    backend_version = %s" % (modeler.client.backend_version,))
    W("    channel = %s" % modeler.client.channel)
    W("[4] 目前设计 = %r" % (modeler.design,))
    W("[5] 可用工具集:")
    for name in ("repair_tools", "prepare_tools", "geometry_commands", "unsupported"):
        try:
            getattr(modeler, name)
            W("        %-20s OK" % name)
        except Exception as e:
            W("        %-20s ERR %s" % (name, e))
    try:
        modeler.measurement_tools
        W("        %-20s OK" % "measurement_tools")
    except Exception as e:
        W("        %-20s ERR %s" % ("measurement_tools", e))
except Exception:
    W("    ❌ 启动失败, 用时 %.1f s" % (time.time() - t0))
    W(traceback.format_exc())
finally:
    if modeler is not None:
        try:
            W("[6] close() ...")
            modeler.close()
            W("    ✅ 已关闭")
        except Exception:
            W("    close 异常:\n" + traceback.format_exc())
W("=== T1 结束 ===")
