"""Guards for operations that report success without doing anything.

Why this module exists
----------------------
On SpaceClaim 2024 R2 we measured two operations whose return value is not
evidence that geometry changed:

* ``Body.subtract`` returns normally, deletes the tool body, and leaves the
  target's volume and face count untouched. The same symptom appears through
  the raw IronPython ``Shape.Subtract`` path, so it is a kernel/geometry
  problem, not an API-path problem.
* ``prepare_tools.share_topology`` returns ``True`` on a 31-body assembly
  while stator faces stay 382 and body count stays 31.

Both were measured on 2026-09-19 and are recorded in the project memory
document ``7d831517``. The rule those measurements produce is simple:

    Never trust a success flag. Compare geometry facts.

Every mutating tool in this server therefore snapshots the design before and
after, and raises :class:`NoGeometryChange` when nothing measurable moved.
Nothing here is theoretical: it is the difference between "we ran the call"
and "the model changed".
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "NoGeometryChange",
    "GeometrySnapshot",
    "snapshot_design",
    "spatial_fingerprint",
    "assert_changed",
    "detect_ansys_roots",
    "detect_fluent_root",
    "package_version",
]

# Relative tolerance for volume comparison.
#
# 1e-4, not 1e-6, and the number comes from measurement rather than taste. The
# same stator volume read 0.00331450520811706 in one session and
# 0.0033145600685288304 in another -- a relative spread of 1.7e-5 from the
# service alone. A tolerance of 1e-6 sits *below* that noise, so the guard meant
# to catch a silent no-op could have passed one by accident. Real boolean changes
# are around 1e-2 relative (unite moved the stator volume by 2.1e-2), so 1e-4
# separates signal from noise by two orders of magnitude on either side.
VOLUME_REL_TOL = 1e-4
# Face counts are exact, but a boolean that only imprints may keep the body
# count. Any change in topology is accepted as "something happened".
FACE_DELTA_TOL = 0


class NoGeometryChange(RuntimeError):
    """Raised when a mutating operation left the design measurably unchanged.

    This is deliberate and loud. A caller that silently accepted the no-op
    would go on to mesh and solve a model that was never modified.

    ``target`` names the body whose own measurements decided the verdict when
    the operation had one; it is `None` for whole-design operations.
    """

    def __init__(
        self,
        operation: str,
        before: "GeometrySnapshot",
        after: "GeometrySnapshot",
        target: str | None = None,
    ):
        self.operation = operation
        self.before = before
        self.after = after
        self.target = target
        detail = f"'{target}' kept " if target else ""
        super().__init__(
            f"{operation!r} reported success but the design did not change.\n"
            f"  before: {before.summary()}\n"
            f"  after : {after.summary()}\n"
            f"  {detail}"
            "the same face count and volume, so the kernel declined to do the "
            "work even though the tool body was removed.\n"
            "On SpaceClaim 2024 R2 this is a known behaviour of Body.subtract and of "
            "share_topology on multi-body assemblies. Treat the call as a no-op: do not "
            "continue as if the geometry had been modified."
        )


@dataclass
class GeometrySnapshot:
    """A cheap, comparable fingerprint of the active design."""

    body_count: int = 0
    total_faces: int = 0
    total_volume: float = 0.0
    bodies: dict[str, tuple[int, float]] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"bodies={self.body_count} faces={self.total_faces} "
            f"volume={self.total_volume:.10g}"
        )

    def same_as(self, other: "GeometrySnapshot") -> bool:
        """True when nothing measurable moved."""
        if self.body_count != other.body_count:
            return False
        if abs(self.total_faces - other.total_faces) > FACE_DELTA_TOL:
            return False
        scale = max(abs(self.total_volume), abs(other.total_volume), 1e-30)
        if abs(self.total_volume - other.total_volume) / scale > VOLUME_REL_TOL:
            return False
        return True

    def diff(self, after: "GeometrySnapshot") -> dict:
        """What moved between this snapshot and a later one.

        ``self`` is the earlier state and ``after`` the later one — the same
        direction ``same_as`` and ``assert_changed`` read. The first version of
        this method had it backwards, which reported a working boolean as
        "faces 838->846, winding 1 added" instead of "846->838, winding 1
        removed": every number was individually correct and the whole sentence
        was wrong. The signature is named for the argument that is easy to
        misplace.
        """
        changed = []
        for name in sorted(set(self.bodies) | set(after.bodies)):
            a = self.bodies.get(name)
            b = after.bodies.get(name)
            if b is None:
                changed.append({"name": name, "change": "removed", "faces": a[0]})
            elif a is None:
                changed.append({"name": name, "change": "added", "faces": b[0]})
            elif a[0] != b[0] or abs(a[1] - b[1]) > VOLUME_REL_TOL * max(abs(a[1]), 1e-30):
                changed.append(
                    {
                        "name": name,
                        "change": "modified",
                        "faces_before": a[0],
                        "faces_after": b[0],
                        "volume_before": a[1],
                        "volume_after": b[1],
                    }
                )
        return {
            "body_count_before": self.body_count,
            "body_count_after": after.body_count,
            "faces_before": self.total_faces,
            "faces_after": after.total_faces,
            "volume_before": self.total_volume,
            "volume_after": after.total_volume,
            "bodies_changed": changed,
        }


def snapshot_design(design) -> GeometrySnapshot:
    """Fingerprint a PyAnsys Geometry ``Design``.

    Reads only what is cheap and reliable. ``Body.volume`` and ``len(Body.faces)``
    were both cross-checked against the IronPython path on the same .scdoc and
    agreed exactly (see memory document ``7d831517``).
    """
    snap = GeometrySnapshot()
    if design is None:
        return snap
    bodies = list(design.bodies)
    snap.body_count = len(bodies)
    for body in bodies:
        try:
            faces = len(body.faces)
        except Exception:
            faces = -1
        try:
            vol = float(body.volume.magnitude)
        except Exception:
            vol = 0.0
        snap.bodies[body.name] = (faces, vol)
        if faces > 0:
            snap.total_faces += faces
        snap.total_volume += vol
    return snap


def spatial_fingerprint(obj: Any, max_vertices: int = 400) -> dict | None:
    """Axis-aligned extent of the vertices reachable through an object's edges.

    Why not the obvious API: measured on 24R2, `Face.bounding_box`,
    `Face.get_bounding_box`, `Face.centroid`, `Edge.bounding_box` and
    `Edge.centroid` all raise `GeometryRuntimeError` demanding 27.1. `Body`'s
    equivalents do too. `Edge.start` / `Edge.end` are the only vertex access that
    works, so the extent is assembled out of those.

    Why a bound: every read is a round trip to the service, and this runs twice
    per transform to prove the transform happened. A 738-face assembly has
    thousands of edges.

    Returns ``None`` when no vertex could be read — which is **not** the same as
    a box that did not move. The caller must not read "nothing changed" out of
    "could not tell", so the count of failed reads is reported alongside.
    """
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    read = 0
    unreadable = 0

    try:
        faces = list(getattr(obj, "faces", None) or [])
    except Exception:  # noqa: BLE001
        return None

    for face in faces:
        if read >= max_vertices:
            break
        try:
            edges = list(getattr(face, "edges", None) or [])
        except Exception:  # noqa: BLE001
            continue
        for edge in edges:
            if read >= max_vertices:
                break
            for attr in ("start", "end"):
                try:
                    point = getattr(edge, attr)
                    xyz = (
                        float(point.x.magnitude),
                        float(point.y.magnitude),
                        float(point.z.magnitude),
                    )
                except Exception:  # noqa: BLE001 - some edges raise, see _numeric_range
                    unreadable += 1
                    continue
                read += 1
                for axis, value in enumerate(xyz):
                    lo[axis] = min(lo[axis], value)
                    hi[axis] = max(hi[axis], value)

    if read == 0:
        return None
    return {
        "min": [round(v, 9) for v in lo],
        "max": [round(v, 9) for v in hi],
        "vertices_read": read,
        "unreadable": unreadable,
    }


def assert_changed(
    operation: str,
    before: GeometrySnapshot,
    after: GeometrySnapshot,
    targets: list[str] | None = None,
) -> dict:
    """Raise :class:`NoGeometryChange` unless the operation measurably did its job.

    Always returns the diff on success, so the caller can report *what* changed
    instead of just "ok".

    ``targets`` is what makes this usable for booleans, and it fixes a real
    miss. A SpaceClaim boolean with ``keep_other=False`` deletes the tool body,
    so "did anything change?" is answered **yes** even by the `subtract` that
    leaves the target completely untouched: 31 bodies become 30, the diff is
    non-empty, and a guard asking only that question waves through the exact
    silent no-op this module exists to catch. Measured on 2024 R2:

        stator - winding 1  ->  stator faces 382 -> 382, volume unchanged,
                                bodies 31 -> 30 (only the tool was deleted)

    So when ``targets`` is given the verdict is about the target: a target that
    survived with identical face count and volume means the kernel declined to
    do the work, whatever the body count says. A target that vanished is not a
    success either.

    Argument order is also load-bearing. `diff` reads ``self`` as the earlier
    state, so this is ``before.diff(after)``. Reversing it produced internally
    consistent labels over reversed data -- "838->846, winding 1 added" for a
    unite that actually went 846->838 and removed a body.
    """
    if before.same_as(after):
        raise NoGeometryChange(operation, before, after)

    for name in targets or []:
        b = before.bodies.get(name)
        a = after.bodies.get(name)
        if b is None:
            continue
        if a is None:
            raise NoGeometryChange(operation, before, after, target=name)
        if b[0] == a[0] and abs(b[1] - a[1]) <= VOLUME_REL_TOL * max(abs(b[1]), 1e-30):
            raise NoGeometryChange(operation, before, after, target=name)

    return before.diff(after)


# ---------------------------------------------------------------------------
# Runtime environment detection
#
# Cross-device portability depends on not hard-coding a version. `AWP_ROOT242`
# on this machine is `AWP_ROOT251` on the next one, and SpaceClaim gates whole
# feature families on the backend version. Everything below is discovery, not
# configuration.
# ---------------------------------------------------------------------------

_COMMON_ANSYS_DIRS = (
    r"C:\Program Files\ANSYS Inc",
    r"C:\Program Files\ANSYS Inc\Shared Files",
    "/usr/ansys_inc",
    "/ansys_inc",
)


def detect_ansys_roots() -> dict:
    """Find every installed ANSYS release, newest first.

    Sources, in order of trust: ``AWP_ROOT<NNN>`` environment variables, then a
    directory scan of the usual install roots. Never guesses a version.
    """
    found: dict[str, str] = {}

    for key, value in os.environ.items():
        if not key.startswith("AWP_ROOT"):
            continue
        suffix = key[len("AWP_ROOT") :]
        if suffix.isdigit() and value:
            found[suffix] = value
    if os.environ.get("AWP_ROOT"):
        # Unversioned AWP_ROOT — usable only if nothing versioned exists.
        found.setdefault("", os.environ["AWP_ROOT"])

    for base in _COMMON_ANSYS_DIRS:
        root = Path(base)
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if not child.is_dir():
                continue
            name = child.name
            if name.startswith("v") and name[1:].isdigit() and len(name) == 4:
                found.setdefault(name[1:], str(child))

    ordered = {k: found[k] for k in sorted(found, key=lambda s: (s == "", s), reverse=True)}
    return {
        "releases": ordered,
        "newest": next(iter(ordered), None),
        "spaceclaim": {
            k: str(Path(v) / "scdm" / "SpaceClaim.exe")
            for k, v in ordered.items()
            if (Path(v) / "scdm" / "SpaceClaim.exe").exists()
        },
    }


def detect_fluent_root() -> str | None:
    """Locate the Fluent product root, honouring ``PYFLUENT_FLUENT_ROOT``."""
    explicit = os.environ.get("PYFLUENT_FLUENT_ROOT")
    if explicit and Path(explicit).is_dir():
        return explicit
    for release, root in detect_ansys_roots()["releases"].items():
        candidate = Path(root) / "fluent"
        if candidate.is_dir():
            return str(candidate)
    return None


# Distribution names for the modules we report on. Kept explicit because a
# module name and its distribution name are not always the same string.
_DISTRIBUTIONS = {
    "ansys.geometry.core": "ansys-geometry-core",
    "ansys.fluent.core": "ansys-fluent-core",
    "mcp": "mcp",
}


def package_version(module_name: str) -> str | None:
    """Installed version of a package, **without importing it**.

    This matters more than it looks. The MCP stdio transport carries JSON-RPC on
    stdout, so any library that prints during import corrupts the protocol
    stream and the client drops the connection. Importing ``ansys.fluent.core``
    inside a tool call did exactly that: the tool body ran in 1.6 s standalone,
    but over stdio the session died mid-call with no traceback.

    Checking whether something is installed is a metadata question, so answer it
    from metadata. Returns ``None`` when absent.
    """
    import importlib.metadata as metadata
    import importlib.util

    try:
        if importlib.util.find_spec(module_name) is None:
            return None
    except (ImportError, ValueError, ModuleNotFoundError):
        return None
    try:
        return metadata.version(_DISTRIBUTIONS.get(module_name, module_name))
    except metadata.PackageNotFoundError:
        return "unknown"
