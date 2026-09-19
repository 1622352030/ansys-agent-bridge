# What was actually verified

Every claim here comes from a run on this machine, not from reading a manual.
Where something is documentation-only it says so. The measured environment:

| | |
|---|---|
| SpaceClaim | 2024 R2 (`AWP_ROOT242`), `backend_version = 24.2.0` |
| `ansys-geometry-core` | 0.17.2 |
| `ansys-fluent-core` | 0.42.1 |
| `mcp` | 1.28.1 (host), 1.30.0 (uv tool environment) |
| Python | 3.13.4 |
| `uv` | 0.11.29 |
| OS | Windows 11 10.0.26200 |

Test model: a 31-body assembly — `stator`, `pip`, `inlet`, `outlet`, and
`winding 1`…`winding 27`. Total 846 faces, total volume `0.007491994003 m³`.
Source files were opened read-only; their SHA-256 was unchanged afterwards.

## End-to-end MCP run

Over the real stdio transport, from `initialize` to `scdm_session_close`:

| Step | Result |
|---|---|
| `tools/list` | 11 tools, read-only hints correct on all 11 |
| `ansys_bridge_doctor` | `ok=true`, detected release `242`, three package versions |
| `scdm_session_start` | connected, `BackendType.SPACECLAIM`, `24.2.0`, **14.2 s** |
| `scdm_open_file` | 31 bodies, 5 named selections, stator 382 faces / `0.003314505208 m³` |
| `scdm_collisions(all_pairs)` | 465 pairs — 435 `NONE`, 30 `TOUCH` |
| `scdm_boolean(unite, stator ← winding 1)` | **846→838 faces, 31→30 bodies**, stator 382→390 faces, volume → `0.003382973513` |
| `scdm_boolean(subtract, stator ← winding 1)` | **`error: no_geometry_change`** — correct |
| `scdm_share_topology` | **refused** — returned `True`, design unchanged |
| `scdm_export(step)` | 1 344 596 B written to a new path |
| `scdm_session_close` | closed, licence released |

The two failures are the point of the server: both return success from
SpaceClaim and are reported as failures here.

## Three bugs this run found and fixed

### 1. A hung tool call, from a lazy import

`scdm_session_start` never returned when `ansys.geometry.core` was imported
inside the tool. `py-spy dump` showed the main thread wedged in numpy's C
extension:

```
_call_with_frames_removed (<frozen importlib._bootstrap>:488)
create_module (<frozen importlib._bootstrap_external>:1321)
<module> (numpy\_core\multiarray.py:11)
...
start (ansys_bridge_mcp\scdm.py)
scdm_session_start (ansys_bridge_mcp\server.py)
_handle_message (mcp\server\lowlevel\server.py)
```

No exception, no timeout, and the client sees only a call that never returns.
The same import finishes in **0.9 s** at process start. FastMCP runs synchronous
tools **on the event loop thread** (`func_metadata.py`: `return fn(**args)`, no
`to_thread`), so the import happens while the server is already serving.

Fix: `main()` imports numpy and both Ansys clients before accepting a request.
`ANSYS_BRIDGE_PRELOAD=0` restores lazy behaviour.

### 2. Every diff read backwards

`GeometrySnapshot.diff` treated its own state as the later one, and
`assert_changed` called `after.diff(before)`, so a working `unite` was reported
as *"faces 838→846, winding 1 added"* — every number individually correct and
the sentence wrong. `assert_changed` now calls `before.diff(after)`, and `diff`'s
parameter is named `after`.

### 3. The guard certified the silent `subtract`

The guard asked "did anything change?". With `keep_other=False` the tool body is
deleted, so 31 bodies became 30, the diff was non-empty, and the `subtract` that
left the stator at exactly 382 faces and `0.003314505208 m³` was reported as
**success**. The one measurement the module exists to protect was the one it
missed.

Fix: a boolean is judged by **its target**. `assert_changed` takes `targets`;
a target that survives with the same face count and volume is the no-op,
whatever the body count says.

## Geometric inspection

Added after the coverage comparison showed eight `RepairTools.find_*` methods
carry no version gate at all — available on 24R2 and the only official way to
diagnose a meshing failure. All eight now sit behind one tool,
`scdm_inspect_geometry`, bringing the server from 11 tools to 12. The 11-tool
results above are left as recorded for that build.

Measured over MCP stdio on `zhuangpeiti_fix_9_10_1.scdoc` (31 bodies, 738
faces), the model whose Fluent Meshing run died at Describe Geometry /
computing regions with **Found overlapping faces**:

| Check | Result |
|---|---|
| `duplicate_faces` | 2 groups / 4 faces |
| `short_edges` | 675 below 10 mm (threshold scanned) |
| `small_faces`, `missing_faces`, `split_edges`, `stitch_faces`, `extra_edges` | 0 |

The duplicate groups name the cause:

```
stator 0:41501   0.09292831069318609 m2   +   pip 0:15387   0.09292831069318609 m2
stator 0:41504   0.12176813125314039 m2   +   pip 0:15396   0.12176813125314039 m2
```

