# -*- coding: utf-8 -*-
"""对照检查: 实测官方 API 里我漏掉的关键方法, 判断哪些是真缺口。

重点:
  1. enhanced_share_topology  —— 若有效, 则此前"share_topology 无效"的结论需修正
  2. find_interferences       —— 干涉检查, 对应 user 已踩的 overlapping faces 坑
  3. inspect_geometry         —— 几何体检, 建网失败诊断
  4. min_distance_between_objects — 最小间距
  5. create_cylinder_enclosure / create_box_enclosure —— 外流场计算域
"""
import os
import sys
import time
import traceback

sys.path.insert(0, r"C:\english_path\github_fork\ansys-agent-bridge\python\src")

T = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test"
ASM = os.path.join(T, "assembly31.scdoc")
LOG = os.path.join(T, "t22_gaps.log")

open(LOG, "w", encoding="utf-8").close()


def W(m):
    s = str(m)
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


from ansys.geometry.core import launch_modeler_with_spaceclaim
from ansys_bridge_mcp.guards import snapshot_design

m = launch_modeler_with_spaceclaim(version=242, hidden=True, timeout=150)
try:
    def reopen():
        return m.open_file(ASM, upload_to_server=False)

    def by_name(d):
        return {b.name: b for b in d.bodies}

    # ── 1. enhanced_share_topology ────────────────────────────────────────
    W("=== 1. enhanced_share_topology vs share_topology ===")
    d = reopen()
    before = snapshot_design(d)
    bodies = list(d.bodies)
    W("  before: %s" % before.summary())
    try:
        t0 = time.time()
        ok = m.prepare_tools.enhanced_share_topology(bodies, tol=0.0)
        after = snapshot_design(d)
        W("  enhanced 返回 %r  用时 %.1fs" % (ok, time.time() - t0))
        W("  after : %s" % after.summary())
        W("  判定: %s" % ("生效" if not before.same_as(after) else "同样无效(静默 no-op)"))
        if not before.same_as(after):
            diff = before.diff(after)
            W("  stator faces %s -> %s" % (before.bodies["stator"][0], after.bodies["stator"][0]))
    except Exception:
        W("  enhanced 抛异常:\n" + traceback.format_exc())

    # ── 2. find_interferences ─────────────────────────────────────────────
    W("\n=== 2. find_interferences (干涉检查) ===")
    d = reopen()
    try:
        t0 = time.time()
        problems = m.repair_tools.find_interferences(list(d.bodies), cut_smaller_body=False)
        W("  返回 %d 条问题, 用时 %.1fs" % (len(problems), time.time() - t0))
        for p in problems[:6]:
            W("    %s" % (str(p)[:200],))
        if not problems:
            W("    (无干涉)")
    except Exception:
        W("  抛异常:\n" + traceback.format_exc())

    # ── 3. inspect_geometry ───────────────────────────────────────────────
    W("\n=== 3. inspect_geometry (几何体检) ===")
    d = reopen()
    try:
        t0 = time.time()
        issues = m.repair_tools.inspect_geometry(list(d.bodies))
        W("  返回 %d 条, 用时 %.1fs" % (len(issues), time.time() - t0))
        for i in issues[:8]:
            W("    %s" % (str(i)[:220],))
        if not issues:
            W("    (未发现问题)")
    except Exception:
        W("  抛异常:\n" + traceback.format_exc())

    # ── 4. min_distance_between_objects ───────────────────────────────────
    W("\n=== 4. min_distance_between_objects ===")
    d = reopen()
    b = by_name(d)
    for a, c in (("stator", "winding 1"), ("stator", "pip"), ("stator", "inlet"),
                 ("winding 1", "winding 2")):
        try:
            dist = m.measurement_tools.min_distance_between_objects(b[a], b[c])
            W("  %-12s <-> %-12s  min distance = %s" % (a, c, dist))
        except Exception as exc:
            W("  %-12s <-> %-12s  ERR %s" % (a, c, type(exc).__name__))

    # ── 5. 外流场 enclosure ───────────────────────────────────────────────
    W("\n=== 5. create_cylinder_enclosure / create_box_enclosure ===")
    d = reopen()
    W("  before: %s" % snapshot_design(d).summary())
    for label, fn in (
        ("create_box_enclosure",
         lambda: m.prepare_tools.create_box_enclosure(list(d.bodies), 0.05, 0.05, 0.05)),
        ("create_cylinder_enclosure",
         lambda: m.prepare_tools.create_cylinder_enclosure(list(d.bodies), 0.05, 0.05, 0.05)),
    ):
        try:
            t0 = time.time()
            res = fn()
            after = snapshot_design(d)
            W("  %-26s 返回 %r  用时 %.1fs" % (label, res, time.time() - t0))
            W("  %-26s after: %s" % ("", after.summary()))
        except Exception:
            W("  %-26s 抛异常:" % label)
            W("    " + traceback.format_exc().splitlines()[-1])

finally:
    try:
        m.close()
    except Exception:
        pass
W("\n=== t22 end ===")
