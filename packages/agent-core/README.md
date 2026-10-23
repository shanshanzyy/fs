# vibe-agent-core

本地化 Vibe Coding 编码智能体的核心引擎。

用自然语言描述需求，Agent 自己读代码、定位问题、生成结构化补丁、等你审批、落盘、跑测试。
**核心链路零强制依赖** —— 只用 Python 标准库，内网离线环境直接跑。
（`repo_map` 可选装 `[precise]` 启用 tree-sitter 精准解析，不装自动回退内置正则。）

> 整体架构设计见 [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md)。
> 本包同时是 M0（CLI 闭环）与 M1（桌面端接入层 `host/`）的交付物；
> 桌面壳见 [`apps/desktop`](../../apps/desktop/README.md)。

---

## 30 秒上手（不需要装任何模型）

本仓库自带一个**脚本化假模型服务**，用来在不具备模型环境时验证完整链路：

```bash
# 终端 1：启动假模型（只监听 127.0.0.1）
cd packages/agent-core
python scripts/mock_model_server.py --port 11500

# 终端 2：让 Agent 指向它，跑一次真实任务
cd packages/agent-core
python -m agent_core \
  --workspace ../../examples/demo-project \
  --base-url http://127.0.0.1:11500/v1 \
  --model mock-agent-model \
  -p "修复 average 遇到空列表崩溃的问题"
```

你会看到：Agent 生成符号地图 → 读取 `src/utils.py` → 提交补丁 → 弹出审批（显示真实 diff）
→ 落盘 → 给出结论。审批时输入 `y` 即可。

---

## 接入真实模型

只要能说 OpenAI 兼容协议就能接（Ollama / vLLM / DeepSeek / 公司自建网关均可）：

```bash
# Ollama 本地模型
ollama pull qwen2.5-coder:7b
ollama serve

python -m agent_core --workspace /path/to/repo --model qwen2.5-coder:7b
```

```bash
# 内网 vLLM
python -m agent_core -w /path/to/repo \
  --base-url http://gpu-node:8000/v1 --model Qwen2.5-Coder-32B-Instruct
```

先跑环境自检确认连通性：

```bash
python -m agent_core -w /path/to/repo --check
```

---

## CLI

| 命令 | 说明 |
|---|---|
| `vibe-agent` | 进入交互式会话 |
| `vibe-agent -p "任务描述"` | 单次任务模式，执行后退出 |
| `vibe-agent --check` | 环境自检：模型连通性、审计目录、工具清单 |
| `vibe-agent --read-only` | 只读模式，禁止一切写操作 |
| `vibe-agent --no-shell` | 禁止执行命令 |
| `vibe-agent --yes` | 自动放行所有需确认的动作（**危险**，务必配合 git） |
| `vibe-agent -v` | 输出完整工具返回值 |

交互模式下的斜杠命令：`/help` `/tools` `/config` `/diff` `/audit` `/reset` `/iterations N` `/exit`

配置也可以通过环境变量注入（内网统一部署时很有用），见 [`.env.example`](.env.example)。

### P0 能力（A–E）新增的环境变量 ↔ 代码字段

| 环境变量 | `Config` 字段 | 默认 | 作用 | 缺失 / 关闭时的行为 |
|---|---|---|---|---|
| `VIBE_EMBED_MODEL`（B5） | `embed_model` | `""` | 语义检索用的 OpenAI 兼容 embedding 模型名 | 空 = **不构造** embedding 客户端，检索走纯词法 + 符号/依赖图（功能可用，精度降一档） |
| `VIBE_SEMANTIC_MAX_FILES`（B5） | `semantic_max_files` | `800` | 单次建索引最多读多少个文件 | 防超大仓库把内存与耗时拉爆 |
| `VIBE_FIM`（D3） | `fim_enabled` | `true` | 内联补全是否优先走 FIM（`/completions` + `suffix`） | `0` = 跳过 FIM 直接 `chat()`；上游不支持 FIM 时也自动回退（`_fim_supported` 缓存，见 ADR-012） |
| `VIBE_COMPLETION_MAX_TOKENS`（D3） | `completion_max_tokens` | `256` | 单次补全的输出 token 上限 | 补全贵在"快"，给太大反而拖慢首字 |
| `VIBE_COMPLETION_PREFIX_CHARS`（D3+） | `completion_prefix_chars` | `4000` | 喂给模型的光标**前**上下文字符数（行对齐截断） | `0` = 不截断；大文件整份上送会让 token 不可控、首字变慢 |
| `VIBE_COMPLETION_SUFFIX_CHARS`（D3+） | `completion_suffix_chars` | `2000` | 喂给模型的光标**后**上下文字符数（行对齐截断） | `0` = 不截断；截断按整行对齐，避免把半行喂给模型 |
| `VIBE_LSP`（E4） | `lsp_mode` | `auto` | 语言服务器开关：`auto` / `on` / `off` | 非法值**启动前报错**；`off` = 不探测、不启动子进程；探测不到时端点回 `available:false` 及原因（**不做远端回退**，ADR-014） |
| `VIBE_LSP_IDLE`（E4） | `lsp_idle_seconds` | `300` | 空闲多少秒后自动 shutdown 语言服务器（**不常驻**） | `0` = 不自动回收 |

> 语义检索的 `embed_model` 若配成公网端点，源码片段会离开本机 —— 这是用户的显式选择，
> 默认不配即不出机（ADR-005 / ADR-012）。补全链路只产文本、不落盘，因此不触发审批闸门（ADR-011）。
> 语言服务器只在本机以 stdio 起子进程，**不存在远端 LSP 服务器模式**（ADR-014）——
> 代码与诊断都不出机器，本机没装就降级，不影响其它功能。

### 编辑面（D）的四个 HTTP 端点

契约详见 [`agent_core/host/protocol.py`](agent_core/host/protocol.py) 的「命令」表：

| 端点 | 请求 | 返回 |
|---|---|---|
| `POST /api/complete` | `{path, prefix, suffix, maxTokens?}` | `{text, backend, path, chars}`（`backend` ∈ `fim`/`chat`） |
| `POST /api/rewrite` | `{path, startLine, endLine, instruction}` | `{token, path, startLine, endLine, hunks[], noChange}` |
| `POST /api/rewrite/apply` | `{token, accepted[]}` | `{ok, changed, path, applied, total, added, removed, notice}` |
| `POST /api/edit/save` | `{path, content, expectedDigest}` | `{ok, changed, path, digest, added?, removed?, notice}` |

三条边界：`rewrite/apply` 与 `edit/save` **运行中 409**；`edit/save` 带 `expectedDigest`
乐观锁（磁盘已被改则 409）；`complete` **不设 409**（只产文本、不写盘）。

### 语言服务器（E）的五个 HTTP 端点

契约同样以 [`agent_core/host/protocol.py`](agent_core/host/protocol.py) 的「命令」表为准：

| 端点 | 请求 | 返回 |
|---|---|---|
| `GET /api/lsp/status` | —— | `{available, mode, reason, servers[], running[]}` |
| `GET /api/lsp/diagnostics?path=` | 查询参数 | `{available, path, counts{}, diagnostics[]}` |
| `GET /api/lsp/definition?path=&line=&char=` | 查询参数 | `{available, locations[]}` |
| `POST /api/lsp/rename/preview` | `{path, line, char, newName}` | `{token, files[]{path, diff, added, removed, edits}}`，**不落盘** |
| `POST /api/lsp/rename/apply` | `{token}` | `{ok, changed, files, paths[], added, removed}` |

三条口径：位置是 `line` **1 基** + `char` **0 基 UTF-16 列**（内部转 LSP 时 `line-1`）；
`lsp_mode=off`、未安装、或已标记不可用时，前三条**只读端点一律返回
`{available:false, reason}` 而不是报错**（界面把 gutter 留空并给一行说明）；
`rename/apply` **运行中 409**，且**先整批校验再落盘** —— 任一文件过期（磁盘已被改）
就整批不动，绝不改一半。

### 计划视图（P2-12）的 HTTP 端点

`update_plan` 是**工具**（`Risk.SAFE`，纯内存），模型通过它把「打算怎么做」写成结构化
步骤表；界面据此渲染可折叠计划卡片。人工勾选走下面的端点：

| 端点 | 请求 | 返回 |
|---|---|---|
| `POST /api/plan/toggle` | `{id, status}`（`status` ∈ `pending`/`in_progress`/`done`/`skipped`） | `{ok, id, status, steps, revision}` |

三条边界：计划**只存内存**（`Agent.plan`），不落盘、不进会话库 —— 刷新页面靠
`session.describe()` 的快照补齐；勾选属「用户本人动作」（ADR-011），不走审批浮层、
**运行中不 409**（只是标注内存状态，不与写盘争用），但记 `plan.toggle` 审计
（只记 `id`/`status`/`steps_count`/`revision`，不含计划正文）；计划更新经 `run.plan`
事件广播（payload `{runId, steps[], revision}`，界面按 `revision` 丢弃乱序旧版本）。

### 上下文窗口管理（P2-17）的环境变量与端点

| 环境变量 | `Config` 字段 | 默认 | 作用 | 缺失 / 关闭时的行为 |
|---|---|---|---|---|
| `VIBE_CONTEXT_BUDGET_TOKENS`（H2） | `context_budget_tokens` | `0` | 上下文裁剪的 **token** 预算（>0 启用 token 口径） | `0` = **未启用**，沿用 `VIBE_MAX_CONTEXT_CHARS` 的**字符**口径，行为与改造前逐字节一致（ADR-020） |
| `VIBE_PROMPT_CACHE`（H2） | `prompt_cache` | `auto` | prompt caching 开关：`auto` / `on` / `off` | 非法值**启动前报错**；`off` = 不注入，请求体不变；`auto` = inline 被动探测，上游拒收则自动回退为 `unsupported` |

| 端点 | 请求 | 返回 |
|---|---|---|
| `GET /api/context/stats` | —— | `{budgetTokens, usedTokens, segments{system,rules,history,toolOutputs}, estimator, trimmedLastRun, cache}` |

四条口径：**只读、无副作用**（界面预算条与刷新快照共用同一份数据）；`estimator` ∈
`tiktoken`/`heuristic`（tiktoken **只探测不打包**，缺则回退启发式，见 ADR-020）；
`segments` 分段之和**恒等于** `usedTokens`，且 `rules` 从 `system` 段里扣除（不重复计数）；
用量在**裁剪之后**上报，报的是模型这一轮真正会看到的长度。运行中每轮 + 收尾各广播一次
`run.context` 事件（payload 即上面的字段），界面渲染 `#ctx-bar` 预算条（超 80% 变黄、
超限变红）。

### 增量索引 / 文件监听（P1-9）的环境变量与端点

| 环境变量 | `Config` 字段 | 默认 | 作用 | 缺失 / 关闭时的行为 |
|---|---|---|---|---|
| `VIBE_INDEX_WATCH`（J3） | `index_watch` | `false` | 是否后台监听工作区变化并**增量更新**索引 | `0`/未设 = **关闭**：不启线程、不做额外索引，行为与改造前逐字节一致 |
| `VIBE_INDEX_WATCH_INTERVAL`（J3） | `index_watch_interval` | `5` | 轮询间隔（秒） | **强制下限 2**（低于 2 被抬到 2），避免高频 `stat` 拖慢工作区 |

| 端点 | 请求 | 返回 |
|---|---|---|
| `GET /api/index/status` | —— | `{watching, interval, files, chunks, edges, lastUpdate, pending}` |

三条口径：**纯 stdlib 轮询**（`(mtime_ns, size)` 快照 diff），**不引 `watchdog`**（ADR-012 零强制
依赖红线），**索引只驻内存、不落盘**（ADR-013）；被 watcher 持有的索引被 **pin 住**，不进 LRU
淘汰、且同 root 调用恒返回同一实例（避免「watcher 更新 A、检索读 B」）；端点**只读**，
`index_watch` 关闭时如实回 0，不会因「查询状态」而凭空建索引。更新结果与**全量重建等价**
（存活文档集的 `total`/`df`/`avgdl`/分词逐项一致），每次更新记一条 `index.rebuild` 审计
（只记 `added`/`changed`/`removed`/`files_count`/`ms`，无路径、无正文）。

### 外部 MCP server（P1-6）的环境变量与端点

