# -*- coding: utf-8 -*-
"""t23: 实测 8 个 RepairTools.find_* 在 24R2 上对"建网失败模型"的行为。

模型: zhuangpeiti_fix_9_10_1.scdoc —— PyFluent 建网时 Describe Geometry 的
computing regions 失败并报 Found overlapping faces。

find_* 是只读查询, 但仍在测前测后校验源文件 sha256。
"""
import hashlib
import os
import sys
import time
import traceback

T = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test"
SRC = r"C:\english_path\fluent\9_1_youcang\scdoc\zhuangpeiti_fix_9_10_1.scdoc"
LOG = os.path.join(T, "t23_checks.log")

open(LOG, "w", encoding="utf-8").close()


def W(m=""):
    s = str(m)
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


before_hash = sha256(SRC)
W("源文件 sha256 (测前) = %s" % before_hash[:16])
W()

from ansys.geometry.core import launch_modeler_with_spaceclaim

m = launch_modeler_with_spaceclaim(version=242, hidden=True, timeout=150)
try:
    d = m.open_file(SRC, upload_to_server=False)
    bodies = list(d.bodies)
    total_faces = sum(len(b.faces) for b in bodies)
    W("模型: %d 个 body, %d 个 face" % (len(bodies), total_faces))
    W()

    def run(label, fn, sample=3):
        t0 = time.time()
        try:
            res = fn()
        except Exception as exc:
            W("  %-24s 异常 %.1fs  %s: %s" % (label, time.time() - t0,
                                              type(exc).__name__, str(exc)[:110]))
            return None
        dt = time.time() - t0
        items = res if isinstance(res, list) else [res]
        W("  %-24s 返回 %-4d 用时 %.1fs" % (label, len(items), dt))
        for it in items[:sample]:
            parts = []
            for attr in ("id", "faces", "edges", "bodies"):
                if hasattr(it, attr):
                    v = getattr(it, attr)
                    if isinstance(v, (list, tuple)):
                        parts.append("%s=%d" % (attr, len(v)))
                    else:
                        parts.append("%s=%r" % (attr, v))
            W("      %s" % ", ".join(parts))
        return items

    W("=== 无阈值类 ===")
    run("find_duplicate_faces", lambda: m.repair_tools.find_duplicate_faces(bodies))
    run("find_extra_edges", lambda: m.repair_tools.find_extra_edges(bodies))
    run("find_inexact_edges", lambda: m.repair_tools.find_inexact_edges(bodies))

    W()
    W("=== 带阈值类（先用默认值） ===")
    run("find_short_edges(default)", lambda: m.repair_tools.find_short_edges(bodies))
    run("find_small_faces(default)", lambda: m.repair_tools.find_small_faces(bodies))
    run("find_missing_faces(default)", lambda: m.repair_tools.find_missing_faces(bodies))
    run("find_split_edges(default)", lambda: m.repair_tools.find_split_edges(bodies))
    run("find_stitch_faces(default)", lambda: m.repair_tools.find_stitch_faces(bodies))

    W()
    W("=== 阈值敏感性 (短边) ===")
    for length in (0.0001, 0.001, 0.01):
        run("short_edges len=%.4f" % length,
            lambda L=length: m.repair_tools.find_short_edges(bodies, length=L), sample=1)

    W()
    W("=== 阈值敏感性 (小面) ===")
    for area in (1e-8, 1e-6, 1e-5):
        run("small_faces area=%.0e" % area,
            lambda A=area: m.repair_tools.find_small_faces(bodies, area=A), sample=1)

finally:
    try:
        m.close()
    except Exception:
        pass

after_hash = sha256(SRC)
W()
W("源文件 sha256 (测后) = %s" % after_hash[:16])
W("源文件是否被改动: %s" % ("否, 一致" if before_hash == after_hash else "是! 已改动"))
W("=== t23 end ===")
