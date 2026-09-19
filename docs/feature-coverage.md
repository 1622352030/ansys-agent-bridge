# 功能对照表

本表对照官方能力与 ansys-agent-bridge 已实现的功能，用于核对缺漏。

对照基准有两个来源。一是 Ansys 官方 Python 客户端 `ansys-geometry-core` 0.17.2 的公开 API，二是本机 SpaceClaim 2024 R2（`backend_version` 24.2.0）的实际可用性。

数据口径：官方客户端的公开方法中，286 个带 `@min_backend_version` 版本门槛，已用 AST 逐项解析源码得到门槛值，其中在 24R2 可用的为 15 个；无版本门槛的方法即全版本可用，已对 `RepairTools` 全部 18 个逐项确认。解析脚本与产物见 `geom_test/api_versions.py`、`api_versions.json`，完整清单与不可用方法及所需版本见配套 Mnemon 文档 `b03e162c`。

状态分三种。已实现指当前有对应工具；未实现指当前版本可用但尚未做成工具；不可用指当前版本调不通，不属缺漏。

## 表1 官方能力域与实现状态

表1按功能域汇总官方能力与实现状态。

| 功能域 | 来源 | 24R2可用方法 | 已实现工具 | 状态 |
|---|---|---|---|---|
| 环境探测 | 自建 | — | `ansys_bridge_doctor` | 已实现 |
| 会话生命周期 | `Modeler` | 15 | `scdm_session_start` `scdm_session_status` `scdm_session_close` | 已实现 |
| 打开设计 | `Modeler.open_file` | 1 | `scdm_open_file` | 已实现 |
| 实体与命名选择查询 | `Design` `Body` | 多 | `scdm_list_bodies` | 已实现 |
| 碰撞状态查询 | `Body.get_collision` | 1 | `scdm_collisions` | 已实现 |
| 布尔运算 | `Body` | 3 | `scdm_boolean` | 已实现 |
| 共享拓扑 | `PrepareTools` | 1 | `scdm_share_topology` | 已实现，该域唯一可用项 |
| 导出 | `Design` | 6 | `scdm_export` | 已实现 |
| 脚本逃生通道 | `Modeler.run_script_file` | 1 | `scdm_run_script` | 已实现 |
| 几何体检 | `RepairTools` | 8 | 无 | **未实现** |
| 最小间距测量 | `MeasurementTools` | 1 | 无 | **未实现** |
| 变换 | `MasterBody` | 4 | 无 | **未实现** |
| 合并外部模型 | `Design.insert_file` | 1 | 无 | **未实现** |
| 草图建模 | `Component` | 4 | 无 | **未实现** |
| 边与面的几何属性 | `Edge` `Face` | 2 | 无 | **未实现** |
| 几何修复 | `RepairTools` | 0 | 无 | 不可用，需 25.2.0 |
| 干涉检查 | `RepairTools` | 0 | 无 | 不可用，需 25.2.0 |
| 直接建模 | `GeometryCommands` | 0 | 无 | 不可用，需 25.2.0 或 27.1.0 |
| 外流场计算域 | `PrepareTools` | 0 | 无 | 不可用，需 26.1.0 |
| 新选择 API | `BodySelection` `FaceSelection` `EdgeSelection` | 0 | 无 | 不可用，需 27.1.0 |
| 材料与参数 | `Design` | 0 | 无 | 不可用，需 25.1.0 或 26.1.0 |

## 表2 当前版本可用但未实现的方法

表2逐项列出 24R2 上可以调用、但尚未做成工具的方法，这是真正的缺漏清单。

