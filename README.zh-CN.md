# ansys-agent-bridge

一个驱动 **Ansys SpaceClaim** 的 MCP 服务器，外加一个 **DSH 插件包**——一条命令
把它装进 DeepSeek Harness 的 profile。

这个项目的重点不是把 API 逐个包一层，而是如实报告 SpaceClaim 到底做了什么。原因很
具体：SpaceClaim 2024 R2 上有两个操作会**返回成功却什么都没改**，而相信返回值的
agent 会一本正经地描述一个从未被修改过的几何体。这个服务器在操作前后测量几何，看
不到变化就抛 `no_geometry_change`，而不是报一个它验证不了的"成功"。

## 功能

共 17 个 MCP 工具：

| 工具 | 只读 | 作用 |
|---|---|---|
| `ansys_bridge_doctor` | 是 | 报告探测到的 Ansys 版本、`SpaceClaim.exe`、Fluent 根目录、Python 与依赖包版本。不导入任何重库。 |
| `scdm_session_start` | 否 | **我控**：启动 SpaceClaim。默认 `hidden=false`，即**用户看得见**的那个窗口（实测 21.8 s 到可用）。 |
| `scdm_attach` | 否 | **你控**：接入用户自己启动的 SpaceClaim。不新增许可占用，且**关不掉用户的窗口**。 |
| `scdm_session_status` | 是 | 会话是否存活、处于哪种模式（`launch`／`attach`）、后端类型/版本。 |
| `scdm_session_close` | 否 | 关闭**本服务器启动的**会话并释放许可；attach 会话下**硬拒绝**（`attach_mode_refuses_close`）。 |
| `scdm_detach` | 否 | 断开与用户 SpaceClaim 的连接：只关 gRPC 通道，进程与许可都不动。 |
| `scdm_open_file` | 否 | 打开 `.scdoc`/`.scdocx`/`.dsco`/`.pmdb`，报告实体、命名选择、面数、体积。 |
| `scdm_list_bodies` | 是 | 当前设计的实体名、面数、体积。 |
| `scdm_collisions` | 是 | 两两碰撞状态（`TOUCH`/`NONE`/…），可全部配对或指定列表。 |
| `scdm_min_distance` | 是 | 两实体最小间距（米），碰撞状态的连续量版本。 |
| `scdm_insert_file` | 否 | 把另一个 CAD 文件并入当前设计（`open_file` 是打开文档，这个是加进已打开的）。 |
| `scdm_transform` | 否 | `rotate`／`scale`／`mirror`，用变换前后的空间指纹验证是否真生效。 |
| `scdm_inspect_geometry` | 是 | 官方八项几何体检：重复面、短边、小面、缺失面、待分割边、可缝合面、多余边、不精确边。 |
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
[`spaceclaim/skills/ansys-spaceclaim/SKILL.md`](spaceclaim/skills/ansys-spaceclaim/SKILL.md)。

## 两种 GUI 模式：我控 / 你控

会话由**谁拥有进程**决定，这件事分成两个工具而不是一个开关——因为许可与所有权语义正好
相反，选错的那一个会结束用户的会话。（与 Fluent 侧的 `ui_mode="gui"` / `fluent_attach` /
`fluent_detach` 对齐。）每个会话都在返回值里给出机器可读的 `mode` 字段。

| | `mode=launch`（我控） | `mode=attach`（你控） |
|---|---|---|
| 工具 | `scdm_session_start` | `scdm_attach` |
| 谁启动的进程 | 本服务器 | 用户 |
| 许可 | 本服务器占一份 | **不新增**，复用用户那份 |
| `process_ownership.owner` | `ai` | `user` |
| 怎么结束 | `scdm_session_close` | `scdm_detach`（只关通道） |
| 另一个工具 | `scdm_detach` → **被拒** | `scdm_session_close` → **被拒** |

### 我控：默认就是看得见的窗口

