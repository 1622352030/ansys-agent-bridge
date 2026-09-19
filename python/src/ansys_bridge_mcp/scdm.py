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

from .guards import GeometrySnapshot, assert_changed, detect_ansys_roots, snapshot_design

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
