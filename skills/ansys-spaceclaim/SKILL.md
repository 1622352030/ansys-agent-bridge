---
name: ansys-spaceclaim
description: Open, inspect, modify, and export Ansys SpaceClaim geometry headlessly through the ansys-agent-bridge MCP server, and hand the result to Fluent. Use when a task involves .scdoc/.scdocx models, SpaceClaim bodies, named selections, CAD booleans, share topology, CAD export, or the handoff from CAD to a Fluent Meshing/CFD run on 2024 R2 or later. Carries measured behaviour and known silent-failure modes, not a restatement of the manuals.
---

# Ansys SpaceClaim through ansys-agent-bridge

## What this skill is for

Driving SpaceClaim from an agent has one dominant hazard: **several operations
report success and change nothing.** A return value of `True` from
`share_topology`, or a normal return from `Body.subtract`, is not evidence that
the geometry moved. This skill exists to make you check the geometry instead of
the return value.

Every fact below was measured on SpaceClaim 2024 R2 (`AWP_ROOT242`) through
`ansys-geometry-core` 0.17.2. Where a claim is documentation-only rather than
measured, it says so.

## Prerequisites and honest limits

- The bridge is an MCP server. Its tools are named `scdm_*` and
  `ansys_bridge_doctor`. If you do not see them, call `ansys_bridge_doctor`
  first; it reports the detected Ansys roots, the Python in use, and the three
  package versions, without importing anything heavyweight.
- A SpaceClaim session holds a licence. Call `scdm_session_close` when you are
  done, and never leave a session open across a long idle period.
- **Never open a file the user has open in a SpaceClaim GUI, and never write to
  a source model.** Open, operate, and export to a new path.
- Only Windows is known to work. `ansys-geometry-core` supports Linux for the
  service backends, but the `SpaceClaim` backend this skill relies on does not.

## Workflow

1. `ansys_bridge_doctor` — confirm the environment before blaming your own call.
2. `scdm_session_start` — start a hidden modeler. **Measured: 30.6 s** to reach
   `backend_type = SPACECLAIM`, `backend_version = 24.2.0`. Budget for it. Pass
   a version only when auto-detection picks the wrong install.
3. `scdm_open_file` — opens `.scdoc` directly, not just the documented
   `.scdocx`/`.dsco`/`.pmdb`. **Measured: a 3.11 MB assembly opens in 0.9 s**
   and reports bodies, named selections, per-body face counts, and volumes.
4. Operators — `scdm_list_bodies`, `scdm_collisions`, `scdm_boolean`,
   `scdm_share_topology`, `scdm_run_script`.
5. `scdm_export` — `.scdocx`, `step`, `iges`, `parasolid_text`,
   `parasolid_bin`, `pmdb`. There is no native `.scdoc` export.
6. `scdm_session_close`.

## The two silent-failure modes

### `subtract` fails on this assembly

Measured on a stator + 27 windings + pipe + inlet assembly (31 bodies, 846 faces
in total):

| | stator volume (m³) | stator faces | bodies | total faces |
|---|---|---|---|---|
| before | 0.003314505208 | 382 | 31 | 846 |
| after `subtract(stator, winding 1)` | 0.003314505208 | 382 | 30 | 830 |

The stator is untouched. The only movement is the body count, 31 → 30, because
`keep_other=False` deletes the tool body — while `unite` in the same state
leaves the stator at 382 → 390 faces and volume `0.003382973513`.

**The trap is the guard, not the call.** "Did anything change?" answers *yes*
here, so a check built on the body count or on a non-empty diff certifies a
no-op. Judge a boolean by **its target**: same face count and same volume on the
named target means the kernel declined to do the work.

The same symptom appears through raw IronPython `Shape.Subtract`, which rules
out the API path — this is a kernel/geometry problem with that model, not a
bridge bug. The bridge reports `error: "no_geometry_change"` on purpose.

**Do not retry it in a loop and do not proceed as if the geometry changed.**
For a multi-part assembly the route that works is Non-Conformal + Mesh
Interface in Fluent Meshing, not a fused boolean in the CAD kernel.

### `share_topology` returns `True` and does nothing