| 环境变量 | `Config` 字段 | 默认 | 作用 | 缺失 / 关闭时的行为 |
|---|---|---|---|---|
| `VIBE_MCP`（L4） | `mcp_enabled` | `false` | 是否接入外部 MCP server | `0`/未设 = **关闭**：不读配置、不起任何 MCP 子进程，行为与改造前逐字节一致 |
| `VIBE_MCP_CONFIG`（L4） | `mcp_config` | `""` | servers JSON 路径 | 留空 = `<工作区>/.vibe/mcp.json` |
| `VIBE_MCP_TIMEOUT`（L4） | `mcp_timeout` | `30` | 单个 MCP 请求超时（秒） | —— |

| 端点 | 请求 | 返回 |
|---|---|---|
| `GET /api/mcp/status` | —— | `{enabled, configPath, servers[]{name,transport,command,url,available,reason,running,tools[]}, configError}` |
| `POST /api/mcp/reload` | —— | 重读配置并重建 `mcp__*` 工具清单，返回最新 status |
| `POST /api/mcp/servers` | `{server{name,transport,command?,args?,url?,env?}}` | 写回一条 server 配置并 reload，返回最新 status（未启用 / 配置非法 / 治理面接管运行时 400，任务执行中 409） |
| `POST /api/mcp/servers/remove` | `{name}` | 移除一条 server 并 reload（不存在 400） |
| `POST /api/mcp/test` | `{name}` | `{ok, name, count?, tools?, error?}`（连接失败不报 5xx，回 `ok:false`） |

**MCP 管理面板**（侧栏 `MCP`，快捷键 `Ctrl+9`）：可视化地**启用开关**（复用
`GET` + `POST /api/settings` 的 `mcpEnabled`，被 `VIBE_MCP` 锁定时置灰）、查看 server 列表与
状态、**增 / 改 / 删 server**、**测试连接**（`/api/mcp/test`）与**重载**（`/api/mcp/reload`）。
写入**只落** `<工作区>/.vibe/mcp.json` —— 界面从不把命令拼进 argv，`command`/`args` 始终只取自该
受信配置文件（ADR-015）。启用开关本身是「用户本人动作」（ADR-011）：走既有设置接口、不落审批，
但**外部 MCP 工具恒 `Risk.REVIEW`**、每次调用仍需人工审批。

三条信任边界（**ADR-015**）：**外部工具恒 `Risk.REVIEW`**（每次调用都走审批，不可配置放行）；
工具名 `mcp__<server>__<tool>` **前缀隔离**，与内置工具或彼此**名字冲突时拒绝启动该 server**
并给原因；server 的 `command`/`args` 只取自**受信配置文件**（**不接受运行时用户输入拼命令**），
子进程 `shell=False`、`cwd` 锁工作区、`env` 过 `scrub_env()`。调用结果经 `wrap_untrusted` 包裹
后才进上下文；审计 `mcp.call` / `mcp.server_start` 只记
`{server, tool, ok, argc, args_digest, ms}`（**参数正文只留本机**）。server 起不来只影响自身
（`/api/mcp/status` 报 `available:false + 原因`），会话完全不受影响。

### 治理面登录（M3+）的端点

桌面端过去只能「先用 CLI 登录、再重启」。这三个端点把「填地址 → 登录 / 邀请码注册 →
**热接入**」做成闭环：登录后令牌写进用户级凭证文件（`~/.vibe/credentials.json`，0o600 原子写），
当前会话的模型客户端与策略闸门**立即**换成治理面那一套 —— **不重建会话**，对话 / 检查点 /
审计链都保留（`Agent.install_governance`）。

| 端点 | 请求 | 返回 |
|---|---|---|
| `POST /api/governance/login` | `{email, password, serverUrl?, deviceName?}` | `{ok, governance, session}`；`serverUrl` 缺省回退配置（都为空则 400） |
| `POST /api/governance/register` | `{enrollmentToken, email, password, displayName?, serverUrl?, deviceName?}` | 同 login；走 `GovernanceClient.register()` |
| `POST /api/governance/logout` | `{allDevices?}` | `{ok, governance, session}`；**网络失败也清本地凭证**，会话回落本地直连 |

四条边界：**令牌绝不外泄** —— 响应只回 `governance.describe()`（掩码视图，不含任何令牌）；
**执行中 409**（登录会换掉模型客户端与策略闸门，与「执行中不可改模型设置」同一纪律）；
**是「用户本人动作」**（ADR-011：不走审批浮层，但记 `governance.login` / `governance.register` /
`governance.logout` 审计）；**退得出去** —— 治理面不可达时仍清本地凭证，不让用户被永久困在登录态。

**模型配置的可改边界**：`POST /api/settings` 只在治理面**真的把模型请求送去云端网关**
（策略 `allowCloud` 且策略未过期）时返回 400。此时本机 `~/.vibe/settings.json` 里的
`base_url` / `model` 不再是权威，允许保存只会留下「提示成功、请求仍走网关」的错位。
策略禁云 / 过期 / 降级时请求全部落到本地客户端，本机配置就是权威 —— 界面照常可改。
`GET /api/settings` 的 `modelLocked` 指示「当前能否改」，`governance` 仍只表示「是否接入治理面」。

**MCP 目录同一判据**：`POST /api/mcp/servers`（及 remove / reload）用同一个
`is_config_locked()` 闸门 —— 治理面接管运行时（走云端网关）才拒绝写入；禁云 / 降级 / 未登录时
本机 `<ws>/.vibe/mcp.json` 就是唯一真源，照常可写。治理面从不托管 MCP 目录。

桌面端入口：标题栏「治理面」按钮（始终可见，未配 `VIBE_SERVER_URL` 时也可用）打开登录弹窗，
支持密码登录 / 邀请码注册 / 退出；「去登录」徽标（治理面已配置但未登录 / 降级时出现）同样打开该弹窗。
弹窗里的地址可经桌面壳配置桥写进 `config.json`（`serverUrl`），下次启动作为 `VIBE_SERVER_URL`
注入后端；**本次登录直接用弹窗里填的地址，无需重启**。

### 交互式终端（P1-7）的环境变量与端点

| 环境变量 | `Config` 字段 | 默认 | 作用 | 缺失 / 关闭时的行为 |
|---|---|---|---|---|
| `VIBE_TERM`（M4） | `terminal_enabled` | `true` | 是否启用交互式终端（用户在 IDE 里直接敲命令） | `0` = **关闭**：界面隐藏终端页签、端点拒绝，Agent 通道不受影响 |
| `VIBE_TERM_SHELL`（M4） | `terminal_shell` | `""` | 终端启动的 shell 可执行文件 | 留空 = 按平台默认（Unix `$SHELL`/`sh`，Windows `powershell.exe`） |
| `VIBE_TERM_MAX`（M4） | `terminal_max_sessions` | `0` | 同时存在的终端会话上限。**`0` = 不限**（默认）：想开几个开几个，资源兜底交给空闲回收 | 设正整数才生效：超了明确拒绝（`TerminalError`），前端把「新建」按钮禁用并说明 |

| 端点 | 请求 | 返回 |
|---|---|---|
| `GET /api/term` | —— | `{enabled, maxSessions, idleSeconds, fallbackReason, sessions:[{termId, mode, shell, cols, rows, pid, alive, idleSeconds}]}`（界面刷新后据此恢复标签并**对账**；只回元数据，不含输出） |
| `POST /api/term/open` | `{cols?, rows?}` | `{termId, mode, shell, cols, rows, pid, fallbackReason}` |
| `POST /api/term/input` | `{termId, data}` | `{ok, chars}`（`data` 为空直接返回，**不记审计**） |
| `POST /api/term/resize` | `{termId, cols, rows}` | `{ok, cols, rows}` |
| `POST /api/term/close` | `{termId}` | `{ok, exitCode}` |

SSE 事件：`term.data`（`{termId, data}`，`data` 为 **base64 原始字节**）、`term.exit`
（`{termId, exitCode}`），与 `run.*` 完全分离。四条边界（**ADR-016**）：**与 Agent 通道彻底隔离**
（Agent 不能写/读终端，也不能借终端绕过审批；Agent 跑命令仍只能走受审批的 `run_command`）；
**终端命令不弹审批浮层**（用户本人动作，对齐 ADR-011）但 `term.open`/`term.close` 记审计
（`{termId, argc, argv_digest, cols, rows, mode, remote, ms}`，**只记 digest、计数与连接名**）；**零强制依赖**
（Unix 标准库 `pty`/`fcntl`，Windows `ctypes` 直调 ConPTY，`dependencies` 保持 `[]`）；
ConPTY 不可用时**降级为管道模式**并把 `mode`/`fallbackReason` 如实回显到界面（**绝不半启动**）；
Windows 上伪控制台子进程会显式清空标准句柄（`STARTF_USESTDHANDLES` + NULL）且不带
`CREATE_NO_WINDOW` —— 桌面壳把内核 stdio 重定向成管道，少了这条 shell 的输出会写回内核管道。
**终端跟随工作区**：工作区是 `@remote/<连接>/<子目录>` 时，`POST /api/term/open` 起的是
`ssh -tt <加固参数> <host>`（`kind="remote"`、标签为连接名、起始目录为连接 root + 子目录），
按键/回显/resize 全部复用同一条伪终端链路；连接未打开时明确报错，不静默退回本机 shell。
**清单驱动的远端项目工作区**（M11 §7：目录里只放 `.vibe/remote.json`、代码全在设备上）
同样接到远端：连接解析沿用 `RemoteStore.resolve`（默认 > 唯一那条），起始目录为连接 root。
判据是「除 `.vibe/` 外没有本地内容」—— 本地项目只是顺手配了远端连接时终端仍在本机。
界面支持**终端右键菜单**（复制 / 剪切 / 粘贴，带快捷键提示）与 `Ctrl+C`（有选区复制、
无选区中断）/ `Ctrl+X`（复制并收起选区）/ `Ctrl+V`（粘贴）三组快捷键；剪贴板在桌面壳里
走 `vibeShell.clipboardRead/Write` 窄桥（只渡纯文本），浏览器里退回 `navigator.clipboard`。
终端宿主里还藏着一个透明 `textarea`（`.term-ime`）专门接**中文输入法**：组合期间
（`compositionstart`）按键全部让给 IME 不上送，`compositionend` 再把最终文本整段送进
PTY（管道模式走本地行编辑）；候选框跟随光标格，焦点高亮用 `:focus-within` 保持不变。
收尾按**整棵进程树**终止（Unix `killpg`、Windows `taskkill /T /F`），另设**空闲回收**
（默认 30 分钟，不设上限时这就是唯一的资源兜底）与 `atexit` 兜底，杜绝孤儿进程。
界面支持**多终端标签**（P1-7 多标签化）：每个终端独立缓冲与 DOM，切标签只换显示、
进程照常跑；进程退出的标签保留（可回看最后的输出），✕ / ⏻ / 全部关闭三条收路都
会把后端进程一并收掉。

### 多模态图片输入（P1-10）的环境变量与端点

| 环境变量 | `Config` 字段 | 默认 | 作用 | 缺失 / 关闭时的行为 |
|---|---|---|---|---|
| `VIBE_VISION`（N1） | `vision_mode` | `off` | 图片输入模式：`off` / `auto` / `on` | 默认 **`off`**（图片会外发，必须显式开）：界面隐藏选图入口、`image` 引用降级为提示、请求体逐字节不变 |
| `VIBE_VISION_MAX_BYTES`（N1） | `vision_max_image_bytes` | `4000000` | 单张图片字节上限（落盘前校验） | —— |

| 端点 | 请求 | 返回 |
|---|---|---|
| `POST /api/upload-image` | `{name, data, mediaType?}`（`data` 为 base64，可带 `data:` 前缀） | `{ref{type,value}, bytes, ext, digest}`；`VIBE_VISION=off` 时 400 |

`auto` 与 `on` 的区别：`auto` 首次带图若上游 **400/422 且报错含图片相关词**（双条件）→
永久回退为纯文本（图片换成一行占位说明）并**重发一次**、给一条 `run.notice`；`on` 不探测。
图片**只落本机** `<工作区>/.vibe/images/`（PathGuard 保护，模型写不进），落盘**按魔数**判类型；
审计 `image.upload` **只记** `{bytes, ext, digest}`（ADR-006）——图片正文只随请求发往用户配置的
模型端点，**该端点非本地时图片会离开本机**（见 ADR-018）。

### 浏览器预览 / 网页自检（O / P1-11，见 ADR-019）

