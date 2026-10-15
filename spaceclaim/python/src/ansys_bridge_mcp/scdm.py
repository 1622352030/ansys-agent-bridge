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

import math
import os
import sys
import tempfile
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

__all__ = [
    "Session",
    "SessionError",
    "SCDOC_SUFFIXES",
    "preload",
    "attach_guidance",
    "check_attach_args",
    "DEFAULT_ATTACH_HOST",
    "DEFAULT_ATTACH_PORT",
    "DEFAULT_ATTACH_TRANSPORT",
    "VALID_TRANSPORT_MODES",
    "CREATE_DESIGN_NAME",
    "EXPORT_SUFFIXES",
    "KERNEL_LIMITATIONS",
    "acis_guidance",
    "export_target",
    "classify_short_edges",
    "resolve_script",
    "ring_areas_mm2",
]


SCDOC_SUFFIXES = (".scdoc", ".scdocx", ".dsco")
_EXPORT_METHODS = {
    "scdocx": "export_to_scdocx",
    "step": "export_to_step",
    "iges": "export_to_iges",
    "parasolid_text": "export_to_parasolid_text",
    "parasolid_bin": "export_to_parasolid_bin",
    "pmdb": "export_to_pmdb",
}

# The suffix each ``Design.export_to_*`` actually writes, which is NOT always the
# format name. Measured 2026-10-15 on 2024 R2: ``format="step"`` produced a file
# ending ``.stp``, not ``.step``. A caller who hands over a full path therefore
# has to be told the extension changed rather than discovering it later.
EXPORT_SUFFIXES = {
    "scdocx": ".scdocx",
    "step": ".stp",
    "iges": ".igs",
    "parasolid_text": ".x_t",
    "parasolid_bin": ".x_b",
    "pmdb": ".pmdb",
}

# The `ExtrudeType` values `scdm_extrude` accepts, and what each one measurably
# does on 2024 R2. `ForceIndependent` creates a NEW body and leaves the source
# alone; the other three fuse the pull into the source body, which is why the tool
# defaults to the independent one and reports the source's volume change either way.
_EXTRUDE_TYPES = ("ForceIndependent", "Add", "ForceAdd", "None")

# Name used when a session has NO document open and a creation tool has to
# bootstrap one. Not a guess about the user's intent: it is the name this server
# gives a document *it* brought into being, so the user can recognise it.
CREATE_DESIGN_NAME = "AI_CREATED_DESIGN"

# ---------------------------------------------------------------------------
# Kernel limitations, and the actionable text for each
# ---------------------------------------------------------------------------
#
# Two of these were measured by the 2026-10-15 pipe-modelling run, and neither is
# a parameter mistake -- retrying with different arguments cannot fix them:
#
# * ``Body.CreatePlanarBody(plane, outerLoop: ICollection[PointUV],
#   innerLoops)`` raises ``AcisModeler::CreatePlanarBody Not implemented for
#   ACIS.``  The overload exists in the API surface and has no implementation on
#   the ACIS kernel this SpaceClaim uses.
# * ``removeInnerRegions=False`` does NOT fail. It returns a plain outer disc:
#   measured 1963.495 mm2 (pi*25^2) where the ring is 706.858 mm2. A caller who
#   trusts the return value builds a solid rod and believes it built a tube.
#
# Correction to the number, and it is worth recording because the first version of
# this comment repeated it: the modelling report quoted "1962.872 mm2 = pi*25^2"
# for the disc and "706.634 mm2" for the ring. pi*25^2 is **1963.495**, and
# pi*(25^2-20^2) is **706.858** -- the first two digits of 706.858 had been
# transposed in the report, and the disc figure was wrong by ~0.6 mm2. The PARAMETER
# behaviour the report documented is not in doubt (removeInnerRegions=False really
# does drop the inner loop and really does raise nothing); only those two printed
# values were. The comparison inside the guard uses the analytic values, so it
# catches the silent case with either wording.
#
# A raw kernel message is useless to a caller, so each limitation carries the
# route that does work.
KERNEL_LIMITATIONS = {
    "pointuv_overload": (
        "Body.CreatePlanarBody(plane, outerLoop: ICollection[PointUV], innerLoops) "
        "is NOT implemented on the ACIS kernel: calling it raises "
        "'AcisModeler::CreatePlanarBody Not implemented for ACIS.' This is a kernel "
        "limitation, not a parameter error, so no argument change will fix it. Use the "
        "curve-based overload instead: build the outer and inner loops from "
        "CurveSegment.Create / CurveSegment.CreateArc, put every curve into ONE "
        "ICollection[ITrimmedCurve] (outer loop first, then the inner loops), and call "
        "Body.CreatePlanarBody(plane, profile, allowNonManifold, mergeTopology, "
        "removeInnerRegions=True). scdm_create_pipe does exactly this."
    ),
    "remove_inner_regions_false": (
        "Body.CreatePlanarBody(..., removeInnerRegions=False) does NOT fail and does NOT "
        "keep the inner loop: measured on 2024 R2 it returns a plain outer disc "
        "(1963.495 mm2 = pi*25^2) where the ring is 706.858 mm2 = pi*(25^2-20^2). The "
        "inner loop is dropped silently, so a caller who trusts the call builds a solid "
        "rod and reports a tube. There is no flag that makes it keep the inner loop; pass "
        "removeInnerRegions=True. Every creation tool in this server reads the section "
        "area back and refuses the result when it is not the expected annulus."
    ),
}

_ACIS_MARKERS = (
    "not implemented for acis",
    "acismodeler",
    "kernel",
)
_INVALID_PROFILE_MARKERS = (
    "invalid profile",
    "not a valid profile",
)


