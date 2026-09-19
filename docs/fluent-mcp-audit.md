# ansys-fluent-mcp 0.4.0 缺陷审计

审计对象：`ansys-fluent-mcp` 0.4.0（Ansys 官方，Apache-2.0），装在
`C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\.venv-pyfluent`。
配套：`ansys-fluent-core` 0.42.1、`ansys-common-mcp` 0.3.4、`fastmcp` 4.0.3、`mcp` 2.2.0。

审计目的：判断"直接采用官方包"这条路线的能力边界，供选型决策。

证据分两级。**实测**指在本机真跑过并给出观察结果；**静态确认**指源码里可查，并给出文件与行号或原话。凡标"已实测"的小节，其结论都来自本机一次真实会话。

## 表1 Fluent 功能域与工具覆盖

表1按 Fluent 工作流的功能域列出官方包的覆盖情况。

| 功能域 | 覆盖工具 | 状态 |
|---|---|---|
| 会话连接 | `connect` `disconnect` `session_status` `manage_fluent` | 已覆盖 |
| 设置树探测 | `probe_path` `get_active_status` `get_allowed_values` `describe_path` `describe_named_object_template` | 已覆盖，设计良好 |
| API 检索与帮助 | `find_api` `get_help` `get_state` `get_targeted_context` | 已覆盖 |
| 命名对象 | `list_named_objects` `find_named_object` `select_named_objects` | 已覆盖 |
| 代码执行 | `run_code` `validate_code` | 已覆盖，带沙箱与守卫 |
| 求解状态 | `solver_status` | 已覆盖，只读状态 |
| 网格质量 | `mesh_quality` | 只读指标，不做判定 |
| 场变量 | `list_fields` | 已覆盖 |
| 报告与截图 | `simulation_report` `screenshot` | 已覆盖 |
| 案例对比 | `compare_files` | 已覆盖 |
| **案例与网格读写** | 无 | **缺口** |
| **模型与边界条件设置** | 无 | **缺口，靠 `run_code`** |
| **初始化与迭代求解** | 无 | **缺口，靠 `run_code`** |
| **网格生成流程** | 无 | **缺口** |
| **对流换热系数计算** | 无，官方明确排除 | **缺口** |
| **写后生效验证** | 无 | **缺口** |

## 表2 缺陷清单

表2按性质分类列出 16 项缺陷，标注影响与证据。

### 一、功能缺口（6 项）

| # | 缺陷 | 影响 | 证据 |
|---|---|---|---|
| 1 | `compute_htc` 被官方明确排除 | CHT 标定对流换热系数无工具可用 | 静态确认，见下方原话 |
| 2 | 无网格生成流程工具 | Fluent Meshing 的 watertight / fault-tolerant 流程无入口 | 静态确认，25 个工具无一项涉及 |
| 3 | 无案例与网格读写工具 | `read_case` / `read_mesh` / `write_case` 不在工具层，也不在后端层 | 静态确认，`solve/backends/` 内无对应方法 |
| 4 | 无初始化与迭代求解工具 | 求解动作只能写代码 | 静态确认，同 3 |
| 5 | 无设置类工具 | 材料、边界条件、模型开关全部写代码 | 静态确认，25 个工具无一项写设置 |
| 6 | 无写后生效验证 | 设置写完是否生效无工具核验 | 静态确认，探测工具只解决"能否写" |

第 1 项的证据是官方源码里的原话（`solve/lib/domain_tools.py` 第 66 至 70 行）：

> NOTE: The engineering reference / correlation tools (lookup_wall_roughness,
> lookup_emissivity, compute_porous_media, compute_htc) encapsulate
> engineering *business logic* and live entirely in the optional
> higher-level agent layer, so the public MCP leaf does not expose them.
> Do NOT add them here.