| 端点 | 请求 | 返回 |
|---|---|---|
| `GET /preview/<relpath>`（**不在 `/api/*`，故免令牌**） | 路径即工作区相对路径 | 网页文件的静态内容（`Content-Type` 按 MIME 白名单）；越权 / `.vibe/` / 白名单外后缀 → 403，目录回落其 `index.html` |
| `POST /api/visual/check` | `{url, width?, height?, image, baseline?, setBaseline?}`（`image` 为 base64 PNG） | `{available, ok, diffRatio, changedBlocks, totalBlocks, baselineMissing, saved?, diffImageDigest, reason?}`；**不带 `image` 时回 `{available:false}`**（浏览器直开没有捕获能力），不是 500 |

预览响应带**专用 CSP**（`connect-src 'none'` 焊死网络出口）+ `Referrer-Policy: no-referrer`。
截图只能由 **Electron 壳**的离屏桥 `vibe:capture-page` 产生（host 是 Python 进程，无法反向调
IPC），host 用标准库 `zlib` 手解 PNG 后与 `<工作区>/.vibe/visual/<hash>.png` 基线做 **16×16
分块均色差**（粗粒度提示，非像素级回归）。基线只落 host 侧 `.vibe/visual/`（模型写不进），
审计 `visual.check` 只记 `{url_digest, width, height, diff_ratio, baseline_missing, ms}`（ADR-006）。

**真实应用预览（P2，见 ADR-039）** —— 让预览面板指向用户自己起的本机 dev server：

| 环境变量 | `Config` 字段 | 默认 | 作用 | 缺失 / 关闭时的行为 |
|---|---|---|---|---|
| `VIBE_PREVIEW_EXTERNAL`（P2） | `preview_external` | `false` | 是否允许预览内嵌本机 dev server（`http://127.0.0.1:<port>`） | `0`/未设 = **关闭**：界面 CSP 的 `frame-src` 仍是 `'self'`（**逐字节不变**）、`POST /api/preview/external` 回 403、界面入口置灰并写明原因 |

| 端点 | 请求 | 返回 |
|---|---|---|
| `GET /api/preview/external` | —— | `{enabled}`（界面据此决定入口亮不亮） |
| `POST /api/preview/external` | `{url}` | `{ok, url, port}`；只接受 `http://127.0.0.1:<port>[/path][?query]`，其余 400；门控关闭 403 |

**隔离来自跨源而非 CSP**：预览帧是另一个源，读不到 `/api/*` 与父页 DOM。因此 CSP 只把
`frame-src` 从 `'self'` 放宽到 `'self' http://127.0.0.1:*`（从 `CSP` **派生**，不手抄第二份），
其余指令与 `/preview/` 的 `connect-src 'none'` 一律不动。地址校验**只认 `127.0.0.1`
字面量**（`localhost` / https / 外部域 / 本应用自身端口一律拒绝），保证「校验放行的集合 ⊆
CSP 放行的集合」。**host 不起进程、不转发字节** —— dev server 由用户自己启动，故零新增
执行面；审计 `preview.external` 只记 `{port, host}`，不记路径与页面内容（ADR-006）。
跨源读不到页面内部错误（`onerror`），该帧也不参与自动刷新与截图自检。

**中间区三视图（浏览器 / 终端 / 编辑器）** —— 三块在同一列（`#editor-stack`）里切换：

- **编辑器**：原有的文件标签区，默认视图。
- **终端**：复用底部那块交互式终端并展开到全高（DOM 只有一份，缓冲与滚动位置不丢）。
- **浏览器**：内嵌本机跑起来的界面（`http://127.0.0.1:<端口>`），与上面的「本机应用」预览
  共用同一条门控（`VIBE_PREVIEW_EXTERNAL`）与服务端地址校验（`POST /api/preview/external`）。
  终端 / Agent 命令输出里出现本机回环地址（如 `http://localhost:5173/`）时，界面会把地址
  填好并露出「打开」入口，但**不自动切视图**（不打断正在看的输出）。桌面壳启动内核时默认
  注入 `VIBE_PREVIEW_EXTERNAL=1`；显式设成 `0` 仍会关闭（内核自身默认不变）。
  渲染载体分两档：**桌面壳里用 `<webview>`**（独立 WebContents 的顶层导航），因为不少本机
  应用会带 `X-Frame-Options` / CSP `frame-ancestors`，用 iframe 会被拒成白屏；壳在
  `will-attach-webview` 里剥掉 `preload` / Node 能力，并只放行 `http://127.0.0.1:<端口>`。
  直接用浏览器打开 host 页面时没有 webview，自动退回原 iframe（跨源 `sandbox`）。

**远端设备预览（P3，见 ADR-040）** —— 把远端设备上的环回端口经 `ssh -L` 搬到本机环回：

| 环境变量 | `Config` 字段 | 默认 | 作用 | 缺失 / 关闭时的行为 |
|---|---|---|---|---|
| `VIBE_PREVIEW_REMOTE`（P3） | `preview_remote` | `false` | 是否允许把远端回环端口转发到本机并预览 | `0`/未设 = **关闭**：`GET /api/preview/tunnel` 回 `enabled:false`、`POST` 回 403、界面「远端」入口置灰。**另需 `VIBE_REMOTE=1`**（复用连接清单与 SSH 加固） |

| 端点 | 请求 | 返回 |
|---|---|---|
| `GET /api/preview/tunnel` | —— | `{enabled, tunnels[], max}`（只含元数据） |
| `POST /api/preview/tunnel` | `{connection, remotePort}` | `{ok, id, url, localPort, remotePort, state}`；端口非法/连接不存在 400、链路起不来 502、门控关闭 403 |
| `POST /api/preview/tunnel/close` | `{id}` | `{ok, closed, refs}` |

三条硬约束：`-L` 规格**两端由代码钉死为环回**（用户只能给整数端口，本地端口由内核分配）；
**不用 `-N`** 而是建会话跑 `cat >/dev/null`，父进程一死 ssh 跟着退 —— 孤儿监听在结构上不可能；
**不新增 CSP 放宽点**（转发落在 `127.0.0.1`，复用 P2 那一条）。关标签、刷新页面（`pagehide`
+ keepalive）、会话结束三处都会释放；审计 `preview.tunnel` 只记连接名与两个端口。

### 规则与记忆体系（P / P2-15，见 ADR-021）

| 环境变量 | `Config` 字段 | 默认 | 作用 | 缺失 / 关闭时的行为 |
|---|---|---|---|---|
| `VIBE_RULES_EXTRA`（P1） | `rules_extra` | `""` | 规则附加目录（glob，相对工作区） | 留空 = 只读 VIBE.md / `.vibe/rules/*.md` / 目录级 |
| `VIBE_MEMORY`（P7） | `memory_enabled` | `true` | 跨会话记忆开关 | `0` = **关闭**：不建库、不注入、不注册 `remember`，行为逐字节一致 |
| `VIBE_MEMORY_MAX`（P7） | `memory_max_items` | `200` | 记忆库容量上限（条） | 达上限写入明确拒绝（**不静默淘汰**） |
| `VIBE_MEMORY_INJECT`（P7） | `memory_inject_limit` | `5` | 单轮最多注入几条相关记忆（top-N） | `0` = 不注入 |
| `VIBE_MEMORY_DB`（P7） | `memory_db` | `""` | 记忆库文件路径 | 留空 = `~/.vibe/memories.db`（用户级，跨工作区） |

| 端点 | 请求 | 返回 |
|---|---|---|
| `GET /api/memories` | —— | `{enabled, memories[]{id, text, tags, scope, createdAt}}` |
| `POST /api/memories` | `{text, tags?}` | `{ok, memory{id, text, tags, scope, createdAt}}`；空文本 / 超长 / 超上限 → 400 |
| `POST /api/memories/delete` | `{id}` | `{ok, removed}` |

规则**多层合并**（用户级 → 仓库级 → `.vibe/rules/*.md` + 附加目录 → 目录级）：单层**无抬头**
（逐字节兼容改造前），多层各带一行 `### 规则来源：<source>`；单层 32KB、总量 64KB 上限，
超限**先丢最泛的层**（目录级优先保留）。审计 `rules.load` 只记 `{layers, chars, truncated_count}`。

记忆是**持久化注入面**：`remember` 工具**恒为 `Risk.REVIEW`**（必须人工审批，ADR-008），
模型只能**提议**、用户确认后才落库，**禁止静默自动学习**。检索注入按词法相关度取 top-N
（不引 embedding，零强制依赖），正文一律 `wrap_untrusted` 包裹；审计 `memory.add` 只记
`{scope, chars, digest}`、`memory.inject` 只记 `{count, chars}` —— 记忆**正文绝不上报**（ADR-006）。
`VIBE_MEMORY=0` 时不建库、不注入、不注册工具，行为与改造前逐字节一致。

### 多 Agent 编排（M3，见 ADR-022）

父 Agent 经 `task` 工具派生一个**只读子 Agent**，把「扫一遍仓库、找调用点、看依赖」
这类**用完即弃**的探索**下沉**出去：子 Agent 在自己的小上下文里探索，只把**压成要点的
结论**交回父 Agent，父的上下文因此变干净、决策更聚焦。**不引入 LangGraph / AutoGen**
（ADR-003：手写编排）。

| 环境变量 | `Config` 字段 | 默认 | 作用 | 缺失 / 关闭时的行为 |
|---|---|---|---|---|
| `VIBE_SUBAGENT`（M3） | `subagent_enabled` | `false` | 是否启用主从只读子 Agent | `0`/未设 = **关闭**：不注册 `task` 工具、无 `run.subagent` 事件，工具清单与行为**逐字节一致** |
| `VIBE_SUBAGENT_MAX_ITERATIONS`（M3） | `subagent_max_iterations` | `8` | 单个子 Agent 的迭代上限（独立于父的 `VIBE_MAX_ITERATIONS`） | —— |

**只读工具面**（`build_readonly_registry()`，白名单）：`repo_map` / `list_dir` / `read_file` /
`grep` / `find_files` / `semantic_search` / `git_diff`（LSP 可用时附 `lsp_diagnostics`）。
**显式排除**：`edit_file` / `write_file` / `edit_files` / `update_plan` / `run_command` /
`remember` / **`task`** / `mcp__*` —— 写盘、执行、递归、外部工具在子 Agent 侧**结构上不存在**。

SSE 事件 `run.subagent`（`{runId, callId, phase, ...}`，`phase ∈ started｜iteration｜tool｜
tool_result｜notice｜finished｜failed`），`callId` 关联到父侧 `task` 工具卡片（界面把它渲染成
**可展开的内嵌子 Agent 卡片**）。

四条硬边界（**ADR-022**）：

1. **子 Agent 只读** —— 白名单（写/执行工具不存在）+ `allow_write=False`（运行时纵深防御）
   两道；任何写盘一律回到父 Agent 走既有审批漏斗，子 Agent **不进审批**。
2. **无递归** —— 子注册表**不含 `task`**，结构上不可能再往下派生（避免无限套娃烧预算）。
3. **共用审计实例** —— 父子**共用同一个 `AuditLog`**（`_last_hash` 是内存态，各持一份会让
   哈希链交错、被 `verify_chain` 判为篡改）；审计 `subagent.run` 只记
   `{ok, iterations, tool_calls, answer_chars, task_digest}`（**只记元数据，无正文**，ADR-006）。
4. **结果恒包裹** —— 子 Agent 摘要是工作区文本的再加工，**天然是注入面**，故 `task` 的返回值
   一律 `wrap_untrusted("subagent", ...)` 包裹后才进父上下文；`task` 本身是 `Risk.SAFE`
   （只读委派，不写盘、不下命令）。

中断**透传**：父被中断时子 Agent 在**轮次边界**响应（`should_stop` 共用），不外溢子 Agent
消息/计划到父视图（`_SubAgentHooks` 不转发 `on_message` / `on_plan` / `on_context`）。

---

### 检索增强与 Repo Wiki（M3+，见 ADR-023 / ADR-024）

两项都**默认关闭** —— 未设环境变量时，`semantic_search` 结果、工具清单与界面与改造前**逐字节一致**。

