# -*- coding: utf-8 -*-
"""t20: 布尔之后, 试几种刷新方式, 看哪一种能读到正确的实体表。"""
import os, sys, time, traceback

sys.path.insert(0, r"C:\english_path\github_fork\ansys-agent-bridge\python\src")
T = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test"
ASM = os.path.join(T, "assembly31.scdoc")

from ansys.geometry.core import launch_modeler_with_spaceclaim

m = launch_modeler_with_spaceclaim(version=242, hidden=True, timeout=150)
try:
    def show(tag, design):
        try:
            bodies = list(design.bodies)
            tot = sum(len(b.faces) for b in bodies)
            print("  %-28s bodies=%d faces=%d" % (tag, len(bodies), tot), flush=True)
        except Exception as e:
            print("  %-28s ERR %r" % (tag, e), flush=True)

    d = m.open_file(ASM, upload_to_server=False)
    show("after open_file(returned)", d)
    show("after open_file(m.design)", m.design)

    by = {b.name: b for b in d.bodies}
    print("uniting stator + winding 1 ...", flush=True)
    by["stator"].unite(by["winding 1"], keep_other=False)
    show("right after unite (same obj)", d)

    for label, fn in (
        ("_update_design_inplace()", lambda: d._update_design_inplace()),
        ("_clear_cached_bodies()", lambda: d._clear_cached_bodies()),
        ("_update_from_tracker()", lambda: d._update_from_tracker()),
    ):
        try:
            fn()
            show(label, d)
        except Exception as e:
            print("  %-28s ERR %r" % (label, e), flush=True)

    print("read_existing_design():", flush=True)
    d2 = m.read_existing_design()
    show("read_existing_design()", d2)
    show("m.design", m.design)
finally:
    m.close()
