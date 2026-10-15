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

`spaceclaim/python/tests/test_scdm_tools.py` — **130 tests**, same offline rules,
added with the creation tools. They cover the seven defects above plus the bugs
found while fixing them: the `script_path` / `script` split; the export file name
being the caller's; `bodies_checked` / `faces_checked` matching `scdm_list_bodies`
for the same design (a three-way invariant: `session_status.body_count` ==
`list_bodies.body_count` == `inspect_geometry.bodies_checked`); the creation route
generating `Body.CreatePlanarBody(..., removeInnerRegions=True)` +
`ExtrudeType.ForceIndependent` with **no** boolean and `MM()` on every length; the
section-area guard refusing the silently-dropped-bore disc; the FAIL-tag
classification of every user-visible script failure; and, because both cost a live
debugging round each, two structural invariants of the generated scripts — no
`{placeholder!r}` inside quotes (`SIDE = ''xy''` is a syntax error that kills the
script *before* it can report why) and exactly one `_guard(_body)` call
(`_guard(_body)()` calls `None` and does the same thing).

Also verified over the real stdio transport and offline:
`_diag/scdm_gui_modes_probe.py` lists the tools, `ansys_bridge_doctor` `ok=true`,
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

## 创建类工具与实测出的七个缺陷（2026-10-15）

这一节是在一次"只用 `scdm_*` 工具从零建水管"的任务之后补的，那次任务把本节里的每个缺陷
都复现了。会话是**用户自己启动的** SpaceClaim 24.2.0，attach 模式，`port=51790`。
相关的三份实机产物：`_diag/scdm_pipe_modeling.md`（缺陷报告）、
`_diag/scdm_tools_fix/`（原始探测日志）、`_diag/scdm_tools_fix.md`（每条毛病的处理）。

### 跑通的建模路线，以及为什么是这一条

```python
# 每个圆用两段半圆弧。单个 CreateArc 在 start == end 时会被内核拒绝。
plane = Plane.PlaneXY                      # 必须是静态平面，见下
profile = List[ITrimmedCurve]()
for r in (r_out, r_in):
    a = point_along(centre, along, MM(r))
    b = point_along(centre, along, MM(-r))
    profile.Add(CurveSegment.CreateArc(centre, a, b, normal))   # 要真正的 Direction！
    profile.Add(CurveSegment.CreateArc(centre, b, a, normal))
raw = Body.CreatePlanarBody(plane, profile, False, False, True)  # removeInnerRegions=True
db  = DesignBody.Create(root, name, raw)
res = ExtrudeFaces.Execute(Selection.Create(db.Faces[0]), DIR, MM(length), opts, None)
```

**整条路线里没有任何布尔运算。** `Body.subtract` 正常返回、删掉工具体、目标体原封不动，
所以在这个内核上没法用"实心柱减实心柱"造管子。

### 支撑这条路线、且都实测过的 API 事实