| 环境变量 | `Config` 字段 | 默认 | 作用 | 缺失 / 关闭时的行为 |
|---|---|---|---|---|
| `VIBE_RETRIEVAL_ENHANCED`（M3+） | `retrieval_enhanced` | `false` | 检索增强：分词子词化 + 符号感知分块 + RRF 融合 + 按文件去冗余 | `0`/未设 = **关闭**：分词/分块/融合全走旧路径，`semantic_search` 结果逐字节一致 |
| `VIBE_WIKI`（M3+） | `wiki_enabled` | `false` | 是否注册 `repo_wiki` 工具（生成落盘的仓库 Wiki） | `0`/未设 = **关闭**：不注册 `repo_wiki`，工具清单逐字节不变 |
| `VIBE_WIKI_DIR`（M3+） | `wiki_dir` | `docs/wiki` | Wiki 落点（相对工作区） | **不得设 `.vibe/`** —— 会被 PathGuard 拒绝写入 |
| `VIBE_WIKI_MAX_FILES`（M3+） | `wiki_max_files` | `400` | 单次生成最多覆盖多少源文件 | 超出会**显式截断标注**，绝不静默丢 |

**检索增强**（`index/lexical.py` + `tools/semantic.py`）：三处改进都用 stdlib，**不引入向量库**。

1. **标识符子词化** —— `getUserName` → get/user/name（snake/kebab/数字边界同理），
   **原词与 CJK 二字组保留**，不丢精确匹配面；
2. **符号感知分块** —— 块边界落在符号定义处；无符号时退化得与旧 `chunk_text` 一致；
3. **RRF 融合（k=60）+ 按文件去冗余（N=3）** —— 词法分与余弦量纲不同，RRF 只按**名次**
   相加、天然免疫量纲；去冗余避免一个文件的相邻窗口占满结果。

默认路径（`retrieval_enhanced=False`）仍走**分数加权相加**（`backend=hybrid`），
增强路径才用 RRF（`backend=rrf`）；缺 `VIBE_EMBED_MODEL` 时回退纯词法（`backend=lexical`）。

**Repo Wiki**（`wiki/generate.py` + `tools/repo_wiki.py`）：`repo_wiki` 把项目结构渲染成
markdown（总览 README + 每模块一页），是给**人**看、可提交进版本库的文档。

- **恒 `Risk.REVIEW`**：落盘逐文件走 `ctx.snapshot_before` + `plan_overwrite` + `commit_plan`
  （与 `write_file` 同一条编辑面，ADR-004）；一次调用产出的全部文件在**一个**审批浮层看全。
- **纯本机、零外发**：只由目录 / 符号 / 依赖边渲染，**不调用模型、正文不出机**（ADR-006）。
- **落点**：默认 `docs/wiki/`；越界 / `.vibe/` / 工作区根目录一律被 `check_write` 拒绝。
- **覆盖**：落点已有同名文件时，未显式 `overwrite=true` 即拒绝；内容一致的文件不落盘。
- **审计**：`tool.repo_wiki` 只记 `path / files / created / updated / bytes / truncated`
  （**元数据，无正文**）。
- **不在只读面**：`repo_wiki` **不进** `build_readonly_registry()`（子 Agent 恒只读，不写盘）。

### 受控联网检索（M7，见 ADR-027）

两个新内置工具 `web_search` / `web_fetch`，**默认关闭**（`web_enabled=False` 时工具注册表、
对话行为与审计流与 M6 收口**逐字节一致**）。

| 环境变量 | `Config` 字段 | 默认 | 作用 | 缺失 / 关闭时的行为 |
|---|---|---|---|---|
| `VIBE_WEB`（M7） | `web_enabled` | `false` | 总开关：是否注册 `web_search` / `web_fetch` | `0`/未设 = **关闭**：不注册两个工具，清单逐字节不变 |
| `VIBE_WEB_SEARCH`（M7） | `web_search_provider` | `gateway` | 搜索形态：`gateway`（治理面中转）/ `searxng`（内网自部署 JSON） | 非法值**启动前报错** |
| `VIBE_WEB_SEARCH_URL`（M7） | `web_search_url` | `""` | searxng 的 JSON 端点（gateway 模式忽略，由 `server_url` 派生 `<server>/api/v1/search`） | 空且 searxng 模式 → 调用时明确报错 |
| `VIBE_WEB_ALLOW_PRIVATE`（M7） | `web_fetch_allow_private` | `false` | 允许 fetch 私网 / 环回地址 | 未设 = 仅公网 |
| `VIBE_WEB_ALLOW_HOSTS`（M7） | `web_allow_hosts` | `""` | 逗号分隔 glob 白名单 | 非空时仅白名单内 host 可 fetch |
| `VIBE_WEB_DENY_HOSTS`（M7） | `web_deny_hosts` | `""` | 逗号分隔 glob 黑名单（**恒优先**；策略下发项并进这里） | —— |
| `VIBE_WEB_TIMEOUT`（M7） | `web_timeout` | `20` | 单次请求超时（秒） | —— |
| `VIBE_WEB_MAX_BYTES`（M7） | `web_max_bytes` | `2000000` | 单次 fetch 字节上限（**超限拒绝，不截断**） | —— |
| `VIBE_WEB_MAX_RESULTS`（M7） | `web_search_max_results` | `8` | 搜索返回条数上限 | —— |

**三条纪律**（ADR-027）：**恒 `Risk.REVIEW`、不可配置放行**（审批浮层 = 外发内容人工确认闸门）；
结果恒 `wrap_untrusted`（取回正文是注入面）；`query`/正文上传只留 `*_digest + *_chars`
（URL 只上传 host + path，query string 换 digest）。**SSRF 六步防护**：协议白名单 → host 名单
（deny 优先）→ **DNS 解析后校验 IP** → 重定向逐跳重跑（≤3 跳）→ Content-Type 白名单 + 字节上限
→ 按 charset 解码（多字节切点按字符回退）。**不进 `build_readonly_registry()`**（子 Agent 恒不联网）。

**零依赖**：`urllib.request` + `socket.getaddrinfo` + `html.parser`，不引 requests/httpx/bs4。

---

### 可复用工作流 / Skill（M9-A，见 ADR-030）

把「怎么做某类任务」固化成**可复用、带描述、按需加载**的工作流（对标 Claude Code 的
`/commit` `/review`、Cursor Rules、Cline custom command）。Skill **不是** `task` 子 Agent
（那是临时只读探索）：它是「指令 + 触发条件 + 编排既有工具」的高阶指令，**不新增底层工具、
不新增写盘路径**。默认关闭。

| 环境变量 | `Config` 字段 | 默认 | 作用 | 缺失 / 关闭时的行为 |
|---|---|---|---|---|
| `VIBE_SKILLS`（M9-A） | `skills_enabled` | `false` | 是否启用 Skill | `0`/未设 = **关闭**：不加载、不注册 `run_skill`，工具清单与提示词**逐字节一致** |
| `VIBE_SKILLS_DIR`（M9-A） | `skills_dir` | `.vibe/skills` | Skill 定义目录（每个子目录一个 `SKILL.md`） | 用户本人手工放置的受信资产，与模型不可写的 `.vibe/` 审计目录语义不同 |
| `VIBE_SKILLS_MAX_FILES`（M9-A） | `skills_max_files` | `50` | 单次最多加载的文件数 | —— |
| `VIBE_SKILLS_MAX_BYTES`（M9-A） | `skills_max_bytes` | `256000` | 加载正文总量上限 | 超限显式截断标注 |

**边界**：`run_skill` 是 **Risk.SAFE**（只展开正文、不写盘、不执行），未知 name 报 `ToolError`
fail-closed；Skill 正文与规则同源，注入一律 `wrap_untrusted`（ADR-021）；Skill 内部要写盘时
仍走既有 `edit_*` / `run_command` 审批漏斗（ADR-008 不变）。**不进 `build_readonly_registry()`**
（子 Agent 不加载 Skill 工作流，保持只读）。审计 `skill.run` 只记 `{name, chars, source}`
（**全元数据，不含 Skill 正文**）。CLI `/skills` 列出、`/skill <name>` 展开。

### 测试驱动的自动修复闭环（M9-C，见 ADR-031）

`test_loop` 工具驱动一个「跑测试 → 失败喂回摘要 → 修复 → 重跑 → 绿」的**目标循环**
（对标 Aider `/test`）。默认关闭。

| 环境变量 | `Config` 字段 | 默认 | 作用 | 缺失 / 关闭时的行为 |
|---|---|---|---|---|
| `VIBE_TEST_LOOP`（M9-C） | `test_loop_enabled` | `false` | 是否启用测试闭环 | `0`/未设 = **关闭**：不注册 `test_loop`，工具清单逐字节一致 |
| `VIBE_TEST_LOOP_MAX`（M9-C） | `test_loop_max_rounds` | `5` | 单次闭环重试上限 | 到顶仍未绿时如实报告并停止（不死循环） |
| `VIBE_TEST_LOOP_TIMEOUT`（M9-C） | `test_loop_timeout` | `300` | 单次测试命令执行超时（秒） | 超时即终止本轮闭环 |

**边界**：`test_loop` 是 **Risk.SAFE**（只调度既有工具，不直接写盘/执行）；测试命令本身是
`run_command`（用户可配、过 CommandGuard 白名单，沿用「用户本人动作」口径）；Agent 的**修复
动作恒走审批**（ADR-008，不可配置绕过）——「测试通过」绝不等于「自动批准写盘」。审计
`test_loop.run` 只记 `{command_digest, rounds, passed, failed, ms}`（只记 digest 与计数，
不含测试输出正文）。

### 后台 / 并行 Agent（M9-D，见 ADR-032）

用户本人可经界面发起**后台任务**（异步执行、结果异步回传），`AgentTaskPool` 用标准库
`concurrent.futures` 线程池跑并行子 Agent。默认关闭。

| 环境变量 | `Config` 字段 | 默认 | 作用 | 缺失 / 关闭时的行为 |
|---|---|---|---|---|
| `VIBE_ASYNC_AGENT`（M9-D） | `async_agent_enabled` | `false` | 是否启用后台任务 | `0`/未设 = **关闭**：无 `/api/tasks` 端点，行为逐字节一致 |
| `VIBE_ASYNC_AGENT_MAX`（M9-D） | `async_agent_max` | `4` | 并行上限 | 超限明确拒绝新提交 |
| `VIBE_ASYNC_AGENT_TIMEOUT`（M9-D） | `async_agent_timeout` | `600` | 单个后台任务超时（秒） | 超时即终止并报告 |

**边界**：后台任务由**用户本人**发起（`POST /api/tasks`，对齐 ADR-011），**不新增 Agent 派
Agent 的工具**（规避递归越权）；子任务**只读工具面**（`build_readonly_registry()` + 受控写回，
写回仍回父 Agent 走审批，ADR-008）；**审计链共用**父会话 `AuditLog` 实例（事件带 `task_id`
归属），`task.spawn` 只记 `{taskId, state, chars, ms}`；纯 stdlib 线程池、零新依赖、不落盘
索引（ADR-013）。端点：`POST/GET /api/tasks`、`GET /api/tasks/{id}`、`POST /api/tasks/{id}/cancel`；
SSE 事件 `task.state`（只回摘要，不回任务正文）。

### 代码审阅工作流（M9-H，见 ADR-033）

`review_changes` 工具对「工作区相对 HEAD 的改动」做一轮**提交前自审**，产出结构化意见
（严重级别 / 类别 / 定位 / 说明 / 建议）。默认关闭。

| 环境变量 | `Config` 字段 | 默认 | 作用 | 缺失 / 关闭时的行为 |
|---|---|---|---|---|
| `VIBE_REVIEW`（M9-H） | `review_enabled` | `false` | 是否启用提交前自审 | `0`/未设 = **关闭**：不注册 `review_changes`，工具清单逐字节一致 |
| `VIBE_REVIEW_MAX_FILES`（M9-H） | `review_max_files` | `50` | 单次审阅文件上限 | 超上限提示用 `paths` 收窄，而非静默截断 |

**边界**：`review_changes` 是 **Risk.SAFE**（只读：不改盘、不执行、不派子 Agent）；产出
「意见」而非「补丁」——**不替代人审**（ADR-008 不变），修复仍走既有 `edit_*` 审批漏斗；
意见是模型输出，回灌给用户须 `wrap_untrusted`（ADR-021）；审计 `review.run` 只记
`{files, issues, severity_high, ms}`（只记计数与严重级分布，不含意见正文）。

### 客户端模型选择器（M9-I，见 ADR-034）

