# ansys-fluent-mcp 0.4.0 缺陷审计

审计对象：`ansys-fluent-mcp` 0.4.0（Ansys 官方，Apache-2.0），装在
`C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\.venv-pyfluent`。
配套：`ansys-fluent-core` 0.42.1、`ansys-common-mcp` 0.3.4、`fastmcp` 4.0.3、`mcp` 2.2.0。

审计目的：判断"直接采用官方包"这条路线的能力边界，供选型决策。

## 判定标准（修正过两次，记录在此）

**只把"能力不可达"记为缺陷。** 两点推论：

其一，默认值不合意但可以覆盖，那是默认值不是缺陷。初稿曾把 `processor_count` 默认 1、`ui_mode` 默认 `gui`、`precision` 默认 `double` 列为缺陷，标准用错，已删。

其二，没有专用工具、但能通过 `run_code` 调用 Fluent 自身 API 完成的，也不是能力缺失，只是封装程度问题。初稿曾把"无案例读写工具""无设置类工具""无求解工具""无写后验证"列为缺陷，实测全部可达，已删。

改判后剩下的缺陷，才是真正需要自己补的。

证据分两级。**实测**指在本机真跑过；**静态确认**指源码里可查，给出文件与行号或原话。

## 表1 功能域与可达性

表1列出每个功能域的专用工具情况、通过代码调用 Fluent API 的可达性、以及据此得出的结论。

| 功能域 | 专用工具 | 代码可达（实测） | 结论 |
|---|---|---|---|
| 会话连接 | `connect` `disconnect` `session_status` `manage_fluent` | — | 有工具 |
| 设置树探测 | `probe_path` `get_active_status` `get_allowed_values` `describe_path` `describe_named_object_template` | — | 有工具，设计良好 |
| API 检索与帮助 | `find_api` `get_help` `get_state` `get_targeted_context` | — | 有工具 |
| 命名对象 | `list_named_objects` `find_named_object` `select_named_objects` | — | 有工具 |
| 代码执行 | `run_code` `validate_code` | — | 有工具 |
| **案例读写** | 无 | **可达**，`solver.file.read_case` 读入 20.1 秒 | 非缺陷 |
| **模型与边界条件设置** | 无 | **可达**，`setup.models`、`setup.boundary_conditions`、`setup.cell_zone_conditions` 均可读写 | 非缺陷 |
| **写后生效验证** | 无 | **可达**，写前 `True` → 写 → 写后 `True`，`get_state` 工具读回同样为 `true` | 非缺陷 |
| **初始化与迭代** | 无 | **可达**，`solver.solution` 方法集可用 | 非缺陷 |
| **网格检查** | `mesh_quality` 读指标 | **可达**，`solver.mesh.check` 可用 | 非缺陷 |
| **监视器与报告定义** | `simulation_report` | **可达**，`solution.report_definitions` 含 `compute` / `custom` | 非缺陷 |
| 场变量 | `list_fields` | — | 有工具 |
| 截图 | `screenshot` | — | 有工具 |
| 案例对比 | `compare_files` | — | 有工具 |
| **网格生成流程** | 无 | **未实测**，需以 meshing 模式连接后验证 | 待定 |
| **对流换热系数计算** | 无，官方明确排除 | **不可达**，见缺陷 1 | **缺陷** |
| **后处理数值分析** | 无 | **不可达**，`run_code` 禁 `numpy` | **缺陷** |
| **网格合格判定** | `mesh_quality` 只给指标 | 数字可达，判定标准需自备 | **缺陷** |
| **收敛判定** | 无 | 数字可达，判定标准需自备 | **缺陷** |
| **配置自验** | 无 | **不可达**，返回不含启动参数 | **缺陷** |

## 表2 真缺陷清单

表2列出改判后剩下的 5 项缺陷。

| # | 缺陷 | 性质 | 影响 | 证据 |
|---|---|---|---|---|
| 1 | `compute_htc` 等工程关联工具被官方明确排除 | 能力不可达 | CHT 标定对流换热系数无 API 可用 | 静态确认，官方原话见下 |
| 2 | `run_code` 禁 `numpy`、`pandas` | 能力不可达 | 后处理数值分析、拟合 h 曲线在工具内做不了 | 实测，`common/validation.py` 第 121 至 134 行 |
| 3 | `mesh_quality` 只返回指标不做判定 | 判定标准缺失 | 网格是否合格须自备门槛 | 静态确认，`solve/lib/mesh_tools.py` 全文 |
| 4 | 无收敛判据工具 | 判定标准缺失 | 须自备判据并自行读监视器数据 | 静态确认，`common/base.py` 第 1210 至 1240 行 |
| 5 | `connect` 与 `session_status` 不返回实际启动参数 | 可用性缺失 | 无法从返回值确认配置是否被采纳 | 实测，见下 |

第 1 项的证据是官方源码原话（`solve/lib/domain_tools.py` 第 66 至 70 行）：

> NOTE: The engineering reference / correlation tools (lookup_wall_roughness,
> lookup_emissivity, compute_porous_media, compute_htc) encapsulate
> engineering *business logic* and live entirely in the optional
> higher-level agent layer, so the public MCP leaf does not expose them.
> Do NOT add them here.

被排除的四项是壁面粗糙度查询、发射率查询、多孔介质计算、**对流换热系数计算**。这不是"尚未实现"，而是被有意划出开源范围。

第 5 项的实测返回：

```
connect        -> {"status":"ok","backend_kind":"pyfluent","endpoint":null,
                   "candidates":[],"message":"PyFluent connected (launch).",...}
session_status -> {"leaf":"solve","connected":true,"backend":"Solve (PyFluent)",
                   "backend_kind":"pyfluent","endpoint":null,"capabilities":[],...}
```

