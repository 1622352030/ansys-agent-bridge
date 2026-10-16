# 故障排查

## DSH：装完把桌面拖死，或在 MCP 控制台关掉之后再也启用不回来

### 症状

任选其一或全部：

1. 装上 `ansys-agent-bridge` 后，**所有会话历史一直显示「加载中」，整个桌面无响应**；
2. 在 MCP 控制台里把 `ansys` 关掉之后**再也恢复不了**：插件卡片显示「已停用」，
   点开关没反应，或开关自己弹回去；
3. MCP 控制台的服务器列表里**根本没有 `ansys`**（只有 lab / mathtype / voicebox 之类）。

### 机制

三条独立的事实叠在一起：

1. **包内默认走网络取源码。** `cordis.patch.yml` 里 MCP 行的启动参数是
   `uv tool run --from git+https://github.com/1622352030/ansys-agent-bridge#subdirectory=spaceclaim/python`，
   所以**每次拉起服务器都要先 `git fetch`**。
2. **本机到 GitHub 的 TLS 握手会失败**（Windows schannel），报错形如
   `fatal: unable to access '…': schannel: failed to receive handshake`。
3. **失败被藏起来，而且被反复重试。** 同一行里 `failOnStartupError: false`，
   再加上 MCP 客户端的自动重连，构成「失败—重试—偶尔成功」的循环；每次重试都要重建
   环境并重新导入 `ansys.geometry.core` / `ansys.fluent.core` 这两个重依赖，
   而 DSH 的 host 是**单线程**的 —— 这些同步导入会把事件循环占满。

**后果 1**：事件循环被占满 → 桌面无响应、所有会话历史卡在「加载中」。

**后果 2**：插件管理器检测到「启用它会写出一个开机即崩的状态」，于是**拒绝启用并保持
停用**，日志原文为
`enable failed; leaving it disabled rather than persisting a state that crashes at boot (#575)`。
此后插件处于「锁死停用」：界面点开关也回不来，因为启用动作需要匹配到 loader entry，
而插件没加载就没有 entry —— 形成一个解不开的环。

### 诊断

```powershell
# 1) 手动按包内方式启动一次，看是否网络失败（doctor 不会挂住 stdio）
uv tool run --from "git+https://github.com/1622352030/ansys-agent-bridge#subdirectory=spaceclaim/python" ansys-bridge-doctor

# 2) 插件管理器日志：toggle 事件与失败原因
Get-Content "$env:APPDATA\dsh-desktop\harness\profiles\web\.dsh-market\log.ndjson" -Tail 20

# 3) 停用名单
Get-Content "$env:APPDATA\dsh-desktop\harness\profiles\web\.dsh-market\state.json" -Raw

# 4) profile patch 末尾有没有被追加的禁用条目
Get-Content "$env:APPDATA\dsh-desktop\harness\profiles\web\cordis.patch.yml" -Tail 8
```

判定特征：

| 位置 | 命中内容 | 含义 |
| --- | --- | --- |
| `log.ndjson` | `no loader entry matched` ＋ `leaving it disabled … (#575)` | 插件被锁死停用 |
| `state.json` | `"disabled":[…,"ansys-agent-bridge"]` | 停用名单里有它 |
| `cordis.patch.yml` 末尾 | `- id: mcp-ansys` 紧跟 `disabled: true` | 这是界面上「关闭 MCP」写进去的；同 id 后者生效，会覆盖前面的正确配置 |

### 解锁与修复

**先备份这两个文件**（它们决定 profile 能否启动，改坏了会更麻烦）：

```powershell
$w = "$env:APPDATA\dsh-desktop\harness\profiles\web"
Copy-Item "$w\.dsh-market\state.json" "$w\.dsh-market\state.json.bak"
Copy-Item "$w\cordis.patch.yml"      "$w\cordis.patch.yml.bak"
```

然后按顺序做四件事：

1. **从停用名单移除**：把 `state.json` 中 `disabled` 数组里的 `"ansys-agent-bridge"` 删掉；
2. **删掉被追加的禁用条目**：移除 `cordis.patch.yml` 末尾的
   `- id: mcp-ansys` 与 `disabled: true` 这两行；
3. **改用本地 checkout 启动**（见下一节）—— 否则解锁后仍然会重复上面的失败循环，
   桌面会再卡一次、插件会再被锁一次；