按任务难度在「服务端已授权的模型集合」内选型：简单任务走小模型、复杂任务走大模型。
默认关闭。

| 环境变量 | `Config` 字段 | 默认 | 作用 | 缺失 / 关闭时的行为 |
|---|---|---|---|---|
| `VIBE_MODEL_ROUTER`（M9-I） | `model_router_enabled` | `false` | 是否启用按任务难度选型 | `0`/未设 = **关闭**：走既有单模型路径，请求体逐字节一致 |
| `VIBE_MODEL_ROUTER_POLICY`（M9-I） | `model_router_policy` | `none` | 选型策略：`none` / `static` / `size` | 非法值回退 `none` 并告警 |
| `VIBE_MODEL_ROUTER_DEFAULT`（M9-I） | `model_router_default` | `""` | 兜底模型（空 = 沿用 `model`） | fail-closed 回退时用 |
| `VIBE_MODEL_ROUTER_STATIC_MAP`（M9-I） | `model_router_static_map` | `""` | `static` 策略映射表 | 留空时 static 退化、按「未匹配回退默认」 |

**边界**：选型只影响「发往哪个已配置端点」（覆盖请求体的 `model` 字段，不改 `chat()` 签名），
**不改「代码不出本机」红线**；选型结果**只能是服务端授权集合的子集**（读 `GET /api/v1/gateway/models`，
本地未接治理面时读本地 `models` 配置），越界即 **fail-closed 回退默认**；FIM `/completions`
不选型（保持 `self.model`）；审计 `model.route` 只记 `{intent, model, reason}`（不含任务正文）。

### 定时任务调度（M10，见 ADR-035）

在 M9-D「后台 / 并行 Agent」的任务池之上补一层**定时 / 周期触发**：用户本人创建 job，
到点把 job 的 `prompt` 投进只读后台任务池；定义落盘、进程重启后**恢复定义并补跑一次**。
默认关闭，且**依赖 M9-D 池**（`VIBE_SCHEDULER=1` 但 `VIBE_ASYNC_AGENT=0` 时不启动调度器）。

| 环境变量 | `Config` 字段 | 默认 | 作用 | 缺失 / 关闭时的行为 |
|---|---|---|---|---|
| `VIBE_SCHEDULER`（M10） | `scheduler_enabled` | `false` | 是否启用定时任务调度 | `0`/未设 = **关闭**：无 `/api/schedules` 端点、无线程、无库文件，行为逐字节一致 |
| `VIBE_SCHEDULER_DB`（M10） | `scheduler_db` | `""` | 任务库路径（空 = 默认落点） | 空 → `<workspace>/.vibe/scheduler.db` |
| `VIBE_SCHEDULER_TICK`（M10） | `scheduler_tick` | `30` | 轮询粒度（秒，**下限 5**） | 小于 5 会被钳到 5，防忙轮询 |
| `VIBE_SCHEDULER_MAX`（M10） | `scheduler_max_jobs` | `50` | job 数上限 | 到顶后新建被拒（明确报错，不静默丢弃） |
| `VIBE_SCHEDULER_MAX_PER_TICK`（M10） | `scheduler_max_per_tick` | `3` | 单轮最多触发数（防风暴） | 余下的留到下一轮，不并发起一堆 |
| `VIBE_SCHEDULER_CATCHUP`（M10） | `scheduler_catch_up` | `true` | 重启补跑开关 | `0` = 只把逾期 job 排到未来、不触发（审计记 `skipped`） |

调度表达式支持三形态：**5 字段 cron**（`分 时 日 月 周`，`*` / `?` / `a,b,c` / `a-b` /
`*/n` / `a-b/n`，周 `0` 与 `7` 均为周日；`日` 与 `周` **同时被限定**时取 OR —— Vixie 语义）、
**`@every <n><s|m|h|d>`**（周期，对齐 epoch 边界）、**`@at <ISO8601>`**（一次性）；非法表达式
一律抛 `ScheduleError`（fail-closed，绝不当「无调度」放行）。

**边界**：触发动作**只有一种** —— 把 prompt 投进 M9-D 的 `AgentTaskPool`，**不新增 shell 通道、
不新增可写盘通道**（只读面与审批漏斗一字不动）；job 由**用户本人**经界面 / API 创建
（对齐 ADR-011），`build_default_registry` 与 `build_readonly_registry` **都不新增**调度工具
（堵死「Agent 自己给自己排期」）；定义库落在 `<workspace>/.vibe/`（宿主资产，同 `.vibe/checkpoints/`，
不走模型写盘通道）；审计 `schedule.job` 只记 `{job_id, action, state, chars, ms}`（**不含 prompt
正文**，ADR-006）。端点：`POST/GET /api/schedules`、`POST /api/schedules/{id}`、
`POST /api/schedules/{id}/delete`、`POST /api/schedules/{id}/run`；SSE 事件 `schedule.state`
（只回摘要，不回 prompt 正文）。**重启补跑**为 catch-up once（逾期 job 各补一次，不连补 N 次）。

### 远程设备文件工作面（M11，见 ADR-038）

在既有的「远程设备接入」（连接清单、系统 `ssh` 传输、路径 Jail、9 个 Agent 工具，
详见 [docs/REMOTE.md](../../docs/REMOTE.md)）之上补一层**面向用户的文件工作面**：
界面管理连接、浏览远端文件树、只读查看、**留底 vs 远端**的代码审查、编辑保存。
**不新增传输协议、不新增门控** —— 与既有远程工具挂在**同一道** `VIBE_REMOTE` 上。

| 环境变量 | `Config` 字段 | 默认 | 关闭时行为 |
|---|---|---|---|
| `VIBE_REMOTE`（M10 链路 + M11 工作面） | `remote_enabled` | `false` | `0`/未设 = **无端点、无页签、无事件、无新工具**，行为逐字节一致 |
| `VIBE_REMOTE_CONFIG` | `remote_config` | `""` | 空 → `<workspace>/.vibe/remote.json` |
| `VIBE_REMOTE_DEFAULT` | `remote_default` | `""` | 默认连接名 |

**新增组件**：

| 文件 | 内容 |
|---|---|
| `agent_core/remote/session.py` | `RemoteSessions`（**会话级状态层**）：活动连接 + 浏览位置 + 本会话远端改动台账 `ChangeEntry`；`open()` 换连接 = **关旧开新** |
| `agent_core/host/protocol.py` | 15 条 `/api/remote/*` 端点 + 两个 SSE 事件 `remote.state` / `remote.edit` |
| `agent_core/tools/remote.py` | 新增只读自审工具 `remote_review_changes`（**恒 SAFE**，不进只读 registry） |
| `agent_core/host/ui/*` | 「远程设备」面板：连接列表 / 远端文件树 / 查看·审查·编辑三页签（`VIBE_REMOTE=0` 时侧栏入口隐藏） |
| `agent_core/loop/prompts.py` | `build_remote_section()`：把本工作区的远端连接**告知模型**，使目标在远端时不必由用户在每句话里点名连接 |
| `agent_core/loop/agent.py` | `_remote_hint()` + 提示词装配（`VIBE_REMOTE=0` 时**零 I/O**，提示词逐字节一致） |

**提示词注入**（M11 补强）：远端工具本身不点名连接也能命中 **默认连接**
（`_resolve_conn` 优先级：工具参数 > `VIBE_REMOTE_DEFAULT` > 清单 `default` > 唯一那条），
缺口在于**模型不知道该用 `remote_*` 而不是本地 `read_file`**。因此在系统提示里追加一节
「远端工作面」，只做两件事：说明目标在哪台机器（名字 / `user@host` / 远端根目录 / 能力位），
以及列出该用哪几个工具。三条纪律：

1. **未启用或无连接 → 一个字不加**（`build_remote_section` 返回空串，提示词与改造前逐字节一致）；
2. **多条且没设默认 → 不猜**，只列名字并指引先调 `remote_connections`（与 `RemoteStore.resolve` 同款纪律）；
3. **子 Agent 恒不注入** —— 远端工具不在 `build_readonly_registry()` 里，注入只会诱导它去试不存在的能力。

**保存语义**（与本地 `POST /api/edit/save` 同口径）：界面写是**用户本人动作**（ADR-011），
不重复走 Agent 审批漏斗，但五道约束一步不省 —— ① 写前**本机留底**（存不下即中止写入，400）；
② `expectedDigest` **乐观锁**（不符即 409，绝不静默覆盖）；③ **受控覆盖 / 结构化补丁**（ADR-004）；
④ 回读字节数校验（不符报 `integrity`）；⑤ 返回**真实 unified diff** 供人当场核对。
`POST /api/remote/patch` 走 `RemoteWorkspace.edit()` 的三级匹配，`changed=false` **不写回**。

**留底目录**：`<工作区>/.vibe/checkpoints/remote/<连接>/<会话>/<相对路径>`；Agent 轮次内的写入
再分层到 `<会话>/<检查点id>/<相对路径>`，并维护该轮 `manifest.json`
（`existed / sha256Before / sha256After / bytes`）。与本地检查点**互不干扰**
（不写本地 manifest、不参与本地 restore），但可由 `POST /api/remote/restore {checkpointId}`
按轮次恢复（乐观锁不匹配**整体拒绝**，纯新建文件才删除），并已接进逐条回退 `POST /api/rollback`。
`GET /api/remote/diff` 就是拿留底与**远端当前内容**比出 unified diff；
留底目录（含 `rollback-*` redo 目录）随远端编辑次数增长且**当前不自动清理**，
更早的、不在轮次 manifest 里的改动仍需**手工**从该目录捞回。
`GET /api/remote/changes` 的 `backup_path` 只回**路径**，正文不出机。

**审计**：新增 `remote.connection`（连接名 / 动作 / 权限位 / 成败 / 耗时）、
`remote.file`（连接 / 路径 / 动作 / 字节 / `sha256` / 增删行 / 留底成败）与
`remote.rollback`（连接 / 路径 / 恢复或删除动作 / 字节 / 是否留底），**只记元数据**（ADR-006）——
**远端文件正文、diff 正文、命令输出一个字符都不出机**；diff 只走端点响应、`remote.edit` 事件与界面。
既有 8 条 `tool.remote_*` 与新增的 `tool.remote_review_changes` **一律登记为纯本地不上传**（`upload=""`）。

**边界**：一次只活动一条连接；只做**文本类**文件（二进制与超限 `MAX_SCAN_BYTES` 明确拒绝，不做截断假预览）；
不做远端二进制 / 图片预览、不做远端构建 / 测试面板、不做双向同步 / 镜像；子 Agent 恒不触远端。

---

### 远端目录「加入工作区」= 命名空间透明路由（M12，见 ADR-041）

把某个**远端目录**登记成命名空间根 `@remote/<连接名>/<相对该连接 root 的路径>`，
它出现在**工作区树**里（多根），模型用**完全相同的本地工具名**直接操作它：

```
read_file("@remote/devbox/项目/smart-writer/src/app.py")
```

内核在**两道 Jail 之前**按前缀把调用分流到远端链路。`PREFIX = "@remote/"`
是单一真源（`agent_core/remote/roots.py`），routing 与提示词都从这里取。

界面上，工作区树里的远端文件**直接在中间编辑区打开**（与本地文件同一套标签与
代码视图，代码区带语法着色），读写走 `/api/remote/file` 与 `/api/remote/save`
（保存沿用乐观锁）；本机 LSP、内联补全与静态网页预览对远端文件不参与 ——
那些能力只认本机路径，硬接会读到同名的本地文件。

| 环境变量 | `Config` 字段 | 默认 | 关闭时行为 |
|---|---|---|---|
| `VIBE_REMOTE` | `remote_enabled` | `false` | **不新增任何环境变量**；`0`/未设 = 无端点、无右键、无事件字段、无路由壳，工具清单 / schema / 提示词**逐字节一致** |

**为什么是「换实现」而不是「加工具」**：可路由工具的 `ToolSpec` 被换成**同名路由壳**
（`name` / `description` / `parameters` / `risk` 原样，只把 `handler` / `assessor` /
`previewer` / `drafter` 换成按 `args["path"]` 选臂的闭包）。于是模型看到的工具面一字不变，
`ToolRegistry.register` 也不会撞重名。四件套共用**同一份**选臂逻辑，结构上排除
「审批时按本地预演、执行时却打到远端」这种最危险错配。

**路由面**（定稿）：

