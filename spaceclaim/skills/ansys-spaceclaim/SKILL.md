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
- **Two session modes, and they are separate tools.** `scdm_session_start` launches
  a modeler this server owns (`mode=launch`): it holds the licence and ends with
  `scdm_session_close`. `hidden` defaults to **false**, so the user sees the window
  you are driving; `hidden=true` is `/Headless=True` — batch mode with **no UI at
  all**, not a minimised window. To drive a SpaceClaim the *user* started, use
  `scdm_attach` (`mode=attach`): it consumes no licence and `scdm_session_close` is
  **refused** there — disconnect with `scdm_detach` instead, which closes only the
  gRPC channel. Attaching needs the user to have launched SpaceClaim with the
  ApiServer add-in and a fixed `API_PORT` (a plain double-click has **no** gRPC
  endpoint); `spaceclaim/dev/launchers/start_spaceclaim_for_ai.bat` is that
  launcher, and a failed `scdm_attach` prints the full command line to paste.

- **Never open a file the user has open in a SpaceClaim GUI, and never write to
  a source model.** Open, operate, and export to a new path.
- Only Windows is known to work. `ansys-geometry-core` supports Linux for the
  service backends, but the `SpaceClaim` backend this skill relies on does not.

## Workflow

1. `ansys_bridge_doctor` — confirm the environment before blaming your own call.
2. `scdm_session_start` — start a modeler. **Measured: 14–31 s** to reach
   `backend_type = SPACECLAIM`, `backend_version = 24.2.0`. Budget for it. Pass
   a version only when auto-detection picks the wrong install. `hidden` defaults
   to false: the returned `mode="launch"` and `ui_visible=true` are what the user
   will actually see. (Or `scdm_attach` when the user already has one running.)
3. `scdm_open_file` — opens `.scdoc` directly, not just the documented
   `.scdocx`/`.dsco`/`.pmdb`. **Measured: a 3.11 MB assembly opens in 0.9 s**
   and reports bodies, named selections, per-body face counts, and volumes.
4. `scdm_inspect_geometry` — **建网格之前先跑它。** 它把"网格失败了"变成"有个名字的原因"。
   见下面那一节。
5. 算子类 —— `scdm_list_bodies`、`scdm_collisions`、`scdm_min_distance`、
   `scdm_boolean`、`scdm_share_topology`、`scdm_transform`、`scdm_insert_file`、
   `scdm_run_script`。
6. **创建几何** —— `scdm_create_pipe` 和 `scdm_extrude` 是仅有的两个会**新增**几何的工具；
   其余工具都是读模型、或对已存在的体做运算。
7. `scdm_export` —— `.scdocx`、`step`、`iges`、`parasolid_text`、
   `parasolid_bin`、`pmdb`。没有原生的 `.scdoc` 导出。
8. `scdm_session_close` —— launch 会话用它。attach 会话下它会被拒绝，
   那里要调 `scdm_detach`，用户的窗口继续留着。

## 从零创建几何

`scdm_create_pipe(outer_diameter_mm, length_mm, inner_diameter_mm)` 用来造空心管或实心棒。
**手上没有现成几何时先用它**——它走的是实测跑通的那条路线：

```python
Body.CreatePlanarBody(plane, profile, False, False, removeInnerRegions=True)  # 环形截面
ExtrudeFaces.Execute(Selection.Create(section.Faces[0]), dir, MM(L), opts, None)
```

**全程不用任何布尔运算。** 不要用"实心柱减实心柱"造管子：这个内核上 `Body.subtract`
正常返回、删掉工具体、而目标体原封不动。

这条路线里编码了三个实测坑，自己写脚本一定会踩：

- **`removeInnerRegions=False` 会静默丢掉内环。** 不报错，给你一个纯外圆盘
  （环面积 `706.858 mm²` 的地方变成 `1963.495 mm²`），你要管子它给你实心棒。
  所以拉伸之前**必须把截面面积读回来**，和 `π(r_out² − r_in²)` 比。
- **整圆必须用两个半圆拼。** `CurveSegment.CreateArc(centre, p, p, axis)` 在
  `start == end` 时报 `Invalid profile.`。取一对对径点，A→B 一段弧、B→A 再一段。
- **`Plane.Create(...)` 和 `Matrix.CreateTranslation(...)` 用不了。** 两者都从
  interop 绑定层抛裸 `TypeError`。只有三个**静态**平面
  （`Plane.PlaneXY` / `PlaneYZ` / `PlaneZX`）构造得出来，所以截面直接在最终圆心处建，
  而不是建好再移动。

`scdm_create_pipe` 会回报期望值、实测值和相对误差；截面不等于你要的环面积时它拒绝
报成功。看 `checks` 再信 `created`。

`scdm_extrude` 用来拉伸已有面。它有两个硬限制，都是实测出来的、不是随便定的：