`hidden` 默认 `false`。实测（走真实 MCP stdio 传输）：返回
`mode="launch"`／`ui_visible=true`，**21.8 s** 到可用，主窗口
`设计(D)1 - SpaceClaim`（1935×1047，可见、未最小化），内存工作集 **979 MB**，
`scdm_session_close` 之后**残余进程 0 个**。

两点实测提醒：

- **`hidden=true` 不是"最小化"**，PyAnsys 把它映射成 `/Headless=True`，Ansys 的定义是
  **无 UI 的批处理**；实测 40 个顶层窗口里只有 2 个 13×13 无标题小窗，没有主窗口。
- **窗口可能落在副屏**：本机两次可见启动都在 `x = -1927`（副屏）。PyAnsys 不暴露窗口位置，
  要锁主屏得自己往命令行里加 `/WindowLocation=`——**未实测**。

### 你控：裸双击不行，必须用带参快捷方式

**裸双击启动的 SpaceClaim 没有 gRPC 端点**，AI 接不上：实测它监听的三个端口
（49013/49014/63186）对 gRPC 健康检查**全部** `UNAVAILABLE`。官方文档也是同一句话：
"Running a SpaceClaim or Discovery normal session does not suffice… Both products need
the ApiServer extension to be running."

所以用户侧要双击的是这个快捷方式（本机已建好）：

```
桌面：SpaceClaim 2024R2 (AI可接入).lnk
  └─> spaceclaim/dev/launchers/start_spaceclaim_for_ai.bat
        set API_PORT=51790            ← 固定端口，AI 侧才知道去哪连
        SpaceClaim.exe /ADDINMANIFESTFILE="...\Addins\ApiServer\Presentation.ApiServerAddIn.Manifest.xml" /transport-mode=wnua
```

环境变量写在 `.bat` 里而不是快捷方式里，因为 Windows 快捷方式**不能设环境变量**——
Fluent 侧的 `-sifile=` 启动器踩过同一个坑。

用法：

1. 双击快捷方式，等窗口起来（和平时启动一样，20–30 s）；
2. AI 侧调 `scdm_attach(port=51790)`（实测这类连接 **0.31 s** 就连上）；
3. AI 侧结束时要调 `scdm_detach`，**不能**调 `scdm_session_close`——后者在 attach 会话下被
   硬拒绝（`error="attach_mode_refuses_close"`、`process_untouched=true`、
   `licence_released=false`）；
4. 用户想让 AI 下线：直接关掉 SpaceClaim，通道随进程终止。SpaceClaim 侧**没有** Fluent 那种
   `server/shutdown-server` 命令，未找到任何等价入口。

连不上时 `scdm_attach` 会直接给出上面那条**完整可粘贴的命令行**，并说明裸双击为什么不行。

**风险（务必知道）**：`/ADDINMANIFESTFILE` 与 `API_PORT` **不在 Ansys 公开命令行文档里**，
只出现在 PyAnsys Geometry 源码（`product_instance.py:92, 113-117, 401-404`）与实测中——
属于**半公开接口**，将来版本可能改。端口也是**约定**而不是发现机制：SpaceClaim 没有 Fluent
`-sifile=` 那样的端口文件。attach 下的**写操作（`open_file`／几何算子／`export`）与许可
计数器均未实测**，详见
[`spaceclaim/docs/verification.md`](spaceclaim/docs/verification.md)。

## 安装