| 工具名 | 远端实现 | 风险 | 备注 |
|---|---|---|---|
| `read_file` / `list_dir` / `find_files` | `RemoteWorkspace` 同名原语 | SAFE | `find_files` 递归 list 走查，上限 2000 条并标 `truncated`，**不调 shell** |
| `grep` | `RemoteWorkspace.grep()` | **REVIEW** | 沿用 M11「模式串会发到远端」口径，理由里点名连接名 |
| `edit_file` | `_plan_remote_edit` + `workspace.edit()` + `_backup_before_write` | **REVIEW** | 复用 M11 同一份三级匹配，返回真实 diff |
| `write_file` | `workspace.write()` + `_backup_before_write` | **REVIEW** | 已存在须显式 `overwrite=true`（ADR-004 受控例外），否则拒 |
| `edit_files` / `run_command` / `repo_map` / `semantic_search` / `git_diff` … | **不路由** | — | 收到 `@remote/...` 由 `PathGuard.reserved_prefixes` 抛 `SandboxViolation(kind="reserved_ns")`，**不静默**当成本地文件 |

**远端写一条不省**：结构化补丁或受控覆盖 + 写前本机留底（存不下则中止）+
返回真实 diff + 审批漏斗；改动登记在**命名空间路径**下（`ctx.record_change(path=ns_path)`），
因此远端改动出现在本机 `/api/diffs` 与改动面板，与本地编辑同口径。

**端点**（`VIBE_REMOTE` 门控，未启用一律 400）：

| 方法与路径 | 作用 |
|---|---|
| `GET /api/remote/roots` | `{roots: [{connection, sub, nsPath, label, ok, reason}], problems[]}`（`ok/reason` 是**纯文本**校验结论，**不探远端**） |
| `POST /api/remote/roots` | `{connection, sub, label?}` → 最新 roots；未知连接 / 越出 root / 重复 → 400（人话） |
| `POST /api/remote/roots/delete` | `{connection, sub}` → 最新 roots；不存在 → 400 |
| `GET /api/remote/tree?connection=&path=` | 已有端点**扩展参数**；`connection` 非活动 → `{ok:false, reason:"该连接未打开"}`，**不静默换连接** |

`remote.state` 事件增字段 `roots`（ns 路径数组，不含正文）。`GET /api/tree` 不改。

**状态落盘**：`.vibe/remote.json` 顶层 `roots` 数组（与连接清单同一份文件、同一套原子写与 0600），
**不升 `FORMAT_VERSION`**（老客户端把 `roots` 当未知键忽略）；坏根（连接不存在 / 写法非法）
载入时**丢弃并记进 `problems`**，不抛错。

**提示词**：`build_remote_section(..., roots=...)` 在 roots 非空时追加一段「已加入工作区的远端目录
（可直接用本地同名工具操作）」，逐行列 ns 路径；**空时一个字符都不加**（逐字节一致）。
根增删后走 `router.invalidate()` + `Agent.refresh_system_prompt()` 重算，故**当轮即生效**
（显式 `system_prompt` 的子 Agent 不重算）。

**审计**：**不新增事件**、**不动三处同步**。路由调用照旧走 `tool.execute`
（`tool=call.name`、`path=result.meta["path"]` 填命名空间路径）；根管理复用
`remote.connection` 的 `action="root_add"/"root_remove"`（该字段两处均为 `str`、不做枚举，零 schema 成本）。

**边界**：一次只活动一条连接（展开非活动根的子树前先打开该连接）；
「加入工作区」只允许 `conn.root` 的子目录（含 root 本身），校验**纯文本**（离线也能加根，
不可达留到展开时如实报错）；不猜连接、不静默换连接；`build_readonly_registry()` 不动（子 Agent 恒不触远端）。

---

### 远端目录「作为工作区」= 本地锚点 + 路由默认根（M13，见 ADR-042）

M12 是把远端目录**加入**本机工作区的多根列表；M13 是更进一步 ——
**切换工作区时直接把某个远端目录当成工作区本体**：

```
切换工作区 → 选「远程目录」→ 选连接 → 浏览远端目录 → 确认
        ↓
Config.workspace        = ~/.vibe/workspaces/remote/<连接>/<指纹>/   ← 本机锚点（只放 .vibe 资产）
Config.remote_workspace = "@remote/<连接>/<子目录>"                  ← 路由默认根
        ↓
RemoteRouter.match("src/app.py") → Hit(connection=<连接>, rel=<子目录>/src/app.py)
```

于是模型写**裸相对路径**也落到那台机器上，而**本机 `.vibe` 资产原地不动**
（审计链 / 会话库 / 检查点 / 远端写的本机留底都在锚点目录里，ADR-006 不动）。
`<workspace>` 在内核里只有两处用法 ——「Jail 的根」与「`.vibe` 的落点」，
ADR-042 把它们拆开了，因此 30+ 个 `config.workspace / ".vibe"` 注入点一行都不用改。

| 环境变量 | `Config` 字段 | 默认 | 关闭时行为 |
|---|---|---|---|
| `VIBE_REMOTE` | `remote_enabled` | `false` | `0`/未设 = 无「远程目录」页签、`remote` 载荷 **400** fail-closed、`describe()` 不含 `remoteWorkspace` 键 |
| `VIBE_REMOTE_WORKSPACE` | `remote_workspace` | `""` | 空 = 本机模式（逐字节不变）；给值只是「启动时省一次手动切换」，**绕不开 `VIBE_REMOTE`** |

**新增组件**：

| 文件 | 内容 |
|---|---|
| `agent_core/remote/anchor.py` | 本机锚点：`safe_name()`（连接名是自由文本，`../../etc` 这种名字要被压平并断言仍在锚点根下，双保险）+ `ensure_anchor()` / `list_anchors()` / `read_anchor()`；标记文件 `.vibe-workspace-anchor.json` 记 `{connection, sub, nsPath, createdAt}`，历史靠**扫锚点**反推（不另存一份 JSON） |
| `agent_core/remote/roots.py` | 新增 `parse_ns_root()` / `default_root_rel()`：拼接 → 整体归一化 → **事后**断言 `..`（越界判定的完备形态） |
| `agent_core/remote/routing.py` | `RemoteRouter._match_default()`：裸路径 / 远端绝对路径 / `..` 越界拒收 / **`@remote/` 显式前缀不遮蔽**（默认根分支放在 ns 分支之后） |
| `agent_core/remote/degrade.py` | 诚实降级清单的单一真源（`Session.remote_degraded()` 取值处） |
| `agent_core/host/session.py` | `switch_workspace(path, *, remote=None)`：**先连后切**（握手 + 目标目录 ls 校验，失败则旧会话原封不动）；`describe()` 追加 `remoteWorkspace` / `remoteConnection` / `remoteSub` / `remoteDegraded` |
| `agent_core/host/ui/*` | `#ws-modal` 双页签（本机目录 / 远程目录）：连接下拉 → 显式 `POST /api/remote/open`（不替用户偷偷换连接）→ 目录树（目录可下钻、**文件只列出不可选**）→「设为工作区并重启」 |

**端点**（`VIBE_REMOTE` 门控，未启用一律 400）：

| 方法与路径 | 变更 |
|---|---|
| `POST /api/workspaces/open` | body 支持 `{remote:{connection, sub}}`（与 `path` 互斥） |
| `GET /api/workspaces` | 追加 `remoteEnabled` 与 `remoteWorkspaces`（扫锚点反推，读不到标记的跳过） |

**降级口径**：远程模式下 Git 面板 / LSP 诊断 / 索引 watch 不可用，界面显示**人话原因**而非空白；
提示词追加「当前工作区在远端 + 本模式无法在本机写文件」，**本机模式一个字符都不加**（逐字节一致）。

**审计**：**不新增事件、不动三处同步** —— 复用 `remote.connection` 的
`action="workspace_switch"` / `"workspace_open_fail"`（该字段两处均为 `str`、不做枚举）。

**边界**：不做 SSH 版 `PathGuard` / 审计 append；不做 `@local/` 逃生前缀；不做多连接同时作为工作区；
不把远端 git 输出 parse 成本机 Git 面板；锚点目录**不自动清理**（删锚点 = 删审计证据与留底）。

---

## 代码地图

```
agent_core/
├─ config.py            运行时配置：环境变量 + 显式覆盖，frozen 防运行中被改写
├─ errors.py            统一异常类型
├─ ui.py                终端渲染：颜色、Diff 着色、审批交互、Windows ANSI 支持
├─ audit.py             审计日志：append-only + 哈希链防篡改
│
├─ sandbox/             ★ 安全底线
│  ├─ paths.py          路径 Jail：越狱防护 + 敏感文件拦截（.env / *.pem / id_rsa ...）
│  └─ commands.py       命令白名单 + 危险模式拒绝 + 风险分级（SAFE/REVIEW/DENY）
│
├─ edit/                ★ 效果上限
│  ├─ patcher.py        结构化补丁引擎：三级容错匹配 + 文件格式保真 + 原子落盘
│  ├─ transaction.py    P1-8：多文件原子事务（两阶段：只算不写 → 整批提交 + 失败回滚）
│  └─ hunks.py          行内改写的 hunk 切分与应用（D7）：SequenceMatcher opcodes
│
├─ context/             P0-A：上下文引用（@file / @symbol / @folder / @image）
│  ├─ references.py     引用意图 → 正文展开（经 PathGuard，失败逐条降级为 notice）
│  ├─ images.py         P1-10：图片引用（按魔数判类型 + base64 data URL，见 ADR-018）
│  └─ tokens.py         P2-17：token 估算（启发式 + tiktoken 探测）+ 多模态固定开销
│
├─ index/               P0-B：检索索引（进程内单例，不落盘，见 ADR-013）
│  ├─ lexical.py        词法倒排（支持按 doc_id 精确摘除，见 P1-9）
│  ├─ depgraph.py       依赖图
│  ├─ watch.py          P1-9：增量索引 / 文件监听（纯 stdlib 轮询，零依赖）
│  └─ store.py          索引门面：签名一致则复用，否则重建；增量 update + pin 防淘汰
│
├─ lsp/                 P0-E：语言服务器（仅本机 stdio，无远端模式，见 ADR-014）
│  ├─ servers.py        探测表：python / js-ts / go / rust → 命令 + shutil.which 判定
│  ├─ client.py         标准库 stdio JSON-RPC（Content-Length 分帧 + 后台读线程）
│  └─ manager.py        按 (工作区, 语言) 缓存 + 空闲自动 shutdown + at_exit 兜底
│
├─ mcp/                 P1-6：外部 MCP server（仅本机 stdio + 可选 HTTP，见 ADR-015）
│  ├─ client.py         标准库 JSON-RPC（NDJSON / Content-Length 双分帧 + 后台读线程）
│  ├─ registry.py       servers 配置 / schema 白名单清洗 / 生成 mcp__<server>__<tool>（恒 REVIEW）
│  └─ manager.py        惰性启动 + 常驻 + 崩溃重启一次 + at_exit 兜底回收
│
├─ terminal/            P1-7：交互式终端（与 Agent 通道彻底隔离，见 ADR-016）
│  ├─ pty_unix.py       Unix 伪终端（标准库 os.openpty + fcntl + select，killpg 收整组）
│  ├─ pty_windows.py    Windows ConPTY（ctypes 直调，失败即抛「不支持」，绝不半启动）
│  └─ manager.py        PTY→管道降级 + 会话上限 + 空闲回收 + atexit + taskkill /T /F
│
├─ memory/              P2-15：跨会话记忆（本地 SQLite，正文不进审计，见 ADR-021）
│  ├─ store.py          MemoryStore：add/remove/list/search（词法相关度 top-N），容量上限明确拒绝
│  └─ __init__.py       导出 MemoryStore / MemoryRecord / MemoryLimitExceeded / default_memory_db_path
│
├─ tools/               Agent 与文件系统/终端之间的唯一接口
│  ├─ base.py           工具注册表、参数纠偏、不可信内容包裹
│  ├─ fs.py             read_file / list_dir / write_file
│  ├─ search.py         grep / find_files
│  ├─ editing.py        edit_file
│  ├─ multi_edit.py     edit_files（P1-8，REVIEW：多文件原子事务，一次审批看全部 diff）
│  ├─ semantic.py       semantic_search（P0-B，embedding 可选）
│  ├─ git.py            git_status / git_diff（P0-C，只读）
│  ├─ diagnostics.py    lsp_diagnostics（P0-E，仅语言服务器可用时注册）
│  ├─ plan.py           update_plan（P2-12，SAFE + 纯内存：结构化计划，不落盘、不进审批）
│  ├─ memory.py         remember（P2-15，REVIEW：模型只能提议，用户确认后才落库）
│  ├─ task.py           task（M3，SAFE：派生只读子 Agent，结果恒 wrap_untrusted 包裹）
│  ├─ skill.py          run_skill（M9-A，SAFE：只展开 Skill 正文、不写盘不执行）
│  ├─ test_loop.py      test_loop（M9-C，SAFE：调度既有工具跑测试闭环，恒审批 + 上限防死循环）
│  ├─ review.py         review_changes（M9-H，SAFE：只读自审、产出结构化意见、不产出补丁）
│  ├─ shell.py          run_command（环境变量脱敏 + shell=False）
│  ├─ repo_map.py       符号地图（最省 token 的项目全貌获取方式）
│  └─ symbols.py        符号抽取后端（正则默认 + 可选 tree-sitter 增强）
│
├─ skill/               M9-A：可复用工作流（Skill）加载（见 ADR-030）
│  └─ loader.py         SkillSpec（frozen）+ load_skills（扫描 SKILL.md，坏文件跳过 fail-closed）
│
├─ async_agent/         M9-D：后台/并行 Agent 任务池（见 ADR-032）
│  └─ pool.py           AgentTaskPool + AgentTask + TaskState（线程池，只读工具面，审计共用）
│
├─ gitinfo.py           P0-C：只读仓库视图（固定 argv + shell=False，非仓库返回 available=false）
├─ project_rules.py     P2-15：项目规则多层加载（全局级 → 用户级 → 仓库级 → 附加 → 目录级，见 ADR-021 / ADR-030）
│
├─ llm/                 模型抽象层（LiteLLM 的位置）
│  ├─ base.py           ChatResponse / ToolCall / 脏参数容错解析 / 文本工具协议
│  ├─ embeddings.py     P0-B：embedding 客户端（未配 VIBE_EMBED_MODEL 则不构造）
│  ├─ router.py         M9-I：ModelRouter（按任务难度选型，授权子集 + fail-closed 回退，见 ADR-034）
│  └─ openai_compat.py  标准库实现的 OpenAI 兼容客户端（含 complete() FIM + chat 回退 + 选型覆盖）
│
├─ loop/                ★ 产品的心脏
│  ├─ prompts.py        系统提示词（针对四类高频失败模式设计；子 Agent 专属只读探索取向提示）
│  ├─ subagent.py       M3：只读子 Agent 引擎（共用父审计实例、异常降级为 ok=False）
│  └─ agent.py          Agent 循环 + 审批闸门 + 上下文预算裁剪 + 子 Agent 委派（_delegate）
│
├─ host/                ★ M1：桌面端接入层（界面与 Core 之间唯一的桥）
│  ├─ protocol.py       通信契约：事件表 + 命令表（改界面先看这个文件）
│  ├─ security.py       本机服务的安全闸门：token / Host / Origin（防 DNS Rebinding）
│  ├─ bus.py            事件总线：带历史回放的广播，队列满时丢最旧、绝不阻塞 Agent
│  ├─ session.py        会话层：hooks→事件翻译、审批会合点、中断语义、编辑面（complete/rewrite/save）、语言服务器（E）、计划视图（P2-12）、上下文用量（P2-17）、增量索引监听（P1-9）、网页自检（P1-11）
│  ├─ visual.py         纯 stdlib PNG 解码 + 分块均色差（不引三方库，见 ADR-019）
│  ├─ files.py          给界面的文件树与文件读取（复用 PathGuard，不另开宽松规则）
│  ├─ server.py         标准库 HTTP + SSE 服务，JSON 接口 + 静态资源（接口清单以 protocol.py 为准）
│  └─ ui/               零构建前端：index.html / styles.css / app.js / diff.js
│
├─ cli.py               命令行入口
└─ __main__.py          支持 python -m agent_core
```

