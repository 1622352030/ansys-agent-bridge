# -*- coding: utf-8 -*-
"""t24: 细看体检结果的内部结构, 为工具返回格式定依据。

重点:
  1. find_duplicate_faces 报的 2 组面, 能否定位到具体 body 与 face
  2. find_inexact_edges 返回 3348 条, 其中多少条真的带边
  3. find_short_edges 675 条分布在哪些 body 上
"""
import os
import collections
import sys

T = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test"
SRC = r"C:\english_path\fluent\9_1_youcang\scdoc\zhuangpeiti_fix_9_10_1.scdoc"
LOG = os.path.join(T, "t24_detail.log")
open(LOG, "w", encoding="utf-8").close()


def W(m=""):
    s = str(m)
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


from ansys.geometry.core import launch_modeler_with_spaceclaim

m = launch_modeler_with_spaceclaim(version=242, hidden=True, timeout=150)
try:
    d = m.open_file(SRC, upload_to_server=False)
    bodies = list(d.bodies)
    W("模型: %d body, %d face" % (len(bodies), sum(len(b.faces) for b in bodies)))

    # ── 1. 重复面细节 ────────────────────────────────────────────────────
    W()
    W("=== find_duplicate_faces 细节 ===")
    dups = m.repair_tools.find_duplicate_faces(bodies)
    W("共 %d 组" % len(dups))
    for i, grp in enumerate(dups):
        W("  组 %d: id=%r, %d 个面" % (i, grp.id, len(grp.faces)))
        for fc in grp.faces:
            info = []
            for attr in ("id", "area", "body", "name"):
                try:
                    v = getattr(fc, attr, "<none>")
                    if attr == "body":
                        v = getattr(v, "name", v)
                    if attr == "area":
                        v = getattr(v, "magnitude", v)
                    info.append("%s=%r" % (attr, v))
                except Exception as exc:
                    info.append("%s=<ERR %s>" % (attr, type(exc).__name__))
            W("      %s" % ", ".join(info))

    # ── 2. 不精确边分布 ──────────────────────────────────────────────────
    W()
    W("=== find_inexact_edges 分布 ===")
    inx = m.repair_tools.find_inexact_edges(bodies)
    W("返回 %d 条" % len(inx))
    hist = collections.Counter(len(x.edges) for x in inx)
    W("按其 edges 列表长度统计: %s" % dict(sorted(hist.items())[:8]))
    nonzero = [x for x in inx if len(x.edges) > 0]
    W("真正带边的条目: %d" % len(nonzero))
    for x in nonzero[:5]:
        W("    id=%r edges=%d" % (x.id, len(x.edges)))

    # ── 3. 短边分布 ──────────────────────────────────────────────────────
    W()
    W("=== find_short_edges (len=0.01) 分布 ===")
    sh = m.repair_tools.find_short_edges(bodies, length=0.01)
    W("返回 %d 条" % len(sh))
    per_body = collections.Counter()
    lengths = []
    for item in sh:
        for e in item.edges:
            try:
                per_body[getattr(e, "body", None) and e.body.name or "<unknown>"] += 1
            except Exception:
                per_body["<err>"] += 1
            try:
                lengths.append(float(e.length.magnitude))
            except Exception:
                pass
    W("按 body 分布 (前 8): %s" % dict(per_body.most_common(8)))
    if lengths:
        lengths.sort()
        W("边长: n=%d  min=%.6g  median=%.6g  max=%.6g" %
          (len(lengths), lengths[0], lengths[len(lengths)//2], lengths[-1]))

    # ── 4. Edge/Face 能读到什么 ──────────────────────────────────────────
    W()
    W("=== Edge / Face 可读属性 ===")
    b0 = bodies[0]
    if len(b0.faces) > 0:
        fc = list(b0.faces)[0]
        W("  Face: %s" % [a for a in dir(fc) if not a.startswith("_")])
        W("  Face.id=%r" % (getattr(fc, "id", None),))
    edges = list(b0.edges) if hasattr(b0, "edges") else []
    if edges:
        e0 = edges[0]
        W("  Edge: %s" % [a for a in dir(e0) if not a.startswith("_")])
        W("  Edge.id=%r  length=%r" % (getattr(e0, "id", None), getattr(e0, "length", None)))

finally:
    try:
        m.close()
    except Exception:
        pass
W()
W("=== t24 end ===")