Two pairs of coincident faces, one on `stator` and one on `pip` in each pair,
with identical areas. The whole run takes 4.2 s and the source file's SHA-256 is
unchanged.

The 675 short edges break down as 459 on `stator` and 8 on each of the 27
windings, median length 9 mm and minimum 1.05 mm.

### Two results the tool refuses to present as clean

- **`find_inexact_edges` returned 3348 groups with every `edges` list empty.**
  `object_count` was 0, so the 3348 carries no information. The tool tags it
  `unreliable` and excludes it from the default check set; asking for it
  explicitly still runs it, so the claim is checkable.
- **162 of those 675 edge lengths could not be read.**
  `Edge.length` raises `ValueError: The norm of the 3D vector is not valid.`
  from inside PyAnsys. The `span` block therefore reports `measured: 513`,
  `objects: 675`, `unreadable: 162` rather than a range that looks like it
  covers all 675.

### Payload size

The first version returned every problem group. That produced a 148 KB result,
95 KB of it the single `short_edges` check — too much to hand a model for what
is really a histogram. Groups are now capped at 20 with `groups_omitted` stating
the remainder, while the counts, the per-body histogram and the numeric span stay
uncapped, so nothing that changes a decision is dropped. The same report is now
7.7 KB locally and 9.5 KB over the wire.

## Measurement, transforms and merging

The last three tool groups, all measured over MCP stdio. The server went from 12
tools to 15.

| Tool | Evidence |
|---|---|
| `scdm_min_distance` | 5 pairs, and every distance agrees with the collision state: `stator`↔`winding 1` and `stator`↔`pip` are 0.0 m (`TOUCH`), `stator`↔`inlet` and `stator`↔`outlet` are 0.05036119537898199 m, `winding 1`↔`winding 2` is 0.2072241239859292 m |
| `scdm_insert_file` | 31 bodies → 62 in 0.6 s, component `定转子装配(1)`, `list_bodies` confirms the same count |
| `scdm_transform` | `scale(1.5)`: volume 0.0033145600685288304 → 0.011186640230753733, ratio 3.375 = 1.5³ exactly. `rotate` z 90°: fingerprint x/y moved, z unchanged. `mirror`: verified |

Five error paths were exercised and each reports a specific message: unknown
operation, missing `scale_factor`, non-positive factor, unknown body, invalid
axis. Source file SHA-256 unchanged after all of it.

### The value in `Distance` is one level deeper than it looks

`min_distance_between_objects` returns a `Gap` whose `distance` repr already
reads `0.05036119537898199 meter`, but `Distance` has no `magnitude` — reading it
raises `AttributeError`. The quantity is at `.value`. Same family of trap as
`UnitVector3D.x` being a bare `float64` while `Point3D.x` carries a `Quantity`.

### A normal is not a direction

`Plane(origin, direction_x, direction_y)` does not take a normal. Measured:
`Plane(Point3D([0,0,0]), UnitVector3D([0,0,1]))` produces a plane whose normal is
`[-1, 0, 0]`, because the second argument is `direction_x` and the normal is the
cross product of the two directions. Passing a normal there **silently mirrors
about the wrong plane** — no error, wrong geometry. The bridge derives both
directions from the normal and the construction is unit-tested against x, y, z,
`[1,1,0]`, `[1,1,1]` and a negative normal.

### A tolerance that sat below the measurement noise

The guard that catches the silent `subtract` compared volumes with a relative
tolerance of `1e-6`. Reading the same stator volume in two sessions gave
`0.00331450520811706` and `0.0033145600685288304` — a spread of **1.7e-5**, which
is larger than the tolerance meant to absorb it. The guard worked in the runs
above by luck of the draw, not by design.

Raised to `1e-4`, which is still two orders of magnitude below a real boolean
change (`unite` moves the stator volume by 2.1e-2). Regression-checked against
three cases: a real change passes, a no-op with identical volumes is caught, and
a no-op carrying that 1.7e-5 service noise is now caught instead of slipping
through.

## Cross-checked against raw IronPython

The same `.scdoc` was read and written through the wild
`SpaceClaim.exe /RunScript=... /Headless=True` path. Body names, face counts
(`stator` 382, `winding 1` 16), volumes, and named selections agree exactly, as
does `unite` moving the stator to 390 faces. That agreement is what makes the
measurements above trustworthy rather than an artifact of one API path.

## Documentation-only

- `.scdocx`, STEP, IGES, Parasolid text/binary and `.pmdb` exports are all
  implemented through the official methods; only STEP was verified end to end.
- `scdm_run_script` was exercised; the IronPython traps in
  `spaceclaim/skills/ansys-spaceclaim/SKILL.md` come from the raw path.
- The Fluent side of the tool list is not implemented yet — `ansys-fluent-core`
  is a dependency so the client is importable, but no Fluent tools are exposed.
  See `fluent/docs/mcp-audit.md` for what the recommended alternative covers.