---

## 桌面接入层（host）

M1 加的，但**没有改动循环一行逻辑** —— 它接的是 M0 早就留好的两个接缝：
`AgentHooks`（UI 回调）和 `Approver`（同步阻塞的审批函数）。

真正要解决的问题是：Agent 的这两处接口天生是「同步、面向单机」的
（M0 里分别是 `print` 和 `input()`）。搬到图形界面上就变成了：

```
Agent 线程暂停 ──事件总线──▶ 浏览器渲染审批卡片 ──fetch──▶ HTTP 收到决定
     ▲                                                          │
     └──────────────── threading.Event 唤醒 ◀────────────────────┘
```

Agent 完全不知道对面是终端还是浏览器，它仍然以为自己在等一个同步函数返回。

### 失效安全：审批闸门默认拒绝

以下任一情况一律**拒绝**，绝不默认放行：

| 情况 | 理由 |
|---|---|
| 超时未响应（默认 30 分钟） | 人可能已经离开了 |
| **当前没有任何界面连着** | 没人能按按钮，干等毫无意义 |
| 任务已被请求中断 | 用户改主意了 |

这道闸门是 ADR-008 的最后一道防线，它的默认值必须是「不放行」。

### 中断：只在轮次边界生效

`Agent.should_stop` 只在每轮循环开始时被检查，刻意不做成「随时可杀」。
原因是在工具批次中途中断会留下 `assistant(tool_calls)` 与 `tool` 结果配不上对的
残缺历史，多数服务端会直接报 400 —— 那等于把会话弄坏。

代价是模型调用正在飞行时按下中断，要等这次调用返回才停。这是有意的取舍：
**宁可晚停一步，不可把会话弄坏。**

### 安全：一个只监听 127.0.0.1 的端口并不天然安全

| 威胁 | 对策 |
|---|---|
| DNS Rebinding（恶意网页把自己的域名解析到 127.0.0.1） | 校验 `Host` 必须是 127.0.0.1/localhost，且端口一致 |
| 同机其他进程 | 每次启动生成随机 token，写进 URL 交给壳 |
| CSRF | `Origin` 白名单 + 不发任何 CORS 头 |
| 注入内容外带数据 | 后端下发 CSP：`default-src 'self'` |
| 越权读文件 | 文件接口复用 `PathGuard`，界面能看到的范围与 Agent 能读到的**完全一致** |

> 头名大小写归一化在 `HostGuard.check()` 内部完成。踩过的坑：HTTP 规范要求头名
> 大小写不敏感，但 Chrome 发 `Host`、Node 的 `fetch` 发 `host`。如果调用方用普通
> `dict` 装着原始大小写，就会出现「浏览器里正常、Electron 里全部 403」。

---

## 四道安全闸门

每一条都对应架构文档里的 ADR，且都有测试锁住。

### 1. 路径 Jail（ADR-008）

所有文件访问都必须先过 `PathGuard`：

- `../` 逃逸、工作区外的绝对路径 → 拒绝
- **工作区内指向外部的符号链接** → `resolve()` 展开后照样拦下
- `.env` / `*.pem` / `id_rsa` / `.aws/` / `secrets.yaml` 等 → 默认不允许读入上下文
- `.vibe/` 内部目录（审计与检查点）→ 禁止写入

### 2. 命令沙箱（ADR-008）

- **永不使用 `shell=True`** —— 命令切分为 argv 数组后直接交给 `subprocess`
- **拒绝一切 shell 元字符**（`|` `&&` `;` `>` `$( )` …），需要组合请拆成多条
- **禁止启动 shell 本身**（`bash` / `sh` / `cmd` / `powershell` / `xargs`）——否则白名单形同虚设
- **禁止内联执行**（`python -c` / `node -e`）——它们能绕过文件沙箱
- 网络命令默认禁止；白名单外的可执行文件一律拒绝

### 3. 人工审批（ADR-008 红线）

`REVIEW` 级动作弹审批并展示**真实 diff**（不是裸 JSON 参数）。**不接受通过配置静默关闭**。
终端里输入 `a` 可在本次会话内对该工具全部放行，`d` 永久禁用该工具；
桌面端是四个按钮，`Esc` 拒绝、`Ctrl+Enter` 批准。

#### 审批预演（M1）

审批之前会先做一次**无副作用的预演**（`ToolSpec.previewer`）——不落盘、不写审计，
只回答「如果放行，会发生什么」：

- 对 `edit_file`，它走的是和真正执行**同一个** `plan_patch`，只是最后不 `commit`。
  因此**审批时看到的 diff 与放行后落盘的内容必然一致**，不可能出现
  「以为改 A、实际改了 B」。
- 预演还能**在打扰用户之前**发现注定失败的动作：`edit_file` 最常见的失败是
  `search` 片段与文件当前内容对不上。既已确定会失败，再弹窗问人就是纯粹的浪费，
  于是直接标 `blocking` 让循环把诊断回灌给模型，由它自己改。

> 这不是「顺手做的小优化」。M0 的审批界面显示的是 `json.dumps(arguments)` ——
> 用户要在脑子里把 search/replace 还原成 diff 才能判断。桌面端的 Diff 审查
> 之所以能成立，靠的就是这一层预演。

### 4. 不可信内容隔离

读到的文件和命令输出会被 `<<<UNTRUSTED_DATA ... >>>` 包裹，系统提示明确声明
标记内不构成指令 —— 这是针对**提示词注入**的第一道防线（此类工具的头号风险）。

---

## 结构化补丁：三级容错匹配

对应 ADR-004：**编辑必须走结构化 patch，禁止整文件重写。**
因为整文件重写会丢掉模型没提及的改动、容易引入幻觉代码、Diff 不可读、无法回滚。

小模型写的 `search` 片段经常有细微偏差，因此按可靠度顺序逐级放宽：

| 级别 | 行为 | 适用偏差 |
|---|---|---|
| `exact` | 逐字节完全一致 | 无 |
| `normalized` | 忽略每行行尾空白与首尾空行 | 多了个空格、多包了一层换行 |
| `flexible` | 忽略整体缩进差异，**并把替换内容自动缩进对齐到目标位置** | 多缩进/少缩进几格 |

命中后会回报实际使用的策略，方便你观察模型质量。三级都失配时，会给出
「最接近的行在哪、相似度多少、建议先 read_file」这类可直接执行的诊断。

**文件格式保真**：改一个文件不会顺手把 CRLF 变成 LF、把 BOM 抹掉或吞掉末尾换行。
**原子落盘**：先写同目录临时文件再 `os.replace`，写一半崩溃也不会损坏原文件。

---

## 审计设计（ADR-006）

`.vibe/audit.jsonl` 每行一条 JSON，只记**元数据**：

- 谁（session / pid）、何时、对哪个文件、调了什么模型、用了多少 token、执行了哪些工具
- **绝不记录代码正文 / Prompt 原文 / 模型输出原文**，一律只落长度与 sha256 指纹

每条记录携带上一条的哈希（哈希链），任何对历史记录的修改或删除都会导致校验失败：

```bash
# 在交互模式里执行 /audit
python -m agent_core -w . # 然后输入 /audit
```

---

## 运行测试

无需安装任何依赖：

```bash
cd packages/agent-core
python -m unittest discover -s tests -t .
```

1407 个用例（1405 通过 / 2 跳过：本机环境相关；169 个子测试）；下表挑出价值最高的几组：