Measured on the same assembly: the call returns `True`, and stator faces stay
382, winding 1 stays at 16, bodies stay at 31. **A `True` return is not
evidence.** Compare face and body counts before and after. The bridge raises
the same `no_geometry_change` error when the reported success produced no
change.

## What does work

`unite` genuinely modifies geometry and is the reliable operator here:

| | stator volume (m³) | stator faces | bodies | total faces |
|---|---|---|---|---|
| before | 0.003314505208 | 382 | 31 | 846 |
| after `unite(stator, winding 1)` | 0.003382973513 | 390 | 30 | 838 |

The volume delta is `6.84683e-5` — exactly winding 1's own volume — and the
stator's face count rises by 8, matching the raw IronPython result. Judge a
boolean by that kind of evidence.

Note the arithmetic, because it is a good sanity check: the stator gains 8
faces and the tool body leaves with its 16, so the *total* falls by 8 even
though the target grew. A report that shows the total rising is describing the
wrong direction.

From the raw IronPython side, `Shape.Imprint` over the 27 windings took the
stator from 382 to 490 faces while leaving the body count at 31. That is the
better operator when you want face-level contact without fusing bodies.

`get_collision` works and is worth calling before any boolean:
`stator × winding 1 → TOUCH`, `stator × pip → TOUCH`,
`stator × inlet → NONE`, `winding 1 × winding 2 → NONE`,
`pip × winding 1 → NONE`.

## Judging a change

- Compare **the target's face count and volume**, not the return value and not
  the body count. A boolean with `keep_other=False` always changes the body
  count, so that number cannot tell success from failure.
- `body.Shape` is a snapshot, so `body.Shape.Volume` is **stale** after a
  boolean. Re-read the body rather than trusting a cached value.
- The bridge's guards do this comparison for you and raise
  `no_geometry_change` when the named target did not move. Treat that as the
  honest answer.
- Read the `bodies_changed` list, not just the counters. It names which bodies
  moved and in which direction.

## Raw IronPython escape hatch (`scdm_run_script`)

For anything the curated tools do not cover, run a headless IronPython script.
SpaceClaim embeds IronPython 2.x, and the following were all measured failures
of the obvious spelling:

- `Body[](n)` is a **parse-time error** — the script dies silently with **no
  output at all**. Use `List[Body]()`.
- `Document.Load(SRC)` performs a bare load, so any later `SaveAs` raises
  `SystemError('序列不包含任何元素')`. Use
  `Document.Open(SRC, ImportOptions.Create())`.
- Passing a Python `list` raises `expected ICollection[Body], got list`.
- `print(some objects)` raises `UnicodeEncodeError` and **aborts the script**.
  Print strings, and keep the script pure ASCII.
- The usual launch is
  `SpaceClaim.exe /RunScript=<file> /Headless=True /ExitAfterScript=True`.

## Handoff to Fluent

- Fluent Meshing needs `AddChildAndUpdate()`; a bare child add does nothing.
- Multi-part assemblies: Non-Conformal + Mesh Interface, **not** Share
  Topology. Keep the interface cell-size ratio below 2.
- A truthful `self-intersections ≈ faces × 0.38` report across all zones is
  mating-face count, not a defect. `Dihedral limit violations → undoing join`
  is a normal response.
- Quality gates before solving: max skewness < 0.95 (target < 0.8), min
  orthogonal quality > 0.05 (target > 0.1), max aspect ratio < 100 (< 50 in
  boundary layers), zero free faces.
- Volume heat source order is energy → `sources.enable` → `terms.energy` as a
  list. `[C]` in a Fluent expression is absolute temperature.
- PyFluent has no geometry boolean at all — no `bool`/`subtract`/`union` in
  `PartManagement` or `m.meshing`. Geometry belongs to this server.
- `processor_count` defaults to 1. Set it explicitly.
- Judge CHT convergence by monitors — inlet/outlet mass-flow difference under
  0.1 % and a stable outlet temperature — not by residual values.

## Reporting

State what was measured and what was reported. When a tool returns
`no_geometry_change`, say so and name the alternative route; do not describe
the design as modified.
