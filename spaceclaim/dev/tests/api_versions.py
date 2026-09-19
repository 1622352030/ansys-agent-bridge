# -*- coding: utf-8 -*-
"""静态解析官方客户端源码, 提取每个方法的版本门槛。

版本门槛由 @min_backend_version(major, minor, sp) 装饰器固定, 是闭包变量,
反射拿不到; 直接调用又会改几何。所以用 AST 解析源码 —— 无副作用且完整。

产出: 每个公开方法在 24.2.0 上"可用 / 需要更高版本"。
"""
import ast
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(r"C:\Users\16223\AppData\Roaming\Python\Python313\site-packages\ansys\geometry\core")
TARGET_VERSION = (24, 2, 0)

rows = []


def decorator_version(node):
    """若函数带 @min_backend_version(a, b, c), 返回 (a, b, c)。"""
    for dec in node.decorator_list:
        call = dec if isinstance(dec, ast.Call) else None
        if call is None:
            continue
        fn = call.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name != "min_backend_version":
            continue
        args = [a.value for a in call.args if isinstance(a, ast.Constant)]
        kwargs = {k.arg: k.value.value for k in call.keywords if isinstance(k.value, ast.Constant)}
        if len(args) == 3:
            return tuple(args)
        if {"major", "minor", "service_pack"} <= set(kwargs):
            return (kwargs["major"], kwargs["minor"], kwargs["service_pack"])
    return None


for path in sorted(ROOT.rglob("*.py")):
    rel = path.relative_to(ROOT).as_posix()
    if "/_vendor/" in rel or rel.startswith("_"):
        continue
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        continue
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        cls = node.name
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name.startswith("_"):
                continue
            ver = decorator_version(item)
            rows.append({
                "class": cls,
                "module": rel,
                "method": item.name,
                "min_version": ".".join(str(v) for v in ver) if ver else None,
                "line": item.lineno,
            })

out = pathlib.Path(r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\api_versions.json")
out.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")

gated = [r for r in rows if r["min_version"]]
above = [
    r for r in gated
    if tuple(int(x) for x in r["min_version"].split(".")) > TARGET_VERSION
]
ok_gated = [r for r in gated if r not in above]

print("源码中带版本门槛的方法: %d" % len(gated))
print("  在当前版本 %s 可用 : %d" % (".".join(map(str, TARGET_VERSION)), len(ok_gated)))
print("  需要更高版本       : %d" % len(above))
print()
from collections import Counter
print("=== 门槛版本分布 (需要更高版本的) ===")
for v, n in sorted(Counter(r["min_version"] for r in above).items()):
    print("   >= %-8s %3d 个" % (v, n))
print()
print("=== 按类统计 ===")
bycls = {}
for r in rows:
    bycls.setdefault(r["class"], {"total": 0, "above": 0})
    bycls[r["class"]]["total"] += 1
    if r in above:
        bycls[r["class"]]["above"] += 1
for cls, d in sorted(bycls.items(), key=lambda kv: -kv[1]["above"]):
    if d["above"]:
        print("  %-28s %2d 个方法中 %2d 个不可用" % (cls, d["total"], d["above"]))
print()
print("written:", out)
