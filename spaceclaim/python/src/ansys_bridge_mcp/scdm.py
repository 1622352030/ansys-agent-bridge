"""SpaceClaim session wrapper built on the official PyAnsys Geometry client.

Measured facts this wrapper is built around (SpaceClaim 2024 R2, 2026-09-19):

* ``launch_modeler_with_spaceclaim(version=242, hidden=True)`` starts in ~31 s
  and reports ``backend_version = 24.2.0``.
* ``modeler.open_file`` opens a ``.scdoc`` directly. The docstring lists only
  ``.scdocx`` / ``.dsco`` / ``.pmdb``; ``.scdoc`` is not in that list but works,
  and the bodies, face counts and named selections it returns match the raw
  IronPython reading of the same file exactly.
* ``Body.subtract`` and ``prepare_tools.share_topology`` can report success while
  changing nothing, so both go through :mod:`ansys_bridge_mcp.guards`.

The import of ``ansys.geometry.core`` is deferred to call time so that this
package installs, imports and answers ``doctor`` on machines without ANSYS.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .guards import (
    GeometrySnapshot,
    assert_changed,
    detect_ansys_roots,
    snapshot_design,
    spatial_fingerprint,
)

__all__ = ["Session", "SessionError", "SCDOC_SUFFIXES", "preload"]


SCDOC_SUFFIXES = (".scdoc", ".scdocx", ".dsco")
_EXPORT_METHODS = {
    "scdocx": "export_to_scdocx",
    "step": "export_to_step",
    "iges": "export_to_iges",
    "parasolid_text": "export_to_parasolid_text",
    "parasolid_bin": "export_to_parasolid_bin",
    "pmdb": "export_to_pmdb",
}

# The eight `RepairTools.find_*` checks that carry NO version gate, so they run
# on 24R2. The five `find_and_fix_*` repair methods and `find_bad_faces` /
# `find_simplify` all require 25.2 or later and are deliberately absent: calling
# them can only raise `GeometryRuntimeError`.
GEOMETRY_CHECKS = {
    "duplicate_faces": "find_duplicate_faces",
    "extra_edges": "find_extra_edges",
    "short_edges": "find_short_edges",
    "small_faces": "find_small_faces",
    "missing_faces": "find_missing_faces",
    "split_edges": "find_split_edges",
    "stitch_faces": "find_stitch_faces",
    "inexact_edges": "find_inexact_edges",
}

# Checks whose RESULT is not trustworthy on 2024 R2, with the measurement that
# disqualified them. Reported rather than silently dropped, because "0 problems"
# and "this check does not work" must not look the same to a caller.
UNRELIABLE_CHECKS = {
    "inexact_edges": (
        "returned 3348 entries on the reference assembly, every one of them with an "
        "empty `edges` list, so the count carries no information"
    ),
}

# Thresholds the official methods do not supply. Their own defaults (0.0 / None)
# found nothing at all on the reference model, while 0.01 m found 675 short
# edges, so a single default is not an answer and the caller cannot be expected
# to guess the model's scale. Scanned instead.
SHORT_EDGE_SCAN_M = (1e-4, 1e-3, 1e-2, 1e-1)
SMALL_FACE_SCAN_M2 = (1e-9, 1e-7, 1e-5, 1e-3)


def _object_ref(obj: Any) -> dict:
    """Identify a Face, Edge or Body without letting one failed read kill the call.

    Every optional read here is guarded because one of them was measured
    failing: `Edge.length` raises `ValueError: The norm of the 3D vector is not
    valid.` from inside PyAnsys on the reference assembly. A geometry report that
    dies because one edge could not be measured is worse than one that says so.
    """
    ref: dict = {}
    for attr, key, unwrap in (
        ("id", "id", False),
        ("area", "area_m2", True),
        ("length", "length_m", True),
        ("curve_type", "curve_type", False),
        ("surface_type", "surface_type", False),
    ):
        try:
            value = getattr(obj, attr, None)
        except Exception:  # noqa: BLE001 - a broken read is data, not a failure
            ref[f"{key}_error"] = "read failed"
            continue
        if value is None:
            continue
        if unwrap:
            value = getattr(value, "magnitude", value)
        try:
            ref[key] = value if isinstance(value, (int, float, str)) else str(value)
        except Exception:  # noqa: BLE001
            continue
    try:
        body = getattr(obj, "body", None)
        if body is not None:
            ref["body"] = getattr(body, "name", None)
    except Exception:  # noqa: BLE001
        pass
    return ref


def _count_objects(items: list) -> int:
    """How many real faces/edges/bodies the problem groups actually carry."""
    total = 0
    for item in items:
        for attr in ("faces", "edges", "bodies"):
            try:
                values = getattr(item, attr, None)
                if values:
                    total += len(values)
            except Exception:  # noqa: BLE001
                continue
    return total


def _body_histogram(items: list) -> dict:
    """How many problem objects sit on each body, worst first.

    This is the useful shape for a large result. A check that reports 675 short
    edges is not actionable as 675 rows, but "459 of them on stator, 8 on each
    winding" says where to look.
    """
    counts: dict[str, int] = {}
    for item in items:
        for attr in ("faces", "edges", "bodies"):
            try:
                values = getattr(item, attr, None)
            except Exception:  # noqa: BLE001
                continue
            if not values:
                continue
            for obj in values:
                name = None
                try:
                    body = getattr(obj, "body", None)
                    name = getattr(body, "name", None) if body is not None else None
                except Exception:  # noqa: BLE001
                    name = None
                if name is None:
                    try:  # StitchFaceProblemAreas carries Body objects directly
                        name = getattr(obj, "name", None)
                    except Exception:  # noqa: BLE001
                        name = None
                key = str(name) if name else "<unknown>"
                counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def problem_summary(items: list, limit: int = 20) -> dict:
    """Turn a list of official `ProblemAreas` into a report.

    The shapes differ per check -- faces for duplicates and small faces, edges
    for short and inexact edges, bodies for stitch faces -- so all three are
    carried through and the empty ones simply do not appear.

    ``groups`` is capped at ``limit`` rows. Measured: returning every group of
    the 675 short edges found on the reference assembly produced a 148 KB
    payload, 95 KB of it that one check, which is too much to hand a model for
    what is really a histogram. The counts, the per-body histogram and the
    numeric span are never capped, so nothing is lost that changes a decision.
    """
    rows = []
    for item in items:
        row: dict = {}
        try:
            row["id"] = getattr(item, "id", None)
        except Exception:  # noqa: BLE001
            pass
        for attr in ("faces", "edges", "bodies"):
            try:
                values = getattr(item, attr, None)
            except Exception:  # noqa: BLE001
                continue
            if not values:
                continue
            try:
                row[attr] = [_object_ref(obj) for obj in values]
            except Exception:  # noqa: BLE001
                row[attr] = []
        rows.append(row)

    summary: dict = {
        "group_count": len(items),
        "object_count": _count_objects(items),
        "by_body": _body_histogram(items),
        "groups": rows[:limit],
    }
    if len(rows) > limit:
        summary["groups_omitted"] = len(rows) - limit
    return summary


def _numeric_range(items: list, attr: str) -> dict | None:
    """min/median/max of a numeric attribute, plus how many reads failed.

    The failure count is not decoration. 675 short edges came back but only 513
    lengths could be read: `Edge.length` raises
    `ValueError: The norm of the 3D vector is not valid.` from inside PyAnsys on
    this assembly. Reporting `measured` without `unreadable` would make the
    span look like it covers all 675.
    """
    values = []
    total = 0
    unreadable = 0
    for item in items:
        try:
            objects = getattr(item, "edges", None) or getattr(item, "faces", None) or []
        except Exception:  # noqa: BLE001
            continue
        for obj in objects:
            total += 1
            try:
                raw = getattr(obj, attr, None)
            except Exception:  # noqa: BLE001
                unreadable += 1
                continue
            raw = getattr(raw, "magnitude", raw)
            if isinstance(raw, (int, float)):
                values.append(float(raw))
            else:
                unreadable += 1
    if not values:
        return None
    values.sort()
    return {
        f"{attr}_min": values[0],
        f"{attr}_median": values[len(values) // 2],
        f"{attr}_max": values[-1],
        "measured": len(values),
        "objects": total,
        "unreadable": unreadable,
    }


class SessionError(RuntimeError):
    """Raised for session lifecycle problems, with the remedy in the message."""


def _stderr(message: str) -> None:
    """Progress notes for the operator's log.

    Deliberately writes to **stderr only**. Stdout carries the JSON-RPC stream,
    so a single stray line on stdout is a protocol violation that surfaces as an
    unexplained "Connection closed" in the client.

    Public in spirit: the server reuses it for start-up notes. The leading
    underscore only keeps it out of the `Session` API surface.
    """
    print(f"{time.strftime('%H:%M:%S')} {message}", file=sys.stderr, flush=True)


def _distance_m(gap: Any) -> float | None:
    """The number inside a ``Distance``, which is one level deeper than it looks.

    Measured: ``min_distance_between_objects`` returns a ``Distance`` whose
    ``repr`` already reads ``0.05036119537898199 meter``, but ``Distance`` has no
    ``magnitude`` -- the quantity is at ``.value``. Reading ``.magnitude`` on the
    ``Distance`` raises ``AttributeError``.
    """
    value = getattr(gap, "distance", None)
    if value is None:
        return None
    for path in (
        lambda: value.value.magnitude,
        lambda: value.value,
        lambda: value.magnitude,
    ):
        try:
            return float(path())
        except Exception:  # noqa: BLE001 - try the next shape
            continue
    return None


def _body_volume(body: Any) -> float | None:
    try:
        return float(getattr(body.volume, "magnitude", body.volume))
    except Exception:  # noqa: BLE001
        return None


def _point(origin: list[float] | None):
    from ansys.geometry.core.math.point import Point3D

    return Point3D(list(origin) if origin else [0.0, 0.0, 0.0])


def _unit_vector(axis: str | list[float]):
    from ansys.geometry.core.math.vector import UnitVector3D

    if isinstance(axis, str):
        table = {"x": [1.0, 0.0, 0.0], "y": [0.0, 1.0, 0.0], "z": [0.0, 0.0, 1.0]}
        key = axis.strip().lower()
        if key not in table:
            raise SessionError(f"axis must be x, y, z or a 3-vector, got {axis!r}")
        return UnitVector3D(table[key])
    if len(axis) != 3:
        raise SessionError(f"axis vector needs 3 components, got {axis!r}")
    return UnitVector3D(list(axis))


def _plane_from_normal(origin: list[float] | None, normal: str | list[float]):
    """A Plane through ``origin`` whose normal is ``normal``.

    ``Plane(origin, direction_x, direction_y)`` does **not** take a normal.
    Measured: ``Plane(Point3D([0,0,0]), UnitVector3D([0,0,1]))`` produces a plane
    whose normal is ``[-1, 0, 0]``, because the second argument is direction_x and
    the normal is the cross product of the two directions. Passing a normal there
    silently mirrors about the wrong plane, so the two directions are derived
    here instead:

        direction_x = normalize(ref x n)
        direction_y = normalize(n x direction_x)
        direction_x x direction_y = n
    """
    from ansys.geometry.core.math.plane import Plane
    from ansys.geometry.core.math.vector import UnitVector3D

    n = _unit_vector(normal)
    # Point3D.x carries a Quantity (has .magnitude); UnitVector3D.x is a bare
    # numpy.float64. Measured, assuming otherwise raises AttributeError.
    nx = float(getattr(n.x, "magnitude", n.x))
    ny = float(getattr(n.y, "magnitude", n.y))
    nz = float(getattr(n.z, "magnitude", n.z))
    ref = (1.0, 0.0, 0.0) if abs(nx) < 0.9 else (0.0, 1.0, 0.0)

    dx = (ref[1] * nz - ref[2] * ny, ref[2] * nx - ref[0] * nz, ref[0] * ny - ref[1] * nx)
    norm = (dx[0] ** 2 + dx[1] ** 2 + dx[2] ** 2) ** 0.5
    if norm == 0.0:
        raise SessionError(f"cannot build a plane for normal {normal!r}")
    dx = tuple(component / norm for component in dx)

    dy = (ny * dx[2] - nz * dx[1], nz * dx[0] - nx * dx[2], nx * dx[1] - ny * dx[0])
    return Plane(_point(origin), UnitVector3D(list(dx)), UnitVector3D(list(dy)))


def _format_version(backend: Any) -> str | None:
    """Render `backend_version` without the trailing None components.

    The client reports a tuple like ``(24, 2, 0, None, None)``; joining it
    verbatim produced the nonsense string ``24.2.0.None.None``.
    """
    if not backend:
        return None
    parts = [str(p) for p in backend if p is not None]
    return ".".join(parts) or None


def preload() -> dict[str, str]:
    """Import the heavy clients BEFORE the server starts answering.

    This is a hang fix, not a warm-up. `ansys.geometry.core` drags in numpy, and
    importing a C extension while the tool-call thread is already live proved
    able to wedge the main thread inside
    ``numpy._core._multiarray_umath``'s ``create_module`` **indefinitely** -- no
    exception, no timeout, the client sees only a tool call that never returns.
    Measured: the same import finishes in 0.9 s when it runs at process start.

    Doing it here means the first ``scdm_session_start`` does not trigger an
    import at all. Each module is reported rather than raised: a machine without
    Ansys must still start the server and answer `ansys_bridge_doctor`.
    """
    status: dict[str, str] = {}
    for name, module in (
        ("numpy", "numpy"),
        ("ansys.geometry.core", "ansys.geometry.core"),
        ("ansys.fluent.core", "ansys.fluent.core"),
    ):
        started = time.time()
        try:
            __import__(module)
            status[name] = f"ok {time.time() - started:.1f}s"
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            status[name] = f"{type(exc).__name__}"
    return status


def _import_geometry():
    try:
        import ansys.geometry.core as agc  # noqa: PLC0415
    except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
        raise SessionError(
            "ansys-geometry-core is not installed in this interpreter.\n"
            "  uv run --with ansys-geometry-core --from "
            "'git+https://github.com/1622352030/ansys-agent-bridge#subdirectory=python' "
            "ansys-bridge-mcp\n"
            "  (or: pip install ansys-geometry-core)\n"
            "package_version() reports the versions this interpreter can actually see."
        ) from exc
    return agc


@dataclass
class Session:
    """One live SpaceClaim modeler, or the absence of one."""

    modeler: Any = None
    path: str | None = None
    version: str | None = None
    # The design opened this session, pinned by `open_file`. See `_require_design`.
    design: Any = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self.modeler is not None

    def start(self, version: str | int | None = None, hidden: bool = True, timeout: int = 180) -> dict:
        """Launch a hidden SpaceClaim and return the backend facts.

        ``version`` is optional on purpose. Hard-coding 242 is exactly what
        breaks when the package moves to another machine, so when it is omitted
        we take the newest installed release and let the product pick.
        """
        if self.connected:
            return self.status()

        agc = _import_geometry()

        if version is None:
            newest = detect_ansys_roots()["newest"]
            version = newest or None

        try:
            _stderr(f"scdm: launching SpaceClaim version={version!r} hidden={hidden} timeout={timeout}s")
            self.modeler = agc.launch_modeler_with_spaceclaim(
                version=version, hidden=hidden, timeout=timeout
            )
            _stderr("scdm: launch returned")
        except Exception as exc:
            raise SessionError(
                f"could not start SpaceClaim (version={version!r}): {exc}\n"
                "Check: the release is installed, the licence is reachable, and "
                f"AWP_ROOT* resolves. Detected: {detect_ansys_roots()['releases']}"
            ) from exc

        backend = getattr(self.modeler.client, "backend_version", None)
        self.version = _format_version(backend)
        return self.status()

    def status(self) -> dict:
        if not self.connected:
            roots = detect_ansys_roots()
            return {
                "connected": False,
                "ansys_releases": roots["releases"],
                "spaceclaim_executables": roots["spaceclaim"],
                "hint": "call scdm_session_start to launch SpaceClaim",
            }
        client = self.modeler.client
        design = self.modeler.design
        return {
            "connected": True,
            "backend_type": str(getattr(client, "backend_type", "")),
            "backend_version": self.version,
            "current_file": self.path,
            "design_name": getattr(design, "name", None),
            "body_count": len(list(design.bodies)) if design is not None else 0,
        }

    def close(self) -> dict:
        if not self.connected:
            return {"closed": False, "reason": "no session"}
        try:
            self.modeler.close()
        finally:
            self.modeler = None
            self.design = None
            self.path = None
        return {"closed": True}

    # -- files -------------------------------------------------------------

    def open_file(self, path: str, upload_to_server: bool = False) -> dict:
        """Open a design and summarise it.

        ``upload_to_server=False`` keeps the file local, which is what we want
        on a single machine: the modeler is a local process.
        """
        if not self.connected:
            raise SessionError("no SpaceClaim session; call scdm_session_start first")

        target = Path(path)
        if not target.exists():
            raise SessionError(f"file not found: {target}")
        if target.suffix.lower() not in SCDOC_SUFFIXES:
            # Not fatal: PyAnsys Geometry reads many CAD formats. Just say so.
            os.environ.setdefault("ANSYS_BRIDGE_LAST_SUFFIX", target.suffix.lower())

        design = self.modeler.open_file(str(target), upload_to_server=upload_to_server)
        self.path = str(target)
        # Pin it. `open_file` adds a design rather than replacing the active one,
        # so `modeler.design` is not a reliable way back to this model later.
        self.design = design
        summary = self.describe(design)
        summary["is_active"] = getattr(design, "is_active", None)
        return {"opened": str(target), **summary}

    # -- reading -----------------------------------------------------------

    def describe(self, design=None) -> dict:
        design = design if design is not None else (self.design or self.modeler.design)
        if design is None:
            return {"design": None, "bodies": [], "named_selections": []}
        bodies = []
        for body in design.bodies:
            try:
                faces = len(body.faces)
            except Exception:
                faces = None
            try:
                volume = float(body.volume.magnitude)
            except Exception:
                volume = None
            bodies.append({"name": body.name, "faces": faces, "volume_m3": volume})
        try:
            selections = [ns.name for ns in design.named_selections]
        except Exception:
            selections = []
        return {
            "design": getattr(design, "name", None),
            "is_active": getattr(design, "is_active", None),
            "body_count": len(bodies),
            "bodies": bodies,
            "named_selections": selections,
        }

    def collisions(self, left: str | None = None, right: str | None = None, all_pairs: bool = False) -> dict:
        """Pairwise collision state using ``Body.get_collision``.

        Returns the semantic enum (NONE / TOUCH / INTERSECT / CONTAINED /
        CONTAINEDTOUCH) instead of a boolean, because TOUCH and INTERSECT mean
        very different things for meshing: TOUCH is a mating face, INTERSECT is
        real overlap.
        """
        design = self._require_design()
        by_name = {b.name: b for b in design.bodies}

        if all_pairs:
            names = sorted(by_name)
            pairs = [(a, b) for i, a in enumerate(names) for b in names[i + 1 :]]
        elif left and right:
            pairs = [(left, right)]
        else:
            raise SessionError("pass left+right, or all_pairs=true")

        results = []
        for a, b in pairs:
            if a not in by_name or b not in by_name:
                results.append({"left": a, "right": b, "error": "body not found"})
                continue
            try:
                state = by_name[a].get_collision(by_name[b])
            except Exception as exc:
                results.append({"left": a, "right": b, "error": str(exc)})
                continue
            results.append({"left": a, "right": b, "collision": getattr(state, "name", str(state))})

        counts: dict[str, int] = {}
        for row in results:
            key = row.get("collision") or row.get("error", "ERROR")
            counts[key] = counts.get(key, 0) + 1
        return {"pair_count": len(results), "counts": counts, "pairs": results}

    # -- geometric inspection (read-only) ----------------------------------

    def inspect_geometry(
        self,
        checks: list[str] | None = None,
        bodies: list[str] | None = None,
        short_edge_length: float | None = None,
        small_face_area: float | None = None,
    ) -> dict:
        """Run the official geometry checks and report what is wrong with the model.

        This is the diagnosis half of the tool set, and it exists for one
        measured reason: a Fluent Meshing run on ``zhuangpeiti_fix_9_10_1.scdoc``
        failed at Describe Geometry / computing regions with **Found overlapping
        faces**. ``find_duplicate_faces`` names the culprits exactly -- two pairs
        of coincident faces, one on ``stator`` and one on ``pip``, each pair with
        an identical area:

            stator 0:41501   area 0.09292831069318609
            pip    0:15387   area 0.09292831069318609
            stator 0:41504   area 0.12176813125314039
            pip    0:15396   area 0.12176813125314039

        Reading that is the difference between "the mesh failed" and "these two
        faces are stacked on top of each other". Every check here is read-only;
        the source file's SHA-256 was unchanged after a full run.

        ``inexact_edges`` is accepted but reported as untrustworthy: it returned
        3348 entries on that assembly, all of them with an empty ``edges`` list,
        so its count means nothing. See :data:`UNRELIABLE_CHECKS`.

        ``short_edges`` and ``small_faces`` need a threshold the official methods
        do not supply. Their defaults found nothing while 0.01 m found 675 short
        edges, so when no threshold is given a ladder is scanned and every rung
        is reported -- the caller should not have to guess the model's scale.
        """
        design = self._require_design()
        by_name = {b.name: b for b in design.bodies}
        names = bodies or sorted(by_name)
        missing = [n for n in names if n not in by_name]
        if missing:
            raise SessionError(f"bodies not found: {missing}")
        targets = [by_name[n] for n in names]

        wanted = checks or [c for c in GEOMETRY_CHECKS if c not in UNRELIABLE_CHECKS]
        unknown = [c for c in wanted if c not in GEOMETRY_CHECKS]
        if unknown:
            raise SessionError(f"unknown checks {unknown}; choose from {sorted(GEOMETRY_CHECKS)}")

        report: dict = {
            "bodies_checked": len(targets),
            "faces_checked": sum(len(b.faces) for b in targets),
            "checks": {},
        }

        for name in wanted:
            method = getattr(self.modeler.repair_tools, GEOMETRY_CHECKS[name])
            try:
                if name == "short_edges":
                    report["checks"][name] = self._scan_threshold(
                        method, targets, "length", short_edge_length, SHORT_EDGE_SCAN_M
                    )
                elif name == "small_faces":
                    report["checks"][name] = self._scan_threshold(
                        method, targets, "area", small_face_area, SMALL_FACE_SCAN_M2
                    )
                else:
                    report["checks"][name] = problem_summary(method(targets))
            except Exception as exc:  # noqa: BLE001 - one dead check must not hide the rest
                report["checks"][name] = {"error": f"{type(exc).__name__}: {exc}"}
            if name in UNRELIABLE_CHECKS:
                report["checks"][name]["unreliable"] = UNRELIABLE_CHECKS[name]

        found = {
            name: data.get("object_count") or 0
            for name, data in report["checks"].items()
            if isinstance(data, dict) and "error" not in data
        }
        report["problems_found"] = found
        report["clean"] = not any(found.values())
        return report

    def _scan_threshold(
        self,
        method: Any,
        bodies: list,
        kwarg: str,
        explicit: float | None,
        ladder: tuple,
    ) -> dict:
        """One check at an explicit threshold, or the whole ladder when none is given."""
        attempts = [explicit] if explicit is not None else list(ladder)
        scan: list[dict] = []
        chosen: tuple | None = None
        for value in attempts:
            try:
                items = method(bodies, **{kwarg: value})
            except Exception as exc:  # noqa: BLE001
                scan.append({"threshold": value, "error": f"{type(exc).__name__}: {exc}"})
                continue
            counted = _count_objects(items)
            scan.append({"threshold": value, "group_count": len(items), "object_count": counted})
            if counted and chosen is None:
                chosen = (value, items)

        result: dict = {
            "scan": scan,
            "threshold_used": chosen[0] if chosen else None,
        }
        if chosen is None:
            result.update({"group_count": 0, "object_count": 0, "by_body": {}, "groups": []})
            return result
        result.update(problem_summary(chosen[1]))
        span = _numeric_range(chosen[1], kwarg)
        if span:
            result["span"] = span
        return result

    # -- mutating operators (guarded) --------------------------------------

    def boolean(self, operation: str, target: str, tools: list[str], keep_tools: bool = False) -> dict:
        """``unite`` / ``subtract`` / ``intersect`` on named bodies, guarded.

        ``subtract`` is expected to raise :class:`NoGeometryChange` on
        SpaceClaim 2024 R2. That exception is the correct outcome, not a bug in
        this wrapper: it tells the caller the kernel declined to do the work.
        """
        design = self._require_design()
        by_name = {b.name: b for b in design.bodies}
        if target not in by_name:
            raise SessionError(f"target body not found: {target}")
        missing = [t for t in tools if t not in by_name]
        if missing:
            raise SessionError(f"tool bodies not found: {missing}")

        before = snapshot_design(design)
        target_body = by_name[target]
        tool_bodies = [by_name[t] for t in tools]

        method = {"unite": "unite", "subtract": "subtract", "intersect": "intersect"}.get(operation)
        if method is None:
            raise SessionError(f"unknown operation: {operation!r}")

        _stderr(f"boolean {operation}: {target} <- {tools}, before {before.summary()}")
        getattr(target_body, method)(tool_bodies, keep_other=keep_tools)

        # Re-read from the service: in-memory handle state is not authoritative.
        # `_require_design` re-activates the pinned design, so the snapshot
        # cannot describe a different model than the one just operated on.
        after = snapshot_design(self._require_design())
        _stderr(f"boolean {operation}: after {after.summary()}")
        # The verdict is about the target. `keep_other=False` deletes the tool
        # body, which is already a design-level change, so a guard that only
        # asked "did anything change" would pass the subtract that left the
        # stator untouched. Naming the target is what catches that.
        diff = assert_changed(operation, before, after, targets=[target])
        return {"operation": operation, "target": target, "tools": tools, **diff}

    def share_topology(self, bodies: list[str] | None = None, tolerance: float = 0.0) -> dict:
        """Shared topology, guarded against the false ``True``.

        This returned ``True`` on a 31-body assembly while stator faces stayed
        at 382 and the body count stayed at 31. The guard turns that into an
        explicit failure rather than a silent no-op.
        """
        design = self._require_design()
        by_name = {b.name: b for b in design.bodies}
        names = bodies or [b.name for b in design.bodies]
        missing = [n for n in names if n not in by_name]
        if missing:
            raise SessionError(f"bodies not found: {missing}")

        before = snapshot_design(design)
        reported = self.modeler.prepare_tools.share_topology(
            [by_name[n] for n in names], tol=tolerance
        )
        after = snapshot_design(self._require_design())

        if before.same_as(after):
            raise SessionError(
                f"share_topology returned {reported!r} but the design did not change "
                f"({before.summary()}).\n"
                "This is the measured behaviour on a multi-body assembly in 2024 R2: the "
                "return value is not evidence. For a multi-part assembly the working route "
                "is Non-Conformal + Mesh Interface in Fluent Meshing, not fused topology."
            )
        return {"share_topology": True, "reported": reported, **after.diff(before)}

    # -- escape hatch ------------------------------------------------------

    def run_script(self, script: str, args: dict[str, str] | None = None, import_design: bool = True) -> dict:
        """Run an IronPython/SpaceClaim script through the official channel.

        This is the coverage escape hatch. Anything the typed tools above do not
        expose is reachable here, and the call carries a real return value and a
        real exception instead of the stdout-scraping the raw ``/RunScript``
        path requires.
        """
        if not self.connected:
            raise SessionError("no SpaceClaim session; call scdm_session_start first")
        path = Path(script)
        if not path.exists():
            raise SessionError(f"script not found: {path}")
        values, design = self.modeler.run_script_file(
            str(path), script_args=args, import_design=import_design
        )
        result: dict = {"script": str(path), "returned": values}
        if design is not None:
            self.design = design
            result.update(self.describe(design))
        return result

    def export(self, fmt: str, location: str) -> dict:
        design = self._require_design()
        method = _EXPORT_METHODS.get(fmt.lower())
        if method is None:
            raise SessionError(f"unsupported format {fmt!r}; choose from {sorted(_EXPORT_METHODS)}")
        out = getattr(design, method)(location)
        out_path = Path(str(out))
        return {
            "format": fmt,
            "path": str(out_path),
            "exists": out_path.exists(),
            "bytes": out_path.stat().st_size if out_path.exists() else 0,
        }

    # -- measurement -------------------------------------------------------

    def min_distance(self, left: str, right: str) -> dict:
        """Minimum distance between two bodies, faces or edges, in metres.

        The continuous counterpart to :meth:`collisions`. Collision state is
        discrete (`TOUCH` / `NONE`); this answers "how far apart", which is what
        you need to decide whether a mesh interface is reasonable or whether a
        gap is a leak.

        Measured on the reference assembly: `stator`↔`winding 1` and
        `stator`↔`pip` are **0.0 m** (in contact, matching their `TOUCH` state),
        `stator`↔`inlet` and `stator`↔`outlet` are 0.05036119537898199 m, and
        `winding 1`↔`winding 2` are 0.2072241239859292 m.

        The value comes back as a `Distance`, whose number lives one level down
        in `.value.magnitude` -- reading `.magnitude` on the `Distance` itself
        raises `AttributeError`.
        """
        design = self._require_design()
        by_name = {b.name: b for b in design.bodies}
        for name in (left, right):
            if name not in by_name:
                raise SessionError(f"body not found: {name}")
        gap = self.modeler.measurement_tools.min_distance_between_objects(
            by_name[left], by_name[right]
        )
        return {
            "left": left,
            "right": right,
            "distance_m": _distance_m(gap),
            "touching": _distance_m(gap) == 0.0,
        }

    # -- files -------------------------------------------------------------

    def insert_file(self, path: str) -> dict:
        """Merge another CAD file into the open design.

        Not the same as :meth:`open_file`, which opens a document. This adds the
        file's contents to the design already open -- measured, a 31-body design
        became 62 bodies, and the call returned a `Component` named
        `定转子装配(1)`.

        Import options are left at their defaults, which is a deliberate choice:
        the default `ImportOptions` already sets `import_named_selections=True`,
        and named selections are what every other tool in this server selects by.
        """
        target = Path(path)
        if not target.exists():
            raise SessionError(f"file not found: {target}")
        design = self._require_design()
        before = len(list(design.bodies))
        component = design.insert_file(str(target))
        design = self._require_design()
        return {
            "inserted": str(target),
            "component": getattr(component, "name", None),
            "bodies_before": before,
            "bodies_after": len(list(design.bodies)),
        }

    # -- transforms --------------------------------------------------------

    def transform(
        self,
        operation: str,
        bodies: list[str],
        scale_factor: float | None = None,
        angle_deg: float | None = None,
        axis: str | list[float] | None = None,
        origin: list[float] | None = None,
    ) -> dict:
        """Rotate, scale or mirror bodies, then verify the geometry moved.

        These change the model, so the same rule as the booleans applies: never
        trust the call, measure the result. A spatial fingerprint is taken before
        and after, built from `Edge.start` / `Edge.end` because every
        bounding-box and centroid API on 24R2 demands 27.1.

        `scale` is easy to verify independently -- the volume should change by
        the cube of the factor. Measured: `scale(1.5)` took the stator from
        0.003314505208 m³ to 0.01118664023 m³, which is 3.375×, exactly 1.5³.
        `rotate` was verified by the fingerprint moving in x and y while z stayed
        put, which is what a rotation about z must do.

        `axis` takes "x" / "y" / "z" or a 3-vector. `origin` defaults to the
        world origin, which is rarely what you want for a part that is not
        centred there -- pass the point you actually mean to rotate about.
        """
        design = self._require_design()
        by_name = {b.name: b for b in design.bodies}
        missing = [n for n in bodies if n not in by_name]
        if missing:
            raise SessionError(f"bodies not found: {missing}")
        targets = [by_name[n] for n in bodies]

        if operation == "scale":
            if scale_factor is None:
                raise SessionError("scale needs scale_factor")
            if scale_factor <= 0:
                raise SessionError("scale_factor must be positive")
            action = lambda body: body.scale(scale_factor)  # noqa: E731
        elif operation == "rotate":
            if angle_deg is None:
                raise SessionError("rotate needs angle_deg")
            point = _point(origin)
            direction = _unit_vector(axis or "z")
            action = lambda body: body.rotate(point, direction, angle_deg)  # noqa: E731
        elif operation == "mirror":
            plane = _plane_from_normal(origin, axis or "z")
            action = lambda body: body.mirror(plane)  # noqa: E731
        else:
            raise SessionError(
                f"unknown operation {operation!r}; choose from rotate, scale, mirror"
            )

        before = {body.name: spatial_fingerprint(body) for body in targets}
        volumes_before = {body.name: _body_volume(body) for body in targets}
        for body in targets:
            action(body)
        after = {body.name: spatial_fingerprint(body) for body in targets}
        volumes_after = {body.name: _body_volume(body) for body in targets}

        moved = {
            name: before[name] != after[name]
            for name in before
            if before[name] is not None and after[name] is not None
        }
        _stderr(f"transform {operation}: moved {sum(moved.values())}/{len(moved)} bodies")
        return {
            "operation": operation,
            "bodies": bodies,
            "verified": bool(moved) and all(moved.values()),
            "moved": moved,
            "fingerprint_before": before,
            "fingerprint_after": after,
            "volume_before_m3": volumes_before,
            "volume_after_m3": volumes_after,
        }

    # -- helpers -----------------------------------------------------------

    def _require_design(self):
        """The design this session is working on, made active.

        Pinning the object returned by ``open_file`` is not tidiness. A
        SpaceClaim modeler starts with a design already open ("Design1"), and
        ``modeler.open_file`` adds another one -- it does **not** replace the
        active design. Reading ``self.modeler.design`` after a second
        ``open_file`` therefore hands back the *first* design, and every
        measured number describes the wrong model. That is exactly what
        happened on the first end-to-end run: opening a 31-body, 838-face
        assembly reported "bodies 30->31 faces 838->846", numbers that only make
        sense if the operators were running against the startup design while the
        summary came from the file.

        So: remember the design that was opened, make it active, and verify.
        """
        if not self.connected:
            raise SessionError("no SpaceClaim session; call scdm_session_start first")
        design = self.design if self.design is not None else self.modeler.design
        if design is None:
            raise SessionError("no design open; call scdm_open_file first")
        if not getattr(design, "is_active", True):
            design.activate()
        if not getattr(design, "is_active", True):
            raise SessionError(
                f"design {getattr(design, 'name', '?')!r} could not be made active; "
                "operations would otherwise run against a different design and the "
                "before/after comparison would describe the wrong model."
            )
        return design

