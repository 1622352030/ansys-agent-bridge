# -*- coding: utf-8 -*-
"""t28: 补探测 (a) Gap.distance 怎么取数值 (b) 用什么做空间指纹。"""
import os
import sys

T = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test"
SRC = r"C:\english_path\fluent\9_1_youcang\scdoc\zhuangpeiti_fix_9_10_1.scdoc"
LOG = os.path.join(T, "t28_probe.log")
open(LOG, "w", encoding="utf-8").close()


def W(m=""):
    s = str(m)
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


from ansys.geometry.core import launch_modeler_with_spaceclaim
from ansys.geometry.core.math.point import Point3D
from ansys.geometry.core.math.vector import UnitVector3D

m = launch_modeler_with_spaceclaim(version=242, hidden=True, timeout=150)
try:
    d = m.open_file(SRC, upload_to_server=False)
    b = {x.name: x for x in d.bodies}
    st = b["stator"]

    # ── a. Gap.distance 结构 ─────────────────────────────────────────────
    W("=== Gap.distance 的结构 ===")
    gap = m.measurement_tools.min_distance_between_objects(st, b["inlet"])
    dist = gap.distance
    W("  type       : %s.%s" % (type(dist).__module__, type(dist).__name__))
    W("  repr       : %r" % (dist,))
    W("  str        : %s" % (dist,))
    W("  dir        : %s" % [a for a in dir(dist) if not a.startswith('_')][:25])
    for attr in ("magnitude", "value", "m", "units", "unit"):
        try:
            W("  .%-10s = %r" % (attr, getattr(dist, attr)))
        except Exception as exc:
            W("  .%-10s ERR %s" % (attr, type(exc).__name__))
    try:
        W("  float()    = %r" % float(dist))
    except Exception as exc:
        W("  float()    ERR %s: %s" % (type(exc).__name__, str(exc)[:80]))
    try:
        W("  .to('m')   = %r" % dist.to("m"))
    except Exception as exc:
        W("  .to('m')   ERR %s" % type(exc).__name__)

    # ── b. 空间指纹候选 ──────────────────────────────────────────────────
    W()
    W("=== 空间指纹候选 (都试一遍) ===")
    fc = list(st.faces)[0]
    for label, fn in (
        ("face.bounding_box", lambda: fc.bounding_box),
        ("face.get_bounding_box()", lambda: fc.get_bounding_box()),
        ("face.centroid", lambda: fc.centroid),
        ("face.point", lambda: fc.point),
    ):
        try:
            v = fn()
            W("  %-26s OK   %r" % (label, v))
        except Exception as exc:
            W("  %-26s ERR  %s: %s" % (label, type(exc).__name__, str(exc)[:70]))

    edges = list(st.edges) if hasattr(st, "edges") else []
    if not edges:
        for f2 in st.faces:
            edges = list(f2.edges)
            if edges:
                break
    if edges:
        e = edges[0]
        for label, fn in (
            ("edge.start", lambda: e.start),
            ("edge.end", lambda: e.end),
            ("edge.centroid", lambda: e.centroid),
            ("edge.bounding_box", lambda: e.bounding_box),
        ):
            try:
                v = fn()
                W("  %-26s OK   %r" % (label, v))
            except Exception as exc:
                W("  %-26s ERR  %s: %s" % (label, type(exc).__name__, str(exc)[:70]))

    # ── c. 变换是否真生效 (用 edge 顶点做指纹) ───────────────────────────
    W()
    W("=== rotate 生效验证 (用边起点最小坐标做指纹) ===")

    def fingerprint(body):
        xs = []
        for f3 in body.faces:
            for e3 in f3.edges:
                try:
                    s3 = e3.start
                    xs.append((float(s3.x.magnitude), float(s3.y.magnitude), float(s3.z.magnitude)))
                except Exception:
                    continue
        if not xs:
            return None
        return (min(p[0] for p in xs), min(p[1] for p in xs), min(p[2] for p in xs),
                max(p[0] for p in xs), max(p[1] for p in xs), max(p[2] for p in xs))

    f0 = fingerprint(st)
    W("  初始: %s" % (f0,))
    try:
        st.rotate(Point3D([0, 0, 0]), UnitVector3D([0, 0, 1]), 30)
        f1 = fingerprint(st)
        W("  rotate(z,30) 后: %s" % (f1,))
        W("  是否变化: %s" % (f0 != f1))
    except Exception as exc:
        W("  rotate ERR %s: %s" % (type(exc).__name__, str(exc)[:100]))

finally:
    try:
        m.close()
    except Exception:
        pass
W()
W("=== t28 end ===")