def acis_guidance(exc: Exception) -> str | None:
    """The actionable text for a kernel refusal, or ``None`` if this is not one.

    Returning ``None`` matters: an exception that merely contains the word
    "kernel" in a body name must not be re-labelled as a kernel limitation, or the
    real error gets hidden behind advice that does not apply.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    if "createplanarbody" in text and any(m in text for m in _ACIS_MARKERS):
        return KERNEL_LIMITATIONS["pointuv_overload"]
    if any(m in text for m in _INVALID_PROFILE_MARKERS):
        return (
            "The kernel rejected the assembled curves as a profile. Measured on 2024 R2, "
            "the commonest cause is a FULL circle built as ONE arc: "
            "CurveSegment.CreateArc(center, start, end, axis) with start == end raises "
            "'Invalid profile.'. Split the circle into two semicircles -- take two "
            "diametrically opposite points and create one arc from A to B and a second "
            "from B to A. scdm_create_pipe builds every circle that way."
        )
    return None


# ---------------------------------------------------------------------------
# Pure helpers (no ANSYS needed; unit-tested offline)
# ---------------------------------------------------------------------------


def ring_areas_mm2(outer_diameter_mm: float, inner_diameter_mm: float | None = None) -> dict:
    """Section area, wall thickness and expected volume factors for a pipe.

    Returns ``{"annulus_mm2", "outer_disc_mm2", "inner_disc_mm2",
    "wall_thickness_mm"}``. The **outer disc** is returned alongside the annulus
    on purpose: it is the value a silently-dropped inner region produces, so the
    guard needs it to name what actually happened rather than only that the number
    was wrong.
    """
    if not isinstance(outer_diameter_mm, (int, float)) or isinstance(outer_diameter_mm, bool):
        raise SessionError(
            f"outer_diameter_mm must be a number, got {outer_diameter_mm!r}",
            code="invalid_parameter",
        )
    if outer_diameter_mm <= 0:
        raise SessionError(
            f"outer_diameter_mm must be positive, got {outer_diameter_mm!r}",
            code="invalid_parameter",
        )
    if inner_diameter_mm is None:
        inner_diameter_mm = 0.0
    if not isinstance(inner_diameter_mm, (int, float)) or isinstance(inner_diameter_mm, bool):
        raise SessionError(
            f"inner_diameter_mm must be a number, got {inner_diameter_mm!r}",
            code="invalid_parameter",
        )
    if inner_diameter_mm < 0:
        raise SessionError(
            f"inner_diameter_mm must be >= 0, got {inner_diameter_mm!r}",
            code="invalid_parameter",
        )
    if inner_diameter_mm >= outer_diameter_mm:
        raise SessionError(
            f"inner_diameter_mm ({inner_diameter_mm}) must be smaller than "
            f"outer_diameter_mm ({outer_diameter_mm}); an equal or larger bore leaves no wall",
            code="invalid_parameter",
        )
    r_out = float(outer_diameter_mm) / 2.0
    r_in = float(inner_diameter_mm) / 2.0
    outer_disc = math.pi * r_out**2
    inner_disc = math.pi * r_in**2
    return {
        "annulus_mm2": outer_disc - inner_disc,
        "outer_disc_mm2": outer_disc,
        "inner_disc_mm2": inner_disc,
        "wall_thickness_mm": r_out - r_in,
    }


def resolve_script(
    script_path: str | None = None,
    script: str | None = None,
) -> tuple[Path, bool, str]:
    """Decide what ``scdm_run_script`` was actually handed.

    This function exists because the parameter contract was the worst trap in the
    tool set: the signature said ``script: str`` and read like "the script", while
    the implementation treated it as a **file path**, so the obvious usage failed
    on the very first call with ``script not found: <the whole script text>``.

    The rule is now explicit and total:

    * ``script_path`` -- the name says what it is. Nothing is inferred.
    * ``script`` -- the script **source text**. It is written to a temporary
      ``.py`` file (deleted after the call) and that file is what runs.

    Both may not be given: guessing which one the caller meant is exactly the
    ambiguity that produced the bug. Returns ``(path, is_temporary, source)``
    where ``source`` is ``"script_path"`` or ``"script_text"``.
    """
    if script_path is not None and script is not None:
        raise SessionError(
            "pass script_path (a file) or script (source text), not both. The two are "
            "different things and the old signature -- which named the file-path argument "
            "'script' -- is what made this ambiguous.",
            code="ambiguous_script_argument",
        )
    if script_path is not None:
        if not isinstance(script_path, str) or not script_path.strip():
            raise SessionError(
                f"script_path must be a non-empty string, got {script_path!r}",
                code="invalid_parameter",
            )
        path = Path(script_path)
        if not path.exists():
            raise SessionError(
                f"script_path not found: {path}\n"
                "This parameter is a PATH to an IronPython file. To pass script source text "
                "instead, use the `script` parameter (it is written to a temporary .py file "
                "and run from there).",
                code="script_not_found",
            )
        if path.is_dir():
            raise SessionError(
                f"script_path is a directory, not a file: {path}",
                code="invalid_parameter",
            )
        return path, False, "script_path"

    if script is not None:
        if not isinstance(script, str) or not script.strip():
            raise SessionError(
                f"script must be non-empty source text, got {script!r}",
                code="invalid_parameter",
            )
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", prefix="ansys_bridge_scdm_", delete=False, encoding="utf-8"
        )
        try:
            handle.write(script)
        finally:
            handle.close()
        return Path(handle.name), True, "script_text"

    raise SessionError(
        "pass script_path=<file> or script=<source text>; neither was given.",
        code="missing_script",
    )


# Short edges that are the CHORDS of a curve approximation, not geometry defects.
#
# The measurement that motivates this: a pipe whose circular section was drawn as
# a 360-gon reported 1440 short edges, 0.349-0.436 mm, with clean=false. Those are
# exactly 2*20*sin(pi/360) and 2*25*sin(pi/360) -- the inner and outer chord
# lengths, 720 per end face. Nothing was wrong with the model; it was round in a
# faceted way. A tool that reports the count and no attribution invites the caller
# to "fix" a defect that does not exist.
#
# The discriminating measurement is *how tightly the lengths cluster*. A
# tessellated circle produces chords of one repeated length (two, for a ring);
# genuine sliver geometry produces a scattered spread. The rules are declared here
# and echoed in the result, so the classification can be argued with.
POLYGON_CHORD_MIN_COUNT = 8
POLYGON_CHORD_MAX_SPAN_RATIO = 2.0
# Why 0.20 and not something rounder. A ring tessellated with one facet count
# produces TWO chord lengths (inner and outer), and their merged coefficient of
# variation is a fixed number, not zero: for the measured 360-gon over 20 mm and
# 25 mm radii the chords are 0.3491 and 0.4363, giving CV = 0.1110. A threshold
# below that would refuse the very case this exists for. 0.20 sits just above it
# and well below the CV of a scattered sliver population (the measured reference
# assembly's own short edges give CV ~ 0.9), so it separates the two without
# needing a model-specific constant.
POLYGON_CHORD_MAX_CV = 0.20


def classify_short_edges(
    lengths_mm: list[float], curve_radius_mm: float | None = None
) -> dict:
    """Attribute a set of short edges to tessellation chords, or decline to.

    Returns a dict carrying the **declared rule**, the measurements it was applied
    to, and one of three verdicts:

    ``polygonal_chords``
        Uniform-length short edges -- the signature of a curved surface
        approximated by flat facets. Expected, not a defect.
    ``mixed_variation``
        The lengths scatter too widely to be one chord family. Treat as a real
        short-edge finding.
    ``undetermined``
        Too few measurable lengths to decide. Returned instead of guessing.

    ``curve_radius_mm`` is only needed for the facet count. ``chord = 2*r*sin(pi/n)``
    has **two** unknowns in it, and the edge lengths supply only one, so no amount
    of arithmetic on them recovers ``n`` -- the radius has to come from someone who
    knows it. Without it the chord lengths and their statistics are reported and
    ``facet_count_estimate`` is ``None``, which is the honest answer rather than a
    number produced by quietly guessing a radius.
    """
    values = sorted(float(v) for v in lengths_mm if isinstance(v, (int, float)))
    n = len(values)
    rule = {
        "min_count": POLYGON_CHORD_MIN_COUNT,
        "max_span_ratio": POLYGON_CHORD_MAX_SPAN_RATIO,
        "max_cv": POLYGON_CHORD_MAX_CV,
    }
    if n < POLYGON_CHORD_MIN_COUNT:
        return {
            "verdict": "undetermined",
            "rule": rule,
            "measured": n,
            "why": (
                f"only {n} measurable lengths (need {POLYGON_CHORD_MIN_COUNT}); too few to "
                "tell a repeated chord family from scattered slivers"
            ),
        }

    mean = sum(values) / n
    span_ratio = values[-1] / values[0] if values[0] > 0 else None
    if mean > 0:
        variance = sum((v - mean) ** 2 for v in values) / n
        cv = math.sqrt(variance) / mean
    else:
        cv = None

    uniform = (span_ratio is not None and span_ratio <= POLYGON_CHORD_MAX_SPAN_RATIO) and (
        cv is not None and cv <= POLYGON_CHORD_MAX_CV
    )
    result: dict = {
        "verdict": "polygonal_chords" if uniform else "mixed_variation",
        "rule": rule,
        "measured": n,
        "length_min_mm": values[0],
        "length_max_mm": values[-1],
        "length_mean_mm": mean,
        "span_ratio": span_ratio,
        "coefficient_of_variation": cv,
    }

    facet_estimates: list[int] = []
    if curve_radius_mm and curve_radius_mm > 0:
        for chord in (values[0], values[-1]):
            ratio = chord / (2.0 * curve_radius_mm)
            if 0 < ratio < 1:
                facet_estimates.append(int(round(math.pi / math.asin(ratio))))
    result["facet_count_estimate"] = sorted(set(facet_estimates)) if facet_estimates else None
    result["curve_radius_mm"] = curve_radius_mm

    if uniform:
        result["why"] = (
            "the short edges have near-uniform length "
            f"(max/min = {span_ratio:.3f}, CV = {cv:.4f}), which is the signature of a "
            "closed uniform edge chain -- the chords of a circle approximated by a regular "
            "polygon, expected whenever a curve is tessellated."
        )
        result["chord_formula"] = "chord = 2*r*sin(pi/n)"
        if facet_estimates:
            result["facet_count_note"] = (
                "computed from the supplied curve radius; a ring has two chord families "
                "(inner and outer radius) so the set holds both candidates"
            )
        else:
            result["facet_count_note"] = (
                "not computed: `chord = 2*r*sin(pi/n)` needs the curve's radius, which the "
                "edge lengths alone cannot supply. Pass curve_radius_mm to get n."
            )
        result["to_remove_this_class"] = (
            "model the circle with real arcs (two semicircles per circle) instead of line "
            "segments. Measured: that removed all 1440 such edges from this exact pipe, "
            "taking short_edges from 1440 to 0."
        )
    else:
        result["why"] = (
            "the short edges are NOT uniform "
            f"(max/min = {span_ratio if span_ratio is None else round(span_ratio, 3)}, "
            f"CV = {cv if cv is None else round(cv, 4)}), so this is not one tessellation "
            "chord family. Treat it as a real short-edge finding."
        )
    return result

# -- attach (the "you drive" mode) ----------------------------------------
#
# These are CONVENTIONS, not discovery. Fluent has an official `-sifile=` that
# makes the server write out an ``ip:port`` file, so the client can find it.
# SpaceClaim has no equivalent: PyAnsys picks a random free port when API_PORT is
# unset (`product_instance.py:600-612`, `:775-789`), and nothing announces it.
# So the port is fixed by agreement -- the user's launcher sets ``API_PORT=51790``
# and this server defaults to the same number.
DEFAULT_ATTACH_HOST = "localhost"
DEFAULT_ATTACH_PORT = 51790
DEFAULT_ATTACH_TRANSPORT = "wnua"

# The full set accepted by the installed client's channel factory
# (`ansys/tools/common/cyberchannel.py:158-172`, `verify_transport_mode` :513-537).
# Only "wnua" was measured working against a user-started SpaceClaim on this
# machine; the other three are accepted because the library accepts them, and are
# marked unverified in docs/verification.md rather than presented as options.
VALID_TRANSPORT_MODES = ("wnua", "insecure", "mtls", "uds")

# The two halves of the SEMI-PUBLIC user-side launch. Neither appears in the
# public Ansys command-line documentation; both are in the PyAnsys Geometry
# source (`product_instance.py:92` for API_PORT, `:113-117` and `:401-404` for the
# add-in manifest) and in measurement (2026-10-15). A future release may change
# them, which is why the guidance below states the exact strings rather than
# "start it with the API server enabled".
SPACECLAIM_ADDIN_ARG = "/ADDINMANIFESTFILE="
SPACECLAIM_TRANSPORT_ARG = "/transport-mode="

# The add-in path PyAnsys itself passes, spelled with the release as a variable so
# the guidance is still correct on another machine.
_ADDIN_MANIFEST = (
    r"{ansys_root}\Addins\ApiServer\Presentation.ApiServerAddIn.Manifest.xml"
)


def _addin_manifest_for(version: str | None = None) -> str:
    """The `Presentation.ApiServerAddIn.Manifest.xml` this machine would use.

    PyAnsys builds this from the detected release
    (``product_instance.py:401-404``). We resolve it the same way, and fall back
    to the newest installed release, so the command line printed in an error
    message is one the user can actually paste.
    """
    roots = detect_ansys_roots()
    releases: dict = roots.get("releases") or {}
    chosen = None
    if version is not None:
        key = str(version)
        if key in releases:
            chosen = key
        else:
            for release in releases:
                if str(release).startswith(key) or key.startswith(str(release)):
                    chosen = release
                    break
    if chosen is None:
        chosen = roots.get("newest")
    root = releases.get(chosen) if chosen else None
    if not root:
        root = rf"C:\Program Files\ANSYS Inc\v{chosen or '242'}"
    return _ADDIN_MANIFEST.format(ansys_root=root)


def _spaceclaim_exe_for(version: str | None = None) -> str:
    """The `SpaceClaim.exe` this machine would launch, for the printed command line."""
    roots = detect_ansys_roots()
    exes: dict = roots.get("spaceclaim") or {}
    if version is not None:
        key = str(version)
        for release, path in exes.items():
            if str(release).startswith(key) or key.startswith(str(release)):
                return path
    newest = roots.get("newest")
    if newest and exes.get(newest):
        return exes[newest]
    for path in exes.values():
        return path
    return r"C:\Program Files\ANSYS Inc\v242\scdm\SpaceClaim.exe"



def attach_guidance(
    host: str, port: int, transport_mode: str, exc: Exception | None = None
) -> str:
    """The actionable text for an attach that did not connect.

    This is the single most useful thing this server can say about attach, so it
    is a function rather than an f-string buried in an except block: the offline
    tests assert that it names the real cause and carries a runnable command.

    Measured basis (2026-10-15, this machine): a SpaceClaim started by a plain
    double-click listened on 49013/49014/63186 and **all three** answered
    ``StatusCode.UNAVAILABLE`` to a gRPC health check -- there is no endpoint to
    attach to at all. The official documentation says the same thing in prose:
    "Running a SpaceClaim or Discovery normal session does not suffice to be able
    to use it with PyAnsys Geometry. Both products need the ApiServer extension to
    be running." The same session started WITH the add-in and a fixed port
    accepted ``Modeler(host, port, transport_mode="wnua")`` in **0.31 s**.
    """
    manifest = _addin_manifest_for()
    exe = _spaceclaim_exe_for()

    reason = f" ({type(exc).__name__}: {exc})" if exc is not None else ""
    return (
        f"could not attach to SpaceClaim at {host}:{port} "
        f"(transport_mode={transport_mode!r}){reason}\n"
        "\n"
        "A SpaceClaim started by a plain double-click has NO gRPC endpoint, so there is "
        "nothing to attach to. Measured on this machine: its three listening ports "
        "(49013/49014/63186) all answered UNAVAILABLE to a gRPC health check. The instance "
        "must be started with the ApiServer add-in and a fixed port:\n"
        "\n"
        f"    set API_PORT={port}\n"
        f'    start "" "{exe}" ^\n'
        f'      {SPACECLAIM_ADDIN_ARG}"{manifest}" ^\n'
        f"      {SPACECLAIM_TRANSPORT_ARG}{transport_mode}\n"
        "\n"
        "Then call scdm_attach again. The launcher shipped with this project "
        "(spaceclaim/dev/launchers/start_spaceclaim_for_ai.bat, installed as the desktop "
        "shortcut \"SpaceClaim 2024R2 (AI可接入).lnk\") does exactly this: one double-click "
        "and the session becomes attachable.\n"
        "\n"
        f"Both {SPACECLAIM_ADDIN_ARG!r} and API_PORT are a SEMI-PUBLIC interface: they are in "
        "the PyAnsys Geometry source (product_instance.py:92, 113-117, 401-404) and in "
        "measurement, but NOT in the public Ansys command-line documentation, so a future "
        "release may change them.\n"
        "\n"
        f"Also check: the port is a convention, not a discovery mechanism -- if the user's "
        f"launcher set a different API_PORT, pass that value here (got {port}). Only one "
        "instance can own a given port."
    )


def _coerce_port(port: Any) -> int:
    """Validate a port before any network call is made.

    Validation happens here, ahead of ``Modeler(...)``, on purpose: a bad port
    should fail as a *parameter* error, not as a connection error whose message
    sends the user off to check whether SpaceClaim is running.
    """
    if isinstance(port, bool):  # bool is an int subclass; True is not port 1
        raise SessionError(f"port must be an integer 1-65535, got {port!r}", code="invalid_port")
    if isinstance(port, int):
        value = port
    elif isinstance(port, str) and port.strip().lstrip("+").isdigit():
        value = int(port.strip())
    else:
        raise SessionError(f"port must be an integer 1-65535, got {port!r}", code="invalid_port")
    if not 1 <= value <= 65535:
        raise SessionError(
            f"port must be between 1 and 65535, got {value}", code="invalid_port"
        )
    return value


def check_attach_args(host: Any, port: Any, transport_mode: Any) -> tuple[str, int, str]:
    """Normalise and validate the three attach parameters. No side effects."""
    if not isinstance(host, str) or not host.strip():
        raise SessionError(
            f"host must be a non-empty string, got {host!r}", code="invalid_host"
        )
    mode = transport_mode
    if not isinstance(mode, str) or mode.strip().lower() not in VALID_TRANSPORT_MODES:
        raise SessionError(
            f"transport_mode must be one of {list(VALID_TRANSPORT_MODES)}, got {transport_mode!r}. "
            "The installed client accepts exactly these four "
            "(ansys/tools/common/cyberchannel.py:158-172); on Windows against a "
            "user-started SpaceClaim only 'wnua' has been measured working here.",
            code="invalid_transport_mode",
        )
    return host.strip(), _coerce_port(port), mode.strip().lower()


def _has_product_instance(client: Any) -> bool | None:
    """Whether this client owns the backend process, or ``None`` if undecidable.

    This one flag is the library-level reason attach is safe: ``GrpcClient.close()``
    shuts the backend down only inside ``elif self._product_instance:``
    (``connection/client.py:493-499``), so a client built by ``Modeler(host, port)``
    -- which leaves ``product_instance=None`` -- closes the channel and nothing
    else. Reported instead of assumed, and ``None`` means "this build does not
    expose it", never ``False``.
    """
    try:
        return getattr(client, "_product_instance", None) is not None
    except Exception:  # noqa: BLE001 - an unreadable flag is "unknown", not "no"
        return None


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
    """Raised for session lifecycle problems, with the remedy in the message.

    ``code`` is the **machine-readable** error name a tool result reports, so a
    caller never has to pattern-match the prose. It mirrors the Fluent side of
    this bridge, where a refused call answers ``error="attach_mode_refuses_exit"``.
    ``extra`` carries the facts a guard is obliged to state next to the refusal --
    ``process_untouched``, ``licence_released``, ``next_step`` -- because "I did
    not do it" is only useful together with "and here is what that means".

    Both are optional; every pre-existing ``raise SessionError("...")`` keeps
    working and reports ``error="session"``.
    """

    def __init__(self, message: str, code: str | None = None, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = dict(extra)


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


# -- creation helpers (pure: importable and testable without ANSYS) ----------


def _unit_direction_vector(axis: str | list[float]) -> tuple[float, float, float]:
    """Normalise ``axis`` to a plain 3-tuple, without importing PyAnsys.

    The creation path builds its geometry from generated IronPython, so it needs a
    tuple it can interpolate into source text -- not an ``ansys.geometry``
    ``UnitVector3D``, which does not exist in the SpaceClaim IronPython session.
    """
    if isinstance(axis, str):
        table = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}
        key = axis.strip().lower()
        if key not in table:
            raise SessionError(
                f"axis must be x, y, z or a 3-vector, got {axis!r}", code="invalid_parameter"
            )
        return table[key]
    if not isinstance(axis, (list, tuple)) or len(axis) != 3:
        raise SessionError(
            f"axis must be x, y, z or a 3-vector, got {axis!r}", code="invalid_parameter"
        )
    values = []
    for component in axis:
        if not isinstance(component, (int, float)) or isinstance(component, bool):
            raise SessionError(
                f"axis vector components must be numbers, got {axis!r}", code="invalid_parameter"
            )
        values.append(float(component))
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0.0:
        raise SessionError(
            f"axis must not be the zero vector, got {axis!r}", code="invalid_parameter"
        )
    return tuple(v / norm for v in values)


def _pipe_name(outer_diameter_mm: float, inner_diameter_mm: float, length_mm: float) -> str:
    """A name that states the geometry, so a body in the tree is self-describing."""

    def tidy(value: float) -> str:
        return f"{value:g}".replace(".", "p")

    if inner_diameter_mm and inner_diameter_mm > 0:
        return (
            f"PIPE_OD{tidy(outer_diameter_mm)}_ID{tidy(inner_diameter_mm)}"
            f"_L{tidy(length_mm)}"
        )
    return f"ROD_D{tidy(outer_diameter_mm)}_L{tidy(length_mm)}"


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    """An int if the script's string form parses, else ``None``.

    Not ``0`` on failure: a face count that could not be read is unknown, and a
    fabricated zero is exactly the kind of plausible-looking wrong number these
    tools exist to avoid.
    """
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _relative_error(measured: float | None, expected: float) -> float | None:
    if measured is None or expected == 0:
        return None
    return abs(measured - expected) / abs(expected)


def _within(measured: float | None, expected: float, tolerance: float) -> bool:
    """Whether ``measured`` is within ``tolerance`` relative error of ``expected``.

    ``measured is None`` is **false**, not true: a value that could not be read has
    not passed anything. The same rule the geometry guards use.
    """
    if measured is None:
        return False
    if expected == 0:
        return abs(measured) <= tolerance
    return abs(measured - expected) <= abs(tolerance * expected)


# The IronPython preamble every generated creation script gets.
#
# Two measured traps are encoded here rather than left to the caller:
#
# * **`MM()` is mandatory.** The internal length unit is metres and a bare number
#   is one metre, so ``25`` where ``MM(25)`` belongs is a 1000x error that produces
#   perfectly plausible-looking geometry.
# * **The API root is discovered, not hard-coded.** ``{scdm_root}`` is filled from
#   :func:`detect_ansys_roots`, the same discovery the launch path uses, so this
#   works on a machine whose newest release is not 242.
_PIPE_SCRIPT = """
NAME = {name!r}
R_OUT_MM = {r_out_mm!r}
R_IN_MM = {r_in_mm!r}
LENGTH_MM = {length_mm!r}
DIR = Direction.Create({dx!r}, {dy!r}, {dz!r})
BASE = Point.Create({bx!r}, {by!r}, {bz!r})
SIDE = {side!r}
EXPECTED_AREA_MM2 = {expected_area!r}
EXPECTED_VOLUME_MM3 = {expected_volume!r}
TOL = {tolerance!r}
REPLACE = {replace!r}


