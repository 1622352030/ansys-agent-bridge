"""MCP server exposing Ansys SpaceClaim (and the Fluent bridge point).

Design rule this server follows, taken from two independent community
projects that converged on it (see the project memory document ``dc4fc804``):
a passing call is not evidence. Tools therefore return geometry facts and name
the evidence layer they reached, and mutating tools refuse to report success
when nothing measurable moved.
"""

from __future__ import annotations

import atexit
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import __version__
from .guards import (
    NoGeometryChange,
    detect_ansys_roots,
    detect_fluent_root,
    package_version,
)
from .scdm import (
    DEFAULT_ATTACH_HOST,
    DEFAULT_ATTACH_PORT,
    DEFAULT_ATTACH_TRANSPORT,
    Session,
    SessionError,
    _stderr,
    preload,
)

_session = Session()

INSTRUCTIONS = (
    "Drive Ansys SpaceClaim through the official PyAnsys Geometry client, and report "
    "what the project has actually measured rather than what the documentation promises.\n"
    "\n"
    "Workflow: ansys_bridge_doctor -> scdm_session_start -> scdm_open_file -> operators -> "
    "scdm_export. Call scdm_session_close when finished; a SpaceClaim process holds a licence.\n"
    "\n"
    "TWO GUI MODES, and they are separate tools because the licence and ownership "
    "semantics are opposite. Every session reports which one it is in a machine-readable "
    "`mode` field:\n"
    "  * mode=launch - scdm_session_start. This server starts SpaceClaim, owns the process "
    "and holds the licence, and it is the only tool allowed to close it. `hidden` defaults "
    "to false so the user SEES the window being driven; hidden=true is /Headless=True, a "
    "batch run with no UI at all rather than a minimised window, and the visible window may "
    "open on a secondary monitor. Finish with scdm_session_close.\n"
    "  * mode=attach - scdm_attach. The user started SpaceClaim themselves and this server "
    "only connects to its gRPC endpoint. It consumes NO licence of its own, and it CANNOT "
    "close the user's window: scdm_session_close is refused in this mode, use scdm_detach. "
    "A plain double-click start does NOT work - such an instance has no gRPC endpoint at all "
    "- so the user must launch it with the ApiServer add-in and a fixed API_PORT (the "
    "desktop shortcut shipped with this project does exactly that). Both switches are a "
    "semi-public interface.\n"
    "\n"
    "Two operations on SpaceClaim 2024 R2 report success without doing anything, and this "
    "server treats that as a failure on purpose:\n"
    "  * Body.subtract returns normally, deletes the tool body, and leaves the target's "
    "volume and face count untouched.\n"
    "  * prepare_tools.share_topology returns true on a multi-body assembly while nothing "
    "moves.\n"
    "When a mutating tool raises 'reported success but the design did not change', that is "
    "the honest result. Do not retry it in a loop and do not proceed as if the geometry "
    "changed. For a multi-part assembly the working route is Non-Conformal + Mesh Interface "
    "in Fluent Meshing rather than fused topology in the CAD kernel.\n"
    "\n"
    "Never open a file the user has open in a SpaceClaim GUI, and never write to a source "
    "model. Open, operate, and export to a new path."
)


