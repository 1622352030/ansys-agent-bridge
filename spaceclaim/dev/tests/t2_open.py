# -*- coding: utf-8 -*-
"""测试2: open_file 能否打开 .scdoc (关键项)"""
import sys, time, traceback, os

T = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test"
LOG = os.path.join(T, "t2_open.log")
lines = []


def W(m):
    s = str(m)
    lines.append(s)
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


open(LOG, "w", encoding="utf-8").close()
W("=== T2: open_file(.scdoc) ===")

from ansys.geometry.core import launch_modeler_with_spaceclaim

modeler = None
try:
    W("[1] 启动 SpaceClaim ...")
    modeler = launch_modeler_with_spaceclaim(version=242, hidden=True, timeout=150)
    W("    ✅ 已连接 %s %s" % (modeler.client.backend_type, modeler.client.backend_version))

    for fn in ("smoke2.scdoc", "assembly31.scdoc"):
        p = os.path.join(T, fn)
        W("")
        W("[2] open_file(%s)  %.2f MB" % (fn, os.path.getsize(p) / 1e6))
        t0 = time.time()
        try:
            design = modeler.open_file(p, upload_to_server=False)
            dt = time.time() - t0
            W("    ✅ 打开成功, 用时 %.1f s" % dt)
            W("    design.name      = %r" % design.name)
            W("    design.is_active = %r" % design.is_active)
            bodies = list(design.bodies)
            W("    顶层 bodies 数  = %d" % len(bodies))
            comps = list(design.components)
            W("    顶层 components = %d" % len(comps))
            total = 0
            for b in bodies[:5]:
                W("        body %-16s vol=%.6g" % (b.name, b.volume.magnitude))
            for c in comps[:5]:
                bs = list(c.bodies)
                total += len(bs)
                W("        comp %-16s bodies=%d" % (c.name, len(bs)))
                for b in bs[:3]:
                    W("            body %-16s vol=%.6g" % (b.name, b.volume.magnitude))
            W("    合计实体(顶层+一级) = %d + %d" % (len(bodies), total))
            ns = list(design.named_selections)
            W("    named_selections = %s" % [n.name for n in ns])
        except Exception:
            W("    ❌ 打开失败, 用时 %.1f s" % (time.time() - t0))
            W(traceback.format_exc())
except Exception:
    W("整体异常:\n" + traceback.format_exc())
finally:
    if modeler is not None:
        try:
            modeler.close()
            W("[3] 已关闭")
        except Exception:
            W("close 异常:\n" + traceback.format_exc())
W("=== T2 结束 ===")