# `Plane.Create(...)` is NOT usable: measured on 2024 R2, every overload tried
# (point + two directions, 4-argument, Frame-based) raises a bare TypeError from the
# interop binder, and so do `Matrix.CreateTranslation`, `Matrix(…)` and assigning
# `Matrix.Translation`. The three STATIC planes work, and they are what the measured
# probe used (`Plane.PlaneXY`). So no section is ever transformed: each circle is
# built directly at its final world centre, with an in-plane construction direction.
PLANE_BY_SIDE = {{'xy': Plane.PlaneXY, 'yz': Plane.PlaneYZ, 'zx': Plane.PlaneZX}}
#   side : (in-plane unit direction the semicircle endpoints are taken along, normal)
PLANE_AXES = {{
    'xy': ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    'yz': ((0.0, 1.0, 0.0), (1.0, 0.0, 0.0)),
    'zx': ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
}}
ALONG, NORMAL = PLANE_AXES[SIDE]
# Real Direction objects, NOT tuples: measured, `CurveSegment.CreateArc(centre, a, b,
# (0,0,1))` raises a bare TypeError from the interop binder, while
# `Direction.Create(0.0, 0.0, 1.0)` and `Direction.DirZ` both work.
ALONG = Direction.Create(ALONG[0], ALONG[1], ALONG[2])
NORMAL = Direction.Create(NORMAL[0], NORMAL[1], NORMAL[2])
CENTRE = Point.Create(BASE.X, BASE.Y, BASE.Z)


def _circle(r_mm, along, normal):
    # TWO semicircles. Measured: a single CreateArc with start == end is rejected
    # by the kernel with 'Invalid profile.'
    a = Point.Create(CENTRE.X + along.X * MM(r_mm),
                     CENTRE.Y + along.Y * MM(r_mm),
                     CENTRE.Z + along.Z * MM(r_mm))
    b = Point.Create(CENTRE.X - along.X * MM(r_mm),
                     CENTRE.Y - along.Y * MM(r_mm),
                     CENTRE.Z - along.Z * MM(r_mm))
    return [CurveSegment.CreateArc(CENTRE, a, b, normal),
            CurveSegment.CreateArc(CENTRE, b, a, normal)]


def _find_body(name):
    for b in root.Bodies:
        try:
            if str(b.GetName()) == name:
                return b
        except Exception:
            continue
    return None


def _section():
    profile = List[ITrimmedCurve]()
    for a in _circle(R_OUT_MM, ALONG, NORMAL):
        profile.Add(a)
    if R_IN_MM > 0.0:
        for a in _circle(R_IN_MM, ALONG, NORMAL):
            profile.Add(a)
    raw = Body.CreatePlanarBody(PLANE_BY_SIDE[SIDE], profile, False, False, True)
    return _add(raw, NAME + '_SECTION')


def _check_section(section):
    area = section.Shape.SurfaceArea * 1e6
    if abs(area - EXPECTED_AREA_MM2) <= TOL * EXPECTED_AREA_MM2:
        return {{'area_mm2': area, 'faces': section.Shape.Faces.Count}}
    # The measured SILENT failure: the kernel drops an inner loop and returns the
    # outer disc without raising anything, so a caller that trusts the call builds
    # a rod and reports a tube.
    outer_disc = math.pi * (R_OUT_MM ** 2)
    if R_IN_MM > 0.0 and abs(area - outer_disc) <= TOL * outer_disc:
        _fail('lost_bore: the section area %.6f mm2 equals the OUTER DISC '
              'pi*r_out^2 = %.6f mm2, not the annulus %.6f mm2. The inner loop was '
              'dropped silently, and nothing was raised.'
              % (area, outer_disc, EXPECTED_AREA_MM2))
    _fail('section_area: the section area %.6f mm2 is not within %.3f%% of the expected '
          'annulus %.6f mm2' % (area, TOL * 100.0, EXPECTED_AREA_MM2))


def _extrude(section):
    opts = ExtrudeFaceOptions()
    opts.ExtrudeType = ExtrudeType.ForceIndependent
    res = ExtrudeFaces.Execute(
        Selection.Create(section.Faces[0]),
        DIR,
        MM(LENGTH_MM),
        opts,
        None)
    made = [cb for cb in res.CreatedBodies]
    if not made:
        _fail('extrude_created_nothing: ExtrudeFaces returned normally and created no '
              'body. Nothing was added.')
    return made[0]


def _body():
    existing = _find_body(NAME)
    if existing is not None:
        if not REPLACE:
            # The same check the Python side makes when it can see the design. Done
            # here too because on an attach session this client usually cannot, and
            # "would this overwrite something" must not depend on which side looks.
            _fail('name_conflict: a body named %r already exists in the open document. '
                  'Pass replace_existing=True to delete it and build a new one, or pass a '
                  'different name.' % NAME)
        existing.Delete()
    section = _section()
    info = _check_section(section)
    tube = _extrude(section)
    volume = tube.Shape.Volume * 1e9
    if abs(volume - EXPECTED_VOLUME_MM3) > TOL * EXPECTED_VOLUME_MM3:
        _fail('volume: the extruded volume %.6f mm3 is not within %.3f%% of the expected '
              '%.6f mm3' % (volume, TOL * 100.0, EXPECTED_VOLUME_MM3))
    try:
        section.Delete()
    except Exception:
        pass
    tube.SetName(NAME)
    result['name'] = NAME
    result['faces'] = str(tube.Shape.Faces.Count)
    result['edges'] = str(tube.Shape.Edges.Count)
    result['section_area_mm2'] = '%.9f' % info['area_mm2']
    result['section_faces'] = str(info['faces'])
    result['volume_mm3'] = '%.9f' % volume
    result['solid'] = str(R_IN_MM <= 0.0)


_guard(_body)
"""


_EXTRUDE_SCRIPT = """
SOURCE = {source!r}
WANTED_INDICES = {face_indices!r}
OP = {extrude_type!r}
DIR = Direction.Create({dx!r}, {dy!r}, {dz!r})
DISTANCE_MM = {distance_mm!r}
NEW_NAME = {name!r}

FACE_HOWTO = ('Determining a face surface type is NOT available: measured on 2024 R2, '
              'DesignFace has no Id / IsPlanar / SurfaceType, Face.Geometry raises '
              'AttributeError, and GetFaceNormal needs a PointUV that cannot be built '
              'from a BoundingBox. Select a face by its 0-based index instead.')


def _body_names():
    out = []
    for b in root.Bodies:
        try:
            out.append(str(b.GetName().encode('ascii', 'replace')))
        except Exception:
            out.append('<name unreadable>')
    return out


def _find_source():
    wanted = SOURCE.encode('ascii', 'replace')
    for b in root.Bodies:
        try:
            if str(b.GetName().encode('ascii', 'replace')) == wanted:
                return b
        except Exception:
            continue
    _fail('body_not_found: no body named %r in the open document. Bodies present: %s'
          % (SOURCE, _body_names()))


def _inventory(db):
    rows = []
    for i, f in enumerate(db.Faces):
        area = None
        perim = None
        try:
            area = f.Area
        except Exception:
            pass
        try:
            perim = f.Perimeter
        except Exception:
            pass
        rows.append('idx=%d area_mm2=%s perimeter_mm=%s' % (
            i,
            '?' if area is None else '%.4f' % (area * 1e6),
            '?' if perim is None else '%.4f' % (perim * 1e3)))
    return rows


def _pick_faces(db):
    if not WANTED_INDICES:
        _fail('no_extrudable_face: no face was selected, and this build cannot tell a '
              'planar face from a curved one. ' + FACE_HOWTO + ' Faces on %r: %s'
              % (SOURCE, _inventory(db)))
    faces = []
    for i in WANTED_INDICES:
        if i < 0 or i >= db.Faces.Count:
            _fail('face_not_found: face index %d is out of range for %r (0..%d). Faces: %s'
                  % (i, SOURCE, db.Faces.Count - 1, _inventory(db)))
        faces.append(db.Faces[i])
    return faces


def _select(faces):
    items = List[DesignFace]()
    for f in faces:
        items.Add(f)
    return Selection.Create(items)


def _name_body(b, name):
    try:
        b.SetName(name)
        return name
    except Exception:
        try:
            return str(b.GetName().encode('ascii', 'replace'))
        except Exception:
            return name


def _body():
    db = _find_source()
    faces = _pick_faces(db)
    used = []
    area_m2 = 0.0
    for f in faces:
        try:
            area_m2 += f.Area
        except Exception:
            pass
        try:
            used.append('%.4f' % (f.Area * 1e6))
        except Exception:
            used.append('?')

    before = []
    for b in root.Bodies:
        before.append(b)
    before_ids = set()
    for b in before:
        before_ids.add(id(b))
    before_volume = db.Shape.Volume * 1e9
    before_faces = db.Shape.Faces.Count

    opts = ExtrudeFaceOptions()
    opts.ExtrudeType = getattr(ExtrudeType, OP)
    res = ExtrudeFaces.Execute(_select(faces), DIR, MM(DISTANCE_MM), opts, None)

    # CreatedBodies is NOT evidence on this release: measured, it comes back empty
    # while the geometry really changed. The document is the evidence, so the effect
    # is established by diffing the document instead of trusting that return value.
    created = []
    for cb in res.CreatedBodies:
        created.append(cb)

    # What the SOURCE body looks like now. On this release `ForceIndependent` can FUSE
    # the pull into the source rather than adding a body -- measured on a hollow pipe,
    # a 5 mm pull of a 235.6194 mm2 annulus moved the source from 14529.692 to
    # 15707.774 mm3, which is +1178.082, the extruded volume. (The same call on a flat
    # disc creates a separate body.) So a source-volume change is an effect, and this
    # is the only place it can be seen.
    after_volume = None
    after_faces = None
    still_there = False
    for b in root.Bodies:
        if id(b) == id(db):
            still_there = True
            try:
                after_volume = b.Shape.Volume * 1e9
                after_faces = b.Shape.Faces.Count
            except Exception:
                pass

    if not created:
        for b in root.Bodies:
            if id(b) not in before_ids:
                created.append(b)
        if (not created
                and after_volume is not None
                and abs(after_volume - before_volume) > 1e-6):
            # The pull went into the source. Naming the source is the honest report:
            # that is where the geometry now is.
            created.append(db)

    named = []
    for i, cb in enumerate(created):
        named.append(_name_body(cb, NEW_NAME if i == 0 else '%s_%d' % (NEW_NAME, i + 1)))

    total_volume = 0.0
    for cb in created:
        try:
            total_volume += cb.Shape.Volume * 1e9
        except Exception:
            pass
    if id(db) in [id(cb) for cb in created] and after_volume is not None:
        # Reporting the whole body as "the extruded volume" would be inflated whenever
        # the pull fused into a body that already had volume. The extruded amount is
        # the source's own delta in that case.
        total_volume = abs(after_volume - before_volume)

    result['created'] = str(named)
    result['created_count'] = str(len(named))
    result['created_bodies_from_result'] = str(res.CreatedBodies.Count)
    result['fused_into_source'] = str(created and id(created[0]) == id(db))
    result['face_area_mm2'] = '%.9f' % (area_m2 * 1e6)
    result['volume_mm3'] = '%.9f' % total_volume
    result['faces_used'] = str(len(faces))
    result['face_indices_used'] = str(WANTED_INDICES)
    result['face_areas_mm2'] = str(used)
    result['extrude_type'] = OP
    result['source_volume_before_mm3'] = '%.9f' % before_volume
    result['source_volume_after_mm3'] = (
        'None' if after_volume is None else '%.9f' % after_volume)
    result['source_faces_before'] = str(before_faces)
    result['source_faces_after'] = (
        'None' if after_faces is None else str(after_faces))
    result['source_survived'] = str(still_there)
    result['bodies_after'] = str(_body_names())


_guard(_body)
"""


_PRELUDE_TEMPLATE = """
import math
import sys
SCDM_ROOT = r'{scdm_root}'
sys.path.append(SCDM_ROOT + r'\\Scripting')
execfile(SCDM_ROOT + r'\\Scripting\\LoadSCDMAPIModuleV242.py')
from SpaceClaim.Api.V242 import *
from System.Collections.Generic import List, ICollection

# NOTE: `import System` was removed on purpose. Measured on this machine, it binds
# `System` to something whose `Double` is Python's `float` type, so
# `System.Double[]([...])` fails at run time -- and it fails BEFORE the dict that
# reports failures can be written, so the caller sees only "Script run failed."
# for what is really a one-attribute problem. `Direction.Create` and
# `Point.Create` are the working spelling and are what the measured probe used.

# The dictionary the service reads back. It must exist BEFORE anything can go
# wrong: `run_script_file` returns `response['values']`, and a script that raises
# before creating it returns nothing at all -- measured, the caller then sees only
# "Script run failed." with no way to tell why. `_guard` writes the reason here.
result = {{}}

root = DocumentHelper.GetRootPart()


def _add(raw, name):
    if isinstance(raw, tuple):
        raw = raw[0]
    return DesignBody.Create(root, name, raw)