| 事实 | 证据 |
|---|---|
| `Plane.Create(...)` 用不了，只有三个**静态**平面行 | 试过的每个重载都从 interop 绑定层抛裸 `TypeError`：点+两个方向、4 参数、`Frame` 形式。`Plane.PlaneXY` / `PlaneYZ` / `PlaneZX` 构造正常。 |
| `Matrix.CreateTranslation(...)` 同样用不了 | `MM()` 包过的和裸参数都抛 `TypeError`；`Matrix.Identity` 可用，但 `Matrix.Translation` 是**只读**的，赋不进去。所以截面直接在最终圆心处建，不靠移动。 |
| `CurveSegment.CreateArc` 要真正的 `Direction` | 传 `(0.0, 0.0, 1.0)` 抛 `TypeError`；`Direction.Create(0.0, 0.0, 1.0)` 和 `Direction.DirZ` 都可用。这一条花掉了一轮排查。 |
| `DesignFace` 没有 `Id`、`IsPlanar`、`SurfaceType`、`Body`、`Loops`、`Geometry` | 这些全抛 `AttributeError`。存在的是 `Area`、`Perimeter`、`Edges`、`Moniker`、`GetFaceNormal`。而 `EvalMid`、`EvalProportion`、`GetFaceNormal()` 都抛 `TypeError`，`GetFaceNormal` 要一个无法从 `BoxUV`/`BoundingBox` 构造的 `PointUV`。 |
| `Body.GetName()` 会抛 `UnicodeEncodeError` | SpaceClaim 自动命名成中文时，裸写 `str(b.GetName())` 会炸。生成的代码里每个名字读取都走 `.encode('ascii', 'replace')`。 |
| `ExtrudeFaces` 的 `CreatedBodies` **是空的** | 在"文档里已存在的体的某个面"上，四种 `ExtrudeType` 实测全部返回空，而几何确实变了。`scdm_extrude` 靠按对象身份对比文档来找出创建了什么。 |
| `ForceIndependent` 有时会**并进目标体**而不是新建 | 在空心管上对 235.6194 mm² 的环形面拉 5 mm，源体增了 1178.081 mm³（没有变成新体）；同一个调用在平面圆盘上却新建了一个体。所以结果并不只由 `extrude_type` 决定。 |
| 脚本失败时**什么都返回不了** | `run_script_file` 返回 `response['values']`，脚本在建立 `result` 之前抛异常，调用方只能拿到 `GeometryRuntimeError("Script run failed.")`。所以生成的脚本先建 `result = {}`、自己 catch 自己的异常并把原因写进 `result['failed']`——而多出来的一个 `_guard(_body)()` 就足以打破这个保证。 |
| `GeometryExitedError.__str__` **会重新抛** | 在处理块里对捕获到的异常调 `str(e)`，把处理块和整个探测脚本一起弄死了。生成的代码改成记 `type(e).__name__`，原因从标记里读。 |

### 一个客户端看不见 design 的活动会话

这是实测出来的，它改变了若干工具能承诺什么。用户的 SpaceClaim 开着文档时，**attach** 会话表现如下：

| 调用 | 结果 |
|---|---|
| 原生 `DocumentHelper.GetRootPart()` | 看得到文档，能列出它的实体 |
| `modeler.get_active_design()` | **失败** |
| `modeler.read_existing_design()` | **失败**（`"There is no opened document."`） |
| `scdm_session_status` | `body_count: null`、`design_visible: false` |
| `scdm_list_bodies` | `body_count: 0`、`design_visible: false`，并附说明：这**不是**"文档是空的" |
| `scdm_create_pipe` / `scdm_extrude` | **可用**：脚本通道够得到那个打开的文档 |
| `scdm_export` | 可用（实测 STEP、IGES、Parasolid 三种都落了字节） |

attach 会话下 **`create_design()` 被拒绝**，因为它会关掉它认为"当前活动"的 design，
而那可能是用户没保存的工作。launch 会话可以创建，因为进程是本服务自己起的。

## 2024 R2 上实测到的建模期陷阱

### `removeInnerRegions=False` 丢掉内环且不吭声

`Body.CreatePlanarBody(..., removeInnerRegions=False)` 正常返回，你要环、它给你一个纯**外圆盘**。
什么都不抛。信返回值的人会造出实心棒并汇报成管子。

> 建模报告里写的是那个圆盘 `1962.872 mm2`、环 `706.634 mm2`。这两个印刷值**都不对**：
> `pi*25^2` 是 **1963.495**，`pi*(25^2-20^2)` 是 **706.858**。报告记录的那个*行为*没有问题
> ——只有这两个数字错了；而守卫是拿解析值和实测值比，所以两种写法下它都抓得住静默失败。

`scdm_create_pipe` 因此会把截面面积读回来，不是期望的环面积就拒绝，并在拿到外圆盘时
明确点出这一步。拉伸体积同样按 `area * length` 校验。

### PointUV 那个重载在 ACIS 上没实现

`Body.CreatePlanarBody(plane, outerLoop: ICollection[PointUV], innerLoops)` 抛
`AcisModeler::CreatePlanarBody Not implemented for ACIS.` 这是内核限制，不是参数写错，
改参数没用。本服务把它翻译成"改用哪个重载"的可操作指引；`acis_guidance` 对其它异常返回
`None`，所以真实错误不会被误标成内核限制。

### 整圆必须用两段半圆

