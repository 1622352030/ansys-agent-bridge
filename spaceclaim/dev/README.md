# spaceclaim/dev

SpaceClaim 侧的开发期材料。刻意留在仓库内，这样测试产生的东西不会落进任何人的模型目录。

| 目录 | 内容 | 是否入库 |
|---|---|---|
| `tests/` | 31 个测试脚本：`t1`–`t29` 加三个 API 盘点脚本（`api_surface.py`、`api_live.py`、`api_versions.py`） | 入库 |
| `logs/` | 与脚本同名的运行日志 | 不入库 |
| `evidence/` | `api_versions.json`（版本门槛全量解析）、`geom_api.json`、`geom_api_live.json`、`t25_inspect.json`、`dsh_local_overlay.yml` | 入库 |
| `models/` | 测试用 `.scdoc` 副本（`assembly31.scdoc`、`smoke2.scdoc`） | 不入库 |
| `scratch/` | `out_scdocx`、`out_step`、`out_x_t`、`out_mcp` 导出产物 | 不入库 |

## 复跑注意

脚本里的路径写在**迁移之前**，指向 `C:\english_path\agent\deepseek_harness_desktop\9_10_fluent\geom_test\`。该目录已不存在，复跑前需要改两个常量：

- `SRC` 或 `GEOM` 之类指向测试模型，应改到 `spaceclaim/dev/models/`
- `LOG` 之类指向日志，应改到 `spaceclaim/dev/logs/`

保留原样是为了让日志与脚本能对上，它们记录的是当时的运行。

## 模型来源

`models/` 里的两个 `.scdoc` 是**副本**。原件在 `C:\english_path\fluent\9_1_youcang\scdoc\`，本目录的副本不写回原件。历史上对原件的只读打开记录见 `../docs/verification.md`。