4. **重启 DSH**。profile patch 与 market state 都只在启动时读取，刷新页面不算。

### 预防：改用本地安装

把 MCP 行的启动源从 GitHub 换成本地 clone，`uv` 就不再需要 `git fetch`。本地做法有两条，
区别只在于 **`uv` 什么时候干活**。

**方式 B：每次启动都交给 `uv tool run`**

```yaml
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

注意路径是 `<clone>/spaceclaim/python` —— Python 包在 `spaceclaim/` 目录下，
早期文档写的 `<clone>/python` 已不适用。**B 的代价**：每次拉起都要解析依赖；而且只要
`<clone>/spaceclaim/python` 下的内容有变化（例如改了 `src/…`），`uv` 还会重新物化整个环境。

**方式 C：装一次，之后启动纯命令名**

与 B 的区别一句话讲清：**B 每次启动都要解析依赖，C 只装一次**（启动时不再解析依赖、
不重建环境、不联网）：

```sh
uv tool install --editable --from <clone>/spaceclaim/python ansys-agent-bridge
```

⚠️ 位置参数是**包名** `ansys-agent-bridge`（`pyproject.toml` 的 `[project].name`），
**不是**命令名 `ansys-bridge-mcp`；写成命令名会被 `uv` 拒绝。装好后命令就在 PATH 上
（Windows 是 `%USERPROFILE%\.local\bin\ansys-bridge-mcp.exe`，确切目录用
`uv tool dir --bin` 查），于是这一行可以写成**纯命令名：零路径、零 `uv`**：

```yaml
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

回滚：`uv tool uninstall ansys-agent-bridge`。

三种安装方式的完整对照见 [README.zh-CN.md](./README.zh-CN.md) 的「安装」一节
（English: [README.md](./README.md) 的 *Install*）。

### 不依赖 DSH 验证启动链路

```powershell
$env:PYTHONIOENCODING='utf-8'

# a) doctor：确认 uv 环境、依赖与 ANSYS 检测；它自己会退出，不会挂住
uv tool run --from "C:/path/to/ansys-agent-bridge/spaceclaim/python" ansys-bridge-doctor

# b) 真的把服务器拉起来：它在等待 stdio 输入即为正常
$p = Start-Process uv -ArgumentList 'tool','run','--quiet','--from','C:/path/to/ansys-agent-bridge/spaceclaim/python','ansys-bridge-mcp' -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 40
if (-not $p.HasExited) { "OK: MCP server alive, waiting on stdio"; Stop-Process -Id $p.Id -Force } else { "FAIL: exited with $($p.ExitCode)" }
```

---

## English summary

**Symptom.** After installing on DSH the desktop hangs with every session history stuck
at "loading"; or, having switched the `ansys` MCP row off in the MCP console, it can
never be turned back on — the plugin card reads *disabled* and the switch does nothing.

**Mechanism.** The bundled row launches via
`uv tool run --from git+https://…#subdirectory=spaceclaim/python`, so **every launch
performs a `git fetch`**. Where the TLS handshake to GitHub fails
(`schannel: failed to receive handshake`), `failOnStartupError: false` hides the error
while the reconnect loop keeps rebuilding the environment and re-importing
`ansys.geometry.core` / `ansys.fluent.core`. Those synchronous imports saturate DSH's
**single-threaded** host event loop, so the desktop appears hung. The plugin manager
then refuses to enable the plugin — *"…would persist a state that crashes at boot
(#575)"* — and latches it disabled; because the unloaded plugin has no loader entry,
the enable action can never match, so the UI switch cannot recover it.

**Fix.** Back up `state.json` and `cordis.patch.yml` under
`%APPDATA%\dsh-desktop\harness\profiles\web\` first, then: (1) drop
`"ansys-agent-bridge"` from `disabled` in `.dsh-market\state.json`; (2) delete the
appended `- id: mcp-ansys` / `disabled: true` pair at the end of `cordis.patch.yml`;
(3) switch the MCP row to a local checkout (`--from <clone>/spaceclaim/python`) —
or install the launcher once and use the bare command name, which resolves nothing
at launch: `uv tool install --editable --from <clone>/spaceclaim/python
ansys-agent-bridge`, then `command: ansys-bridge-mcp`; (4) restart DSH. Skipping
step 3 means the failure loop returns and the plugin gets latched again.
