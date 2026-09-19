# -*- coding: utf-8 -*-
"""t21: 每个算子用全新的 modeler (close + relaunch), 消除跨调用状态污染。

对比 t19: t19 复用同一个 modeler, open_file 两次。
"""
import sys, time

sys.path.insert(0, r"C:\english_path\github_fork\ansys-agent-bridge\python\src")
T = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test"
ASM = T + r"\assembly31.scdoc"

from ansys.geometry.core import launch_modeler_with_spaceclaim
from ansys_bridge_mcp.guards import snapshot_design


def run(op, reuse_modeler, modeler):
    d = modeler.open_file(ASM, upload_to_server=False)
    before = snapshot_design(d)
    by = {b.name: b for b in d.bodies}
    if "winding 1" not in by:
        print("  !! winding 1 missing; bodies=%d" % before.body_count, flush=True)
        return modeler
    try:
        getattr(by["stator"], op)([by["winding 1"]], keep_other=False)
        after = snapshot_design(d)
        print("  %-9s reuse=%-5s  before %s -> after %s   stator faces %d -> %d" % (
            op, reuse_modeler, before.summary(), after.summary(),
            before.bodies["stator"][0], after.bodies["stator"][0]), flush=True)
    except Exception as e:
        print("  %-9s reuse=%-5s  EXC %r" % (op, reuse_modeler, e), flush=True)
    return modeler


print("=== fresh modeler per operation ===")
for op in ("unite", "subtract"):
    m = launch_modeler_with_spaceclaim(version=242, hidden=True, timeout=150)
    try:
        run(op, False, m)
    finally:
        m.close()

print("=== one modeler, operations in sequence ===")
m = launch_modeler_with_spaceclaim(version=242, hidden=True, timeout=150)
try:
    for op in ("unite", "subtract"):
        run(op, True, m)
finally:
    m.close()
print("=== t21 end ===")
