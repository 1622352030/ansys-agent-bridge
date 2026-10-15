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

### A tolerance that was justified by a wrong measurement

An earlier version of this section claimed the guard's `1e-6` volume tolerance
sat below real read-to-read noise, quoting `0.00331450520811706` and
`0.0033145600685288304` as "the same stator volume read in two sessions".

Those two numbers are **two different models**. `assembly31.scdoc` has 382 stator
faces and volume `0.00331450520811706`; `zhuangpeiti_fix_9_10_1.scdoc` has 274
stator faces and volume `0.003314560068528831`. Their volumes differ by 1.7e-5
relative, but that is a difference between files, not noise inside one. No
measurement of read-to-read wobble was ever taken, so the old justification was
an invented measurement.

The tolerance stays at `1e-4`. It is two orders of magnitude below a real boolean
change (`unite` moved the stator volume by 2.1e-2), and erring wide is the safe
direction for a guard whose whole job is to catch a no-op. What changed is the
stated reason, not the value. The regression cases reported below still hold.

## Cross-checked against raw IronPython

The same `.scdoc` was read and written through the wild
`SpaceClaim.exe /RunScript=... /Headless=True` path. Body names, face counts
(`stator` 382, `winding 1` 16), volumes, and named selections agree exactly, as
does `unite` moving the stator to 390 faces. That agreement is what makes the
measurements above trustworthy rather than an artifact of one API path.

## Two GUI modes: who owns the SpaceClaim window

Added 2026-10-15, aligned with the Fluent side of this bridge (`ui_mode="gui"` by
default there, and a separate `fluent_attach` / `fluent_detach` pair). Every
session now reports which mode it is in, as a machine-readable field:

| | mode=launch | mode=attach |
|---|---|---|
| Tool | `scdm_session_start` | `scdm_attach` |
| Who created the process | this server | the user |
| Licence | one is consumed by this server | none is consumed; the user's own licence is reused |
| `process_ownership.owner` | `ai` | `user` |
| How a session ends | `scdm_session_close` | `scdm_detach` (channel only) |
| The other tool in this column | `scdm_detach` → **refused** (`detach_requires_attach`) | `scdm_session_close` → **refused** (`attach_mode_refuses_close`) |

The two are separate tools rather than one tool with a flag, for the same reason
as on the Fluent side: the licence and ownership semantics are opposites, and
getting the wrong one ends a user's session. A switch would have to mean both
"release the licence" and "do not release the licence".

### I drive: `hidden=False` by default, and it is a real window

`hidden` now defaults to **false** (`scdm.py` `Session.start`, `server.py`
`scdm_session_start`). Measured on this machine over the **real MCP stdio
transport** (evidence: `_diag/scdm_launch_visible_verify.py` and its `.out`):

| Observation | Value |
|---|---|
| `scdm_session_start(hidden=False)` result | `ok=true`, `mode="launch"`, `ui_visible=true` |
| Launch to a usable gRPC endpoint | **21.8 s** |
| Visible main window | `设计(D)1 - SpaceClaim`, rect `[-1927, 90, 1935×1047]`, `IsWindowVisible=true`, not minimised, pid 34800 |
| Memory while running | working set **979.2 MB**, private **1499.5 MB** |
| `scdm_session_close` | `closed=true, process_untouched=false, licence_released=true` |
| Residual SpaceClaim processes | **0** |
| Verdict the script computes for itself | `PASS` |

Two things worth keeping in mind, both measured:

- **`hidden=True` is not a minimised window.** PyAnsys maps it to `/Headless=True`
  (`product_instance.py:396-400`), which Ansys documents as batch mode *without UI*.
  Measured, of 40 top-level windows the only visible ones were two 13×13 untitled
  stubs, with no main window and no splash screen; it is also quicker and lighter
  (23.47 s, 885–888 MB) precisely because there is no UI to draw.
