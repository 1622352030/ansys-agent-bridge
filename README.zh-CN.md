# ansys-agent-bridge

一个驱动 **Ansys SpaceClaim** 的 MCP 服务器，外加一个 **DSH 插件包**——一条命令
把它装进 DeepSeek Harness 的 profile。

这个项目的重点不是把 API 逐个包一层，而是如实报告 SpaceClaim 到底做了什么。原因很
具体：SpaceClaim 2024 R2 上有两个操作会**返回成功却什么都没改**，而相信返回值的
agent 会一本正经地描述一个从未被修改过的几何体。这个服务器在操作前后测量几何，看
不到变化就抛 `no_geometry_change`，而不是报一个它验证不了的"成功"。

## 功能

共 11 个 MCP 工具：

| 工具 | 只读 | 作用 |
|---|---|---|
| `ansys_bridge_doctor` | 是 | 报告探测到的 Ansys 版本、`SpaceClaim.exe`、Fluent 根目录、Python 与依赖包版本。不导入任何重库。 |
| `scdm_session_start` | 否 | 启动隐藏的 SpaceClaim（自动探测版本；实测 **30.6 s**）。 |
| `scdm_session_status` | 是 | 会话是否存活，以及后端类型/版本。 |
| `scdm_session_close` | 否 | 关闭会话并释放许可。 |
| `scdm_open_file` | 否 | 打开 `.scdoc`/`.scdocx`/`.dsco`/`.pmdb`，报告实体、命名选择、面数、体积。 |
| `scdm_list_bodies` | 是 | 当前设计的实体名、面数、体积。 |
| `scdm_collisions` | 是 | 两两碰撞状态（`TOUCH`/`NONE`/…），可全部配对或指定列表。 |
| `scdm_boolean` | 否 | `unite`/`subtract`/`intersect`，带前后几何校验。 |
| `scdm_share_topology` | 否 | Share Topology，同样带前后校验。 |
| `scdm_run_script` | 否 | 对当前会话运行 headless IronPython 脚本。 |
| `scdm_export` | 否 | 导出 `.scdocx`、STEP、IGES、Parasolid 文本/二进制、`.pmdb`。 |

### 两个静默失效，实测数据

在"定子 + 27 个绕组 + 管 + 入口"这个装配体上：

**`subtract` 会失败。** 目标体积 `0.003314505208 m³`、382 个面；操作后体积与面数
**完全相同**，只有工具实体被删掉了（实体数 31 → 30）。用原生 IronPython
`Shape.Subtract` 得到同样的症状，说明这是该模型的几何/内核问题，不是 API 路径问题。

**`share_topology` 返回 `True` 却什么都没做。** 定子仍是 382 面，绕组 1 仍是 16 面，
实体数仍是 31。

**`unite` 是真的生效。** 定子体积 `0.003314505208 → 0.003382973513`（增量
`6.84683e-5`，正好等于绕组 1 自身体积），面数 `382 → 390`，与原生 IronPython 结果
一致。

完整的实测记录——包括原生 IronPython 的那些坑（`Body[](n)` 是**解析期**错误、会让
脚本静默死掉；`Document.Load` 会让之后每一次 `SaveAs` 报错）——见
[`skills/ansys-spaceclaim/SKILL.md`](skills/ansys-spaceclaim/SKILL.md)。

## 安装