需要 **Windows**、装了 SpaceClaim 的 Ansys（2024 R2 已实测），以及
[`uv`](https://docs.astral.sh/uv/)。

### 三种安装方式

DSH 上有三种装法。A 与 B 的区别只在于 **MCP 服务器从哪里加载**；C 则是**先装一次，
之后从 PATH 直接启动**，所以 `uv` 在每次拉起时**什么都不用解析**：

| | 方式 A：从 GitHub 安装 | 方式 B：从本地 checkout 安装 | 方式 C：把启动命令装进 PATH |
| --- | --- | --- | --- |
| 插件包 | `github:1622352030/ansys-agent-bridge` | `link:<本地 clone 路径>` | 与 A 或 B 相同 —— C 只替换 MCP 那一行 |
| MCP 启动源 | `git+https://github.com/…#subdirectory=spaceclaim/python` | `<本地 clone>/spaceclaim/python` | 已装好的 console script `ansys-bridge-mcp`（不经过 `uv`，不带路径） |
| 每次拉起是否联网 | **是**（先 `git fetch`） | **否** | **否** |
| 每次拉起是否解析依赖 | 每次都要 | 每次都要；`<本地 clone>/spaceclaim/python` 下任何改动都会重新物化环境 | 不用 —— 依赖只在 `uv tool install` 那一次解析 |
| 无梯子／GitHub 不可达 | ⚠ 会失败，且可能拖垮整个桌面 | 正常 | 正常 |
| 改了本仓库代码 | 需重装才生效 | 立即生效 | 立即生效，且不重新解析依赖、不重建环境 |
| 自动跟随上游 | 是 | 否 | 否 |
| 怎么卸 | `dsh plugin … remove` | `dsh plugin … remove` | `uv tool uninstall ansys-agent-bridge` |

**在国内、或到 GitHub 的连通性不稳时，请直接用方式 B。** 方式 A 的 `git fetch` 一旦
失败，包内的 `failOnStartupError: false` 会把错误藏起来，自动重连又反复重建环境、重复
导入 `ansys.geometry.core` / `ansys.fluent.core`，占满 DSH 单线程 host 的事件循环——
表现为所有会话历史一直「加载中」、桌面无响应；随后插件管理器会判定「启用它会写出开机
即崩的状态」而把它**锁死为停用**，此后在界面上点开关也启用不回来。
完整机制、诊断命令与解锁步骤见 [TROUBLESHOOTING.md](./TROUBLESHOOTING.md)。

### 方式 A：从 GitHub 安装

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
代价。如果你本机就有 clone、想要一个已经热好的环境，改用下面的**方式 B**——
注意路径是 `<clone>/spaceclaim/python`（Python 包在 `spaceclaim/` 下），
早期文档里写的 `<clone>/python` 已不再适用。

### 方式 B：从本地 checkout 安装

两步：把插件指向本地目录，再用 `--patch` 覆盖 MCP 行的启动源。

```sh
# 1) 插件包指向本地 clone（等价于把 profile 依赖写成 link:<path>）
dsh plugin --profile web add link:C:/path/to/ansys-agent-bridge
```

```yaml
# 2) 在你自己的 --patch 覆盖层里重述这一行（id 定向覆盖会替换整行 config）
#    %APPDATA%\dsh-desktop\harness\profiles\web\cordis.patch.yml
- id: mcp-ansys
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: ansys
    transport: stdio
    command: uv
    args:
      - 'tool'
      - 'run'
      - '--quiet'
      - '--from'
      - 'C:/path/to/ansys-agent-bridge/spaceclaim/python'
      - 'ansys-bridge-mcp'
    env:
      PYTHONIOENCODING: 'utf-8'
      ANSYS_BRIDGE_LOG_LEVEL: 'WARNING'
    toolCallTimeoutMs: 900000
    failOnStartupError: false
```

改完重启 DSH 生效。

### 方式 C：把启动命令装进 PATH

方式 B 仍然把每次拉起交给 `uv tool run`：它每次都要解析依赖、重新物化那份隔离环境，
而且只要 `<本地 clone>/spaceclaim/python` 下有**任何**改动（比如改了 `src/…`），这套活
就从头再来一遍。改成**只装一次**，之后让 DSH 直接启动一条普通命令：

```sh
uv tool install --editable --from <本地 clone>/spaceclaim/python ansys-agent-bridge
```

⚠️ 位置参数是**包名**（`pyproject.toml` 里的 `[project].name`），**不是**命令名。
写成命令名会被 `uv` 自己拒绝，没有别的解释余地：
`error: Package name (ansys-agent-bridge) provided with --from does not match install request (ansys-bridge-mcp)`。

装好之后会得到一个 editable 的 tool 环境，命令脚本落到 PATH 上 —— Windows 上是
`%USERPROFILE%\.local\bin\ansys-bridge-mcp.exe`，确切目录可以用 `uv tool dir --bin` 查。
只要那个目录在 PATH 里，DSH 侧的覆盖就可以写成**纯命令名：零路径、零 `uv`**：

```yaml
# 在你自己的 --patch 覆盖层里重述这一行
# %APPDATA%\dsh-desktop\harness\profiles\web\cordis.patch.yml
- id: mcp-ansys
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: ansys
    transport: stdio
    command: ansys-bridge-mcp
    args: []
    env:
      PYTHONIOENCODING: 'utf-8'
      ANSYS_BRIDGE_LOG_LEVEL: 'WARNING'
    toolCallTimeoutMs: 900000
    failOnStartupError: false
```

改完重启 DSH 生效。改了本仓库代码也照旧立即生效：`--editable` 把装好的环境指向
`<本地 clone>/spaceclaim/python/src`，`uv` 自己的帮助里就是这么写的 ——
"changes in the package's source directory are reflected without reinstallation"，
下次启动**不解析、不重建、不拉取**。卸载：

```sh
uv tool uninstall ansys-agent-bridge
```

### 为什么 patch 里既没有 `!!js`、也没有绝对路径

这是刻意的：实测 harness CLI（0.1.1-rc.1）求值 `!!js` 的作用域里**没有
`createRequire`**，而且它的 `interpolate` 是同步的、**不会 await 返回值**。用上这两样
中的任何一个，都会在桌面版（0.1.5-rc.2）正常启动，却让 CLI 的 profile 直接起不来——
两条路都试过，都炸了。`command: uv` 两个都不需要。如果你的 harness 启动时的 PATH
里没有 `uv`，把 `command` 换成绝对路径即可：

```yaml
    command: C:/Users/you/.local/bin/uv.exe
```

### 随包附带的 skill

同样因为 `!!js` 的这个限制，`spaceclaim/skills/ansys-spaceclaim/` **没有**由 patch 自动挂载：
挂载它需要在加载期解析出一个路径，就必然要用上面那两种写法之一。改成手动装，
它属于宿主的默认 skill 根 `$DSH_HOME/skills`：

```sh
mkdir -p "$DSH_HOME/skills"                      # Windows 上是 %APPDATA%\dsh-desktop\harness\skills
cp -r <repo>/spaceclaim/skills/ansys-spaceclaim "$DSH_HOME/skills/"
```

不装也不影响 MCP 工具能用，少的是那份实测操作要点与陷阱清单。

### 其他 MCP 客户端

服务器本身就是一个普通的 stdio MCP 服务器，并不绑定 DSH。直接生成对应客户端的配置块：

```sh
uvx --from "git+https://github.com/1622352030/ansys-agent-bridge#subdirectory=spaceclaim/python" \
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
        "--from", "git+https://github.com/1622352030/ansys-agent-bridge#subdirectory=spaceclaim/python",
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
uvx --from "git+https://github.com/1622352030/ansys-agent-bridge#subdirectory=spaceclaim/python" \
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
- **attach 会话不占新许可，也不许被关掉。** 那是用户自己启动的 SpaceClaim：`scdm_session_close`
  在该模式下被硬拒绝，断开只能用 `scdm_detach`。这条护栏在**工具层**实现，不依赖库的默认
  行为（库只在 `product_instance` 非空时才关后端，而 `force_exit()` 之类路径并不看这个字段）。
- 包版本探测只读元数据。早先的版本在工具调用里 `import` 了
  `ansys.fluent.core` 来读版本号，而该库在导入时会打印内容，污染 stdio 的
  JSON-RPC 流并导致会话中途断开；现在 `package_version()` 只读元数据、不导入。

## 目录结构

仓库按产品域严格二分：SpaceClaim 的东西和 Fluent 的东西不共用一个目录，连测试脚本都分开。

```
package.json            DSH bundle 清单（`dsh.bundle.patch`）+ npm 入口
cordis.patch.yml        bundle 的 patch 层
TROUBLESHOOTING.md      桌面端装不上／把桌面卡死／MCP 关掉后无法启用回来的排查与解锁
screenshots.json
tools/verify-patch.mjs             离线校验 cordis.patch.yml

spaceclaim/
  python/               MCP 服务器（uv/pip 可装，src 布局）
  python/tests/         离线单测（不打 ANSYS、不占许可、不联网）
  skills/ansys-spaceclaim/SKILL.md 实测操作要点与陷阱
  docs/feature-coverage.md         实现功能与官方 API 的逐项对照表
  docs/verification.md             验证记录，以及抓出的 bug
  dev/launchers/        start_spaceclaim_for_ai.bat（你控模式的双击入口）
  dev/tests|logs|evidence|models|scratch

fluent/
  docs/mcp-audit.md                官方 ansys-fluent-mcp 的审计
  dev/tests|logs|evidence|scratch
  （暂无代码——Fluent 侧目前直接用官方包）
```

`dev/` 放开发期材料，**刻意留在仓库内**，这样任何东西都不会写进你的模型目录。transcript、导出产物、复制的测试模型已被 gitignore；测试脚本与 API 盘点结果保留。

根目录只留必须在根的东西：npm 只从根读 `package.json`，`dsh.bundle.patch` 的 `./cordis.patch.yml` 也是相对根解析。

## 实现了什么、没实现什么

[`spaceclaim/docs/feature-coverage.md`](spaceclaim/docs/feature-coverage.md) 是与官方 API 的逐项对照表：每个能力域、是否实现、以及未实现的原因。它不是"服务器能做什么"的清单，而是读者用来找"它没做什么"的清单。

最要紧的一条结论是：**官方客户端声明的方法远多于这个版本能跑的**。286 个带 `@min_backend_version` 门槛的公开方法里，**只有 15 个能在 24R2 上调用**，另外 271 个需要 25.1 到 27.1，其中包括 `GeometryCommands` 全部 44 个建模方法。所以对照表先按版本过滤——这一步才分得清"真缺漏"和"调了只会抛 `GeometryRuntimeError` 的方法"。

规划工作前需要知道的两条：

- **几何体检现在是主打功能。** `RepairTools` 里有 8 个 `find_*` 方法完全没有版本门槛，24R2 可用，这 8 项现在都在 `scdm_inspect_geometry` 里。对着那个建网失败的装配体，它直接点名原因：两对完全重合的面，每对分别是 stator 和 pip 上的一个面，面积完全相同。把"建网失败"换成"这两张面叠在一起了"，就是它的全部意义。
- **外流场计算域无法通过这个 API 生成。** 三个 `create_*_enclosure` 需要 26.1.0。做外部流场 CFD 时，计算域要在 SpaceClaim 界面建，或改用 Fluent Meshing 的 enclosure 功能。

## 开发

```sh
node tools/verify-patch.mjs        # 不需要任何 profile
uv run --directory python pytest -q
```

`spaceclaim/python/tests/` 里的单测是**离线**的：不打 ANSYS、不占许可、不联网。它们用一个
假的 PyAnsys 客户端记录"服务器愿意发起哪些调用"——这正是两种 GUI 模式的实质（`hidden` 默认值、
attach 必须传 `transport_mode` 而**不能**传 `product_instance`、attach 下 `close()` 必须在碰到
库之前就被拒、参数校验必须在联网之前完成）。当前 **70 项**。

`verify-patch.mjs` 把 patch 里任何 `!!js` 表达式放在一个**只有 `ctx` 的裸作用域**里求值——这正是 CLI 的构造方式——并拒绝返回 Promise，所以"只能在较新 harness 上启动"的 patch 会在这里失败，而不是在用户机器上。它还会校验 patch 启动的 console script 确实是 `pyproject.toml` 里声明的那个。

## 许可

MIT，见 [LICENSE](LICENSE)。