- **The visible window can land on a secondary monitor.** Both visible launches on
  this machine put it at `x = -1927`, the negative-coordinate second display,
  because placement follows what the system last remembered. PyAnsys does not
  expose window placement, so pinning the primary monitor would mean appending
  `/WindowLocation=` or `/WindowMaximized` to the executable arguments by hand.
  **Not measured** — listed below.

### You drive: `scdm_attach`, and a plain double-click is not enough

`scdm_attach(host="localhost", port=51790, transport_mode="wnua", timeout_s=30)`
connects to a SpaceClaim the user started. Measured basis (report
`_diag/spaceclaim_gui_modes.md`, whose raw logs are `attach_probe.txt`):

- A SpaceClaim started by a **plain double-click has no gRPC endpoint at all**.
  Measured: it listened on 49013/49014/63186 and **all three** answered
  `StatusCode.UNAVAILABLE` to a gRPC health check. The official documentation says
  the same in prose: "Running a SpaceClaim or Discovery normal session does not
  suffice to be able to use it with PyAnsys Geometry. Both products need the
  ApiServer extension to be running."
- Started **with** the add-in and a fixed port, the same thing connected in
  **0.31 s**, reporting `BackendType.SPACECLAIM` / `24.2.0` /
  `has_product_instance=false`, and **the user's process was still alive after
  `close()`**.

`transport_mode` is always passed, never defaulted (since PyAnsys 0.14
`wait_until_healthy` raises `ValueError` when it is `None`,
`connection/client.py:173-182`), and `product_instance` is deliberately **not**
passed — that field is what decides whether the library may shut the backend down
(`connection/client.py:493-499`). Only `wnua` (Windows Named User Authentication:
the local user *is* the credential, so no password and no certificate) has been
measured working here; the client also accepts `insecure`, `mtls` and `uds`
(`ansys/tools/common/cyberchannel.py:158-172`).

The port is a **convention, not a discovery mechanism**: SpaceClaim has no
equivalent of Fluent's `-sifile=`, and PyAnsys picks a random free port when
`API_PORT` is unset (`product_instance.py:600-612`, `:775-789`). Hence the fixed
51790 below.

### The user-side launcher

`spaceclaim/dev/launchers/start_spaceclaim_for_ai.bat` (ASCII, CRLF), installed on
this machine as the desktop shortcut **`SpaceClaim 2024R2 (AI可接入).lnk`**
(created with `WScript.Shell`, then read back and verified: target = the `.bat`,
working directory = `spaceclaim\dev\launchers`, icon = SpaceClaim's own; the user's
existing `SpaceClaim 2024 R2.lnk` was left untouched):

```bat
set API_PORT=51790
set API_ADDRESS=localhost

start "" "C:\Program Files\ANSYS Inc\v242\scdm\SpaceClaim.exe" /ADDINMANIFESTFILE="C:\Program Files\ANSYS Inc\v242\Addins\ApiServer\Presentation.ApiServerAddIn.Manifest.xml" /transport-mode=wnua
```

The environment variables live in the `.bat` rather than in the shortcut because a
Windows shortcut cannot set environment variables — the same constraint the Fluent
side hit with its `-sifile=` launcher. User-side flow:

1. double-click the shortcut, wait for the window (the same ~20–30 s as any start);
2. the AI side calls `scdm_attach(port=51790)`;
3. the AI side ends with `scdm_detach` — never `scdm_session_close`, which is refused;
4. to get rid of the AI, close SpaceClaim: the channel dies with it.

There is **no** "kick the AI off" console command on the SpaceClaim side. Fluent
has `server/shutdown-server`; no equivalent was found here. Closing SpaceClaim is
the available action.

### `/ADDINMANIFESTFILE` and `API_PORT` are a semi-public interface

This is the main long-term risk of the attach mode and must not be buried:

- They are **not** in the public Ansys command-line documentation. The 242 option
  table lists `/Headless`, `/RunScript`, `/Splash`, `/Window*` and so on — not
  these.