| 文件 | 覆盖内容 |
|---|---|
| `test_patcher.py` | 三级匹配策略、缩进对齐、歧义拦截、CRLF/BOM/GBK 保真、原子写 |
| `test_sandbox.py` | 路径越狱（含符号链接）、敏感文件、命令黑名单/白名单/分级 |
| `test_llm.py` | 响应解析、脏 JSON 容错、文本工具协议、**可达性探测不谎报**、prompt caching 被动探测（`off` 不注入 / `auto` 拒收后只降级一次 / **普通 400 不被误判**） |
| `test_agent_loop.py` | **端到端闭环**：假模型驱动完整循环、四道闸门、审计链完整性、上下文裁剪不破坏消息配对 |
| `test_host.py` | **桌面接入层**：鉴权与来源校验（含头名大小写回归）、事件回放与丢旧、**审批失效安全**、中断后消息仍自洽、路径与 diff 不错位、工作区切换后上下文与审计链一致、真实 HTTP + SSE |
| `test_stream.py` | 流式 SSE 聚合（含分片 tool_call）、delta 不渲染半截 JSON、会话层 delta 折叠回放 |
| `test_store.py` | 会话落盘与恢复、消息成组校验、标题只写一次、`GET /api/sessions` 系列 |
| `test_checkpoint.py` | 写前快照 + manifest、链式 restore / redo、keep 淘汰、与 Agent 及 API 的接线 |
| `test_governance_wiring.py` | **治理面接线**：未配 `VIBE_SERVER_URL` 时行为一行不变、策略收紧、配额视图、桌面壳契约、**`describe()` 只在配置了治理面时暴露 `serverUrl`（Q：界面据此给「去登录」入口）** |
| `test_host_governance.py` | **治理面登录闭环（M3+）**：`POST /api/governance/login\|register\|logout` 的 host 端点 —— 登录成功**热接入**当前会话（Agent 客户端与 `ctx.paths/commands`、`_policy_approval` 同步换新，不重建会话）、凭证落盘、**响应逐字不含令牌**、执行中 409、无地址 / 非法协议 400、凭证错误透传 4xx、不可达 502、**登出在治理面不可达时仍清本地凭证** |
| `test_refs.py`（P0-A） | 上下文引用：形状校验、越权逐条降级为 notice、缺省与不传逐字节一致 |
| `test_semantic.py`（P0-B） | 语义检索：无 embedding 时回退词法、索引缓存签名、依赖图 |
| `test_gitinfo.py`（P0-C） | Git 只读视图：非仓库返回 `available=false`、固定 argv 不拼 shell |
| `test_hunks.py`（P0-D） | hunk 切分与应用：**子集只应用勾选项**、空 accepted 原文返回、末尾换行保真 |
| `test_completion.py`（P0-D） | 补全/改写/保存链路：FIM 探测降级、越权 403、`expectedDigest` 乐观锁、运行中 409 |
| `test_lsp.py`（P0-E） | 语言服务器：假语言服务器钉 stdio 分帧与生命周期、`lsp_mode=off` 降级不 spawn、崩溃只重启一次、重命名预览不落盘 / 应用**先整批校验再落盘** |
| `test_plan.py`（P2-12） | 计划视图：`update_plan` 校验**整批拒绝**、幂等全量替换、**不落盘**；工具→`run.plan` 事件→快照链路；人工勾选记审计并广播、运行中不 409、`POST /api/plan/toggle` 坏请求 400 |
| `test_tokens.py`（P2-17） | 上下文窗口管理：启发式量级（中文 > 同长度 ASCII）、多模态固定开销、**字符/token 口径切换**、`context_stats` 分段之和恒等于总量、`run.context` 每轮 + 收尾各一次、`GET /api/context/stats` 只读、快照一致性 |
| `test_transaction.py`（P1-8） | 多文件原子事务：**阶段一失败整体不写**（search 失配 / 乐观锁不符 / 超上限 / 覆盖未显式 overwrite）、**阶段二失败回滚**（已写文件恢复原内容、新建文件被删）、落盘前逐文件收快照；`edit_files` 工具注册为 REVIEW、五类坏输入**整批拒绝**、审计只含计数、预演阻塞与汇总 |
| `test_watch.py`（P1-9） | 增量索引 / 文件监听：新增/修改/删除文件后 `update()` 与**全量重建等价**（chunks/files/doc_count/term_count 与 4 个 query 逐项一致）、空词桶清理、依赖边随 update 变；watcher 无变化不写、`poll_once` 记审计且不含路径与正文、`stop` 回收线程且幂等、与检索**共享同一实例**、pinned 不被 LRU 淘汰；watch 关行为不变且不起线程 |
| `test_rules_layers.py`（P2-15） | 规则多层加载：合并顺序（用户级 → 仓库级 → 附加 → 目录级）、根源优先不叠加、**单层无抬头**（逐字节兼容）、`rules_extra` glob、目录级就近与只匹配路径、工作区外忽略、总预算超限**先丢最泛层** |
| `test_memory.py`（P2-15） | 跨会话记忆：`MemoryStore` 增删查（词法相关度 top-N / 容量上限拒绝 / 空文本与超长拒绝）、`remember` **恒 REVIEW** 且未启用记忆时报「未启用」、审计**不含正文**；注入被 `wrap_untrusted` 包裹、不相关不注入、`limit` 生效且记 `memory.inject`、`reset()` 复位系统提示、`VIBE_MEMORY=0` 不建库不注入不注册 |
| `test_subagent.py`（M3） | 多 Agent 编排：`task` 仅在 `subagent=True` 时注册、参数**整批拒绝**、无 sink 明确失败、结果被 `wrap_untrusted` 包裹；只读注册表**不含**写/执行/`task`/`mcp__*` 且全为 SAFE；`run_subagent` 返回结构化摘要、**不能写**、**不能递归**、异常降级 `ok=False`、`should_stop` 透传、`subagent_max_iterations` 生效；`subagent.run` 只记元数据且父/子**共用同一 `AuditLog`**（`verify_chain` 通过）；`run.subagent` 各 phase 齐全且 `callId` 关联、未启用时**无 `task` 无事件** |
| `test_cron.py`（M10） | 调度表达式解析：三形态（5 字段 cron / `@every` / `@at`）、`*` `?` `,` `-` `/` 全形态、周 `0` 与 `7` 均为周日、`日`+`周` 同限定取 **OR**（单限定取 AND）、`next_after` 逐字段跳进（跨月 / 跨日 / 工作日）、一次性触发后不再排期、**非法表达式 fail-closed**（`ScheduleError`） |
| `test_scheduler_store.py`（M10） | 定时任务库：sqlite round-trip（全字段一致）、`enabled=0` 保留定义、父目录自动创建、`upsert` 更新 / `get` / `delete`、**坏行跳过并计数**（非法 schedule / 缺 prompt）、库损坏挪走重建 |
| `test_scheduler.py`（M10） | 调度器：`add/list/update/toggle/delete/run_now`、**到点才触发** + 重排到未来、`max_per_tick` 单轮限流、`max_jobs` 上限、**投递失败不弄挂调度线程**、重启补跑**恰好一次**、`CATCHUP=0` 记 `skipped`、`stop()` 后无残留线程、`status()` 字段、审计**不含 prompt** |
| `test_scheduler_api.py`（M10） | 定时任务宿主接线：`VIBE_SCHEDULER=0` 门面 fail-closed（无调度器 / 无端点 / 无库文件）、打开态 create/list/update/toggle/runNow/delete 全链路、`schedule.state` 事件**不含 prompt**、审计只记元数据、**重启补跑一次** + 关补跑记 `skipped`、只开调度不开后台 → 不起调度器 + notice、protocol 登记事件与端点 |
| `test_remote_ui.py`（M11） | 远程工作面：`VIBE_REMOTE=0` 门控 fail-closed（端点 400 / 未知路径 404）、连接增改删 / 设默认 / 权限位（**热生效**）/ 测试不改清单 / 坏 spec 不落盘 / 原子写 round-trip 无 `.tmp` 残渣、文件树一层与逃逸 403、只读查看元数据与**逃逸 403**、**二进制 400** 与**超限 400**、**乐观锁 409 且远端未动**、保存留底 + 台账 + diff、同内容不写、新建 `created` 无留底、**留底不可写则中止**、只读连接拒写、`changed=false` 不写回、**审计不含正文** |
| `test_remote_review.py`（M11） | 远程审查面：改动台账（save / patch / create）、**留底 vs 远端的 unified diff**（修改含 `-`/`+` 行）、**hunks 两块切分**（`oldStart` 1 基口径）、新建按全新增、远端已删 / 无记录 / **属别的连接** 均 `available:false` + `reason`、`remote_review_changes` **恒 SAFE 且不进只读 registry**、工具审计**无正文**（`@@` / `+++` / `---` 与行文本均不得出现） |

`test_agent_loop.py` 与 `test_host.py` 是价值最高的两组：它们用假模型驱动真实循环，
不依赖任何模型服务就能验证整条链路，任何一环回归都会立刻暴露。

另有六个不依赖测试框架的脚本：

```bash
# 脚本化假模型服务（没有任何模型时也能验证链路）
python scripts/mock_model_server.py --port 11500

# 会话/恢复链路冒烟：对话 → 关进程 → 重开 → 恢复 → 继续（真 HTTP + 真 sqlite）
python scripts/e2e_session.py

# 桌面端端到端冒烟：起真实 host 服务，用 HTTP 驱动完整闭环（含审批）
python scripts/e2e_desktop.py

# 打包产物冒烟：内嵌二进制与回退系统 Python 两条路径都跑一遍
python scripts/e2e_package.py

# 治理面冒烟（可选，需 apps/server 在场）：认证 → 策略 → 网关 → 脱敏 → 配额 → 审计，8 档
python scripts/e2e_governance.py

# 远端链路真机冒烟（M11，手动，需一个你自有的可登录远端设备与凭据；不进 CI）
python scripts/e2e_remote.py --target root@host --key ~/.ssh/id_ed25519_remote
```

---

## 如何新增一个工具

1. 在 `tools/` 下新建模块，实现 `handler(ctx: ToolContext, args: dict) -> ToolResult`
2. 定义 `ToolSpec`（名字、描述、JSON Schema、默认风险等级、可选动态评估器）
3. 在 `tools/__init__.py` 的 `build_default_registry()` 里注册

动态风险用 `assessor`：

```python
def _assess(ctx, args):
    analysis = ctx.commands.analyze(args.get("command") or "")
    return analysis.risk, analysis.reason
```

想让审批界面看到真实的改动而不是裸参数，再加一个 `previewer`：

```python
def _preview(ctx, args) -> ToolPreview:
    # 只计算、不落盘；确定会失败就返回 blocking=True
    plan = plan_patch(path, args["search"], args["replace"], rel_path=rel)
    return ToolPreview(kind="diff", body=plan.diff, summary=plan.summary())
```

**注意**：工具数量要克制。工具越多，弱模型的选错率越高。当前 12 个内置工具已覆盖
「看懂 → 定位 → 修改 → 验证」全部动作，没有为了"看起来强"而凑数的工具
（`remember` / `task` / `lsp_diagnostics` / `mcp__*` / `web_search` / `web_fetch` 属**条件注册**，不配置时不出现）。

---

## 边界（明确没做的事）

已经做完的：**M0（CLI 闭环）**、**M1（桌面客户端）**、**M1.5（效果与体验补全）**、
**M2（服务端治理面，且已接进 CLI 与桌面端）**、**M3（多 Agent 编排，主从只读子 Agent）**，
以及 M6 系列能力（编辑面 / 语言服务器 / 计划视图 / 上下文窗口 / 增量索引 / 外部 MCP /
交互式终端 / 多模态图片 / 规则与记忆 / 网页自检 / 多租户运营落地），
**M7（受控联网检索 `web_search`/`web_fetch` + Git 写操作面板）**、
**M9（复用与自治：Skill / 分层规则继承 / 测试闭环 / 后台 Agent / 代码审阅 / 模型选择器）**。

尚未做的：

- ❌ 向量检索增强 / Repo Wiki / 配额的动态调整（顺延至 **M3+**）
- ❌ 多角色协作（planner / coder / reviewer）、可配置 DAG 工作流（**明确不做**，见 ADR-022）
- ❌ 子 Agent 递归派生 / 并行 fan-out / 写盘（**明确不做**，见 ADR-022）
- ⚠️ `repo_map` 已支持可选的 tree-sitter 后端（函数签名/继承/import），但需另装 `[precise]`；未装时回退正则，召回率不如 AST
- ⚠️ M4 规模化只**部分落地**：监控告警 / 成本看板数据源 / 灰度分发已落代码；高可用与 K8s 只出方案（见 `docs/M4-OPS-PLAN.md`）
- ❌ `run_command` 的输出仍是**一次性返回**的，没有边跑边流式推给界面
- ❌ Skill 市场 / 远程 Skill 下载、Skill 跨机同步、embedding 语义匹配（**明确不做**，见 ADR-030）
- ❌ 测试闭环对非 pytest 框架的适配、后台 Agent 跨进程持久化（**明确不做**，见 ADR-031 / ADR-032）
- ❌ 审阅产出「自动修复补丁」、模型选择器自动成本预测（**明确不做**，见 ADR-033 / ADR-034）

以上都是**刻意不做**。Core 的目标始终是先把本地闭环跑通并测得可信，
桌面壳、服务端与多 Agent 编排只是给同一个 Agent Core 换前端、加治理、加编排。
