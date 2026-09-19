# fluent/dev

Fluent 侧的开发期材料。与 `spaceclaim/dev` 严格分开，两边不共用脚本、日志或证据文件。

| 目录 | 内容 | 是否入库 |
|---|---|---|
| `tests/` | 8 个测试脚本：`t30`–`t37` | 入库 |
| `logs/` | 与脚本同名的运行日志 | 不入库 |
| `evidence/` | `fluent_mcp_tools.json`（官方包 25 个工具的名称、描述、参数） | 入库 |
| `scratch/` | 8 个 `fluent-*.trn` transcript 与 `cleanup-fluent-*.bat` | 不入库 |

## 这批测试要记住的一件事

`t35_reach.py` 在**用户的原始 case** 上执行过一次写操作：

```
solver.setup.models.energy.enabled = True
```

用的是 `C:\english_path\fluent\9_1_youcang\case\deliver_oil\motor_cht_oil.cas.h5`，直接读的原文件，没有先复制副本。写前读回是 `True`，写后读回也是 `True`，所以写入的是同一个值，模型设置状态没有实际变化；也没有调用 `write_case`，磁盘文件未改动（该文件修改时间仍为 2026/9/11，Fluent 自己的 transcript 里只有 `Reading`、没有 `write` 或 `save`）。

但这是错的：**不该在用户的模型上做任何写操作，读也不该读原件。**

正确做法已经写进 `README.md` 的根目录结构说明与 `.gitignore`：测试材料一律留在 `dev/` 内，模型只操作副本。

`t36`、`t37` 同样直接读了那个原文件，但没有写操作，只有网格质量查询。`t34` 里也出现过一次同样的写语句，因当时未读入 case、setup 树未激活而失败。