- They appear in the **PyAnsys Geometry source**:
  `API_PORT` at `product_instance.py:92`, and the add-in manifest at
  `product_instance.py:113-117` and `:401-404`.
- And in measurement: the 2026-10-15 run above.

So a future release may change or remove them. When `scdm_attach` fails it says so,
in the message, together with the exact command line to paste.

### Offline tests

`spaceclaim/python/tests/test_gui_modes.py` — **70 tests**, `pytest`, no ANSYS, no
licence, no network. They use a fake PyAnsys client that records what it was asked
to do, because the substance of this feature is *which* calls the server is willing
to make. Covered: the `hidden` default and its truthful reporting; attach passing
`transport_mode` while omitting `product_instance`; the licence / ownership blocks;
port, host and transport-mode validation **before** any network call; attach
refusal on the wrong backend with the channel handed back; the `close`/`detach`
guards firing before the library is reached (asserted by "the fake modeler's
`close()` was never called"); and the attach failure text carrying
`set API_PORT=51790`, `/ADDINMANIFESTFILE=`, `/transport-mode=wnua`.

Also verified over the real stdio transport and offline:
`_diag/scdm_gui_modes_probe.py` lists **17 tools**, `ansys_bridge_doctor` `ok=true`,
`scdm_detach` / `scdm_session_close` as safe no-ops without a session, the three
parameter errors as structured results, and `scdm_attach` against a dead port
returning `error="attach_unreachable"` with the full command line.

The `spaceclaim/dev/tests/` scripts are **not** offline unit tests: `dev/README.md`
calls them "31 测试脚本" and they are live runs whose model and log paths predate
the repository split. `api_surface.py` still runs offline and was rerun (81 public
methods on `Design`, 33 on `Body`, and so on); `api_versions.py` needed its output
path redirected (a copy with `geom_test` → `_diag` was run for that reason).

### Not measured — do not treat as verified

1. **Attach was never exercised against a live user-started instance from this
   code.** The measurement above (0.31 s, process survives `close()`) comes from the
   2026-10-15 probe run; `scdm_attach` itself was verified only *offline* (fake
   client) and against a dead port. The user has to be running the launcher for this
   to be tested end to end.
2. **No write operation over an attach session has been measured** —
   `scdm_open_file`, the geometry operators, `scdm_export`, `scdm_run_script`. Only a
   read of `backend_type` / `backend_version` was measured on attach.
3. **The launcher `.bat` / shortcut itself was never run.** It was written from the
   measured command line and read back to confirm its content, but double-clicking it
   has not been done, so "the shortcut produces an attachable instance" is inferred,
   not observed.
4. **The licence counter was not measured.** There is no `lmutil.exe` under this
   machine's `Shared Files\Licensing`, and `ansyslm_report.exe` is not real time. The
   claim "attach consumes no new licence" is a design statement (no process is
   started), not a counter reading.
5. **`/WindowLocation=` / `/WindowMaximized` were not tried**, so moving the window
   to the primary monitor is unproven.
6. **Starting without `/transport-mode=wnua` and still being attachable** was not
   tried; the measured command line included it.
7. **`SPACECLAIM_MODE=2`** (set by PyAnsys at `product_instance.py:125-131`) has an
   undocumented purpose. It was only falsified as a cause of hiding: it is set in
   both modes and the window appears anyway.
8. **Concurrency / two instances on two ports** was not tested.

## Documentation-only

- `.scdocx`, STEP, IGES, Parasolid text/binary and `.pmdb` exports are all
  implemented through the official methods; only STEP was verified end to end.
- `scdm_run_script` was exercised; the IronPython traps in
  `spaceclaim/skills/ansys-spaceclaim/SKILL.md` come from the raw path.
- The Fluent side of the tool list is not implemented yet — `ansys-fluent-core`
  is a dependency so the client is importable, but no Fluent tools are exposed.
  See `fluent/docs/mcp-audit.md` for what the recommended alternative covers.
