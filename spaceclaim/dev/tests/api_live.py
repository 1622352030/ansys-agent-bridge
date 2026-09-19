# -*- coding: utf-8 -*-
"""从活动的 modeler 实例枚举官方 API 全貌 —— 这才是真实可用的边界。"""
import inspect
import json
import sys
import traceback

from ansys.geometry.core import launch_modeler_with_spaceclaim

OUT = r"C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\geom_api_live.json"


def methods_of(obj):
    out = []
    for name in sorted(dir(obj)):
        if name.startswith("_"):
            continue
        try:
            member = getattr(obj, name)
        except Exception as exc:
            out.append({"name": name, "signature": "", "summary": f"<unreadable {type(exc).__name__}>"})
            continue
        if not callable(member):
            continue
        try:
            sig = str(inspect.signature(member))
        except (ValueError, TypeError):
            sig = "(?)"
        doc = (inspect.getdoc(member) or "").strip().split("\n\n")[0].replace("\n", " ")
        out.append({"name": name, "signature": sig, "summary": doc[:160]})
    return out


report = {}
m = launch_modeler_with_spaceclaim(version=242, hidden=True, timeout=150)
try:
    # modeler 自身的属性
    report["Modeler"] = {
        "type": type(m).__module__ + "." + type(m).__name__,
        "members": methods_of(m),
    }

    # 工具集: modeler 上那些"看起来像工具集"的属性
    toolsets = {}
    for name in sorted(dir(m)):
        if name.startswith("_"):
            continue
        try:
            value = getattr(m, name)
        except Exception:
            continue
        if value is None or callable(value):
            continue
        cls = type(value)
        if cls.__module__.startswith("ansys.geometry") and "tool" in cls.__module__.lower() or \
           cls.__name__.endswith("Tools") or "Commands" in cls.__name__ or "Management" in cls.__name__:
            toolsets[name] = {
                "type": cls.__module__ + "." + cls.__name__,
                "methods": methods_of(value),
            }
    report["toolsets"] = toolsets

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, ensure_ascii=False)

    print("=== modeler 工具集 ===")
    total = 0
    for name, data in toolsets.items():
        n = len(data["methods"])
        total += n
        print("  %-24s %3d  (%s)" % (name, n, data["type"].split(".")[-1]))
    print("  %-24s %3d" % ("TOTAL toolset methods", total))
    print("\n=== modeler 自身 ===")
    print("  %s: %d members" % (report["Modeler"]["type"], len(report["Modeler"]["members"])))
    print("\nwritten:", OUT)
finally:
    try:
        m.close()
    except Exception:
        print(traceback.format_exc())