- **`face_indices` 是 0 基的序号，且 `direction` 必填。** 这个版本上无法识别也无法
  判定面的类型：`DesignFace` 没有 `Id`/`IsPlanar`/`SurfaceType`，`Face.Geometry` 抛异常，
  `GetFaceNormal` 要一个无法从 `BoundingBox` 构造的 `PointUV`。因此**没有**
  "拉伸所有平面"的默认行为——不传 `face_indices` 时，拒绝消息会把每个面的面积和周长
  列出来，让你按证据选。
- **`CreatedBodies` 即使拉伸成功也返回空。** 工具是靠对比文档找出来的新几何。
  注意 `extrude_type: ForceIndependent` **有时会把拉伸量并进源体**而不是新建体——
  实测在空心管上，对 `235.6194 mm²` 的环形面拉 5 mm，源体增了 `1178.081 mm³`
  （`source_effect.fused_into_source: true`）。这是正常结果，不是失败；
  读 `source_effect.volume_delta_mm3`。

## attach 会话：这个客户端看得见什么、看不见什么

以下都是实测，它决定了驱动用户自己的 SpaceClaim 时哪些工具能用。用户文档开着时，
attach 会话会这样回报：

| 调用 | 结果 |
|---|---|
| `scdm_session_status` | `body_count: null`、`design_visible: false` |
| `scdm_list_bodies` | `body_count: 0`、`design_visible: false` |
| 脚本里的原生 `DocumentHelper.GetRootPart()` | **能看到文档和它的实体** |

所以在 attach 会话里 `body_count: null` 的意思是**"这个客户端看不见文档"**，
不是"文档是空的"；只读类实体工具在这种情况下不可用。
**`scdm_create_pipe` 和 `scdm_extrude` 仍然可用**，因为它们是通过服务自己的脚本通道
去够那个打开的文档的——它们返回里的 `verification: "service"` 表示校验在 SpaceClaim
内部跑过了、而通过本客户端的第二次读取做不到。那是"少了一次交叉验证"，不是"跳过了校验"。

`scdm_export` 需要一个本客户端看得见的 design，看不见时抛
`attach_export_needs_visible_design`。导出本身**不区分格式**——六种格式走的是同一个库调用。

## 渲染到下游：中文名传不到 Fluent

实测：把 SpaceClaim 的 STEP 导入 Fluent Meshing 时打印 `Warning: ?????????`，
**三个命名集合全丢**，网格里没有可用的命名选择。几何到了，名字没到。

所以模型要交给 Fluent 时，SpaceClaim 侧的图层名和命名选择**一律用 ASCII**：
`inlet`、`outlet`、`wall`、`fluid`。下游每个边界条件的指定都依赖这些名字，
在 CAD 阶段一次做对很便宜，事后返工很贵。

## Measuring a gap

`scdm_min_distance` is the continuous counterpart to `scdm_collisions`. Collision
state is discrete; this answers **how far apart**, which is what decides whether a
gap is a leak or a mesh interface is reasonable.

Measured on the reference assembly, and it agrees with the collision states:

| Pair | Distance | Collision |
|---|---|---|
| `stator` ↔ `winding 1` | 0.0 m | `TOUCH` |
| `stator` ↔ `pip` | 0.0 m | `TOUCH` |
| `stator` ↔ `inlet` | 0.05036119537898199 m | `NONE` |
| `stator` ↔ `outlet` | 0.050361195378982 m | `NONE` |
| `winding 1` ↔ `winding 2` | 0.2072241239859292 m | `NONE` |

`touching: true` in the result means exactly 0.0 m. A sub-millimetre but non-zero
gap is **not** flagged as touching, and that is the case worth looking at: it is
too small to mesh and too large to be a proper contact.

## Transforming bodies

`scdm_transform` does `rotate`, `scale` and `mirror`. All three are verified
against a before/after spatial fingerprint, because the same rule as the booleans
applies — do not trust the call, measure the result.

**Read `verified` before reporting success.** It is false when no fingerprint
could be read at all, which is not the same as geometry that did not move.

`scale` is independently checkable, so use it as your sanity check: volume must
change by the cube of the factor. Measured, `scale(1.5)` on the stator moved
0.003314505208 m³ → 0.01118664023 m³, exactly 1.5³ = 3.375. `rotate` about z was
verified by the fingerprint moving in x and y while z stayed put.

Two traps in the parameters:

- **`origin` defaults to the world origin**, which is rarely the point you want
  for a part that is not centred there. Pass the point you actually mean.
- **`axis` is a direction, not a plane.** For `mirror` it is the plane *normal*
  through `origin`; `axis: "z"` mirrors about the z = 0 plane. The underlying
  `Plane` constructor takes two direction vectors rather than a normal, and
  handing it a normal silently mirrors about the wrong plane — the bridge derives
  the two directions from the normal instead.

## Merging another model

`scdm_insert_file` adds a file's contents to the **open** design. That is not
`scdm_open_file`, which opens a document. Measured: a 31-body design became 62,
and the call returned a component named `定转子装配(1)`.