mcp = FastMCP(
    "ansys-agent-bridge",
    instructions=INSTRUCTIONS,
    # One INFO line per request by default. That goes to stderr, which is legal
    # for stdio servers but noisy enough to look like a failure in clients and
    # shells that treat any stderr as an error. Raise it with
    # ANSYS_BRIDGE_LOG_LEVEL=INFO when you actually want the chatter.
    log_level=os.getenv("ANSYS_BRIDGE_LOG_LEVEL", "WARNING").upper(),
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
STATE_CHANGE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


def _err(exc: Exception) -> dict[str, Any]:
    """Turn an exception into a structured, actionable tool result."""
    if isinstance(exc, NoGeometryChange):
        return {
            "ok": False,
            "error": "no_geometry_change",
            "operation": exc.operation,
            "target": getattr(exc, "target", None),
            "before": exc.before.summary(),
            "after": exc.after.summary(),
            "message": str(exc),
            "guidance": (
                "Reported success without changing the target. On SpaceClaim 2024 R2 this "
                "is the measured behaviour of Body.subtract, and of share_topology on a "
                "multi-body assembly: the tool body is removed and the target keeps its "
                "face count and volume. Do not retry; choose a different route."
            ),
        }
    if isinstance(exc, SessionError):
        # `code` is the machine-readable name; `extra` carries the facts a guard
        # must state next to a refusal (`process_untouched`, `licence_released`,
        # `next_step`, the session snapshot). Pre-existing raises with no code
        # still report `error: "session"`.
        result: dict[str, Any] = {
            "ok": False,
            "error": getattr(exc, "code", None) or "session",
            "message": str(exc),
        }
        for key, value in (getattr(exc, "extra", None) or {}).items():
            result.setdefault(key, value)
        return result
    return {"ok": False, "error": type(exc).__name__, "message": str(exc)}


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
def ansys_bridge_doctor() -> dict[str, Any]:
    """Report what this machine can actually do, before anything is launched.

    Detects installed ANSYS releases, the SpaceClaim executable, the Fluent root,
    the Python running this server, and the versions of the three packages the
    bridge depends on. Every path here is discovered at run time, never
    configured, so the same package works on another machine where the release
    number differs.

    Version probes read package METADATA and never import the package. That is
    not a micro-optimisation: importing ``ansys.fluent.core`` prints during
    import, which corrupts the stdio JSON-RPC stream and kills the session
    mid-call.
    """
    result: dict[str, Any] = {
        "bridge_version": __version__,
        "python": None,
        "ansys": detect_ansys_roots(),
        "fluent_root": detect_fluent_root(),
        "packages": {},
        "session": _session.status(),
    }
    result["python"] = {
        "executable": sys.executable,
        "version": ".".join(str(p) for p in sys.version_info[:3]),
    }
    # Metadata only. Importing ansys.fluent.core here would print to stdout and
    # corrupt the stdio JSON-RPC stream, killing the session mid-call.
    for module in ("ansys.geometry.core", "ansys.fluent.core", "mcp"):
        version = package_version(module)
        result["packages"][module] = version if version else "missing"
    result["ok"] = bool(result["ansys"]["spaceclaim"]) and bool(
        result["packages"].get("ansys.geometry.core") not in (None, "missing")
    )
    return result


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@mcp.tool(annotations=STATE_CHANGE)
def scdm_session_start(version: str | None = None, hidden: bool = False, timeout_s: int = 180) -> dict[str, Any]:
    """Launch SpaceClaim and connect. Holds a licence until scdm_session_close.

    This is the **launch** mode: this server creates the process, owns it, and is
    the only party allowed to close it. To drive a SpaceClaim the *user* started,
    call `scdm_attach` instead - that mode consumes no licence and this server is
    forbidden from closing that window.

    `hidden` defaults to **false**, mirroring the Fluent side's default
    `ui_mode="gui"`. `hidden=false` was measured to really produce a visible main
    window (`设计(D)1 - SpaceClaim`, 1935x1047, `IsWindowVisible=True`), 28.75 s to a
    usable gRPC endpoint, ~971-973 MB working set.

    `hidden=true` is **not** a minimised window: PyAnsys turns it into
    `/Headless=True`, which Ansys documents as batch mode *without UI*. Measured,
    of 40 top-level windows the only visible ones were two 13x13 untitled stubs,
    and it is faster and lighter (23.47 s, ~885-888 MB) precisely because there is
    no UI. Use it only when nobody needs to watch.

    The visible window can land on a **secondary monitor** (measured at x=-1927 on
    this machine, a negative-coordinate second display); PyAnsys does not expose
    window placement, so pinning a monitor would take `/WindowLocation=` in the
    executable arguments.

    Leave `version` unset and the newest installed release is used, which is what
    makes this portable between machines. Set it (for example "242") only when a
    specific release is required; if that release is not installed the launch
    fails with the detected list attached.
    """
    try:
        session = _session.start(version=version, hidden=hidden, timeout=timeout_s)
    except Exception as exc:
        return _err(exc)
    return {
        "ok": True,
        # Machine-readable, and taken from the session rather than from this
        # call: if a session was already live, `start` reports that one and a
        # caller asking for `hidden=False` here must be told "attach" instead of
        # being told what it wanted to hear.
        "mode": session.get("mode") or "launch",
        "ui_visible": session.get("ui_visible"),
        "session": session,
    }


@mcp.tool(annotations=STATE_CHANGE)
def scdm_attach(
    host: str = DEFAULT_ATTACH_HOST,
    port: int = DEFAULT_ATTACH_PORT,
    transport_mode: str = DEFAULT_ATTACH_TRANSPORT,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    r"""Attach to a SpaceClaim the USER started. Consumes no licence, closes no window.

    This is the **attach** mode, and it is a separate tool on purpose: the licence
    and ownership semantics are the opposite of `scdm_session_start`. Nothing here
    creates a process, so nothing here may end one - `scdm_session_close` is
    refused in this mode and `scdm_detach` closes only the gRPC channel.

    Precondition, and the reason a plain double-click is not enough: the instance
    must have been started with the ApiServer add-in and a fixed port. Measured on
    this machine, a SpaceClaim started by double-click listened on 49013/49014/63186
    and **all three** answered UNAVAILABLE to a gRPC health check - there is no
    endpoint to attach to. The same instance started with
    `/ADDINMANIFESTFILE=<...>\Addins\ApiServer\Presentation.ApiServerAddIn.Manifest.xml`
    and `API_PORT=51790` accepted this connection in **0.31 s**. The desktop
    shortcut shipped with this project starts it exactly that way, and a failed
    attach returns that full command line so it can be pasted.

    `port` is a convention rather than a discovery mechanism - SpaceClaim has no
    equivalent of Fluent's `-sifile=`, and PyAnsys picks a random port when
    `API_PORT` is unset - so pass whatever port the user's launcher set.

    `transport_mode` is one of wnua / insecure / mtls / uds. Only `wnua` has been
    measured working here; on Windows WNUA authenticates the local user and needs
    no password or certificate, which is why it is the default.

    Returns `mode="attach"`, the `observed` backend facts, `licence`, and
    `process_ownership.owner="user"`.

    `/ADDINMANIFESTFILE` and `API_PORT` are a **semi-public** interface: they appear
    in the PyAnsys Geometry source and in measurement, but not in the public Ansys
    command-line documentation, so a future release may change them.
    """
    try:
        session = _session.attach(
            host=host, port=port, transport_mode=transport_mode, timeout=timeout_s
        )
    except Exception as exc:
        return _err(exc)
    return {
        "ok": True,
        "mode": "attach",
        "transport": {
            "host": session.get("attached_to", {}).get("host", host),
            "port": session.get("attached_to", {}).get("port", port),
            "mode": session.get("attached_to", {}).get("transport_mode", transport_mode),
            "encrypted": str(transport_mode).lower() == "mtls",
            "note": (
                "wnua is Windows Named User Authentication: the local user is the "
                "credential, so there is no password and no certificate (unlike the Fluent "
                "bridge, which uses a plaintext channel with a password file)"
            ),
        },
        "observed": session.get("observed"),
        "licence": session.get("licence"),
        "process_ownership": session.get("process_ownership"),
        "session": session,
    }


@mcp.tool(annotations=READ_ONLY)
def scdm_session_status() -> dict[str, Any]:
    """Report whether a SpaceClaim session is live, and where it came from.

    `mode` distinguishes `launch` (this server owns the process and its licence)
    from `attach` (the user owns it; this server cannot close it).
    """
    return {"ok": True, "mode": _session.mode, "session": _session.status()}


@mcp.tool(annotations=STATE_CHANGE)
def scdm_session_close() -> dict[str, Any]:
    """Close the SpaceClaim session and release the licence. LAUNCH sessions only.

    In an **attach** session this call is refused with
    `error="attach_mode_refuses_close"`, `process_untouched=true` and
    `licence_released=false`. That SpaceClaim belongs to the user; this server never
    closes it. Use `scdm_detach` to disconnect and leave the window running.
    """
    try:
        return {"ok": True, **_session.close()}
    except Exception as exc:
        return _err(exc)


@mcp.tool(annotations=STATE_CHANGE)
def scdm_detach() -> dict[str, Any]:
    """Disconnect from a user-owned SpaceClaim: closes the gRPC channel, nothing else.

    The counterpart of `scdm_session_close`, for **attach** sessions. The user's
    SpaceClaim keeps running with its own licence and its window is untouched
    (`process_untouched=true`).

    Refused on a launch session with `error="detach_requires_attach"`: that process
    was created by this server, so dropping the channel without closing it would
    leave a SpaceClaim holding a licence with no handle left to release it. Use
    `scdm_session_close` there.
    """
    try:
        return {"ok": True, **_session.detach()}
    except Exception as exc:
        return _err(exc)



# ---------------------------------------------------------------------------
# Files and reading
# ---------------------------------------------------------------------------


@mcp.tool(annotations=STATE_CHANGE)
def scdm_open_file(path: str) -> dict[str, Any]:
    """Open a design, replacing whatever the session had open.

    `.scdoc` opens directly. The PyAnsys Geometry docstring lists only `.scdocx`,
    `.dsco` and `.pmdb`, but `.scdoc` was measured working, returning bodies,
    face counts and named selections identical to the raw IronPython reading of
    the same file. Other CAD formats (STEP, SOLIDWORKS, NX, ...) are read too.

    The file is opened read-only in effect: nothing is written back unless
    scdm_export is called with an explicit new path.
    """
    try:
        return {"ok": True, **_session.open_file(path)}
    except Exception as exc:
        return _err(exc)


@mcp.tool(annotations=READ_ONLY)
def scdm_list_bodies() -> dict[str, Any]:
    """List bodies with face count and volume, plus the named selections.

    Body names matter: the raw kernel preserves them, and named selections
    (`stator`, `windings`, `inlet`, `outlet`, ...) come back intact. Prefer
    selecting by name over positional indices, which shift when topology
    changes.
    """
    try:
        return {"ok": True, **_session.describe()}
    except Exception as exc:
        return _err(exc)


@mcp.tool(annotations=READ_ONLY)
def scdm_collisions(left: str | None = None, right: str | None = None, all_pairs: bool = False) -> dict[str, Any]:
    """Check collision state between bodies.

    Returns the semantic state rather than a yes/no, because the states mean
    different things downstream:

      NONE            separate
      TOUCH           mating faces, no volume overlap - normal for a slot and a
                      winding pressed against it
      INTERSECT       real overlap; this is what blocks Fluent Meshing
      CONTAINED       one body inside another
      CONTAINEDTOUCH  inside and touching

    Pass left+right for one pair, or all_pairs=true to sweep the whole assembly
    (O(n^2); fine for tens of bodies, not for hundreds).
    """
    try:
        return {"ok": True, **_session.collisions(left=left, right=right, all_pairs=all_pairs)}
    except Exception as exc:
        return _err(exc)


@mcp.tool(annotations=READ_ONLY)
def scdm_min_distance(left: str, right: str) -> dict[str, Any]:
    """Minimum distance between two bodies, in metres — the continuous counterpart to collisions.

    Collision state is discrete (`TOUCH` / `NONE`). This answers "how far apart",
    which is the question that decides whether a gap is a leak or a mesh
    interface is reasonable.

    Measured on the reference assembly, and it agrees with the collision states:
    `stator`↔`winding 1` and `stator`↔`pip` are 0.0 m (in contact), while
    `stator`↔`inlet` and `stator`↔`outlet` are 0.05036119537898199 m and
    `winding 1`↔`winding 2` are 0.2072241239859292 m.
    """
    try:
        return {"ok": True, **_session.min_distance(left, right)}
    except Exception as exc:
        return _err(exc)


@mcp.tool(annotations=STATE_CHANGE)
def scdm_insert_file(path: str) -> dict[str, Any]:
    """Merge another CAD file into the open design.

    Not the same as `scdm_open_file`, which opens a document. This adds the
    file's contents to the design already open — measured, a 31-body design
    became 62 and the call returned a component named `定转子装配(1)`.

    Import options stay at their defaults, and that is deliberate: the default
    already imports named selections, which is what every other tool here
    selects by.
    """
    try:
        return {"ok": True, **_session.insert_file(path)}
    except Exception as exc:
        return _err(exc)


@mcp.tool(annotations=STATE_CHANGE)
def scdm_transform(
    operation: str,
    bodies: list[str],
    scale_factor: float | None = None,
    angle_deg: float | None = None,
    axis: str | list[float] | None = None,
    origin: list[float] | None = None,
) -> dict[str, Any]:
    """Rotate, scale or mirror bodies, then verify the geometry actually moved.

    These change the model, so the same rule as the booleans applies: measure the
    result rather than trusting the call. A spatial fingerprint is taken before
    and after.

    `operation` is `rotate`, `scale` or `mirror`. `axis` takes `"x"`, `"y"`,
    `"z"` or a 3-vector; `origin` defaults to the world origin, which is rarely
    the point you want for a part that is not centred there.

    `scale` is independently checkable — volume should change by the cube of the
    factor. Measured: `scale(1.5)` on the stator moved 0.003314505208 m³ to
    0.01118664023 m³, exactly 1.5³. `rotate` about z was verified by the
    fingerprint moving in x and y while z stayed where it was.

    Read `verified` before reporting success. It is false when no fingerprint
    could be read at all, which is not the same as geometry that did not move.
    """
    try:
        return {
            "ok": True,
            **_session.transform(
                operation,
                bodies,
                scale_factor=scale_factor,
                angle_deg=angle_deg,
                axis=axis,
                origin=origin,
            ),
        }
    except Exception as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Inspection (read-only)
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
def scdm_inspect_geometry(
    checks: list[str] | None = None,
    bodies: list[str] | None = None,
    short_edge_length: float | None = None,
    small_face_area: float | None = None,
) -> dict[str, Any]:
    """Diagnose the model: duplicates, short edges, small or missing faces, and more.

    This is the tool to reach for when meshing fails. Run it before blaming the
    mesher. Measured on `zhuangpeiti_fix_9_10_1.scdoc`, where a Fluent Meshing
    run died at Describe Geometry / computing regions with "Found overlapping
    faces", `duplicate_faces` returned two groups -- one face on `stator` and
    one on `pip` in each, with identical areas:

        0.09292831069318609 m2    stator 0:41501  +  pip 0:15387
        0.12176813125314039 m2    stator 0:41504  +  pip 0:15396

    That is the overlapping-face failure, named. The same run also found 675
    edges shorter than 10 mm, 459 of them on `stator`.

    `checks` selects from duplicate_faces, extra_edges, short_edges,
    small_faces, missing_faces, split_edges, stitch_faces, inexact_edges.
    Defaults to all of them except `inexact_edges`, which is reported as
    unreliable: it returned 3348 entries with every `edges` list empty, so its
    count means nothing. Pass it explicitly if you want to see that for yourself.

    `short_edge_length` and `small_face_area` are thresholds the official
    methods do not supply, and their own defaults found nothing. Leave them
    unset and a ladder of thresholds is scanned, with every rung reported, so
    you can see where this model's problems actually start.

    `short_edges` also carries an **attribution**, because a bare count misleads.
    A pipe whose circular section was modelled as a 360-gon reported 1440 short
    edges of 0.349-0.436 mm and `clean: false` -- and those numbers are exactly
    `2*r*sin(pi/360)` for r = 20 mm and 25 mm, the chords of the approximation.
    Nothing was wrong with the model. The check now reports the length statistics
    that separate a repeated chord family (near-uniform lengths) from scattered
    slivers, states the rule it applied, and adds an `assessment` line when the
    only thing keeping `clean` false is tessellation.

    Read-only: the source file's SHA-256 was unchanged after a full run.
    """
    try:
        return {
            "ok": True,
            **_session.inspect_geometry(
                checks=checks,
                bodies=bodies,
                short_edge_length=short_edge_length,
                small_face_area=small_face_area,
            ),
        }
    except Exception as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Mutating operators (guarded)
# ---------------------------------------------------------------------------


@mcp.tool(annotations=STATE_CHANGE)
def scdm_boolean(operation: str, target: str, tools: list[str], keep_tools: bool = False) -> dict[str, Any]:
    """Run unite / subtract / intersect on named bodies, then verify it happened.

    Returns the before/after face counts, volumes and a per-body change list. If
    nothing moved, raises no_geometry_change instead of reporting success.

    Measured on 2024 R2: `unite` genuinely works (stator 382 -> 390 faces,
    volume up by exactly the absorbed body's volume). `subtract` does not: it
    returns normally and deletes the tool body while leaving the target
    untouched. That is a kernel/geometry behaviour reproduced through both the
    official API and the raw IronPython path, so switching API does not fix it.
    """
    try:
        return {"ok": True, **_session.boolean(operation, target, tools, keep_tools=keep_tools)}
    except Exception as exc:
        return _err(exc)


@mcp.tool(annotations=STATE_CHANGE)
def scdm_share_topology(bodies: list[str] | None = None, tolerance: float = 0.0) -> dict[str, Any]:
    """Apply shared topology, verified against the geometry.

    This is the operation whose return value cannot be trusted. On a 31-body
    assembly it returned true while stator faces stayed at 382 and the body
    count stayed at 31. The tool therefore compares face counts and volumes
    before and after and raises unless something moved.

    A `no_geometry_change` result here usually means the assembly is genuinely
    multi-part, and fused topology is the wrong tool - use Non-Conformal + Mesh
    Interface in Fluent Meshing.
    """
    try:
        return {"ok": True, **_session.share_topology(bodies, tolerance=tolerance)}
    except Exception as exc:
        return _err(exc)


@mcp.tool(annotations=STATE_CHANGE)
def scdm_run_script(
    script_path: str | None = None,
    args: dict[str, str] | None = None,
    import_design: bool = True,
    script: str | None = None,
) -> dict[str, Any]:
    """Run a SpaceClaim/IronPython script through the official channel.

    The coverage escape hatch: anything the typed tools do not expose is reachable
    here, and unlike scraping stdout from the raw `/RunScript` path, this call
    returns the script's reported values and raises on failure.

    **Two parameters, and they mean different things.** The old signature had one
    parameter named `script` that actually required a *file path*, so handing it
    script text failed immediately with `script not found: <the whole script>`.
    Now:

    * `script_path` - a path to an IronPython `.py` file on this machine (the file
      is uploaded to the service, so it has to exist locally).
    * `script` - the script **source text**. It is written to a temporary `.py`
      file, run from there, and the temp file is deleted afterwards.

    Pass exactly one. How to read a script from a path:

        read the file yourself, then pass its text as `script` -- or pass the path
        as `script_path` and this tool will read it for you. There is no third
        interpretation: `script_path` is never treated as source, and `script` is
        never treated as a path.

    The script receives `argsDict` on the server side and returns values by
    populating a `result` dictionary, which comes back under `returned`. Three
    traps worth remembering when authoring one: `Body[](n)` array syntax is a
    parse-time error that kills the whole script silently with no output, so use
    `List[Body]()`; printing certain objects raises UnicodeEncodeError mid-script,
    so write ASCII-safe strings to a log file and flush after each line; and
    `str()` on a failed service exception can itself re-raise, so log
    `type(exc).__name__` rather than the exception.

    Because this runs arbitrary code in the user's session, it is the one tool
    here with no read-back guard. Prefer `scdm_create_pipe` / `scdm_extrude` when
    they cover the geometry you need -- they verify what they built.
    """
    try:
        return {
            "ok": True,
            **_session.run_script(
                script_path=script_path, args=args, import_design=import_design, script=script
            ),
        }
    except Exception as exc:
        return _err(exc)


@mcp.tool(annotations=STATE_CHANGE)
def scdm_create_pipe(
    outer_diameter_mm: float,
    length_mm: float,
    inner_diameter_mm: float = 0.0,
    name: str | None = None,
    axis: str | list[float] = "z",
    origin: list[float] | None = None,
    tolerance: float = 1e-3,
    replace_existing: bool = False,
) -> dict[str, Any]:
    """Create a hollow pipe (or a solid rod) from a ring section and an extrude.

    This is a **creation** tool: before it existed, not one of the `scdm_*` tools
    could make geometry, so "model from scratch" meant hand-writing IronPython and
    running it through `scdm_run_script` -- the escape hatch whose parameter
    contract was itself the easiest thing to misread.

    **No boolean is used, on purpose.** The section is a ring built directly
    (outer loop plus inner loop in one profile) and then extruded. Building a tube
    by subtracting a rod from a rod cannot work on 2024 R2: `Body.subtract`
    returns normally, deletes the tool body, and leaves the target untouched.

    Circles are drawn as **two semicircles**: a single `CreateArc` with
    `start == end` raises `Invalid profile.`, while two half-circles produce a
    section whose area matches `pi*(r_out^2 - r_in^2)` to six decimals. Line
    segments also work but tessellate the circle into chord edges, which
    `scdm_inspect_geometry` then reports as thousands of short edges.

    **It verifies what it built.** The section area is read back against the
    analytic annulus, and the extruded volume against `area * length`. That is
    what catches the measured *silent* failure mode: `removeInnerRegions=False`
    does not raise, it just drops the inner loop and hands back a plain disc
    (1962.872 mm2 where the ring is 706.634 mm2), so an unguarded tool would report
    a rod as a tube.

    `inner_diameter_mm=0` makes a solid rod, and the result says `solid: true`.
    `length_mm` is along `axis` from `origin` (world origin by default).
    Re-creating a name that already exists is refused unless
    `replace_existing=True`; nothing in the design is deleted as a side effect.
    """
    try:
        return {
            "ok": True,
            **_session.create_pipe(
                outer_diameter_mm=outer_diameter_mm,
                length_mm=length_mm,
                inner_diameter_mm=inner_diameter_mm,
                name=name,
                axis=axis,
                origin=origin,
                tolerance=tolerance,
                replace_existing=replace_existing,
            ),
        }
    except Exception as exc:
        return _err(exc)


@mcp.tool(annotations=STATE_CHANGE)
def scdm_extrude(
    body: str,
    distance_mm: float,
    direction: str | list[float] | None = None,
    face_indices: list[int] | None = None,
    name: str | None = None,
    extrude_type: str = "ForceIndependent",
    expected_volume_mm3: float | None = None,
    tolerance: float = 5e-2,
) -> dict[str, Any]:
    """Extrude faces of a body that is already in the design.

    The general-purpose companion to `scdm_create_pipe`: it starts from geometry
    that already exists (an imported region, a planar face of a STEP import, a
    section a script left behind) instead of building a section from parameters.

    **Faces are selected by 0-based `face_indices`, and `direction` is required.
    Both are measured limitations, not taste.** On 2024 R2 a face cannot be
    identified or typed from the raw API: `DesignFace` has no
    `Id`/`IsPlanar`/`SurfaceType`, `Face.Geometry` raises `AttributeError`,
    `EvalMid`/`EvalProportion`/`GetFaceNormal` all raise `TypeError`, and
    `GetFaceNormal` wants a `PointUV` that cannot be built from a `BoundingBox`.
    Only `Area` and `Perimeter` read cleanly. So there is no "every planar face"
    default -- it would need an API that does not work here, and silently pulling a
    cylinder wall is worse than a refusal. Omit `face_indices` and the refusal
    lists every face with its area and perimeter so you can choose from evidence.

    `direction` defaults to nothing for the same reason: the face normal cannot be
    read, and guessing an axis would extrude along the wrong one while reporting
    success.

    **Why raw IronPython:** `GeometryCommands.extrude_faces` carries a 25R2 version
    gate and raises on this machine's 24R2 before it reaches the service. The raw
    kernel call `ExtrudeFaces.Execute(Selection.Create(faces), direction,
    MM(distance), ForceIndependent, None)` was measured working on 24R2 against a
    face of a body that was already in the document: a 12 mm disc extruded 40 mm
    gave 18095.573685 mm3 against an analytic 18095.57515 mm3.

    If the kernel creates nothing, this reports `no_geometry_change` rather than an
    empty success -- the same rule the booleans follow. When `expected_volume_mm3`
    is given it is checked; otherwise the face area times the distance is reported
    as a cross-check that is deliberately **not** enforced, because an arbitrary
    face has no exact analytic extruded volume.

    `extrude_type` is one of `ForceIndependent` (default; creates a new body and
    leaves the source alone), `Add`, `ForceAdd` or `None` (all three fuse the pull
    into the source body). `source_effect` reports the source's volume before and
    after either way, so "did this modify my part?" is answered by a number.

    **`CreatedBodies` is not evidence here.** Measured on 2024 R2 with a 5 mm pull
    of a 235.6194 mm2 annulus: `ExtrudeFaces.Execute` returned **zero** created
    bodies for all four extrude types, while `ForceIndependent` really did create a
    separate body (+1178.066 mm3) and `Add` really did fuse into the source
    (+2748.988 mm3). This tool therefore finds what was created by diffing the
    document, not by reading that return value.
    """
    try:
        return {
            "ok": True,
            **_session.extrude(
                body=body,
                distance_mm=distance_mm,
                direction=direction,
                face_indices=face_indices,
                name=name,
                extrude_type=extrude_type,
                expected_volume_mm3=expected_volume_mm3,
                tolerance=tolerance,
            ),
        }
    except Exception as exc:
        return _err(exc)


@mcp.tool(annotations=STATE_CHANGE)
def scdm_export(
    format: str,
    location: str | None = None,
    output_dir: str | None = None,
    file_stem: str | None = None,
) -> dict[str, Any]:
    """Export the open design, with a file name the caller controls.

    Formats: scdocx, step, iges, parasolid_text, parasolid_bin, pmdb.

    **Two ways to say where the file goes, because the one-parameter version was
    ambiguous.** `location` is treated as a *directory*, and that is what made the
    old behaviour surprising: handing over `...\\pipe_round.step` produced
    `...\\pipe_round.step\\设计(D)1.stp`, so the file name was uncontrollable (it
    followed the document's name), the extension silently became `.stp`, and the
    returned path was the directory rather than the file.

    * `output_dir` + `file_stem` - `file_stem` names the file. When the service
      insists on its own name or extension the file is **renamed to match**, so
      the returned `path` is the file that exists on disk.
    * `location` - the export directory, kept for compatibility.

    `path` in the result is always the **absolute path of the file on disk**, and
    `expected_suffix` reports the extension this format really uses on this
    release (for example `step` writes `.stp`). There is no `.scdoc` export; the
    native output is `.scdocx`.
    """
    try:
        return {
            "ok": True,
            **_session.export(
                format, location=location, output_dir=output_dir, file_stem=file_stem
            ),
        }
    except Exception as exc:
        return _err(exc)


def main() -> None:
    transport = os.getenv("ANSYS_BRIDGE_TRANSPORT", "stdio")
    if transport not in {"stdio", "sse", "streamable-http"}:
        raise ValueError("ANSYS_BRIDGE_TRANSPORT must be stdio, sse, or streamable-http")
    # Import the heavy clients NOW, on the main thread, before any request can
    # arrive. Lazy-importing `ansys.geometry.core` from inside a tool call was
    # measured wedging the process inside numpy's C extension `create_module`
    # with no exception and no timeout. Set ANSYS_BRIDGE_PRELOAD=0 to skip it;
    # startup is then fast, and the first SpaceClaim call pays that import.
    if os.getenv("ANSYS_BRIDGE_PRELOAD", "1") not in {"0", "false", "no"}:
        status = preload()
        _stderr("preload: " + ", ".join(f"{k}={v}" for k, v in status.items()))
    mcp.run(transport=transport)


atexit.register(_session.shutdown)

if __name__ == "__main__":
    main()
