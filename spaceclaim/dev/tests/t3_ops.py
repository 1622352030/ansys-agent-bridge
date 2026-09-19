# -*- coding: utf-8 -*-
"""测试3: 几何算子 get_collision / subtract / unite / share_topology"""
import os, time, traceback

T = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test"
ASM = os.path.join(T, "assembly31.scdoc")
LOG = os.path.join(T, "t3_ops.log")


def W(m):
    s = str(m)
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


open(LOG, "w", encoding="utf-8").close()
from ansys.geometry.core import launch_modeler_with_spaceclaim
from ansys.geometry.core.designer.body import CollisionType

modeler = None


def reopen(tag):
    W("    [reopen] %s" % tag)
    d = modeler.open_file(ASM, upload_to_server=False)
    return d


def by_name(design):
    m = {}
    for b in design.bodies:
        m[b.name] = b
    return m


try:
    W("=== T3: 几何算子 ===")
    modeler = launch_modeler_with_spaceclaim(version=242, hidden=True, timeout=150)
    W("已连接 %s %s\n" % (modeler.client.backend_type, modeler.client.backend_version))

    # ---------- A. get_collision (非破坏) ----------
    W("--- A. get_collision ---")
    d = reopen("for collision")
    m = by_name(d)
    pairs = [("stator", "winding 1"), ("stator", "pip"), ("stator", "inlet"),
             ("winding 1", "winding 2"), ("pip", "winding 1")]
    for a, b in pairs:
        if a not in m or b not in m:
            W("    %-12s x %-12s  (缺实体)" % (a, b)); continue
        try:
            c = m[a].get_collision(m[b])
            W("    %-12s x %-12s -> %s" % (a, b, c))
        except Exception as e:
            W("    %-12s x %-12s -> ERR %s" % (a, b, e))

    # ---------- B. subtract (破坏, 不保存) ----------
    W("\n--- B. subtract: stator - winding 1 ---")
    d = reopen("for subtract")
    m = by_name(d)
    st, w1 = m["stator"], m["winding 1"]
    v0, f0 = st.volume.magnitude, len(st.faces)
    W("    BEFORE: stator vol=%.10g  faces=%d  bodies=%d" % (v0, f0, len(list(d.bodies))))
    t0 = time.time()
    try:
        st.subtract(w1, keep_other=False)
        dt = time.time() - t0
        n = len(list(d.bodies))
        v1, f1 = st.volume.magnitude, len(st.faces)
        W("    subtract OK, %.1f s" % dt)
        W("    AFTER : stator vol=%.10g  faces=%d  bodies=%d" % (v1, f1, n))
        W("    Δvol=%.6g   Δfaces=%+d   Δbodies=%+d" % (v1 - v0, f1 - f0, n - 31))
        W("    判定: %s" % ("✅ 真生效" if (f1 != f0 or abs(v1 - v0) > 1e-12) else "❌ 静默失效(体积/面数都没变)"))
    except Exception:
        W("    ❌ subtract 抛异常 %.1f s" % (time.time() - t0))
        W(traceback.format_exc())

    # ---------- C. share_topology (破坏, 不保存) ----------
    W("\n--- C. prepare_tools.share_topology(31 bodies) ---")
    d = reopen("for share_topology")
    m = by_name(d)
    bodies = list(d.bodies)
    f0 = {b.name: len(b.faces) for b in bodies}
    W("    BEFORE: stator faces=%d  winding1 faces=%d  bodies=%d"
      % (f0["stator"], f0["winding 1"], len(bodies)))
    t0 = time.time()
    try:
        ok = modeler.prepare_tools.share_topology(bodies, tol=0.0)
        dt = time.time() - t0
        W("    share_topology 返回 %r, 用时 %.1f s" % (ok, dt))
        d2 = modeler.read_existing_design()
        m2 = by_name(d2)
        W("    AFTER : bodies=%d" % len(list(d2.bodies)))
        for nm in ("stator", "winding 1", "pip"):
            if nm in m2:
                W("            %-10s faces=%d (was %d)" % (nm, len(m2[nm].faces), f0.get(nm, -1)))
    except Exception:
        W("    ❌ share_topology 抛异常 %.1f s" % (time.time() - t0))
        W(traceback.format_exc())

    # ---------- D. unite (破坏, 不保存) ----------
    W("\n--- D. unite: stator + winding 1 ---")
    d = reopen("for unite")
    m = by_name(d)
    st, w1 = m["stator"], m["winding 1"]
    v0, f0 = st.volume.magnitude, len(st.faces)
    try:
        st.unite(w1, keep_other=False)
        n = len(list(d.bodies))
        W("    unite OK: stator vol=%.10g faces=%d bodies=%d  (Δfaces=%+d Δbodies=%+d)"
          % (st.volume.magnitude, len(st.faces), n, len(st.faces) - f0, n - 31))
    except Exception:
        W("    ❌ unite 抛异常")
        W(traceback.format_exc())

except Exception:
    W("整体异常:\n" + traceback.format_exc())
finally:
    if modeler is not None:
        try:
            modeler.close()
            W("\n已关闭")
        except Exception:
            W("close 异常:\n" + traceback.format_exc())
W("=== T3 结束 ===")