| # | 方法 | 类 | 门槛 | 用途 | 优先级 |
|---|---|---|---|---|---|
| 1 | `find_duplicate_faces` | RepairTools | 无 | 查重复面，对应 `Found overlapping faces` | 高 |
| 2 | `find_missing_faces` | RepairTools | 无 | 查缺失面 | 高 |
| 3 | `find_small_faces` | RepairTools | 无 | 查小面，网格质量杀手 | 高 |
| 4 | `find_short_edges` | RepairTools | 无 | 查短边，网格质量杀手 | 高 |
| 5 | `find_split_edges` | RepairTools | 无 | 查待分割的边 | 高 |
| 6 | `find_inexact_edges` | RepairTools | 无 | 查不精确的边 | 高 |
| 7 | `find_stitch_faces` | RepairTools | 无 | 查可缝合的面 | 高 |
| 8 | `find_extra_edges` | RepairTools | 无 | 查多余的边 | 高 |
| 9 | `min_distance_between_objects` | MeasurementTools | 24.2.0 | 两实体最小间距，判断接触或留隙 | 高 |
| 10 | `Design.insert_file` | Design | 24.2.0 | 把外部 CAD 并入当前设计 | 中 |
| 11 | `MasterBody.rotate` | MasterBody | 24.2.0 | 旋转 | 中 |
| 12 | `MasterBody.scale` | MasterBody | 24.2.0 | 缩放 | 中 |
| 13 | `MasterBody.mirror` | MasterBody | 24.2.0 | 镜像 | 中 |
| 14 | `MasterBody.map` | MasterBody | 24.2.0 | 映射变换 | 中 |
| 15 | `Component.sweep_sketch` | Component | 24.2.0 | 草图扫掠建模 | 低 |
| 16 | `Component.sweep_chain` | Component | 24.2.0 | 链式扫掠建模 | 低 |
| 17 | `Component.revolve_sketch` | Component | 24.2.0 | 草图旋转建模 | 低 |
| 18 | `Component.create_body_from_loft_profile` | Component | 24.2.0 | 放样建模 | 低 |
| 19 | `Edge.shape` | Edge | 24.2.0 | 读边的几何定义 | 低 |
| 20 | `Face.shape` | Face | 24.2.0 | 读面的几何定义 | 低 |

第 1 至 8 项是当前最大的缺漏。它们全部无版本门槛，即 24R2 完全可用，且正好对应当前项目中 Fluent Meshing 在 computing regions 阶段报 `Found overlapping faces` 而失败的诊断需求。

## 表3 当前版本不可用的能力

表3列出官方有、但当前版本调不通的能力，说明它们不属实现缺漏。

| 能力 | 方法或类 | 所需最低版本 |
|---|---|---|
| 几何修复与干涉检查 | `repair_geometry` `inspect_geometry` `find_interferences` `find_simplify` `find_bad_faces` `find_and_fix_*` | 25.2.0 或 27.1.0 |
| 增强共享拓扑 | `enhanced_share_topology` | 25.2.0 |
| 圆角移除与标识识别 | `remove_rounds` `find_logos` `find_and_remove_logos` | 25.2.0 |
| 体积提取 | `extract_volume_from_faces` `extract_volume_from_edge_loops` | 25.1.0 |
| 外流场计算域 | `create_box_enclosure` `create_cylinder_enclosure` `create_sphere_enclosure` | 26.1.0 |
| 泄漏检测 | `detect_leaks` | 27.1.0 |
| 直接建模 | `GeometryCommands` 全部 44 个 | 25.2.0 或 27.1.0 |
| 新选择 API | `BodySelection` `FaceSelection` `EdgeSelection` | 27.1.0 |
| 实体几何属性 | `Body.centroid` `Body.get_bounding_box` `Body.detach_faces` | 27.1.0 |
| 基准几何 | `create_datum_plane` `create_datum_point` `create_datum_line` 及对应删除 | 27.1.0 |
| 材料与参数 | `remove_material` `get_all_parameters` `set_parameter` `get_raw_tessellation` | 25.1.0 或 26.1.0 |

## 结论

已实现 11 个工具，覆盖环境探测、会话、文件读写、查询、布尔、导出、逃生通道七个域。`PrepareTools` 十四个方法里唯一在 24R2 可用的共享拓扑已经做了。

缺漏集中在表2，共 20 个方法，其中 8 个几何体检方法优先级最高。

表3 的能力全部受版本限制，在当前 24R2 环境下无法实现，实现它们只会得到运行时报错。其中外流场计算域一项对电机外部流场 CFD 标定有直接影响，需要改在 SpaceClaim 界面手工建域或使用 Fluent Meshing 的 enclosure 功能。
