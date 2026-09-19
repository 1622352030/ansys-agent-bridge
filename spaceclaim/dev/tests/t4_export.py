# -*- coding: utf-8 -*-
"""测试4: 操作后能否导出/保存"""
import os, time, traceback

T = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test"
ASM = os.path.join(T, "assembly31.scdoc")
LOG = os.path.join(T, "t4_export.log")


def W(m):
    print(m, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(str(m) + "\n")


open(LOG, "w", encoding="utf-8").close()
from ansys.geometry.core import launch_modeler_with_spaceclaim
from ansys.geometry.core.designer.design import Design

W("=== T4: 导出/保存 ===")
W("[1] Design 上与导出/保存有关的方法:")
for n in dir(Design):
    if any(k in n.lower() for k in ("export", "save", "write", "download", "close")):
        W("        %s" % n)

modeler = None
try:
    modeler = launch_modeler_with_spaceclaim(version=242, hidden=True, timeout=150)
    d = modeler.open_file(ASM, upload_to_server=False)
    W("\n[2] 已打开 %s, bodies=%d" % (d.name, len(list(d.bodies))))

    for meth, arg in (("export_to_scdocx", os.path.join(T, "out_scdocx")),
                      ("export_to_scdoc", os.path.join(T, "out_scdoc")),
                      ("export_to_step", os.path.join(T, "out_step")),
                      ("export_to_parasolid_text", os.path.join(T, "out_x_t"))):
        if not hasattr(d, meth):
            W("    %-24s 不存在" % meth); continue
        t0 = time.time()
        try:
            r = getattr(d, meth)(arg)
            W("    %-24s ✅ %.1f s -> %r" % (meth, time.time() - t0, r))
        except Exception as e:
            W("    %-24s ❌ %.1f s -> %s: %s" % (meth, time.time() - t0, type(e).__name__, str(e)[:160]))
finally:
    if modeler is not None:
        try:
            modeler.close()
        except Exception:
            pass

W("\n[3] 产物:")
for root, dirs, files in os.walk(T):
    for f in files:
        if f.startswith("out_"):
            p = os.path.join(root, f)
            W("    %-40s %8d B" % (os.path.relpath(p, T), os.path.getsize(p)))
W("=== T4 结束 ===")