也就是说，这四个工程关联工具不是"尚未实现"，而是**被有意划出开源范围**，归入可选的高层 agent 层。同一排除名单里还包括壁面粗糙度查询、发射率查询、多孔介质计算。

### 二、默认值对无头批处理有害（3 项，已实测）

| # | 缺陷 | 影响 | 证据 |
|---|---|---|---|
| 7 | `processor_count` 默认 1 | 不显式传即单核运行 | **实测**，见下方日志 |
| 8 | `ui_mode` 默认 `"gui"` | 无头场景应传 `no_gui` | 静态确认，同下 |
| 9 | `precision` 默认 `double` | 双精度内存占用约为单精度两倍 | **实测**，同下 |

第 7 至 9 项的证据分两层。源码层（`solve/backends/pyfluent.py` 第 492 至 496 行与第 912 至 913 行）：

```
FLUENT_LAUNCH_DEFAULTS: dict[str, Any] = {
    "precision": ...,
    "processor_count": 1,
    "dimension": ...,
    "ui_mode": "gui",
}
```

实测层：调用 `connect()` 不传任何参数，该包自己的日志打出实际启动参数。

```
session connected mode=launch precision=double processor_count=1 dimension=3
                  solver_mode=None gpu=None journals=None case=None
```

`processor_count=1`、`precision=double` 两项由此从源码推断变为本机实测。连接耗时 23.1 秒。

本机 32 逻辑核。按既有记录，单核导致每轮 Fluent Meshing 耗时 4 至 5 分钟。这三项都必须在 `connect` 时通过 `connect_kwargs` 显式覆盖。

### 三、返回值不含实际启动参数（1 项，已实测）

| # | 缺陷 | 影响 | 证据 |
|---|---|---|---|
| 10 | `connect` 与 `session_status` 不返回启动参数 | 无法从工具返回确认跑在几个核、什么精度上 | **实测**，见下 |

实测返回：

```
connect        -> {"status":"ok","backend_kind":"pyfluent","endpoint":null,
                   "candidates":[],"message":"PyFluent connected (launch).",...}
session_status -> {"leaf":"solve","connected":true,"backend":"Solve (PyFluent)",
                   "backend_kind":"pyfluent","endpoint":null,"capabilities":[],...}
```

`processor_count`、`precision`、`dimension`、`ui_mode` 全部不在返回值里，只出现在服务端日志。也就是说，agent 调用 `connect` 后**无法自行验证**它要求的核数与精度是否被采纳，只能相信输入参数。对以"不信任返回值、测量实际结果"为原则的工作方式，这是个反向的例子。

### 四、代码执行沙箱的实际约束（4 项，已实测）

`run_code` 是唯一的执行通道，但它是受限的 Python 子集，不是通用解释器。八项限制全部实测确认：

| 写入的代码 | 实测结果 |
|---|---|
| `import numpy` | `status=error`，`forbidden_import` |
| `import pandas` | `status=error`，`forbidden_import` |
| `import math` | `status=ok`，允许 |
| `open(...)` | `status=error`，`forbidden_call` |
| `import subprocess` | `status=error`，`forbidden_call` |
| `import os; os.system(...)` | `status=error`，`forbidden_call` |
| `setattr(solver, ..., ...)` | `status=error`，`forbidden_call` |
| `eval('1+1')` | `status=error`，`forbidden_call` |

| # | 约束 | 影响 | 证据 |
|---|---|---|---|
| 11 | 禁止 `open` | 不能自行读写文件，导出须走 Fluent 自身 API | 实测，`common/validation.py` 第 109 行 |
| 12 | 禁止 `subprocess` 与 `os.system` | 不能调用外部程序 | 实测，第 92 至 100 行 |
| 13 | import 白名单仅 math、json、itertools、functools、collections、dataclasses、typing 与 ansys 系列 | **不能 import numpy、pandas、matplotlib** | 实测，第 121 至 134 行 |
| 14 | 禁止反射写入，`setattr` 与 `__setitem__` 被拦 | 必须用直接赋值或 `.set_state()` | 实测，`solve/backends/pyfluent.py` 第 2758 行 |