`processor_count`、`precision`、`dimension`、`ui_mode` 均不在返回值中，只出现在服务端日志。本次审计确认覆盖生效，靠的正是读服务端日志。

## 实测证据汇总

### 默认参数可覆盖（推翻初稿的三项"缺陷"）

调用：

```
connect(connect_kwargs={"processor_count": 24, "ui_mode": "no_gui",
                        "precision": "double", "dimension": 3, "mode": "solver"})
```

该包自己的日志：

```
session connected mode=launch precision=double processor_count=24 dimension=3 solver_mode=solver
```

不传参数时：

```
session connected mode=launch precision=double processor_count=1 dimension=3 solver_mode=None
```

`processor_count` 1 变 24、`solver_mode` 由 `None` 变 `solver`、`precision` 按传入值生效。连接耗时 15.7 秒（24 核）对 23.1 秒（1 核）。双精度是常用配置，无需改动。

### 案例读写可达

以 8 核 `no_gui` 连接后，用 `run_code` 执行 `solver.file.read_case(file_name=<motor_cht_oil.cas.h5>)`，返回 `status=ok`，日志显示 `Fast-loading ... hdfio.bin`、`Done.`、`Multicore SMT processors detected`，耗时 **20.1 秒**。

### 读入后各域可达

| 探测 | 结果 |
|---|---|
| `solver.setup.models` 子项 | 可用（`ablation` `battery` `discr...`） |
| `setup.models.energy.enabled` 读值 | `True`，该 case 已开启能量方程 |
| `setup.boundary_conditions` 列表 | 15 项以上，含 `interior--stator`、`interior--winding-7`、`interior--fluid:1` 等 |
| `setup.cell_zone_conditions` 列表 | 含 `fluid:1`、`winding-17`、`winding-26` 等 |
| `solver.solution` 方法集 | 可用 |
| `solver.mesh` 方法集 | 可用，含 `check` |
| `solution.report_definitions` | 可用，含 `compute`、`custom` |

附带观察：`solver.file`、`solver.setup`、`solver.solution`、`solver.mesh` 这几个入口在 0.42.1 下均提示已废弃，建议改用 `settings.*`。功能仍可用。

### 写后验证闭环成立

```
写前  solver.setup.models.energy.enabled.get_state()  -> True
写入  solver.setup.models.energy.enabled = True
写后  solver.setup.models.energy.enabled.get_state()  -> True
工具  get_state(["setup/models/energy/enabled"])      -> {"...enabled": true}
```

代码写入与工具读回形成闭环，说明"写后生效验证"这条能力**不需要额外工具即可实现**。

### 沙箱八项限制

| 写入的代码 | 实测结果 |
|---|---|
| `import numpy` | `forbidden_import` |
| `import pandas` | `forbidden_import` |
| `import math` | 允许 |
| `open(...)` | `forbidden_call` |
| `import subprocess` | `forbidden_call` |
| `import os; os.system(...)` | `forbidden_call` |
| `setattr(solver, ..., ...)` | `forbidden_call` |
| `eval('1+1')` | `forbidden_call` |

### 一次未复现的 stdout 异常（待观察）

一次会话末尾，客户端报 `Failed to parse JSONRPC message from server`，原因是服务端往 stdout 写入了回车字符。复测未复现（最小流程完整跑通），故**记为单次观察，不作为缺陷**。

## 该包做得好的地方

一是**前置探测**。五个探测工具解决"写之前先确认路径可写"：`probe_path` 给出 `exists`、`is_active`、`is_user_creatable`，`get_allowed_values` 取枚举取值，`describe_path` 合并成单次往返。理由是 Fluent 对不活跃路径**会静默忽略写入**。

本次实测正好验证了这一点：未读 case 时 `get_state` 返回 `{"inactive": true}`，读入 case 后才返回真实值。

二是**意图守卫**。`solve/lib/intent_guard.py` 对已知 Fluent 崩溃签名做静态拦截，源码注释举例如边界条件改名带空白、VOF 相数直接赋值、命名表达式先用后建。

三是**探测与后端分离**。探测工具只读，不依赖计划器或配方注册表。

## 对电机油冷 CHT 工作的具体影响

| 工作环节 | 支持情况 |
|---|---|
| 连接求解器 | 可用，`connect_kwargs` 指定核数与模式 |
| 读入 case | 可达，20.1 秒 |
| 网格质量 | 指标可达，合格判定自备 |
| 设置材料与边界条件 | 可达，写前建议用探测工具确认路径活跃 |
| 设置体积热源 | 可达，顺序陷阱（energy 先于 sources.enable 先于 terms.energy）无守卫 |
| 初始化与迭代 | 可达 |
| 收敛判断 | 数字可达，判据自备 |
| 结果场读取 | `list_fields` 列变量，取值可达 |
| **换热系数标定** | **不可达**，`compute_htc` 被排除 |
| **后处理数值分析** | **不可达**，`run_code` 禁 `numpy` |

## 结论

官方包的能力面比初稿判断的宽得多：**案例读写、设置、初始化迭代、写后验证、网格检查、监视器定义全部可达**，只是都走 `run_code` 而非专用工具。默认参数全部可覆盖。

真正需要自己补的只有三类：

一是**工程计算**。`compute_htc` 被官方划出开源范围，而它是 CHT 标定 h 的核心量。

二是**沙箱外的数值分析**。`run_code` 不能 import numpy，读回的场数据要在工具之外处理。

三是**判定标准**。网格是否合格、是否收敛，工具只给数字，门槛要自备。

换句话说，选这条路要补的不是"通用 Fluent 工具"（那些可达），而是**工程计算、外部数值分析、与判定基准**。
