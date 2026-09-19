# -*- coding: utf-8 -*-
"""t25: 直接调用新的 inspect_geometry, 验证返回结构与结论。"""
import hashlib
import json
import os
import sys
import time
import traceback

sys.path.insert(0, r"C:\english_path\github_fork\ansys-agent-bridge\python\src")

T = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test"
SRC = r"C:\english_path\fluent\9_1_youcang\scdoc\zhuangpeiti_fix_9_10_1.scdoc"
OUT = os.path.join(T, "t25_inspect.json")


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


h0 = sha256(SRC)
from ansys_bridge_mcp.scdm import Session

s = Session()
t0 = time.time()
try:
    s.start(hidden=True, timeout=150)
    s.open_file(SRC)
    r = s.inspect_geometry()
    dt = time.time() - t0
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(r, f, indent=1, ensure_ascii=False)

    print("=== 体检完成 %.1fs ===" % dt)
    print("bodies=%s faces=%s clean=%s" % (r["bodies_checked"], r["faces_checked"], r["clean"]))
    print("problems_found=%s" % json.dumps(r["problems_found"], ensure_ascii=False))
    print()
    for name, data in r["checks"].items():
        if "error" in data:
            print("%-16s ERROR %s" % (name, data["error"][:90]))
            continue
        print("%-16s groups=%-4s objects=%-4s %s" % (
            name, data.get("group_count"), data.get("object_count"),
            ("UNRELIABLE" if data.get("unreliable") else "")))
        if name == "duplicate_faces":
            for g in data.get("groups", []):
                rows = ["%s %s area=%.10g" % (f.get("body"), f.get("id"), f.get("area_m2", 0))
                        for f in g.get("faces", [])]
                print("      %s" % "  |  ".join(rows))
        if data.get("scan"):
            print("      scan: %s" % json.dumps(data["scan"], ensure_ascii=False))
        if data.get("span"):
            print("      span: %s" % json.dumps(data["span"], ensure_ascii=False))
    print()
    print("源文件未改动: %s" % (sha256(SRC) == h0))
    print("written:", OUT)
except Exception:
    print("FAIL:\n" + traceback.format_exc())
finally:
    try:
        s.close()
    except Exception:
        pass