`CurveSegment.CreateArc(centre, p, p, axis)` 在 `start == end` 时抛 `Invalid profile.`。
对径两点之间的两段弧可用，得到的截面面积和解析环面积在 12 位有效数字上一致。

### 用线段逼近的圆会被判成短边

用 **360 边形**画圆截面的管子报了 1440 条短边，长度 0.3491–0.4363 mm——正好是
`2*20*sin(pi/360)` 和 `2*25*sin(pi/360)`，每个端面 720 条。`scdm_inspect_geometry`
现在会给它们**归因**：长度高度聚集（CV 0.111，max/min 1.25），这是一个重复弦长家族的
特征，而不是散乱的碎片；结果会这么说，而不只是给一个 `clean: false`。
改成每圆两段真弧之后，这个计数降到 **0**，这也就是那个归因正确的交叉验证。

## 未解决

### `Profile` 类型没有构造入口

`Body.ExtrudeProfile(profile: Profile, distance)` 和 `Body.SweepProfile(profile, path)`
看起来是更正统的入口，但两个都调不了：`Profile` 没有 `Create` 静态工厂，也没找到别的构造途径，
所以拿不到 `Profile`。**没解决。** 值得后续挖——真的能用上 `Profile` API 的话，
很可能能去掉 `scdm_extrude` 现在背着的"按面序号选"这个限制。

### `DesignFace` 无法被识别也无法判定类型

见上面的表。它给 `scdm_extrude` 留下的是一条硬限制：`face_indices` 是 0 基的**位置**，
不是稳定 id；而且没有任何办法问"这个面是不是平面"——所以没有"拉伸所有平面"的默认行为，
工具宁可拒绝也不猜。如果哪天找到可用的 `IsPlanar` / `Id` 路径，
序号要求和 `direction` 必填这两条都可以变成可选。

### 导出扩展名由内核决定，不由调用方决定

`format="step"` **写出的文件以 `.stp` 结尾**，不是 `.step`。传进去的路径里写 `.step`
也没有用 —— 内核按自己的格式表落盘，调用方说了不算。每种格式的真实后缀登记在
`scdm.EXPORT_SUFFIXES` 里（`step` → `.stp`、`iges` → `.igs`、`parasolid_text` → `.x_t`、
`parasolid_bin` → `.x_b`），返回值里带 `expected_suffix` 与 `suffix_matches_format`
如实说明这一点。

**因此返回值必须回报实际落盘文件的绝对路径**，不能回报调用方传进来的那个路径 ——
这也是本条与"`location` 被当目录"（同一处代码）一起修的原因：那个 bug 的后果之一正是
**调用方拿不到文件到底在哪**。现在 `scdm_export` 的 `path` 永远是磁盘上真实存在的文件
（实测：`output_dir=...` + `file_stem="verify_pipe"` + `format="step"` →
`...\verify_pipe.stp`，19614 B，扩展名按内核的 `.stp` 而不是传的 `.step`）。

### 为什么同一次会话里导出会先失败后成功

实测到过一次：attach 会话下 STEP 导出成功 8631 字节，而 IGES 和 Parasolid 都报 `no_design`。
但六种格式走的是**同一个**库调用
（`Design.download(file_location, format)` → `designs.download_export`），
所以本服务里没有按格式分叉的代码路径，也没有按格式的前置条件。重测时，同一次会话上
三种格式全部导出成功。**机制没有查清。** 改变的是消息：现在的拒绝码是
`attach_export_needs_visible_design`，说的是会话而不是格式的问题，并给出 STEP 这个
实测可用的退路，而不是留一个读起来像"会话坏了"的裸 `no_design`。

## 下游：中文名传不到 Fluent

另一个任务实测：把 SpaceClaim 的 STEP 导入 Fluent Meshing 时，导入打印 `Warning: ?????????`，
**三个命名集合全部丢失**，网格里没有可用的命名选择。几何到了，名字没到。

**模型要交给 Fluent Meshing 时，不要在 SpaceClaim 侧用中文命名图层或命名选择。**
用 `inlet`、`outlet`、`wall`、`fluid` 这种 ASCII 名字，选择就能保住。
这件事值得在 CAD 阶段之前就知道，因为下游每一次选择都依赖这些名字。