Import options are left at their defaults on purpose — the default already
imports named selections, which is what every other tool here selects by.

## Diagnosing a failed mesh

**When meshing fails, run `scdm_inspect_geometry` before changing anything.**
Fluent Meshing failures such as `Found overlapping faces` at Describe Geometry /
computing regions have a specific geometric cause, and this tool names it.

Measured on the assembly that actually failed that way (`zhuangpeiti_fix_9_10_1.scdoc`,
31 bodies, 738 faces), the report was:

| Check | Result |
|---|---|
| `duplicate_faces` | 2 groups, 4 faces |
| `short_edges` | 675 edges below 10 mm |
| everything else | 0 |

The duplicate groups are the diagnosis, and the tool gives them face by face:

```
stator 0:41501   area 0.09292831069318609   +   pip 0:15387   area 0.09292831069318609
stator 0:41504   area 0.12176813125314039   +   pip 0:15396   area 0.12176813125314039
```

Two pairs of coincident faces, one on `stator` and one on `pip`, each pair with
an identical area. **That is the overlapping-face failure, identified.** Do not
re-mesh and hope; the duplicate faces are the thing to resolve.

Short edges are the second signal. 459 of the 675 sit on `stator`, the other 216
are 8 per winding, and their median length is 9 mm. Those become sliver cells,
which is why mesh quality fails even after the duplicates are fixed.

### Two results you must not trust

- **`inexact_edges` reports 3348 groups and every one is empty.** The count means
  nothing; the tool marks it `unreliable` and skips it by default. Pass it
  explicitly if you want to see that for yourself.
- **Some edge lengths cannot be read.** `Edge.length` raises
  `ValueError: The norm of the 3D vector is not valid.` from inside PyAnsys on
  162 of those 675 edges. The tool's `span` block separates `measured` from
  `unreadable` for exactly this reason, so a range covering 513 of 675 is not
  silently presented as covering all of them.

### Thresholds

`short_edges` and `small_faces` need a threshold the official methods do not
supply, and **their own defaults find nothing at all** (0.0 m and `None`). On the
reference assembly, 1 mm found nothing while 10 mm found 675 edges and 100 mm
found 1314. So leave the threshold unset: the tool scans a ladder and reports
every rung, which is how you find the scale this model's problems start at.

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

需要跑原生 IronPython 时用它——策展过的工具覆盖不到的东西都在这里。

**参数契约，因为它以前是错的。** `scdm_run_script` 接受 `script_path`（`.py` 文件路径）
**或** `script`（脚本正文，会写进临时文件），**只能给一个**。旧签名的参数名就叫 `script`，
但实际要的是**文件路径**，所以直接传脚本正文会失败并报
`script not found: <整段脚本>`。

SpaceClaim 内嵌 IronPython 2.x，以下是实测"最直觉的写法反而失败"的清单：

- `Body[](n)` is a **parse-time error** — the script dies silently with **no
  output at all**. Use `List[Body]()`.
- `Document.Load(SRC)` performs a bare load, so any later `SaveAs` raises
  `SystemError('序列不包含任何元素')`. Use
  `Document.Open(SRC, ImportOptions.Create())`.
- Passing a Python `list` raises `expected ICollection[Body], got list`.
- **方向要传真正的 `Direction`，不能传元组。** `CurveSegment.CreateArc(centre, a, b,
  (0,0,1))` 从 interop 绑定层抛裸 `TypeError`；
  `Direction.Create(0.0, 0.0, 1.0)` 和 `Direction.DirZ` 都可以。
- **`System.Double[]` 不存在。** 执行 `from SpaceClaim.Api.V242 import *` 之后，
  `System.Double` 绑定到的是 Python 的 `float`，所以 `System.Double[3]` 抛 `TypeError`
  ——而且它抛在"能写失败信息"之前，调用方只会看到 `Script run failed.`。
  改用 `Direction.Create` / `Point.Create`。
- **对捕获到的异常调 `str()` 本身会抛。** `GeometryExitedError.__str__` 会重新抛异常，
  所以 `except Exception as e: log(str(e))` 会死在自己的处理块里。
  记 `type(e).__name__`，原因用你自己打的标记带出来。
- **服务会把所有脚本失败压成同一句**两词消息
  `GeometryRuntimeError("Script run failed.")`；而脚本如果在建立 `result` 字典之前就抛了，
  就**什么都返回不了**。所以先写 `result = {}`，自己 catch 自己的异常，
  需要让调用方知道原因就写进那个字典里。
- **`print` 某些对象会抛 `UnicodeEncodeError` 并让脚本中断。** 只打印字符串，脚本保持纯 ASCII。
- 脚本的 **stdout 会被丢弃**；要二分定位失败点就把进度写进文件。
- 常见启动方式是
  `SpaceClaim.exe /RunScript=<file> /Headless=True /ExitAfterScript=True`。

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
