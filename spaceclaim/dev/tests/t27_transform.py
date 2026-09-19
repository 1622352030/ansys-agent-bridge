# -*- coding: utf-8 -*-
"""t27: 实测 min_distance / rotate / scale / mirror / insert_file 的真实行为。

变换会改几何, 所以只测不导出, 并在测前测后校验源文件 sha256。
变换不改面数与体积, 因此用"空间指纹"(所有面包围盒的聚合)判断是否真动了。
"""
import hashlib
import os
import sys
import traceback

T = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test"
SRC = r"C:\english_path\fluent\9_1_youcang\scdoc\zhuangpeiti_fix_9_10_1.scdoc"
OTHER = r"C:\english_path\fluent\9_1_youcang\scdoc\youcang_new_9_11.scdoc"
LOG = os.path.join(T, "t27_transform.log")
open(LOG, "w", encoding="utf-8").close()


def W(m=""):
    s = str(m)
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


h0 = sha256(SRC)

from ansys.geometry.core import launch_modeler_with_spaceclaim
from ansys.geometry.core.math.point import Point3D
from ansys.geometry.core.math.vector import UnitVector3D
from ansys.geometry.core.math.plane import Plane
from ansys.geometry.core.misc.measurements import Distance

m = launch_modeler_with_spaceclaim(version=242, hidden=True, timeout=150)
try:
    def fingerprint(body):
        """空间指纹: 汇总该 body 所有面的包围盒, 用来判断位置是否变了。"""
        lo = [float("inf")] * 3
        hi = [float("-inf")] * 3
        for fc in body.faces:
            try:
                bb = fc.bounding_box
                for i, v in enumerate((bb.min.x.magnitude, bb.min.y.magnitude, bb.min.z.magnitude)):
                    lo[i] = min(lo[i], v)
                for i, v in enumerate((bb.max.x.magnitude, bb.max.y.magnitude, bb.max.z.magnitude)):
                    hi[i] = max(hi[i], v)
            except Exception:
                continue
        if lo[0] == float("inf"):
            return None
        return tuple(round(v, 9) for v in lo + hi)

    d = m.open_file(SRC, upload_to_server=False)
    b = {x.name: x for x in d.bodies}
    W("模型: %d body" % len(b))

    # ── 1. 最小间距 ──────────────────────────────────────────────────────
    W()
    W("=== min_distance_between_objects ===")
    pairs = [("stator", "winding 1"), ("stator", "pip"), ("stator", "inlet"),
             ("winding 1", "winding 2"), ("stator", "outlet")]
    for a, c in pairs:
        if a not in b or c not in b:
            W("  %-12s <-> %-12s (缺)" % (a, c)); continue
        try:
            gap = m.measurement_tools.min_distance_between_objects(b[a], b[c])
            dist = getattr(gap, "distance", None)
            W("  %-12s <-> %-12s  Gap.distance=%r  (magnitude=%r)" % (
                a, c, dist, getattr(dist, "magnitude", None)))
        except Exception as exc:
            W("  %-12s <-> %-12s  ERR %s: %s" % (a, c, type(exc).__name__, str(exc)[:80]))

    # ── 2. 变换 ──────────────────────────────────────────────────────────
    W()
    W("=== 变换 (stator) ===")
    st = b["stator"]
    f0 = fingerprint(st)
    W("  初始指纹: %s" % (f0,))

    def try_transform(label, fn):
        before = fingerprint(st)
        try:
            fn()
            after = fingerprint(st)
            W("  %-28s 指纹变化: %s" % (label, "是" if before != after else "否 (未生效)"))
            if before != after and after:
                W("      after: %s" % (after,))
            return after
        except Exception as exc:
            W("  %-28s ERR %s: %s" % (label, type(exc).__name__, str(exc)[:110]))
            return before

    origin = Point3D([0, 0, 0])
    axis_z = UnitVector3D([0, 0, 1])
    f0 = try_transform("rotate(z, 30deg)", lambda: st.rotate(origin, axis_z, 30))
    f0 = try_transform("scale(1.5)", lambda: st.scale(1.5))
    try:
        pl = Plane(origin, UnitVector3D([0, 0, 1]))
        f0 = try_transform("mirror(z=0 plane)", lambda: st.mirror(pl))
    except Exception as exc:
        W("  Plane 构造失败: %s: %s" % (type(exc).__name__, str(exc)[:150]))
        try:
            pl = Plane(origin, UnitVector3D([1, 0, 0]), UnitVector3D([0, 1, 0]))
            f0 = try_transform("mirror(3-arg plane)", lambda: st.mirror(pl))
        except Exception as exc2:
            W("  3 参数 Plane 也失败: %s" % str(exc2)[:150])

    # ── 3. 体积是否受影响 ────────────────────────────────────────────────
    W()
    W("  rotate/scale 后体积: %.10g (原 0.003314505208)" % st.volume.magnitude)

    # ── 4. insert_file ───────────────────────────────────────────────────
    W()
    W("=== Design.insert_file ===")
    d2 = m.open_file(SRC, upload_to_server=False)
    W("  insert 前: %d body" % len(list(d2.bodies)))
    if os.path.exists(OTHER):
        try:
            comp = d2.insert_file(OTHER)
            W("  返回: %r" % (comp,))
            W("  insert 后: %d body" % len(list(d2.bodies)))
        except Exception as exc:
            W("  d2.insert_file 失败: %s: %s" % (type(exc).__name__, str(exc)[:200]))
    else:
        W("  备用模型不存在: %s" % OTHER)

finally:
    try:
        m.close()
    except Exception:
        pass

W()
W("源文件 sha256 未变: %s" % (sha256(SRC) == h0))
W("=== t27 end ===")