第 13 项对后处理影响最直接：读回流场数据做数值分析、拟合换热系数曲线这类工作需要 numpy，而它是被挡住的。数据必须先由 Fluent 自身导出，再在 MCP 之外处理。第 11 项意味着连写一个中间结果文件都不行。

### 五、判定类能力缺失（2 项）

| # | 缺陷 | 影响 | 证据 |
|---|---|---|---|
| 15 | `mesh_quality` 只返回指标不做判定 | 网格是否合格须自行判断 | 静态确认，`solve/lib/mesh_tools.py` 全文 |
| 16 | 无收敛判据工具 | `solver_status` 只给迭代数与残差，不判断是否收敛 | 静态确认，`common/base.py` 第 1210 至 1240 行 |

第 15 项的返回结构是 `{cell_count, face_count, node_count, quality: {min_orthogonal_quality, max_ortho_skew, max_aspect_ratio}, check?}`，即给数字不给定论。

第 16 项：按既有记录，CHT 稳态收敛应看进出口质量流量差小于 0.1% 与出口温度稳定，而不是看残差绝对值。官方包不提供这类判据。

### 六、它对"路径写错"的防护良好

需要说明的是，上面 16 项缺陷里**不包括**"静默写错路径"。该包对这一类问题有专门设计，见下一节。

## 该包做得好的地方

审计也发现它有三处值得肯定的设计，这些是自建方案容易忽略的。

一是**前置探测**。五个探测工具解决"写入前先确认路径可写"：`probe_path` 同时给出 `exists`、`is_active`、`is_user_creatable`，`get_active_status` 判断路径是否被同级开关激活，`get_allowed_values` 取枚举取值，`describe_path` 把四项合并成单次往返。工具描述里写明理由：Fluent 对不活跃路径**会静默忽略写入**，或抛出 `InactiveObjectError`。

二是**意图守卫**。`solve/lib/intent_guard.py` 针对已知的 Fluent 崩溃签名做静态拦截，源码注释举例如边界条件改名带空白、VOF 相数直接赋值、命名表达式先用后建、迭代过程中写设置。

三是**探测结论与后端分离**。探测工具只读，不依赖计划器或配方注册表，因此可以独立暴露在开源叶子上。

## 对电机油冷 CHT 工作的具体影响

按当前工作流逐项对照，采用官方包后仍需要自行解决的是：

| 工作环节 | 官方包支持情况 |
|---|---|
| 连接求解器 | 可，但必须覆盖 `processor_count` 与 `ui_mode` |
| 网格质量检查 | 可读指标，合格判定要自己做 |
| 设置材料与边界条件 | 靠 `run_code` 写设置树，写前可用探测工具确认路径 |
| 设置体积热源 | 靠 `run_code`，顺序陷阱（energy 先于 sources.enable 先于 terms.energy）无守卫 |
| 初始化与迭代 | 靠 `run_code` |
| 收敛判断 | 无判据工具，需自行读监视器数据判断 |
| 结果场读取 | `list_fields` 可列变量，取值靠 `run_code` |
| 换热系数标定 | **无工具**，`compute_htc` 被官方排除 |
| 后处理数值分析 | 受限，`run_code` 不能 import numpy |

## 结论

官方包的强项是**探索与执行**：帮 agent 找到正确的设置路径、用代码去写、读完再报告。它对"路径写错"和"已知崩溃签名"有防护。

它的边界在**判定与工程计算**：不判断网格是否合格、不判断是否收敛、不算换热系数、不做后处理数值分析，也不生网格。这些恰好是 CHT 工作里最容易出错、也最需要固化成工具的部分。

因此选择该路线时，需要补的不是"通用 Fluent 工具"，而是这四类**判定型与工程计算型**能力。
