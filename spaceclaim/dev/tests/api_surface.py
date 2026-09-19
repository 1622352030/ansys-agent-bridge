# -*- coding: utf-8 -*-
"""枚举 ansys-geometry-core 的完整 API surface, 作为对照基准。

静态反射, 不启动 SpaceClaim。
"""
import inspect
import json
import sys

TARGETS = [
    ("designer.Design", "ansys.geometry.core.designer.design", "Design"),
    ("designer.Body", "ansys.geometry.core.designer.body", "Body"),
    ("designer.Component", "ansys.geometry.core.designer.component", "Component"),
    ("designer.Edge", "ansys.geometry.core.designer.edge", "Edge"),
    ("designer.Face", "ansys.geometry.core.designer.face", "Face"),
    ("tools.RepairTools", "ansys.geometry.core.tools.repair_tools", "RepairTools"),
    ("tools.PrepareTools", "ansys.geometry.core.tools.prepare_tools", "PrepareTools"),
    ("tools.GeometryCommands", "ansys.geometry.core.tools.geometry_commands", "GeometryCommands"),
    ("tools.MeasurementTools", "ansys.geometry.core.tools.measurement_tools", "MeasurementTools"),
    ("tools.UnsupportedCommands", "ansys.geometry.core.tools.unsupported", "UnsupportedCommands"),
    ("tools.PartManagement", "ansys.geometry.core.tools.part_management", "PartManagement"),
    ("tools.ComponentTransfer", "ansys.geometry.core.tools.component_transfer", "ComponentTransfer"),
    ("tools.DesignCurves", "ansys.geometry.core.tools.design_curves", "DesignCurves"),
    ("tools.NamedSelection", "ansys.geometry.core.tools.named_selection", "NamedSelection"),
    ("Modeler", "ansys.geometry.core.modeler", "Modeler"),
]


def public_methods(cls):
    out = []
    for name, member in inspect.getmembers(cls):
        if name.startswith("_"):
            continue
        if not (inspect.isfunction(member) or inspect.ismethod(member)):
            continue
        try:
            sig = str(inspect.signature(member))
        except (ValueError, TypeError):
            sig = "(?)"
        doc = (inspect.getdoc(member) or "").strip().split("\n\n")[0].replace("\n", " ")
        out.append({"name": name, "signature": sig, "summary": doc[:150]})
    return out


report = {}
for label, modname, clsname in TARGETS:
    try:
        mod = __import__(modname, fromlist=[clsname])
        cls = getattr(mod, clsname)
    except Exception as exc:
        report[label] = {"error": f"{type(exc).__name__}: {exc}"}
        continue
    methods = public_methods(cls)
    report[label] = {"count": len(methods), "methods": methods}

out = sys.argv[1] if len(sys.argv) > 1 else "geom_api.json"
with open(out, "w", encoding="utf-8") as f:
    json.dump(report, f, indent=1, ensure_ascii=False)

total = 0
for label, data in report.items():
    if "error" in data:
        print("%-28s ERROR %s" % (label, data["error"]))
        continue
    total += data["count"]
    print("%-28s %3d public methods" % (label, data["count"]))
print("-" * 50)
print("%-28s %3d" % ("TOTAL", total))
print("written:", out)