需要 **Windows**、装了 SpaceClaim 的 Ansys（2024 R2 已实测），以及
[`uv`](https://docs.astral.sh/uv/)。

### DSH：一条命令

```sh
dsh plugin --profile web add ansys-agent-bridge
```

包内声明了 `dsh.bundle`，所以加载器会应用它的 `cordis.patch.yml`：一层 MCP client
注册 `ansys` 服务器。重启 profile 后调用 `ansys_bridge_doctor` 即可。

卸载就是反过来：

```sh
dsh plugin --profile web remove ansys-agent-bridge
```

MCP 那行用 `uv tool run --from <本仓库>` 启动服务器，所以目标机器**不需要 clone、
也不需要预建虚拟环境**——但**第一次**调用要付解析并构建 `ansys-geometry-core` 的
代价。如果你本机就有 clone、想要一个已经热好的环境，用 `--patch` 覆盖同一个
serverName，把参数换成
`args: [run, --directory, <clone>/python, ansys-bridge-mcp]` 即可。

patch 里**没有 `!!js`，也没有绝对路径**，这是刻意的：实测 harness CLI
（0.1.1-rc.1）求值 `!!js` 的作用域里**没有 `createRequire`**，而且它的
`interpolate` 是同步的、**不会 await 返回值**。用上这两样中的任何一个，都会在桌面
版（0.1.5-rc.2）正常启动，却让 CLI 的 profile 直接起不来——两条路都试过，都炸了。
`command: uv` 两个都不需要。如果你的 harness 启动时的 PATH 里没有 `uv`，用
`--patch` 覆盖这一行：

```yaml
# --patch 覆盖层，在 bundle 层之后应用
- id: mcp-ansys
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: ansys
    transport: stdio
    command: C:/Users/you/.local/bin/uv.exe
    args: [tool, run, --quiet, --from, <repo>, ansys-bridge-mcp]
    toolCallTimeoutMs: 900000
```

### 随包附带的 skill

同样因为 `!!js` 的这个限制，`skills/ansys-spaceclaim/` **没有**由 patch 自动挂载：
挂载它需要在加载期解析出一个路径，就必然要用上面那两种写法之一。改成手动装，
它属于宿主的默认 skill 根 `$DSH_HOME/skills`：

```sh
mkdir -p "$DSH_HOME/skills"                      # Windows 上是 %APPDATA%\dsh-desktop\harness\skills
cp -r <repo>/skills/ansys-spaceclaim "$DSH_HOME/skills/"
```

不装也不影响 MCP 工具能用，少的是那份实测操作要点与陷阱清单。

### 其他 MCP 客户端

服务器本身就是一个普通的 stdio MCP 服务器，并不绑定 DSH。直接生成对应客户端的配置块：

```sh
uvx --from "git+https://github.com/1622352030/ansys-agent-bridge#subdirectory=python" \
    ansys-bridge-doctor --config claude    # 也可用 cursor、vscode、dsh
```

`claude` 与 `cursor` 输出 `mcpServers` 块，`vscode` 输出带显式 `"type": "stdio"` 的
`servers` 块，`dsh` 输出可直接粘进 profile 的 `cordis.patch.yml` 的 `insert` 条目。
把结果贴进客户端的配置即可。

想手写的话，命令就是：

```jsonc
{
  "mcpServers": {
    "ansys": {
      "command": "uv",
      "args": [
        "tool", "run", "--quiet",
        "--from", "git+https://github.com/1622352030/ansys-agent-bridge#subdirectory=python",
        "ansys-bridge-mcp"
      ]
    }
  }
}
```

要用 `uv tool run`，不要用 `uv run`。在 uv 0.11.29 上实测：`uv run` 会以
`unexpected argument '--from' found` 拒绝，而 MCP 客户端那边只显示
`Connection closed`，看不出真实原因。

## 先体检

```sh
uvx --from "git+https://github.com/1622352030/ansys-agent-bridge#subdirectory=python" \
    ansys-bridge-doctor
```

```
ansys-agent-bridge 0.1.0
  python        3.13.4  C:\...\python.exe
  platform      Windows-11-10.0.26100-SP0

ANSYS releases detected (AWP_ROOT* and standard install roots):
  242          C:\Program Files\ANSYS Inc\v242
                 SpaceClaim.exe: C:\Program Files\ANSYS Inc\v242\scdm\SpaceClaim.exe

  Fluent root   C:\Program Files\ANSYS Inc\v242\fluent

Python packages:
  ansys.geometry.core      0.17.2
  ansys.fluent.core        0.42.1
  mcp                      1.28.1

Ready:
  server       yes
  spaceclaim   yes
  fluent       yes
```

加 `--json` 得到机器可读的报告。

## 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `ANSYS_BRIDGE_UV` | 自动探测 | `uv` 的路径，供 DSH patch 的启动命令使用。 |
| `ANSYS_BRIDGE_PRELOAD` | `1` | 启动时就把 Ansys 客户端导入好。设 `0` 可以快速启动，代价是第一次调用 SpaceClaim 时才付这个导入时间。 |
| `ANSYS_BRIDGE_TRANSPORT` | `stdio` | `stdio`、`sse` 或 `streamable-http`。 |
| `ANSYS_BRIDGE_LOG_LEVEL` | `WARNING` | FastMCP 请求日志；`INFO` 会每个请求打一行。 |

### 为什么启动时要预导入、以及为什么工具是串行的

FastMCP 的同步工具**跑在事件循环线程上**
（`mcp/server/fastmcp/utilities/func_metadata.py`：`return fn(**args)`，没有
`to_thread`）。实测在工具调用里惰性导入 `ansys.geometry.core`，会让整个进程卡死在
numpy 的 C 扩展 `create_module` 里，**既不抛异常也不超时**——客户端看到的是一个永远
不返回的工具调用：

```
_call_with_frames_removed (<frozen importlib._bootstrap>:488)
create_module (<frozen importlib._bootstrap_external>:1321)
<module> (numpy\_core\multiarray.py:11)
...
start (ansys_bridge_mcp\scdm.py)
scdm_session_start (ansys_bridge_mcp\server.py)
_handle_message (mcp\server\lowlevel\server.py)
```

同一个导入放在进程启动时只要 **0.9 s**，所以 `main()` 在接受请求之前就把 numpy 和两
个 Ansys 客户端导入完。上面的栈是用 `py-spy dump` 抓的。

同一件事也意味着**长工具会阻塞服务器**：启动 SpaceClaim 的 30 秒会占住事件循环，并发
的调用只能排队。这在这里是可接受的——SpaceClaim 操作的是同一份设计，串行本来就是你
想要的——也正是 DSH patch 里 `toolCallTimeoutMs` 设成 900 s 而不是默认 60 s 的原因。

## 安全约定

- **绝不要打开用户已经在 SpaceClaim GUI 里打开的文件，也绝不要写回源模型。**
  打开、操作、导出到新路径。
- 会话占用一个许可，用完调 `scdm_session_close`。
- 包版本探测只读元数据。早先的版本在工具调用里 `import` 了
  `ansys.fluent.core` 来读版本号，而该库在导入时会打印内容，污染 stdio 的
  JSON-RPC 流并导致会话中途断开；现在 `package_version()` 只读元数据、不导入。

## 目录结构

```
package.json            DSH bundle 清单（`dsh.bundle.patch`）+ npm 入口
cordis.patch.yml        bundle 的 patch 层
skills/ansys-spaceclaim/SKILL.md   实测操作要点与陷阱
tools/verify-js-expr.mjs           离线校验 patch 里的 `!!js` 表达式
python/                 MCP 服务器（uv/pip 可装，src 布局）
```

## 开发

```sh
node tools/verify-js-expr.mjs      # 不需要任何 profile：校验两个 !!js 表达式
uv run --directory python pytest -q
```

`verify-js-expr.mjs` 复现了加载器的 `with (ctx) { eval(expr) }` 作用域，所以表达式
写坏了不必启动任何东西就能发现。

## 许可

MIT，见 [LICENSE](LICENSE)。