def _fail(message):
    # Tagged rather than bare because the service collapses every script exception
    # into the same two-word message ("Script run failed."), so the tag is the only
    # thing that reaches the caller.
    #
    # Note what is NOT done here: str() on a caught exception. Measured on this
    # service, GeometryExitedError.__str__ itself raises, so stringifying an
    # exception inside a handler kills the handler. Type names are safe.
    raise RuntimeError('FAIL/' + message)


def _guard(fn):
    # Runs the body and turns a FAIL-tagged raise into `result['failed']`, so the
    # script returns NORMALLY and the reason survives the service's error
    # collapsing. Anything else is reported by exception type -- never by str(),
    # which can itself raise.
    try:
        fn()
    except Exception:
        e = sys.exc_info()[1]
        text = ''
        if type(e).__name__ == 'RuntimeError':
            try:
                text = str(e)
            except Exception:
                text = ''
        if text.startswith('FAIL/'):
            result['failed'] = text[5:]
        else:
            result['failed'] = 'unexpected ' + type(e).__name__
"""


def _scdm_script_root() -> str:
    """The ``scdm`` directory of the newest installed release, for the API loader."""
    roots = detect_ansys_roots()
    exes: dict = roots.get("spaceclaim") or {}
    newest = roots.get("newest")
    for key in ([newest] if newest else []) + list(exes):
        path = exes.get(key)
        if path:
            return str(Path(path).parent)
    release = newest or "242"
    return rf"C:\Program Files\ANSYS Inc\v{release}\scdm"


def _plane_side(direction: tuple[float, float, float]) -> str:
    """Which static SpaceClaim plane holds a section normal to ``direction``.

    Restricted to the three cardinal axes, and the restriction is measured rather
    than chosen. Building the section on an arbitrary plane needs
    ``Plane.Create(origin, direction_x, direction_y)``, and on 2024 R2 **every**
    overload of it raises a bare ``TypeError`` from the interop binder -- point plus
    two directions, the four-argument form, and the ``Frame`` form were all tried.
    The static ``Plane.PlaneXY`` / ``PlaneYZ`` / ``PlaneZX`` fields are what works.

    Refusing a skew axis is the honest option: silently building the section on the
    wrong plane would produce a plausible solid along the wrong direction, which is
    exactly the class of error this module exists to prevent.
    """
    table = {
        (1.0, 0.0, 0.0): "yz",
        (0.0, 1.0, 0.0): "zx",
        (0.0, 0.0, 1.0): "xy",
    }
    key = tuple(round(v, 12) for v in direction)
    if key in table:
        return table[key]
    raise SessionError(
        f"axis {list(direction)!r} is not one of the three cardinal axes. A section can only "
        "be built on a static plane on this release: "
        "Plane.Create(origin, direction_x, direction_y) raises TypeError from the interop "
        "binder for every overload tried on 2024 R2, while Plane.PlaneXY / PlaneYZ / "
        "PlaneZX work. Pass axis='x', 'y' or 'z' (or the matching unit vector).",
        code="unsupported_axis",
    )


def _plane_axes(
    origin: list[float] | None, normal: tuple[float, float, float]
) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    """``(origin, direction_x, direction_y)`` for a plane with the given normal.

    The same derivation :func:`_plane_from_normal` uses, returned as plain tuples
    because the creation path interpolates them into generated IronPython.
    ``direction_x x direction_y == normal``, by construction.
    """
    nx, ny, nz = normal
    ref = (1.0, 0.0, 0.0) if abs(nx) < 0.9 else (0.0, 1.0, 0.0)
    dx = (ref[1] * nz - ref[2] * ny, ref[2] * nx - ref[0] * nz, ref[0] * ny - ref[1] * nx)
    norm = math.sqrt(sum(c * c for c in dx))
    if norm == 0.0:
        raise SessionError(
            f"cannot build a plane for extrusion axis {normal!r}", code="invalid_parameter"
        )
    dx = tuple(c / norm for c in dx)
    dy = (ny * dx[2] - nz * dx[1], nz * dx[0] - nx * dx[2], nx * dx[1] - ny * dx[0])
    base = tuple(float(v) for v in (origin or (0.0, 0.0, 0.0)))
    if len(base) != 3:
        raise SessionError(
            f"origin needs 3 components, got {origin!r}", code="invalid_parameter"
        )
    return base, dx, dy


def _script_error(context: str, values: dict) -> None:
    """Turn a generated script's ``failed`` marker into a classified SessionError.

    Why the marker exists: the Ansys Geometry Service collapses every IronPython
    exception into the same two-word message -- measured, three different failures
    all came back as ``GeometryRuntimeError("Script run failed.")``. A generated
    script therefore catches its own ``FAIL/``-tagged errors, writes the reason to
    ``result['failed']`` and returns normally, and the classification happens here.

    The code is the text before the first colon for the tags this module defines,
    so a caller can branch on ``lost_bore`` / ``name_conflict`` / ``body_not_found``
    without matching prose.
    """
    message = str(values.get("failed") or "")
    if not message:
        return
    known = (
        "lost_bore",
        "section_area",
        "volume",
        "name_conflict",
        "body_not_found",
        "face_not_found",
        "no_extrudable_face",
        "extrude_created_nothing",
    )
    code = message.split(":", 1)[0].strip() if ":" in message else ""
    if code not in known:
        code = f"{context}_failed"
    raise SessionError(
        f"{context} did not complete: {message}",
        code=code,
        returned=values,
    )


def _as_list(value: Any) -> list:
    """A list from whatever the script returned for a multi-valued field.

    ``run_script_file`` hands back ``dict[str, str]``, so a list arrives as a
    string like ``"['A', 'B']"``. Anything unparseable becomes an empty list, and
    the caller treats "empty" as "nothing was created" -- which is the safe
    reading, because a creation that cannot be named cannot be verified.
    """
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            import ast

            parsed = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return []
        if isinstance(parsed, (list, tuple)):
            return [str(v) for v in parsed]
    return []


def _safe_flag(obj: Any, attribute: str) -> bool | None:
    """Read a boolean property, or ``None`` when it cannot be read.

    ``None`` rather than ``False``: a body whose flag could not be read has not
    been shown to be a surface.
    """
    try:
        value = getattr(obj, attribute)
    except Exception:  # noqa: BLE001
        return None
    return bool(value) if isinstance(value, bool) else None


def _iter_paths(comp, prefix: str = "") -> list[str]:
    """Every path in the design tree, bodies and components alike.

    Used for the "which names could you have meant" list on a lookup failure.
    """
    here = f"{prefix}/{comp.name}" if prefix else comp.name
    paths = [here]
    for _, body in _iter_bodies_with_path(comp):
        paths.append(body.name)
    for sub in comp.components:
        paths.extend(_iter_paths(sub, here))
    return sorted(set(paths))


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


# -- design-tree traversal ------------------------------------------------
#
# Imported CAD does not land in the root component. A STEP or SolidWorks
# assembly produces a root that holds *zero* bodies while its children hold every
# solid, and those solids are usually all named "实体" -- the component name is
# what identifies the part. Reading only ``design.bodies`` therefore reports an
# empty design for a perfectly good import. A 131-solid STEP looked exactly like
# a failed translation for this reason, and the flaw was here, not in the file
# and not in the licence.


def _iter_bodies_with_path(comp, prefix: str = ""):
    """Yield ``(path, body)`` over the whole design tree, path unique per body.

    A component can hold many solids -- an imported winding arrived with 79 --
    so the plain component path would collide. Such a component gets an index
    suffix on each body. A one-solid component keeps its bare path, which is
    what a caller normally types.
    """
    here = f"{prefix}/{comp.name}" if prefix else comp.name
    bodies = list(comp.bodies)
    for i, body in enumerate(bodies, 1):
        yield (here if len(bodies) == 1 else f"{here}#{i}"), body
    for sub in comp.components:
        yield from _iter_bodies_with_path(sub, here)


def _all_bodies(comp) -> list:
    """Every body in the tree, whichever component happens to hold it."""
    return [body for _, body in _iter_bodies_with_path(comp)]


def _body_index(comp) -> dict:
    """Look a body up by component path, or by bare name while that stays unique.

    Up to three keys are registered per body, in decreasing specificity:

    * the full path, e.g. ``定转子装配/绕组``
    * the same path without the design name, e.g. ``绕组`` -- the part name a
      caller would actually type
    * the bare body name, e.g. ``stator``

    Imported models name every solid "实体", so a bare-name key is dropped as
    soon as a second body claims it and callers are forced to qualify. Native
    ``.scdoc`` bodies have unique names and keep working by bare name.

    **This is an alias table, not a body list.** One body can own three keys, so
    ``len(index)`` is not a body count. That distinction was lost once and cost a
    real bug: ``inspect_geometry`` derived ``bodies_checked`` from ``len(index)``
    and reported exactly 2x the truth (1 body / 4 faces came back as 2 / 8, while
    ``scdm_list_bodies`` said 1). Use :func:`_unique_bodies` whenever the number
    of *things* is what matters.
    """
    index: dict = {}
    root = getattr(comp, "name", None)
    prefix = f"{root}/" if root else None
    ambiguous: set = set()
    for path, body in _iter_bodies_with_path(comp):
        index[path] = body
        if prefix and path.startswith(prefix):
            # Only when it genuinely differs. For a body held directly by the root
            # the path is exactly the prefix plus the body name, so the stripped
            # form duplicates the bare-name key below and would otherwise add a
            # phantom entry that aliases nothing new.
            stripped = path[len(prefix) :]
            if stripped != body.name:
                index.setdefault(stripped, body)
        if body.name in ambiguous:
            continue
        if body.name in index and index[body.name] is not body:
            index.pop(body.name, None)
            ambiguous.add(body.name)
        else:
            index[body.name] = body
    return index


def _unique_bodies(index: dict) -> list:
    """The distinct bodies an alias table points at, in first-seen order.

    Deduplicates by object **identity**, not by name: an imported assembly names
    every solid "实体", and a name-based set would collapse 79 real bodies into
    one. Order follows insertion order of the index (root first, then children).
    """
    seen: set = set()
    out: list = []
    for body in index.values():
        key = id(body)
        if key in seen:
            continue
        seen.add(key)
        out.append(body)
    return out


@dataclass
class Session:
    """One live SpaceClaim modeler, or the absence of one.

    A session has an **origin**, and it decides what this server is allowed to do
    with the process:

    ``"launch"``
        ``scdm_session_start`` created the process. This server owns it and holds
        its licence; ``close()`` shuts it down and releases the licence.
    ``"attach"``
        The user created the process and this server only connected to its gRPC
        endpoint. It holds no licence of its own, and it is **not allowed to close
        the window**: ``close()`` refuses and ``detach()`` disconnects the channel.
    """

    modeler: Any = None
    path: str | None = None
    version: str | None = None
    # The design opened this session, pinned by `open_file`. See `_require_design`.
    design: Any = None
    # "launch" | "attach" | None. Read by the guards, never inferred from the
    # modeler: both origins hand back the same kind of object.
    origin: str | None = None
    # launch: whether the process was started with a UI (`not hidden`).
    # attach: None, because the window belongs to the user's own start action and
    # this server cannot observe it -- reporting a guess here would be a lie.
    ui_visible: bool | None = None
    # attach only: how we reached it (no credentials are ever stored here).
    target: dict | None = None
    # attach only: seconds `Modeler(...)` needed, and whether this client owns the
    # backend process (the library-level reason close() cannot kill it).
    attach_seconds: float | None = None
    has_product_instance: bool | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self.modeler is not None

    @property
    def mode(self) -> str | None:
        """Machine-readable mode name for tool results: ``launch`` / ``attach``."""
        return self.origin

    def _forget(self) -> None:
        self.modeler = None
        self.design = None
        self.path = None
        self.origin = None
        self.ui_visible = None
        self.target = None
        self.attach_seconds = None
        self.has_product_instance = None

    def _ownership(self) -> dict:
        """Who owns the process, and which tool may end this session."""
        if self.origin == "attach":
            return {
                "owner": "user",
                "created_by": "the user, before scdm_attach ran",
                "tool_will_close_it": False,
                "disconnect_tool": "scdm_detach",
            }
        return {
            "owner": "ai",
            "created_by": "scdm_session_start",
            "tool_will_close_it": True,
            "close_tool": "scdm_session_close",
        }

    def _licence(self) -> dict:
        """Whether this session occupies a licence of its own."""
        if self.origin == "attach":
            return {
                "new_licence_consumed": False,
                "note": (
                    "reuses the licence the user's already-running SpaceClaim holds; "
                    "scdm_detach does not release it"
                ),
            }
        return {
            "new_licence_consumed": True,
            "note": "held by the SpaceClaim this server launched; scdm_session_close releases it",
        }

    def start(
        self, version: str | int | None = None, hidden: bool = False, timeout: int = 180
    ) -> dict:
        """Launch SpaceClaim and return the backend facts.

        ``hidden=False`` is the default, mirroring the Fluent side's default
        ``ui_mode="gui"``: the user asked to see the instance the AI is driving,
        and a bridge that hides it makes every operation unauditable.

        ``hidden=True`` is **not** a minimised window. PyAnsys maps it to
        ``/Headless=True`` (`product_instance.py:396-400`), which Ansys documents as
        batch mode *without UI*. Measured on this machine: of 40 top-level windows,
        the only visible ones were two 13x13 untitled stubs -- no main window and no
        splash screen. ``hidden=False`` produced a real, visible main window
        (`title='设计(D)1 - SpaceClaim'`, 1935x1047, `IsWindowVisible=True`) in
        **28.75 s** at ~971-973 MB against 23.47 s / ~885-888 MB for ``hidden=True``.

        The visible window can land on a **secondary monitor**: both visible
        launches put it at ``x=-1927``, which is this machine's negative-coordinate
        second display, because placement follows what the system last remembered.
        PyAnsys does not expose window placement, so forcing the primary monitor
        would mean appending ``/WindowLocation=`` or ``/WindowMaximized`` to the
        executable arguments by hand; see docs/verification.md.

        ``version`` is optional on purpose. Hard-coding 242 is exactly what
        breaks when the package moves to another machine, so when it is omitted
        we take the newest installed release and let the product pick.
        """
        if self.connected:
            # Already live. Report what is actually there rather than starting a
            # second process: the returned status carries the real mode, so a
            # caller that asked for `hidden=False` on an existing attach session
            # is told "attach" instead of being told what it wanted to hear.
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
        self.origin = "launch"
        self.ui_visible = not hidden
        # Adopt whatever design the modeler already reports, so `status()` and
        # `describe()` answer from the same handle. Without this, `status()` read
        # `modeler.design` while `describe()` read `self.design` and the two could
        # disagree about the same document.
        self.design = self.modeler.design
        _stderr(f"scdm: launched mode=launch ui_visible={self.ui_visible}")
        return self.status()

    def status(self) -> dict:
        """Current session facts, including which mode this session is in.

        ``mode``, ``licence`` and ``process_ownership`` are always present so a
        caller can tell "I own this" from "the user owns this" without reading the
        prose. On an attach session the ownership block is the machine-readable
        form of the promise this server makes: ``tool_will_close_it: False``.
        """
        if not self.connected:
            roots = detect_ansys_roots()
            return {
                "connected": False,
                "mode": None,
                "ui_visible": None,
                "ansys_releases": roots["releases"],
                "spaceclaim_executables": roots["spaceclaim"],
                "hint": (
                    "call scdm_session_start to launch SpaceClaim (mode=launch), or "
                    "scdm_attach to connect to one the user started (mode=attach)"
                ),
            }
        client = self.modeler.client
        # `self.design` FIRST, then the modeler's. They are not always the same
        # object, and this tool was reading only the modeler's -- which is why it
        # reported `body_count: 0` for a session whose `scdm_open_file` had just
        # reported 1 body, and whose `scdm_inspect_geometry` was counting the same
        # single body. Three tools, three different answers, one document.
        design = self.design if self.design is not None else self.modeler.design
        bodies = self._unique_or_none(design)
        result = {
            "connected": True,
            # "launch" | "attach". This is the field to branch on.
            "mode": self.mode,
            # launch: the measured meaning of the `hidden` flag it was started
            # with. attach: None -- the user's own window, which this server
            # cannot observe; a guess would be worse than an empty answer.
            "ui_visible": self.ui_visible,
            "backend_type": str(getattr(client, "backend_type", "")),
            "backend_version": self.version,
            "current_file": self.path,
            "design_name": getattr(design, "name", None),
            # `None`, NOT 0, when no design is visible to this client. Measured: an
            # attach session with the user's document open reports exactly this --
            # the raw IronPython API lists the bodies while this client sees nothing.
            # Reporting 0 there is a number that looks like a reading and is not one.
            "body_count": None if bodies is None else len(bodies),
            "design_visible": bodies is not None,
            "licence": self._licence(),
            "process_ownership": self._ownership(),
        }
        if bodies is None:
            result["body_count_note"] = (
                "no design is visible to this client, so a body count is unknown rather than "
                "zero. Measured on an ATTACH session with the user's document open: the raw "
                "IronPython API (DocumentHelper.GetRootPart) lists that document's bodies "
                "while this client's get_active_design() / read_existing_design() both fail. "
                "scdm_create_pipe and scdm_extrude still work -- they reach the open document "
                "through the service's script channel."
            )
        if self.origin == "attach":
            result["attached_to"] = dict(self.target or {})
            result["observed"] = {
                "backend_type": result["backend_type"],
                "backend_version": self.version,
                "has_product_instance": self.has_product_instance,
                "connect_seconds": self.attach_seconds,
            }
            result["note"] = (
                "this SpaceClaim was started by the user and is only being driven over gRPC; "
                "scdm_session_close refuses to touch it, use scdm_detach to disconnect"
            )
        return result

    def _unique_or_none(self, design) -> list | None:
        """Distinct bodies of ``design``, or ``None`` when there is no design to read.

        ``None`` and ``[]`` are different answers and every reporting tool here must
        keep them apart: ``None`` is "this client cannot see a document", ``[]`` is
        "the document is empty". Collapsing them is what produced a `body_count` of 0
        for a document that had a body in it.
        """
        if design is None:
            return None
        try:
            return _unique_bodies(_body_index(design))
        except Exception:  # noqa: BLE001
            return None

    def attach(
        self,
        host: str = DEFAULT_ATTACH_HOST,
        port: int = DEFAULT_ATTACH_PORT,
        transport_mode: str = DEFAULT_ATTACH_TRANSPORT,
        timeout: float = 30.0,
    ) -> dict:
        """Connect to a SpaceClaim the USER started. Owns nothing, closes nothing.

        Why this is a separate tool and not a flag on :meth:`start`: the licence
        and ownership semantics are opposite. ``start`` creates a process (a licence)
        that this server must close; ``attach`` borrows a process somebody else
        created and must never close. A boolean switch would have to mean both
        "release the licence" and "do not release the licence", and the failure mode
        of getting it wrong is killing a user's unsaved work.

        The mechanism, and what is measured about it:

        * ``Modeler(host, port, transport_mode=...)`` is the official entry point
          for an existing session. There is no ``connect_to_spaceclaim`` /
          ``from_existing_session`` helper in PyAnsys Geometry; the documented
          "Use an existing session" recipe is exactly this constructor call.
        * ``transport_mode`` is passed, never defaulted: since PyAnsys 0.14
          ``wait_until_healthy`` raises ``ValueError`` when it is ``None``
          (``connection/client.py:173-182``).
        * ``product_instance`` is deliberately **not** passed. That is what makes
          ``Modeler.close()`` close only the channel
          (``connection/client.py:493-499``).
        * Measured against a user-started instance on this machine: connected in
          **0.31 s**, reported ``BackendType.SPACECLAIM`` / ``24.2.0``, and the
          SpaceClaim process was still alive after ``close()``.

        The port is a convention rather than a discovery mechanism: SpaceClaim has
        no equivalent of Fluent's ``-sifile=``, and PyAnsys picks a random port when
        ``API_PORT`` is unset. If the user's launcher picked another port, pass it.

        Raises :class:`SessionError` with ``code="attach_unreachable"`` and the full
        command line when the instance is not reachable -- the commonest cause being
        a SpaceClaim started by a plain double-click, which has no gRPC endpoint at
        all.
        """
        if self.connected:
            raise SessionError(
                f"a session is already live (mode={self.mode}); detach it (scdm_detach) or "
                "close it (scdm_session_close) before attaching. Attaching again would "
                "overwrite the handle and leak the existing channel.",
                code="session_already_connected",
                next_step="scdm_detach" if self.origin == "attach" else "scdm_session_close",
                session=self.status(),
            )

        host, port, transport_mode = check_attach_args(host, port, transport_mode)
        agc = _import_geometry()

        started = time.time()
        _stderr(
            f"scdm: attaching to {host}:{port} transport_mode={transport_mode} timeout={timeout}s"
        )
        try:
            modeler = agc.Modeler(
                host=host, port=port, transport_mode=transport_mode, timeout=timeout
            )
        except Exception as exc:
            raise SessionError(
                attach_guidance(host, port, transport_mode, exc), code="attach_unreachable"
            ) from exc
        elapsed = time.time() - started

        client = getattr(modeler, "client", None)
        backend_type = str(getattr(client, "backend_type", "")) if client is not None else ""
        if "SPACECLAIM" not in backend_type.upper():
            # Refuse, and hand the channel back rather than leave it dangling.
            # `close()` here is safe by construction: this client has no
            # product_instance, so it can only close the channel.
            try:
                modeler.close()
            except Exception as exc:  # noqa: BLE001 - the refusal below matters more
                _stderr(f"scdm: attach cleanup failed: {exc}")
            raise SessionError(
                f"connected to {host}:{port} but the backend reports backend_type="
                f"{backend_type!r}, not SPACECLAIM. Refusing to use it: every operation in "
                "this server is a SpaceClaim operation, and the geometry API of another "
                "product is not the same one. The channel was closed again; the process was "
                "not touched.",
                code="wrong_backend",
                process_untouched=True,
            )

        self.modeler = modeler
        self.origin = "attach"
        self.ui_visible = None
        self.target = {"host": host, "port": port, "transport_mode": transport_mode}
        self.attach_seconds = round(elapsed, 3)
        self.has_product_instance = _has_product_instance(client)
        self.version = _format_version(getattr(client, "backend_version", None))
        # Adopt the design the modeler reports, if it reports one. Usually it does
        # not: measured, an attached session whose SpaceClaim has a document open
        # still hands back no design here, and `status().body_count` is then None
        # rather than a fabricated 0.
        self.design = getattr(modeler, "design", None)
        _stderr(
            f"scdm: attached in {elapsed:.2f}s backend={backend_type} "
            f"version={self.version} has_product_instance={self.has_product_instance} "
            f"design_visible={self.design is not None}"
        )
        return self.status()

    def close(self) -> dict:
        """Close a session this server launched, and release its licence.

        **Refused on an attach session.** That instance is the user's, and this
        server is not allowed to close it. The refusal is a guard at this layer on
        purpose: measurement says ``Modeler.close()`` would not kill the process
        anyway, because ``_product_instance`` is ``None`` for a client built by
        ``Modeler(host, port)`` and ``GrpcClient.close()`` only shuts the backend
        down inside ``elif self._product_instance:`` (``client.py:493-499``). But
        that is a *default value*, and other teardown paths in this library are not
        so careful -- ``force_exit()`` does not consult it at all. A guarantee that
        depends on a field somebody else might populate is not a guarantee, so the
        check lives here.
        """
        if not self.connected:
            return {"closed": False, "reason": "no session"}
        if self.origin == "attach":
            raise SessionError(
                "refusing to close: this session ATTACHED to a SpaceClaim the user started, "
                "and this tool is not allowed to close the user's window. Use scdm_detach to "
                "disconnect the channel and leave the process running; the user closes their "
                "own SpaceClaim. (Measured, Modeler.close() with product_instance=None only "
                "closes the gRPC channel -- but that is a library default, not a contract, so "
                "the refusal is enforced here.)",
                code="attach_mode_refuses_close",
                process_untouched=True,
                licence_released=False,
                next_step="scdm_detach",
                session=self.status(),
            )
        try:
            self.modeler.close()
        finally:
            self._forget()
        return {
            "closed": True,
            "mode": "launch",
            "process_untouched": False,
            "licence_released": True,
        }

    def detach(self) -> dict:
        """Disconnect from a user-owned SpaceClaim: close the channel, nothing else.

        The counterpart of ``close()``, and refused on a launch session for the
        mirror-image reason: detaching a session this server created would close the
        channel and leave a SpaceClaim process running that nobody can close -- it
        would still hold a licence, and there would be no handle left to release it.
        """
        if not self.connected:
            return {"detached": False, "reason": "no session"}
        if self.origin != "attach":
            raise SessionError(
                f"detach is for attach sessions only; this session was {self.origin or 'unknown'} "
                "and this server owns its process. Detaching would close the channel and leave a "
                "SpaceClaim holding a licence with no handle left to close it. Use "
                "scdm_session_close.",
                code="detach_requires_attach",
                process_untouched=True,
                licence_released=False,
                next_step="scdm_session_close",
                session=self.status(),
            )
        attached_to = dict(self.target or {})
        try:
            # No product_instance was passed to Modeler(...), so this closes the
            # gRPC channel and cannot reach the process.
            self.modeler.close()
        finally:
            self._forget()
        return {
            "detached": True,
            "mode": "attach",
            "attached_to": attached_to,
            "process_untouched": True,
            "licence_released": False,
            "note": (
                "only the gRPC channel was closed; the user's SpaceClaim keeps running with "
                "its own licence, and its window is untouched"
            ),
        }

    def shutdown(self) -> dict | None:
        """Best-effort teardown for process exit. Never kills a user-owned window.

        Registered with :mod:`atexit` by the server. It routes by origin instead of
        calling ``close()`` unconditionally, because ``close()`` refuses on attach
        sessions -- an exception raised while the interpreter is shutting down would
        print a traceback on stderr for a case that is not an error at all.
        """
        if not self.connected:
            return None
        try:
            if self.origin == "attach":
                return self.detach()
            return self.close()
        except Exception as exc:  # noqa: BLE001 - teardown must not raise
            _stderr(f"scdm: shutdown left the session as it was: {exc}")
            return None

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
        """Body names, face counts and volumes for one design.

        ``design=None`` means "the design this session is working on", resolved
        through the same fallback `status()` uses so the two cannot disagree about
        the same document. When neither the caller, this session, nor the modeler can
        offer one the answer is ``body_count: 0`` **with** ``design_visible: false``,
        which is what keeps "the document is empty" and "I cannot see a document"
        from reading the same.
        """
        if design is None:
            design = self.design if self.design is not None else getattr(
                self.modeler, "design", None
            )
        if design is None:
            return {
                "design": None,
                "design_visible": False,
                "bodies": [],
                "body_count": 0,
                "named_selections": [],
                "note": (
                    "no design is visible to this client, so this is NOT 'the document is "
                    "empty' -- measured, an attach session with the user's document open "
                    "reports exactly this. scdm_create_pipe / scdm_extrude reach that "
                    "document through the service's script channel."
                ),
            }
        bodies = []
        by_component: dict = {}
        for path, body in _iter_bodies_with_path(design):
            try:
                faces = len(body.faces)
            except Exception:
                faces = None
            try:
                volume = float(body.volume.magnitude)
            except Exception:
                volume = None
            entry = {"name": body.name, "faces": faces, "volume_m3": volume}
            # The last path segment is the component; a "#n" there only
            # separates several solids held by one component.
            component = path.rsplit("/", 1)[-1].split("#", 1)[0]
            if "/" in path:
                entry["in_component"] = component
                roll = by_component.setdefault(
                    component, {"bodies": 0, "faces": 0, "volume_m3": 0.0}
                )
                roll["bodies"] += 1
                roll["faces"] += faces or 0
                roll["volume_m3"] += volume or 0.0
            bodies.append(entry)
        try:
            selections = [ns.name for ns in design.named_selections]
        except Exception:
            selections = []
        result = {
            "design": getattr(design, "name", None),
            "design_visible": True,
            "is_active": getattr(design, "is_active", None),
            "body_count": len(bodies),
            "bodies": bodies,
            "named_selections": selections,
        }
        if by_component:
            # Imported CAD only. One line per part, which is the view a mass or
            # volume roll-up actually needs: an import names every solid "实体",
            # so the component is the only thing that identifies the part.
            for roll in by_component.values():
                roll["volume_m3"] = round(roll["volume_m3"], 12)
            result["by_component"] = dict(
                sorted(by_component.items(), key=lambda kv: -kv[1]["volume_m3"])
            )
        return result

    def collisions(self, left: str | None = None, right: str | None = None, all_pairs: bool = False) -> dict:
        """Pairwise collision state using ``Body.get_collision``.

        Returns the semantic enum (NONE / TOUCH / INTERSECT / CONTAINED /
        CONTAINEDTOUCH) instead of a boolean, because TOUCH and INTERSECT mean
        very different things for meshing: TOUCH is a mating face, INTERSECT is
        real overlap.
        """
        design = self._require_design()
        by_name = _body_index(design)

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

        ``short_edges`` additionally carries an **attribution**. On a pipe whose
        circular section was drawn as a 360-gon this check reported 1440 short
        edges (0.349-0.436 mm, exactly the inner and outer chord lengths
        ``2*r*sin(pi/360)``) and ``clean=false``. That is not a defect; it is what
        a faceted circle looks like. The count is therefore accompanied by the
        length statistics that separate a repeated chord family from scattered
        slivers -- see :func:`classify_short_edges` for the declared rule.
        """
        design = self._require_design()
        by_name = _body_index(design)
        if bodies:
            missing = [n for n in bodies if n not in by_name]
            if missing:
                raise SessionError(f"bodies not found: {missing}")
            targets = [by_name[n] for n in bodies]
        else:
            # NOT `sorted(by_name)`: that is the alias table, and one body can own
            # several keys, so it reported exactly twice the real body and face
            # count (measured: 1 body / 4 faces came back as bodies_checked=2,
            # faces_checked=8 while scdm_list_bodies said 1).
            targets = _unique_bodies(by_name)

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
                        method,
                        targets,
                        "length",
                        short_edge_length,
                        SHORT_EDGE_SCAN_M,
                        attribution=True,
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
        short = (report["checks"].get("short_edges") or {}).get("attribution")
        if short and short.get("verdict") == "polygonal_chords":
            # Say plainly that clean=false is not a defect here, and keep the raw
            # count intact so the caller can still see what the kernel reported.
            report["assessment"] = (
                "clean is false only because of short_edges, and those edges are the "
                "facets of an approximated curve (uniform chord lengths), not geometry "
                "defects. Nothing needs fixing; the count falls to zero when the curve is "
                "modelled with real arcs."
            )
        report["clean"] = not any(found.values())
        return report

    def _scan_threshold(
        self,
        method: Any,
        bodies: list,
        kwarg: str,
        explicit: float | None,
        ladder: tuple,
        attribution: bool = False,
    ) -> dict:
        """One check at an explicit threshold, or the whole ladder when none is given.

        ``attribution`` adds a :func:`classify_short_edges` block over the measured
        object lengths. It is opt-in because only ``short_edges`` has the
        "is this a defect or a modelling choice" question; ``small_faces`` does not
        need it.
        """
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
            if attribution:
                result["attribution"] = classify_short_edges([])
            return result
        result.update(problem_summary(chosen[1]))
        span = _numeric_range(chosen[1], kwarg)
        if span:
            result["span"] = span
        if attribution:
            result["attribution"] = self._attribute_short_edges(chosen[1], kwarg)
        return result

    @staticmethod
    def _attribute_short_edges(items: list, kwarg: str = "length") -> dict:
        """Lengths for :func:`classify_short_edges`, read straight off the groups.

        Deliberately not derived from ``span``: the span reports min/median/max only,
        and the classification is *about* the distribution, so it needs the actual
        per-edge numbers. Reads are individually guarded because ``Edge.length``
        raises on some edges (measured: 162 of 675 on the reference assembly).
        """
        lengths: list[float] = []
        unreadable = 0
        for item in items:
            try:
                objects = getattr(item, "edges", None) or getattr(item, "faces", None) or []
            except Exception:  # noqa: BLE001
                continue
            for obj in objects:
                try:
                    raw = getattr(obj, kwarg, None)
                except Exception:  # noqa: BLE001 - a broken read is data, not a failure
                    unreadable += 1
                    continue
                raw = getattr(raw, "magnitude", raw)
                if isinstance(raw, (int, float)):
                    lengths.append(float(raw) * 1000.0 if kwarg == "length" else float(raw))
                else:
                    unreadable += 1
        result = (
            classify_short_edges(lengths)
            if kwarg == "length"
            else classify_short_edges([])
        )
        result["unreadable"] = unreadable
        return result

    # -- mutating operators (guarded) --------------------------------------

    def boolean(self, operation: str, target: str, tools: list[str], keep_tools: bool = False) -> dict:
        """``unite`` / ``subtract`` / ``intersect`` on named bodies, guarded.

        ``subtract`` is expected to raise :class:`NoGeometryChange` on
        SpaceClaim 2024 R2. That exception is the correct outcome, not a bug in
        this wrapper: it tells the caller the kernel declined to do the work.
        """
        design = self._require_design()
        by_name = _body_index(design)
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
        by_name = _body_index(design)
        names = bodies or [path for path, _ in _iter_bodies_with_path(design)]
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

    def run_script(
        self,
        script_path: str | None = None,
        args: dict[str, str] | None = None,
        import_design: bool = True,
        script: str | None = None,
    ) -> dict:
        """Run an IronPython/SpaceClaim script through the official channel.

        The coverage escape hatch. Anything the typed tools do not expose is
        reachable here, and the call carries a real return value and a real
        exception instead of the stdout-scraping the raw ``/RunScript`` path
        requires.

        **The parameter contract, stated because it used to be wrong.** The old
        signature was ``script: str`` and read like "the script", while the
        implementation required a *file path*. Handing it script text failed on
        the very first call with ``script not found: <the entire script>`` -- and
        echoed the whole script back in the error message. Now:

        * ``script_path`` -- a path to an IronPython ``.py`` file.
        * ``script`` -- the script **source text**, written to a temporary file and
          run from there (the temp file is removed afterwards).

        Exactly one of the two. They are never guessed at, because guessing is what
        produced the original failure.

        Scripts are uploaded to the service, so the file has to exist on this
        machine. The script may return values by populating a ``result``
        dictionary; they come back under ``returned``.
        """
        if not self.connected:
            raise SessionError("no SpaceClaim session; call scdm_session_start first")

        result: dict = {}
        if import_design:
            # `run_script_file(..., import_design=True)` ends in
            # `self.read_existing_design()`, which fails on a session with no
            # document open ("There is no opened document"). Scripts themselves
            # often do not need one, so the refresh is made conditional rather
            # than letting it mask a successful script.
            self._ensure_design(create=True)

        path, temporary, source = resolve_script(script_path=script_path, script=script)
        try:
            values, design = self.modeler.run_script_file(
                str(path), script_args=args, import_design=import_design
            )
        except Exception as exc:
            guidance = acis_guidance(exc)
            if guidance is not None:
                raise SessionError(f"{exc}\n\n{guidance}", code="kernel_limitation") from exc
            raise
        finally:
            if temporary:
                try:
                    path.unlink()
                except OSError:
                    pass

        result = {
            # Kept as `script` because callers already read it, but it now names
            # the path that actually ran.
            "script": str(path),
            "script_source": source,
            "script_was_temporary": temporary,
            "returned": values,
        }
        if design is not None:
            self.design = design
            result.update(self.describe(design))
        return result

    def export(
        self,
        fmt: str,
        location: str | None = None,
        output_dir: str | None = None,
        file_stem: str | None = None,
    ) -> dict:
        """Export the open design, with a file name the caller controls.

        **What was wrong.** ``location`` alone was passed straight through as the
        export *directory*, so handing over ``...\\pipe_round.step`` produced
        ``...\\pipe_round.step\\设计(D)1.stp``: the file name was uncontrollable
        (it followed the document's name, which on a Chinese SpaceClaim is
        ``设计(D)1``), and the extension silently became ``.stp``. The caller could
        not learn where the bytes went from the return value either, because the
        path it echoed was the directory.

        **Two names, so the intent is never inferred:**

        * ``output_dir`` + ``file_stem`` -- say what you mean. The file is written
          to ``output_dir\\<stem><real extension>`` and, when the service picks a
          different name or extension anyway, the file is **renamed to match**, so
          the returned ``path`` is the file that exists.
        * ``location`` -- kept for compatibility. It is the export *directory*.
          The returned ``path`` is the file that was actually written.

        ``path`` in the result is always the final absolute path of the file on
        disk. ``expected_suffix`` reports the extension this format really uses,
        which is not always the format's own name.

        **One format-dependent failure is reported explicitly rather than as
        ``no_design``.** All six formats go through the same library call --
        ``Design.download(file_location, <format>)``, which reaches
        ``designs.download_export`` -- so there is **no** format-specific code path
        and no per-format precondition in this bridge. What did differ, measured on
        2026-10-15, is the *session state*: on an attach session whose client could
        not see the design, some calls came back ``no_design`` while others
        succeeded. Neither that mechanism nor a format-specific difference could be
        reproduced on demand here, so this tool does not pretend to have explained
        it. What it does instead is refuse with ``attach_export_needs_visible_design``
        -- naming the workaround (STEP is the one format measured landing bytes)
        rather than leaving a bare ``no_design`` for the caller to misread as "the
        session is broken".
        """
        # Validate the format first: an unknown format is a parameter error no matter
        # what the session looks like, and answering it with a session complaint would
        # send the caller looking in the wrong place.
        key = str(fmt).lower()
        method = _EXPORT_METHODS.get(key)
        if method is None:
            raise SessionError(f"unsupported format {fmt!r}; choose from {sorted(_EXPORT_METHODS)}")

        design = self.design
        if design is None and self.modeler is not None:
            design = getattr(self.modeler, "design", None)
        if design is None:
            # NOT "no design open". On an attach session this happens while the
            # user's document IS open; measured, the raw IronPython API lists its
            # bodies while get_active_design() / read_existing_design() both fail.
            raise SessionError(
                f"cannot export to {fmt!r}: this client cannot see the design, so there is "
                "nothing for the export call to act on. This is measured behaviour of an "
                "ATTACH session whose SpaceClaim has a document open -- it is NOT an empty "
                "session and it is NOT format-specific: every format goes through the same "
                "library call (Design.download -> designs.download_export), so no format is "
                "more or less supported here. Workarounds, in the order worth trying: "
                "(1) use scdm_run_script to export from inside the service, which reaches the "
                "open document; (2) export STEP, the one format measured landing bytes in "
                "this situation; (3) have the user start SpaceClaim from the launcher with no "
                "document open, or call scdm_open_file so this client can see a design.",
                code="attach_export_needs_visible_design",
                process_untouched=True,
                format=str(fmt),
                next_step="scdm_run_script",
            )
        if output_dir is None and location is None:
            raise SessionError(
                "pass output_dir (with file_stem) or location; neither was given.",
                code="missing_parameter",
            )
        if file_stem is not None and output_dir is None:
            # Checked before the mkdir below: refusing after creating a directory
            # would leave a side effect behind for a call that never ran.
            raise SessionError(
                "file_stem needs output_dir: file_stem is only the name, the directory has "
                "to come from somewhere. Pass output_dir=<directory> as well.",
                code="invalid_parameter",
            )

        directory = Path(output_dir if output_dir is not None else location)
        directory.mkdir(parents=True, exist_ok=True)

        # The service appends its own extension to whatever directory it is given,
        # so it is given a directory and then the result is moved into place.
        raw = Path(str(getattr(design, method)(str(directory))))
        expected_suffix = EXPORT_SUFFIXES.get(key)
        final = raw

        if file_stem:
            wanted = directory / f"{file_stem}{raw.suffix or expected_suffix or ''}"
            if wanted != raw:
                try:
                    if wanted.exists():
                        wanted.unlink()
                    raw.replace(wanted)
                    final = wanted
                except OSError as exc:
                    # Do not lose the result over cosmetics: report both paths.
                    final = raw
                    _stderr(f"scdm: export rename {raw} -> {wanted} failed: {exc}")

        existed = final.exists()
        suffix_ok = expected_suffix is None or final.suffix.lower() == expected_suffix.lower()
        return {
            "format": key,
            "path": str(final.resolve()) if existed else str(final),
            "exists": existed,
            "bytes": final.stat().st_size if existed else 0,
            "directory": str(directory.resolve()),
            # Named so a caller reading a path with the "wrong" ending knows it is
            # the service's doing and not a mistake on their side.
            "expected_suffix": expected_suffix,
            "suffix_matches_format": suffix_ok,
            "note": (
                "`path` is the file on disk. This format writes "
                f"{expected_suffix} on this release"
                + ("" if suffix_ok else " and the service chose a different one")
            ),
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
        by_name = _body_index(design)
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
        before = len(_all_bodies(design))
        component = design.insert_file(str(target))
        design = self._require_design()
        return {
            "inserted": str(target),
            "component": getattr(component, "name", None),
            "bodies_before": before,
            "bodies_after": len(_all_bodies(design)),
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
        by_name = _body_index(design)
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
            raise SessionError(
                f"no design open on this session; call scdm_open_file first, or "
                f"scdm_create_pipe / scdm_extrude which create one ({CREATE_DESIGN_NAME}) "
                f"when the session has none.",
                code="no_design",
            )
        if not getattr(design, "is_active", True):
            design.activate()
        if not getattr(design, "is_active", True):
            raise SessionError(
                f"design {getattr(design, 'name', '?')!r} could not be made active; "
                "operations would otherwise run against a different design and the "
                "before/after comparison would describe the wrong model."
            )
        return design

    def _ensure_design(self, create: bool = False):
        """The design this client can see, optionally creating one when it sees none.

        **Two different situations look identical from here, and only one of them is
        safe to "fix".** Measured 2026-10-15 against a user-started SpaceClaim
        (attach, `mode=attach`):

        * Genuinely nothing open -- ``get_active_design()`` and
          ``read_existing_design()`` both raise
          ``GeometryExitedError("There is no opened document.")``. Creating a design
          is then the only way to proceed.
        * A document **is** open, and this client still cannot see it. The same two
          calls fail, while the raw IronPython API (``DocumentHelper.GetRootPart()``)
          sees the user's bodies perfectly -- measured, it listed
          ``PIPE_ROUND_D50_t5_L200`` while ``scdm_list_bodies`` reported nothing.
          ``create_design()`` closes the design it believes is active
          (``modeler.py:186-188``), so on an attach session creating one could close
          the user's unsaved work.

        Hence the rule: **``create=True`` may create a design only on a launch
        session**, where this server owns the process and the document is its own.
        On an attach session the refusal is the honest answer, and it points at the
        route that does work -- the creation tools drive the user's own document
        through the service's script channel and never need this client to see it.
        ``create=False`` never creates anything, in any mode.
        """
        if not self.connected:
            raise SessionError("no SpaceClaim session; call scdm_session_start first")

        if self.design is not None and getattr(self.design, "is_active", False):
            return self.design

        modeler = self.modeler
        existing = None
        try:
            existing = modeler.get_active_design()
        except Exception as exc:  # noqa: BLE001 - "no document" is a state, not a failure
            _stderr(f"scdm: get_active_design reported {type(exc).__name__}")
        if existing is None:
            try:
                existing = modeler.read_existing_design()
            except Exception as exc:  # noqa: BLE001
                _stderr(f"scdm: read_existing_design reported {type(exc).__name__}")

        if existing is not None:
            self.design = existing
            return existing

        if not create:
            raise SessionError(
                "this client cannot see a design, and this operation is read-only, so it "
                "will not create one. Note that on an ATTACH session this can happen while "
                "the user does have a document open -- measured: the raw IronPython API "
                "listed the user's bodies while this client reported none. Use "
                "scdm_create_pipe / scdm_extrude, which reach the open document through the "
                "service's script channel, or scdm_open_file to open one this client can see.",
                code="no_design",
            )

        if self.origin == "attach":
            raise SessionError(
                "this session ATTACHED to a SpaceClaim the user started, and no design is "
                "visible to this client. Measured: when the user has a document open, "
                "get_active_design() and read_existing_design() both fail here while the raw "
                "IronPython API sees that document normally -- so this is a limitation of the "
                "client, not an empty session. This server will NOT create a document in that "
                "situation: create_design() closes the previous design, and that could be the "
                "user's unsaved work. Use scdm_create_pipe or scdm_extrude, which build their "
                "geometry through the service's own script channel and reach the open document "
                "without needing this client to see it. If this client needs to see a design, "
                "call scdm_open_file.",
                code="attach_refuses_create_design",
                process_untouched=True,
                next_step="scdm_create_pipe",
            )

        created = modeler.create_design(CREATE_DESIGN_NAME)
        self.design = created
        _stderr(
            f"scdm: no document was open, created {CREATE_DESIGN_NAME!r} "
            f"so the geometry could be built"
        )
        return created

    # -- geometry creation -------------------------------------------------

    def _run_snippets(self, script: str, args: dict[str, str] | None = None) -> dict:
        """Run generated IronPython, returning its ``result`` dict.

        Wraps :meth:`run_script` so generated code goes through exactly the same
        channel, the same temporary-file handling and the same error translation
        as a caller-supplied script. ``import_design=False``: the generated scripts
        create bodies and the caller re-reads the design itself, so there is
        nothing to sync here.
        """
        return self.run_script(script=script, args=args, import_design=False)

    def create_pipe(
        self,
        outer_diameter_mm: float,
        length_mm: float,
        inner_diameter_mm: float = 0.0,
        name: str | None = None,
        axis: str | list[float] = "z",
        origin: list[float] | None = None,
        tolerance: float = 1e-3,
        replace_existing: bool = False,
    ) -> dict:
        """Create a hollow pipe (or a solid rod) by section + extrude. No booleans.

        **Why this tool exists.** Before it, none of the fifteen ``scdm_*`` tools
        could create geometry: every one either read the model or operated on
        bodies that already existed. "Model from scratch" therefore meant writing
        IronPython by hand and running it through ``scdm_run_script`` -- the escape
        hatch whose parameter contract was itself the most misreadable thing in the
        tool set. This closes that gap for the route that was measured working.

        **The route, and why it is this one.** A ring section is built directly --
        outer loop plus inner loop in one ``ICollection[ITrimmedCurve]`` handed to
        ``Body.CreatePlanarBody(plane, profile, False, False, removeInnerRegions=True)``
        -- and then extruded with ``ExtrudeFaces.Execute(..., ForceIndependent)``.
        **No boolean is involved**, which is the point: ``Body.subtract`` on
        2024 R2 returns normally, deletes the tool body and leaves the target
        untouched, so building a tube by subtracting a rod from a rod cannot work
        on this kernel.

        Circles are drawn as **two semicircles**, not one full arc. Measured: a
        single ``CreateArc(center, p, p, axis)`` with ``start == end`` raises
        ``Invalid profile.``, while two half-circles from diametrically opposite
        points produce a section whose area matches the analytic value to six
        decimal places. Line segments also work but tessellate the circle: the
        360-gon version reported 1440 chord edges in ``scdm_inspect_geometry``.

        **Read-back guard.** The section area is compared against
        ``pi*(r_out^2 - r_in^2)`` before the body is accepted, and the extruded
        volume against ``area * length``. This is what catches the measured silent
        failure ``removeInnerRegions=False`` -- which does not raise, and returns a
        plain outer disc (1963.495 mm2 where the ring is 706.858 mm2). Without the
        guard a caller builds a solid rod believing it built a tube.

        **Where the guard runs, and why it matters here.** Both readings and both
        comparisons happen **inside the script**, then a second, independent read
        is attempted through this client. On an attach session that second read
        usually cannot happen: measured, when the user has a document open, this
        client's ``get_active_design()`` and ``read_existing_design()`` both fail
        while the raw IronPython API sees that document. The tool therefore reports
        ``verification`` as ``"service+client"`` or ``"service"`` rather than
        pretending the second read happened -- and it deliberately does **not**
        create a document to make it happen, because on an attach session
        ``create_design()`` could close the user's work.

        Parameters
        ----------
        outer_diameter_mm, length_mm
            Millimetres, always. Internally lengths are metres and ``MM()`` is the
            only correct conversion; the report carries both.
        inner_diameter_mm
            Bore. ``0`` (the default) makes a solid rod, which is an honest result
            and is reported as one rather than as a pipe.
        name
            Body name. Defaults to a name that states the dimensions, e.g.
            ``PIPE_OD50_ID40_L200``.
        axis
            ``"x"`` / ``"y"`` / ``"z"`` or a 3-vector -- the extrusion direction.
        origin
            Base point of the axis, in metres. Defaults to the world origin, so a
            ``z`` pipe spans ``z in [0, length]``.
        tolerance
            Relative tolerance for the section-area and volume guards.
        replace_existing
            Off by default: a body of the same name is not overwritten, and
            ``name_conflict`` is reported instead. Deleting somebody's body as a
            side effect of a create call is not a reasonable default.

        Returns ``created`` (the final body facts), ``section``, ``expected`` and
        ``checks`` with the guard results.
        """
        # Deliberately NO `_ensure_design(create=True)` here. The script reaches the
        # document the user has open through the service's own channel, so this tool
        # works whether or not this client can see a design -- and on an attach
        # session creating one would risk the user's unsaved work (see
        # `_ensure_design`). If the client *can* see a design it is refreshed, so the
        # independent verification below has something to read.

        params = ring_areas_mm2(outer_diameter_mm, inner_diameter_mm)
        if not isinstance(length_mm, (int, float)) or isinstance(length_mm, bool):
            raise SessionError(
                f"length_mm must be a number, got {length_mm!r}", code="invalid_parameter"
            )
        if length_mm <= 0:
            raise SessionError(
                f"length_mm must be positive, got {length_mm!r}", code="invalid_parameter"
            )

        direction = _unit_direction_vector(axis)
        side = _plane_side(direction)
        base = tuple(float(v) for v in (origin or (0.0, 0.0, 0.0)))
        if len(base) != 3:
            raise SessionError(
                f"origin needs 3 components, got {origin!r}", code="invalid_parameter"
            )
        body_name = name or _pipe_name(outer_diameter_mm, inner_diameter_mm, length_mm)
        if not isinstance(body_name, str) or not body_name.strip():
            raise SessionError(
                f"name must be a non-empty string, got {body_name!r}", code="invalid_parameter"
            )

        # Only checkable when the client can see the design. When it cannot, the
        # script does the same check against root.Bodies and reports name_conflict
        # itself, so the guarantee is kept either way.
        visible = self._visible_bodies()
        if visible is not None and body_name in visible and not replace_existing:
            raise SessionError(
                f"a body named {body_name!r} already exists. Pass replace_existing=True to "
                "delete it and build a new one, or pass a different `name`.",
                code="name_conflict",
                existing_bodies=visible,
            )

        r_out_mm = float(outer_diameter_mm) / 2.0
        r_in_mm = float(inner_diameter_mm) / 2.0
        expected_area = params["annulus_mm2"]
        expected_volume = expected_area * float(length_mm)

        script = _PRELUDE_TEMPLATE.format(scdm_root=_scdm_script_root())
        script += _PIPE_SCRIPT.format(
            name=body_name,
            r_out_mm=r_out_mm,
            r_in_mm=r_in_mm,
            length_mm=float(length_mm),
            dx=direction[0],
            dy=direction[1],
            dz=direction[2],
            bx=base[0],
            by=base[1],
            bz=base[2],
            side=side,
            expected_area=expected_area,
            expected_volume=expected_volume,
            tolerance=float(tolerance),
            replace=bool(replace_existing),
        )

        raw = self._run_snippets(script)
        values = raw.get("returned") or {}
        _script_error("scdm_create_pipe", values)
        if not values.get("name"):
            raise SessionError(
                "the geometry script returned no body; raw result: "
                f"{values!r}. Nothing was accepted.",
                code="creation_failed",
                returned=values,
            )

        measured_area = _as_float(values.get("section_area_mm2"))
        measured_volume = _as_float(values.get("volume_mm3"))

        checks = {
            "section_area": {
                "expected_mm2": expected_area,
                "measured_mm2": measured_area,
                "relative_error": _relative_error(measured_area, expected_area),
                "ok": _within(measured_area, expected_area, tolerance),
            },
            "volume": {
                "expected_mm3": expected_volume,
                "measured_mm3": measured_volume,
                "relative_error": _relative_error(measured_volume, expected_volume),
                "ok": _within(measured_volume, expected_volume, tolerance),
            },
        }
        failed = [k for k, v in checks.items() if not v["ok"]]
        if failed:
            raise SessionError(
                "the body was created but failed read-back verification: "
                + "; ".join(
                    f"{k} expected "
                    f"{checks[k].get('expected_mm2', checks[k].get('expected_mm3'))!r} got "
                    f"{checks[k].get('measured_mm2', checks[k].get('measured_mm3'))!r}"
                    for k in failed
                )
                + f". tolerance={tolerance!r}. Nothing is being reported as created.",
                code="creation_verification_failed",
                checks=checks,
                body=values["name"],
            )

        # Independent second read, when this client can see the design at all. The
        # script's own figures already passed the same comparison, so this is
        # corroboration, not the only guard -- and it is reported as absent rather
        # than silently skipped when the design is not visible.
        facts = self._describe_body(self.design, values["name"]) if self.design is not None else None
        verification = "service+client" if facts is not None else "service"
        if facts is None:
            facts = {
                "name": values["name"],
                "faces": _as_int(values.get("faces")),
                "edges": _as_int(values.get("edges")),
                "volume_mm3": measured_volume,
                "volume_m3": None if measured_volume is None else measured_volume * 1e-9,
                "verified_by": "the SpaceClaim service, inside the script",
            }

        return {
            "created": facts,
            "solid": values.get("solid") == "True",
            "section": {
                "outer_diameter_mm": float(outer_diameter_mm),
                "inner_diameter_mm": float(inner_diameter_mm),
                "wall_thickness_mm": params["wall_thickness_mm"],
                "outer_disc_mm2": params["outer_disc_mm2"],
                "inner_disc_mm2": params["inner_disc_mm2"],
            },
            "length_mm": float(length_mm),
            "axis": list(direction),
            "origin": list(base),
            "expected": {
                "annulus_mm2": expected_area,
                "volume_mm3": expected_volume,
                "volume_m3": expected_volume * 1e-9,
            },
            "checks": checks,
            "tolerance": tolerance,
            # Named so a caller can tell "two independent reads agreed" from "the
            # service's own read was the only one available".
            "verification": verification,
            "verification_note": (
                "guard ran inside the SpaceClaim service; a second read through this "
                "client also succeeded"
                if verification == "service+client"
                else "guard ran inside the SpaceClaim service; this client cannot see the "
                "design it was written into, so no second read was possible. That is the "
                "measured behaviour of an attach session with a document open -- it is not "
                "a skipped check."
            ),
            "route": (
                "Body.CreatePlanarBody(plane, profile, False, False, True) -> "
                "ExtrudeFaces.Execute(..., ForceIndependent). Two semicircles per circle, "
                "no boolean."
            ),
        }

    def extrude(
        self,
        body: str,
        distance_mm: float,
        direction: str | list[float] | None = None,
        face_indices: list[int] | None = None,
        name: str | None = None,
        extrude_type: str = "ForceIndependent",
        expected_volume_mm3: float | None = None,
        tolerance: float = 5e-2,
    ) -> dict:
        """Extrude faces of a body that already exists in the design.

        This is the general-purpose companion to :meth:`create_pipe`. Where that
        one builds a section and pulls it in a single step, this one starts from
        geometry that is already in the document -- an imported sketch region, a
        planar face of a STEP import, a section a script left behind.

        **Why it goes through generated IronPython.** The PyAnsys route is
        ``GeometryCommands.extrude_faces``, and it is decorated
        ``@min_backend_version(25, 2, 0)`` -- on this machine's 24R2 the call
        raises before it reaches the service, so it cannot be the implementation.
        The raw kernel call ``ExtrudeFaces.Execute(Selection.Create(face),
        direction, distance, options, None)`` **was measured working on 24R2
        against a face of a body that was already in the document** (a 12 mm disc
        in the design extruded 40 mm to a 18095.573685 mm3 cylinder, against an
        analytic 18095.57515 mm3). It also works through ``DesignFace`` obtained
        from ``DesignBody.Faces``, which is the handle a by-name selection reaches.

        **Faces are selected by 0-based index, and that is a measured
        limitation rather than a design choice.** On 2024 R2 a face cannot be
        identified or typed from the script: ``DesignFace`` has no
        ``Id`` / ``IsPlanar`` / ``SurfaceType``, ``Face.Geometry`` raises
        ``AttributeError``, ``EvalMid`` / ``EvalProportion`` / ``GetFaceNormal`` all
        raise ``TypeError``, and ``GetFaceNormal`` wants a ``PointUV`` that cannot be
        constructed from a ``BoundingBox``. Only ``Area`` and ``Perimeter`` read
        cleanly. So the tool does **not** guess which face is planar. Pass
        ``face_indices``; the refusal lists every face with its area and perimeter so
        the choice can be made from evidence. ``ForceIndependent`` is used so the
        result is a new body rather than a merged one.

        There is deliberately no "all planar faces" default: it would have to come
        from an API that does not work here, and a silent wrong choice is worse than
        a refusal that tells you what the faces are.

        Parameters
        ----------
        body
            Name of the source body, as ``scdm_list_bodies`` reports it.
        distance_mm
            Extrusion distance in millimetres (internally metres via ``MM()``).
        direction
            ``"x"`` / ``"y"`` / ``"z"`` or a 3-vector. Defaults to the normal of
            the first selected face, which is the usual intent for a single planar
            face.
        face_indices
            0-based face indices to extrude, as ``scdm_extrude``'s own refusal lists
            them (``idx=N area_mm2=... perimeter_mm=...``). Required: nothing about
            a face's kind can be read on this release.
        name
            Rename the created body. With several created bodies the first keeps
            this name and the rest are suffixed.
        expected_volume_mm3
            Optional independent check: the created bodies' total volume is
            compared against it. The source face area times ``distance_mm`` is
            reported either way, so the caller can do the arithmetic themselves.
        tolerance
            Relative tolerance for that check. The default is wide (5%) on
            purpose: unlike the pipe section, an arbitrary face's extruded volume
            has no exact analytic value to compare against, so a tight bound would
            be a guess dressed up as a verification.
        """
        if not isinstance(distance_mm, (int, float)) or isinstance(distance_mm, bool):
            raise SessionError(
                f"distance_mm must be a number, got {distance_mm!r}", code="invalid_parameter"
            )
        if distance_mm == 0:
            raise SessionError("distance_mm must not be zero", code="invalid_parameter")

        # As with create_pipe: no `_ensure_design`, and the face picking happens in
        # the script when this client cannot see the design. `Faces.Count` and
        # `Face.Id` are read the same way there, against the open document.
        #
        # A body this client CANNOT find is reported as body_not_found rather than
        # falling through to the script. It is the more useful error, and the two cases
        # are distinguishable: the lookup returns a definite miss, not an inability to
        # look. (The script checks again anyway, so the guarantee holds either way.)
        looked_up, source_body = self._lookup_body(body)
        if looked_up and source_body is None:
            raise SessionError(
                f"body not found: {body!r}",
                code="body_not_found",
                candidates=self._visible_paths(),
                note=(
                    "this client can see the design, so this is a definite miss -- not an "
                    "inability to look"
                ),
            )

        if face_indices is not None:
            if not isinstance(face_indices, (list, tuple)) or not all(
                isinstance(i, int) and not isinstance(i, bool) for i in face_indices
            ):
                raise SessionError(
                    f"face_indices must be a list of integers, got {face_indices!r}",
                    code="invalid_parameter",
                )
            face_indices = list(face_indices)
        if extrude_type not in _EXTRUDE_TYPES:
            raise SessionError(
                f"extrude_type must be one of {list(_EXTRUDE_TYPES)}, got {extrude_type!r}. "
                "Measured on 2024 R2: ForceIndependent creates a NEW body and leaves the "
                "source alone; Add / ForceAdd / None fuse the pull into the source body.",
                code="invalid_parameter",
            )

        direction_vector = _unit_direction_vector(direction) if direction else None
        if direction_vector is None:
            # The face's normal cannot be read on this release (Face.Geometry raises,
            # GetFaceNormal wants a PointUV that cannot be built). Guessing "z" would
            # extrude along the wrong axis and report success, so this is required.
            raise SessionError(
                "direction is required: a face's normal cannot be read on this release "
                "(measured -- Face.Geometry raises AttributeError, GetFaceNormal needs a "
                "PointUV that cannot be constructed from a BoundingBox), and guessing an "
                "axis would extrude along the wrong one and still report success. Pass "
                "direction='x' / 'y' / 'z' or a 3-vector.",
                code="no_direction",
            )

        before = self._snapshot_or_none()
        script = _PRELUDE_TEMPLATE.format(scdm_root=_scdm_script_root()) + _EXTRUDE_SCRIPT.format(
            source=body,
            face_indices=list(face_indices or []),
            extrude_type=extrude_type,
            dx=direction_vector[0],
            dy=direction_vector[1],
            dz=direction_vector[2],
            distance_mm=float(distance_mm),
            name=name or f"{body}_EXTRUDED",
        )
        raw = self._run_snippets(script)
        values = raw.get("returned") or {}
        _script_error("scdm_extrude", values)

        created_names = _as_list(values.get("created"))
        fused = str(values.get("fused_into_source")) == "True"
        if not created_names:
            raise SessionError(
                "the extrusion produced no new body and left the source body's volume "
                "unchanged, so nothing happened. Measured on 2024 R2: the shape of a "
                "declined extrusion is a normal return with no change. (Note that "
                "`CreatedBodies` comes back empty even when an extrusion DOES succeed, which "
                "is why this verdict is taken from the document instead of that return "
                "value.) Do not retry the same call unchanged.",
                code="no_geometry_change",
                before=None if before is None else before.summary(),
                returned=values,
            )

        after = self._snapshot_or_none()
        faces_area_mm2 = _as_float(values.get("face_area_mm2"))
        reported_volume = _as_float(values.get("volume_mm3"))
        expected_from_area = (
            None if faces_area_mm2 is None else faces_area_mm2 * float(distance_mm)
        )

        check: dict = {
            "expected_volume_mm3": expected_volume_mm3,
            "expected_from_face_area_mm3": expected_from_area,
            "measured_volume_mm3": reported_volume,
            "ok": None,
        }
        reference = expected_volume_mm3 if expected_volume_mm3 is not None else expected_from_area
        if expected_volume_mm3 is not None:
            check["ok"] = _within(reported_volume, expected_volume_mm3, tolerance)
            check["relative_error"] = _relative_error(reported_volume, expected_volume_mm3)
        elif reference is not None:
            # Reported, never enforced: the face area itself includes whatever
            # curvature the face has, so area*distance is a cross-check rather than
            # a specification.
            check["relative_error_vs_face_area"] = _relative_error(reported_volume, reference)
        if check["ok"] is False:
            raise SessionError(
                f"the extrusion created {created_names!r} but its volume "
                f"{reported_volume!r} mm3 does not match the expected "
                f"{expected_volume_mm3!r} mm3 within {tolerance:.1%}. The body was created; "
                "nothing is being reported as verified.",
                code="creation_verification_failed",
                check=check,
                created=created_names,
            )

        described = [
            d for d in (self._describe_body(self.design, nm) for nm in created_names) if d
        ]
        if len(described) != len(created_names):
            # Falls back to the script's own figures for the ones this client could
            # not re-read, saying so per body rather than dropping them.
            described = [
                self._describe_body(self.design, nm)
                or {"name": nm, "faces": None, "edges": None, "volume_m3": None,
                    "volume_mm3": None, "verified_by": "the SpaceClaim service, in the script"}
                for nm in created_names
            ]

        source_before = _as_float(values.get("source_volume_before_mm3"))
        source_after = _as_float(values.get("source_volume_after_mm3"))
        source_delta = (
            None if source_before is None or source_after is None else source_after - source_before
        )
        # `source_untouched` is read from the SCRIPT now, not from the client's
        # snapshot: on an attach session the snapshot is unavailable, and the raw API
        # inside the script is the one that can actually see the body. It also has to
        # distinguish "absent from my snapshot" (unknown) from "gone".
        script_says = values.get("source_survived")
        source_untouched = None if script_says is None else str(script_says) == "True"
        if source_untouched and source_delta is not None and abs(source_delta) > 1e-6:
            # A surviving source whose volume moved means the pull was FUSED into it,
            # which is what Add / ForceAdd do. Saying "untouched" there would be false.
            source_untouched = False

        return {
            "created": described,
            "source": body,
            "source_faces_used": list(face_indices or []),
            "face_areas_mm2": _as_list(values.get("face_areas_mm2")),
            "faces_used": _as_int(values.get("faces_used")),
            "direction": list(direction_vector),
            "distance_mm": float(distance_mm),
            "faces_area_mm2": faces_area_mm2,
            "extrude_type": extrude_type,
            "check": check,
            "tolerance": tolerance,
            "body_counts": {
                "before": None if before is None else before.body_count,
                "after": None if after is None else after.body_count,
            },
            "source_effect": {
                "volume_before_mm3": source_before,
                "volume_after_mm3": source_after,
                "volume_delta_mm3": source_delta,
                "faces_before": _as_int(values.get("source_faces_before")),
                "faces_after": _as_int(values.get("source_faces_after")),
                "fused_into_source": fused,
                "note": (
                    "A volume increase here with `fused_into_source: true` means the pull "
                    "went INTO the source body rather than becoming a new one -- which is "
                    "what Add / ForceAdd / None do, and what ForceIndependent did too, "
                    "measured, when the target was a hollow pipe: a 5 mm pull of a "
                    "235.6194 mm2 annulus added 1178.08 mm3 to the source. The same call on "
                    "a flat disc created a separate body instead, so this is not determined "
                    "by `extrude_type` alone."
                ),
            },
            "verification": (
                "service+client" if len(described) == len(created_names) and described
                and described[0].get("faces") is not None else "service"
            ),
            "route": (
                "ExtrudeFaces.Execute(Selection.Create(face), direction, MM(distance), "
                "ForceIndependent). Measured working on 24R2; PyAnsys' own "
                "extrude_faces is 25R2+ and cannot be used here. `CreatedBodies` comes back "
                "EMPTY on this release, so the created bodies are found by diffing the "
                "document instead."
            ),
            "source_untouched": source_untouched,
            "created_bodies_from_result": _as_int(values.get("created_bodies_from_result")),
        }

    def _lookup_body(self, name: str) -> tuple[bool, Any]:
        """``(could_look, body_or_None)`` for a body name.

        The first element is the part that matters: ``False`` means this client
        cannot see a design at all, so a missing body proves nothing and the script
        has to check. ``True`` with ``None`` is a definite miss. Collapsing the two
        into one ``None`` would let an invisible document be reported as an empty
        one -- the exact mistake this module keeps finding elsewhere.
        """
        if self.design is None:
            return False, None
        try:
            index = _body_index(self.design)
        except Exception:  # noqa: BLE001
            return False, None
        if name in index:
            return True, index[name]
        for candidate_body in index.values():
            if getattr(candidate_body, "name", None) == name:
                return True, candidate_body
        return True, None

    def _visible_paths(self) -> list[str]:
        """Every name the caller could have meant, when a body lookup missed."""
        if self.design is None:
            return []
        try:
            return _iter_paths(self.design)
        except Exception:  # noqa: BLE001
            return []

    def _snapshot_or_none(self):
        """``snapshot_design`` of the visible design, or ``None`` when there is none."""
        if self.design is None:
            return None
        try:
            return snapshot_design(self.design)
        except Exception:  # noqa: BLE001
            return None

    def _face_inventory(self, body) -> dict:
        """Per-face ``{id: {planar_hint, normal, area_m2}}`` for one body.

        Every read is guarded and ``planar_hint`` is ``None`` when it could not be
        determined -- "unknown" must not be flattened into "not planar", or a body
        whose faces cannot be interrogated would be refused with a reason that is
        not true.
        """
        out: dict = {}
        try:
            faces = list(body.faces)
        except Exception:  # noqa: BLE001
            return out
        for face in faces:
            fid = None
            try:
                fid = str(face.id)
            except Exception:  # noqa: BLE001
                continue
            info: dict = {"planar_hint": None, "normal": None, "area_m2": None}
            try:
                surface = getattr(face, "surface_type", None)
                info["surface_type"] = getattr(surface, "name", None) or str(surface)
                info["planar_hint"] = str(info["surface_type"]).upper().endswith("PLANE")
            except Exception:  # noqa: BLE001
                info["surface_type"] = None
            try:
                area = getattr(face, "area", None)
                info["area_m2"] = float(getattr(area, "magnitude", area))
            except Exception:  # noqa: BLE001
                pass
            try:
                # `Face.normal` needs a uv point on 24R2 and is not used: the
                # normal is left for the generated script, which reads it from the
                # kernel. Reported as None rather than guessed.
                pass
            except Exception:  # noqa: BLE001
                pass
            out[fid] = info
        return out

    def _visible_bodies(self) -> list[str] | None:
        """Body names this client can see, or ``None`` when it can see no design.

        ``None`` is not ``[]``. "I cannot see the design" and "the design is empty"
        are different facts, and a create tool must not read the first as the
        second: that is how it would decide a name is free when the user already
        has a body with that name. On an attach session with a document open this
        returns ``None`` (measured), and the generated script does the same name
        check against ``root.Bodies`` instead.
        """
        if self.design is None:
            return None
        try:
            return [b.name for b in _unique_bodies(_body_index(self.design))]
        except Exception:  # noqa: BLE001
            return None

    def _describe_body(self, design, name: str) -> dict | None:
        """Re-read one body from the service, by name.

        Re-read rather than trusting the script's own numbers: the script's
        ``DesignBody`` handle is a snapshot, and the whole point of the guard is
        that the *service's* view of the body is what the caller will act on.

        Returns ``None`` when there is no design this client can see, or when a
        failed read means the body's presence cannot be established. It does not
        raise on either, because both callers already verified the body inside the
        script and report ``verification`` accordingly -- turning "I could not
        check twice" into a hard failure would refuse a creation that measurably
        succeeded.
        """
        if design is None:
            return None
        try:
            index = _body_index(design)
        except Exception:  # noqa: BLE001
            return None
        body = index.get(name)
        if body is None:
            return None
        faces = None
        try:
            faces = len(list(body.faces))
        except Exception:  # noqa: BLE001
            pass
        volume_m3 = _body_volume(body)
        return {
            "name": name,
            "faces": faces,
            "volume_m3": volume_m3,
            "volume_mm3": None if volume_m3 is None else volume_m3 * 1e9,
            "is_surface": _safe_flag(body, "is_surface"),
        }

