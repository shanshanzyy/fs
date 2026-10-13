"""桌面端与 Agent Core 之间的通信契约。

## 传输选型

本机 HTTP（只监听 127.0.0.1）+ Server-Sent Events 做单向推送。

为什么不用 stdio JSON-RPC：

1. **壳可以极薄** —— 界面就是普通网页，任何浏览器也能直接打开调试，
   不必为了看一眼 UI 就套上 Electron；
2. **前端零桥接** —— `EventSource` / `fetch` 原生就能用，不需要 preload 暴露 IPC；
3. **壳可替换** —— 换 Tauri、换纯浏览器、把 Core 部署到远程开发机，
   这三种场景 Core 一行都不用改。

代价是多开了一个本机端口，因此必须做鉴权与来源校验（见 `security.py`）。
这是明确的取舍：**用一层本机 HTTP 换掉壳与 Core 的耦合**。

## 事件（服务端 → 客户端，单向流，`GET /api/events`）

每个事件形如 `{"id": 12, "type": "run.tool_call", "ts": ..., ...payload}`，
`id` 单调递增，客户端断线重连时可通过 `Last-Event-ID` 或 `?since=` 补齐漏掉的事件，
因此刷新页面不会丢失当前会话的上下文。

| type | 触发时机 | 关键字段 |
|---|---|---|
| `session.ready` | 连接建立 | `sessionId` `workspace` `model` `tools[]` `config{}` |
| `run.started` | 用户提交任务 | `runId` `prompt` |
| `run.iteration` | 每轮循环开始 | `runId` `index` `total` |
| `run.assistant_delta` | 模型纯文本回复的增量（流式输出，A1） | `runId` `text`（片段，按序拼接即全文；工具围栏后的内容不会出现） |
| `run.tool_call` | 工具调用前（含风险分级） | `runId` `callId` `tool` `arguments` `risk` `reason` |
| `run.tool_result` | 工具执行后 | `runId` `callId` `tool` `ok` `content` `diff` `meta` |
| `run.approval` | 需要人工审批 | `runId` `approvalId` `tool` `risk` `reason` `preview{}` |
| `run.approval_resolved` | 审批已处理 | `approvalId` `decision` |
| `run.notice` | 提醒/警告 | `runId` `text` |
| `run.plan` | 计划更新（P2-12，模型调用 `update_plan` 或用户手动勾选） | `runId` `steps[]`（`{id,text,status}`）`revision` |
| `run.context` | 上下文用量（P2-17，每轮**裁剪之后** + 本轮收尾各一次） | `runId` `budgetTokens` `usedTokens` `segments{system,rules,history,toolOutputs}` `estimator` `trimmedLastRun` `cache` |
| `run.subagent` | 子 Agent 进度（M3，一次 `task` 委派的生命周期） | `runId` `callId` `phase`（`started`/`iteration`/`tool`/`tool_result`/`notice`/`finished`/`failed`）+ 该 phase 的元数据（`index`/`total`/`name`/`risk`/`ok`/`text`/`chars`/`iterations`/`tool_calls`/`answer_chars`/`task_chars`/`error`）；**不含子 Agent 正文**（ADR-006） |
| `run.finished` | 任务完成 | `runId` `answer` `stats{}` `changedFiles[]` |
| `run.failed` | 任务异常 | `runId` `error` `kind` |
| `run.interrupted` | 用户中断 | `runId` |
| `test.started` | 用户点了「跑测试」（P2-14，与 `run.*` 分离） | `runId` `total` `nodeIds[]` |
| `test.finished` | 测试跑完（或跑不起来） | `runId` `total` `passed` `failed` `skipped` `ms` `failures[]`（`{nodeId,file,line,head}`）`error` |
| `term.data` | 交互式终端有输出（P1-7，**独立通道**，与 `run.*` 分离） | `termId` `data`（原始字节的 base64） |
| `term.exit` | 终端进程退出（用户敲 `exit` 或点了关闭） | `termId` `exitCode` |

`preview` 的结构见 `agent_core.tools.base.ToolPreview`：
`{"kind": "diff"|"text"|"json"|"error", "body": ..., "summary": ..., "error": ...}`。
对 `edit_file` 而言 `body` 是真实的 unified diff —— 这是 M1「Diff 审查」的数据来源。

## 命令（客户端 → 服务端）

| 方法与路径 | 作用 |
|---|---|
| `GET  /api/health` | 就绪探测（Electron 主进程靠它决定何时加载页面） |
| `GET  /api/state` | 会话快照：配置、工具清单、风险分级、当前运行状态 |
| `GET  /api/settings` | 模型 / 补全设置（可编辑部分）：`{model, baseUrl, apiKeyMasked, apiKeySet, workspace, settingsPath, fimEnabled, completionMaxTokens, completionPrefixChars, completionSuffixChars, envLocked{model,base_url,api_key,fim_enabled,completion_max_tokens,completion_prefix_chars,completion_suffix_chars}, governance}`；密钥只回末 4 位掩码，`envLocked` 标出被环境变量锁死、界面改不动的字段 |
| `POST /api/settings` | 保存模型 / 补全设置：`{model?, baseUrl?, apiKey?, workspace?, fimEnabled?, completionMaxTokens?, completionPrefixChars?, completionSuffixChars?}`（字符串字段留空 = 保留原值；补全字段仅在显式给出时落盘，`0`/`false` 均为合法值）→ `{ok, switched, settingsPath, settings, session}`；**空闲态**热替换 LLM 客户端并保留当前对话（执行中 409，治理面接管时 400），补全设置变更同时清空补全缓存。真源为用户级 `~/.vibe/settings.json`（`VIBE_SETTINGS` 可覆盖路径） |
| `GET  /api/models?baseUrl=&apiKey=` | 列出端点可用模型名：`{baseUrl, models[]}`；`list_models()` 吞错返回空，**不代表不可达**（判可达性请用 `POST /api/model/test`） |
| `POST /api/model/test` | 连通性探测（不落盘、无副作用）：`{baseUrl?, apiKey?}` → `{ok, message, baseUrl, models[]}`；先 `probe_endpoint()`（不吞错）确认可达，再 `list_models()`（吞错返回空） |
| `POST /api/run` | `{text, refs?}` 提交任务；已有任务在跑时返回 409 |
| `POST /api/approval` | `{approvalId, decision}` 其中 decision ∈ approve/approve_all/deny/deny_all |
| `POST /api/interrupt` | 请求中断当前任务 |
| `POST /api/reset` | 清空对话历史（不清审计） |
| `GET  /api/tree` | 工作区文件树 |
| `GET  /api/file?path=` | 读取单个文件（经路径沙箱，敏感文件会被拒）；返回含 `digest`（乐观锁指纹） |
| `GET  /api/diffs` | 本会话累计产生的所有 diff |
| `GET  /api/refs?type=&q=&limit=` | 上下文引用（P0-A）候选：type ∈ file/symbol/folder，上限 50 |
| `POST /api/complete` | 内联补全（D5）：`{path, prefix, suffix, maxTokens?}` → `{text, backend, path, chars, truncated, cached}`；后端按 `completionPrefixChars`/`completionSuffixChars` 对前后文做行对齐截断（`truncated` 标出是否发生截断），`maxTokens` 缺省回退 `completionMaxTokens`；相同 `(path,prefix,suffix,limit,fim)` 在 TTL 30s 内直接命中内存缓存（`cached=true`） |
| `POST /api/rewrite` | 行内改写预览（D8）：`{path, startLine, endLine, instruction}` → `{token, hunks[], noChange, …}` |
| `POST /api/rewrite/apply` | 应用勾选的 hunk（D8）：`{token, accepted[]}` → `{ok, changed, applied, total, …}` |
| `POST /api/edit/save` | 编辑模式保存（D11）：`{path, content, expectedDigest}` → `{ok, changed, digest, …}` |
| `GET  /api/lsp/status` | 语言服务器（E）状态：`{available, mode, reason, servers[], running[]}` |
| `GET  /api/lsp/diagnostics?path=` | 单文件诊断（E）：`{available, path, counts{}, diagnostics[]}` |
| `GET  /api/lsp/definition?path=&line=&char=` | 跳转定义（E）：`{available, locations[]}` |
| `POST /api/lsp/rename/preview` | 重命名预览（E）：`{path, line, char, newName}` → `{token, files[]{path,diff,added,removed,edits}}`，**不落盘** |
| `POST /api/lsp/rename/apply` | 应用重命名（E）：`{token}` → `{ok, changed, files, paths[], added, removed}` |
| `GET  /api/mcp/status` | 外部 MCP server 状态（P1-6）：`{enabled, configPath, servers[]{name,transport,command,args[],env{},url,available,reason,running,tools[]}, configError}`（`command`/`args`/`env` 回完整值，供界面原地编辑） |
| `POST /api/mcp/reload` | 重读 MCP servers 配置（P1-6）：不落盘、不 409；重建 `mcp__*` 工具清单后返回最新 status |
| `POST /api/mcp/servers` | 新增 / 替换一个 MCP server（L 管理面）：`{server{name,transport,command,args,url,env}}` → 最新 status；只写配置文件、不拼命令；未启用 400、执行中 409、治理面 400、非法 400 |
| `POST /api/mcp/servers/remove` | 删除一个 MCP server（L 管理面）：`{name}` → 最新 status；不存在 400；写后自动 reload 并记 `mcp.server_removed` |
| `POST /api/mcp/test` | 测一个 MCP server 的连接（L 管理面）：`{name}` → `{ok, name, count, tools[]}` 或 `{ok:false, error}`；首次调用惰性启动该 server |
| `POST /api/governance/login` | 登录治理面（M3+）：`{email, password, serverUrl?, deviceName?}` → `{ok, governance, session}`；`serverUrl` 缺省回退配置里的治理面地址，为空则以 400 拒绝；调 `GovernanceClient.login()` 把令牌落 `~/.vibe/credentials.json`（0o600）后**热接入**当前会话（不重建对话 / 检查点 / 审计链）；执行中 409，治理面拒登按其状态码（4xx），不可达 502。响应只回 `governance.describe()`（**不含令牌**） |
| `POST /api/governance/register` | 邀请码注册并登录（M3+）：`{enrollmentToken, email, password, displayName?, serverUrl?, deviceName?}` → `{ok, governance, session}`；走 `GovernanceClient.register()`，成功后同 login 热接入 |
| `POST /api/governance/logout` | 退出登录（M3+）：`{allDevices?}` → `{ok, governance, session}`；调 `GovernanceClient.logout()` 后**无论网络是否可达都清本地凭证**（`CredentialStore.clear()` 兜底），再热接入回本地直连客户端；执行中 409 |
| `POST /api/plan/toggle` | 人工标注计划中某一步（P2-12）：`{id, status}` → `{ok, id, status, steps[], revision}` |
| `GET  /api/context/stats` | 上下文用量（P2-17）：`{budgetTokens, usedTokens, segments{}, estimator, trimmedLastRun, cache}`；只读无副作用 |
| `GET  /api/index/status` | 增量索引健康度（P1-9）：`{watching, interval, files, chunks, edges, lastUpdate, pending}`；**只读**，`index_watch` 关时如实回 0（不因此建索引） |
| `GET  /api/tests` | 测试树（P2-14）：`{configured, command, cases[{nodeId,file,name}], total, truncated, error}`；`?refresh=1` 强制重新收集（缺省用缓存） |
| `POST /api/tests/run` | 跑测试（P2-14）：`{nodeIds[]}`（空 = 全部）→ `{runId}`；结果走 `test.started`/`test.finished` 事件 |
| `GET  /api/tests/last` | 最近一次测试结果（P2-14）：`{configured, runId, result{…}}`，从未跑过则 `result: null` |
| `POST /api/term/open` | 起一个交互式终端（P1-7）：`{cols, rows}` → `{termId, mode, cols, rows, pid, fallbackReason}`；输出走 `term.data`/`term.exit` 事件 |
| `POST /api/term/input` | 把按键/粘贴内容写进终端（P1-7）：`{termId, data}` → `{ok, chars}` |
| `POST /api/term/resize` | 重设终端窗口（P1-7）：`{termId, cols, rows}` → `{ok, cols, rows}` |
| `POST /api/term/close` | 关闭终端并收子进程树（P1-7）：`{termId}` → `{ok, mode, exitCode, ms}` |
| `POST /api/upload-image` | 上传一张图片作多模态输入（P1-10）：`{name, data, mediaType?}`（`data` 为 base64，可带 `data:` 前缀）→ `{ref{type,value}, bytes, ext, digest}`；`VIBE_VISION=off` 时拒绝 |
| `GET  /preview/<relpath>` | 工作区网页文件的**静态预览**（P1-11）：PathGuard 读校验 + MIME 白名单 + 预览专用 CSP（`connect-src 'none'` 焊死网络出口）；目录回落到 `index.html`。**不是** `/api/*`，故不查令牌（同静态壳），但 Host/Origin 双校验照常生效 |
| `POST /api/visual/check` | 网页自检（P1-11）：`{url, width?, height?, image, baseline?, setBaseline?}`（`image` 为 base64 PNG 截图，来自 Electron 壳的离屏桥）→ `{available, ok, diffRatio, changedBlocks, totalBlocks, baselineMissing, saved?, diffImageDigest, reason?}`；**无截图时如实回 `available:false`**（浏览器直开没有捕获能力），不是 500 |
| `GET  /api/memories` | 跨会话记忆（P2-15）：`{enabled, memories[]{id, text, tags, scope, createdAt}}`；`VIBE_MEMORY=0` 时 `enabled:false` |
| `POST /api/memories` | 新增一条记忆（P2-15）：`{text, tags?}` → `{ok, memory{…}}`；空文本 / 超长 / 超上限 → 400。属「用户本人动作」（ADR-011） |
| `POST /api/memories/delete` | 删除一条记忆（P2-15）：`{id}` → `{ok, removed}` |
| `GET  /api/audit` | 审计链校验结果 + 最近事件 |

### `POST /api/run` 的 `refs`（P0-A）

`refs` 是**引用意图**列表，前端只传「类型 + 值」，正文由后端经 `PathGuard`
读取（见 `agent_core.context.references`）。缺省或空数组时，服务端行为与
不传该字段**逐字节一致**。

```json
{"text": "把登录逻辑改成…", "refs": [
  {"type": "file",   "value": "src/auth/login.py"},
  {"type": "symbol", "value": "src/auth/login.py::LoginService"},
  {"type": "folder", "value": "src/auth"},
  {"type": "image",  "value": ".vibe/images/3f9c….png"}
]}
```

`image` 类型的 `value` 是**本机工作区内的图片路径**（由 `/api/upload-image`
返回，见下）—— 它不产出文本块，而是作为 `image_url` part 附到首条用户消息上
（前提 `VIBE_VISION` 不为 `off`；为 `off` 时降级为一条 `run.notice`）。

形状非法（类型不在枚举内、value 为空或过长、refs 不是数组）一律 400 ——
前端 bug 要当场暴露，而不是被静默丢弃。解析失败（越权、敏感文件、符号找不到）
**不阻断任务**：逐条降级为 `run.notice`，其余引用照常展开。

### 编辑面（D）的三条链路边界

这四条端点承载「用户本人直接编辑代码」，与 Agent 发起的写有**三条不同**：

1. **不走审批浮层** —— 它们是人自己敲的键，审批闸门约束的是 Agent（ADR-011）。
   ADR-008 未被削弱：Agent 发起的写仍恒走审批。
2. **落盘一律经同一套引擎** —— 改写走 `plan_overwrite` + `commit_plan`，**不新增
   写盘路径**（ADR-004）；改写「勾掉一半」只是对 hunk 子集套用同一引擎，审计
   策略标签记作 `hunk-subset`。
3. **落盘前收检查点 + 写审计** —— 与 Agent 改动一样进 `changedFiles` 与检查点，
   因此编辑面**不会污染** Agent 的改动链；审计只记 digest / 长度 / 计数
   （ADR-006），不记代码正文。

两条状态约束：

- **运行中 409**：`/api/rewrite/apply` 与 `/api/edit/save` 在 Agent 任务执行中
  返回 409（`BusyError`）；`/api/complete` **不设 409** —— 它只产文本、不写盘、
  不与写盘路径争用，任务跑着时补全反而是想要的行为。
- **乐观锁**：`/api/edit/save` 的 `expectedDigest` 是文件**打开时**的指纹
  （来自 `GET /api/file` 的 `digest`）。磁盘已被改（Agent 落盘 / 外部编辑器 /
  另一次保存）则返回 409，**绝不静默覆盖**。缺省空串表示不做乐观锁校验。

`/api/rewrite` 返回的 `token` 是落盘的唯一凭据：`/api/rewrite/apply` 按 token
取**服务端缓存**的改写结果，前端回传的正文一律不被信任 —— 否则伪造整段内容
就能绕过 hunk 的「逐块勾选」语义。

### 语言服务器（E）的边界

LSP 只做三件事：**看看有什么诊断、跳到定义、改名**。三条约束：

1. **仅本地、不常驻、零依赖** —— 只 spawn 本机语言服务器，`shutil.which` 探不到
   就降级（`available:false` + 中文 `reason`）；空闲 `lsp_idle_seconds` 自动
   shutdown。不连远端，也没有「远端服务器模式」（ADR-014）。
2. **只读端点永不报错** —— `/api/lsp/status|diagnostics|definition` 在
   `lsp_mode=off`、未安装、或已标记不可用时一律返回 `{available:false, reason}`，
   绝不 500。界面据此把 gutter 留空并显示一行说明。
3. **改名是「用户本人动作」，但仍不是免费动作** —— 与编辑面同范式（ADR-011）：
   不走审批浮层（是人自己敲的键），但 `/api/lsp/rename/apply` **运行中 409**、
   落盘前收检查点、逐文件写审计；且**先整批校验再落盘**，任一文件过期
   （`StaleFileError` → 409）就整批不动。预览返回的 `token` 同样是落盘的唯一凭据。

位置约定：**行号 1 基、列 0 基（UTF-16）**。`diagnostics` 的 `line`/`endLine`、
`definition` 的 `line`/`endLine` 与 `rename/preview` 的入参 `line` 统一按此口径 ——
与 gutter、`/api/rewrite` 的 `startLine` 一致，前端不必记两套。

### 计划视图（P2-12）的边界

计划是**运行态**，不是产物。三条约束：

1. **只存内存、不落盘** —— 计划来自模型对「这次怎么做」的叙述，写进会话库只会
   让历史里多出一堆早已作废的清单。页面刷新不丢计划靠 `session.ready` /
   `GET /api/state` 的 `plan` 快照（`{steps[], revision}`），不靠持久化。
2. **模型驱动状态、人可改标注** —— 步骤的 `status` 由模型调用 `update_plan`
   全量替换；`POST /api/plan/toggle` 是「用户本人动作」（ADR-011 同范式）：
   **不走审批浮层、不写盘**，但仍**记审计**（`plan.toggle`，只记 id/状态/条数）。
3. **运行中不 409** —— 勾选只是改内存里的一个状态，不与写盘路径争用；
   Agent 正在跑时手动更正某一步，恰恰是这个功能最有用的时刻。
   不使用 `update_plan` 时界面与今天**逐字节一致**（没有计划卡片）。

### 测试面板（P2-14）的边界

跑测试是「用户本人动作」（ADR-011 同范式），三条约束：

1. **命令来自用户配置，模型改不了它** —— `VIBE_TEST_COMMAND` 或 VIBE.md 的
   `test:` 行；执行前过 `CommandGuard`（DENY 即拒），但**不走审批浮层**
   （是人自己点的按钮）。不落盘 → 与检查点无关。
2. **运行中不 409** —— 跑测试不与写盘路径争用，Agent 正在跑时也可以点。
   但**同一时刻只跑一批**：由一把锁串行（并发点第二次会拿到 409 提示，
   而不是两个 pytest 同时在同一个工作区里跑）。
3. **审计只记计数与位置** —— `test.run` 记 `{scope, requested, passed, failed,
   skipped, ms, file, line}`；失败正文只留本机（ADR-006）。未配 `test_command`
   时三个端点都如实回「未配置」，不报错、不影响其他功能。

### 上下文窗口管理（P2-17）的边界

两条约束（详见 ADR-020）：

1. **预算口径可切换、默认不变** —— 配了 `VIBE_CONTEXT_BUDGET_TOKENS` 才按 token
   估算裁剪；不配仍走 `VIBE_MAX_CONTEXT_CHARS` 的字符口径，**裁剪时机与请求体
   逐字节一致**。`/api/context/stats` 与 `run.context` 报的分段**恒为 token**
   （估算器总在，缺 tiktoken 走启发式），`budgetTokens: 0` 表示这套部署按字符
   裁剪 —— 界面据此只显示用量、不画进度条。
2. **prompt caching 会外发一个供应商专有字段** —— `VIBE_PROMPT_CACHE=auto`
   （默认）首次带上 `prompt_cache_key` 探测：上游以 400/422 **且报错文本里出现
   该字段名**为据判「不支持」，此后永久不注入并重发一次；`on` 强制注入，
   `off` 不注入。字段值只是会话 id，**不含任何代码或对话正文**（ADR-006）。

### 外部 MCP server（P1-6）的边界

外部工具是**不可信来源**，三条约束（详见 ADR-015）：

1. **外部工具恒 REVIEW、不可配置放行** —— 无论 server 声明的 schema 看着多"安全"，
   `mcp__*` 一律 `Risk.REVIEW`：与会话内 `approve_all` 的**显式放行**语义一致，
   但**不新增**任何可关闭审批的开关（ADR-008）。
2. **工具名前缀隔离、argv 只来自受信配置** —— 工具名 `mcp__<server>__<tool>`，
   与内置工具同 registry、同一审批漏斗（`previewer` 展示 server / tool / 参数 JSON）；
   子进程 `shell=False`、`cwd` 锁工作区、`env` 过 `scrub_env()`；命令仅来自
   `.vibe/mcp.json`（或 `VIBE_MCP_CONFIG`），**不接受运行时用户输入拼命令**；
   与内置工具（或彼此之间）**名字冲突时拒绝启动该 server** 并给原因。
   界面里的「新增 / 编辑 / 删除 server」也**只写这份配置文件**（`POST /api/mcp/servers`
   / `.../remove`），同样不拼命令；写入前后各记一条 `mcp.server_added` / `mcp.server_removed`
   （仅元数据：server 名、transport、argc）。
3. **审计只记 digest 与计数** —— `mcp.call` / `mcp.server_start` 记
   `{server, tool, ok, argc, args_digest, ms}`，**参数正文只留本机**（ADR-006）；
   调用结果经 `wrap_untrusted` 包裹，模型据此知其为外部内容。默认
   `mcp_enabled=false`：不配时**不启动任何 MCP 子进程**，行为与今天逐字节一致。

### 交互式终端（P1-7）：一条与 Agent 彻底隔离的独立通道

终端让用户**在 IDE 里直接敲命令**（`top` / `python` REPL 这类交互程序）。它是
「用户本人动作」（ADR-011 范式），但比编辑面走得更远：四条约束。

1. **与 Agent 通道彻底隔离** —— Agent 仍然**只能**走受审批的 `run_command`；
   终端是用户自己开的 PTY，Agent **不能**读写它。因此这里**不走审批浮层**
   （是用户自己在敲键盘），但终端进程**受同一套路径沙箱**约束（`cwd` 锁工作区）。
2. **输出走独立事件流** —— `term.data` / `term.exit` 刻意与 `run.*` 分开：
   终端输出不属于 Agent 的一轮任务，混进 run 事件流会污染会话视图。`term.data`
   的 `data` 是**原始字节的 base64** —— 终端输出的字节可能不是合法 UTF-8
   （程序半屏刷新时的转义序列截断），用 JSON 字符串承载会丢字节。
3. **审计只记 open/close** —— `term.open` / `term.close` 记
   `{termId, argc, argv_digest, cols, rows, mode, exit_code, ms}`；
   **逐次键入不记**（`/api/term/input` 会爆量），**输入内容与命令正文只留本机**
   （ADR-006）。`termId` 只是进程内的短标识，不含用户数据。
4. **零强制依赖、缺能力就降级** —— Unix 用标准库 `pty`（`os.openpty` + `fcntl`），
   Windows 用 `ctypes` 直调 ConPTY，**都不引三方库**（核心链路 `dependencies` 仍为
   `[]`）。ConPTY 任一 API 探不到就**降级为管道模式**（`mode:"pipe"`，无 TTY 语义、
   `resize` 为 no-op），并在 `/api/term/open` 的 `fallbackReason` 里给出原因 ——
   界面据此说明，功能不崩。`VIBE_TERM=0` 时 `/api/term/open` 明确拒绝、界面隐藏入口。

### 网页预览 / 自检（P1-11）的边界

预览与自检是两件事，各有各的边界：

1. **预览是纯静态服务，零依赖** —— `GET /preview/<relpath>` 只把工作区里的
   文件原样送出：过 `PathGuard.check_read`（越权 → 403）、**显式拒 `.vibe/`**
   （`check_read` 本身不拒该目录，但预览不该把它暴露出去）、按 MIME 白名单
   决定 `Content-Type`（未知后缀 → 403，`.exe` 之类不在白名单内），并带上
   **预览专用 CSP**（`connect-src 'none'` / `form-action 'none'`）把网络出口焊死。
   `frame-ancestors 'self'` 允许编辑器同源内嵌。它**不在 `/api/*` 下**，因此
   与静态壳一样**不查令牌**（iframe 子资源带不了令牌），但 Host/Origin 双校验照常。
2. **截图只能由壳提供，host 只做比对** —— host 是 Python 进程，**不可能反向调
   Electron 的 IPC**。于是 `/api/visual/check` 的 `image` 由界面经壳桥
   （`vibe:capture-page` 离屏截图）取好再 POST 上来；host 只负责解码 PNG、
   与 `.vibe/visual/<hash>.png` 基线做**粗粒度分块色差**、必要时覆盖基线。
   **不带 `image` 时如实回 `{available:false, reason}`**（浏览器直开没有捕获
   能力），不是 500 —— 与 LSP / MCP / 终端同范式，界面据此提示「需桌面端」。
3. **基线只落 host 侧、审计不记内容** —— 基线写进 `.vibe/visual/`（模型经
   PathGuard 写不进该目录），差异是**粗粒度提示**（分块均色差，非像素级判定）。
   审计 `visual.check` 只记 `{url_digest, width, height, diff_ratio,
   baseline_missing, ms}`，**不记截图内容**（ADR-006）。

### 多 Agent 编排（M3）的三条边界

`run.subagent` 承载父 Agent 派生的**只读子 Agent**的进度。一条委派拆成一串帧
（`phase`），由父侧 `task` 工具卡片的 `callId` 归位成内嵌的可展开子列表。三条约束：

1. **子 Agent 只读** —— 它的工具面由 `build_readonly_registry()` 白名单给出，
   结构上没有写盘 / 执行能力。因此委派**不进审批**（`task` 是 `Risk.SAFE`），
   而 ADR-008 未被削弱：写盘一律回到父 Agent 走既有审批漏斗。
2. **无递归** —— 子 Agent 的工具面里**没有** `task`，它不能再往下派生。
3. **事件只带元数据** —— `run.subagent` 不含子 Agent 正文（工具名、迭代序号、
   计数、摘要长度而已）。子 Agent 的结论只随 `task` 工具的返回值回到父上下文，
   经 `wrap_untrusted` 包裹（子 Agent 读到的是工作区文本，天然是注入面）。
"""

from __future__ import annotations

import time
from typing import Any

# ---- 事件类型 --------------------------------------------------------------
SESSION_READY = "session.ready"
RUN_STARTED = "run.started"
RUN_ITERATION = "run.iteration"
RUN_ASSISTANT_DELTA = "run.assistant_delta"
RUN_TOOL_CALL = "run.tool_call"
RUN_TOOL_RESULT = "run.tool_result"
RUN_APPROVAL = "run.approval"
RUN_APPROVAL_RESOLVED = "run.approval_resolved"
RUN_NOTICE = "run.notice"
RUN_PLAN = "run.plan"
RUN_CONTEXT = "run.context"
# 子 Agent 进度（M3）。一次 `task` 委派拆成一串帧：父侧工具卡片（`callId`）内嵌
# 一个可展开的子列表。**只带元数据** —— 子 Agent 的正文只在 `task` 工具的返回值里，
# 经 `wrap_untrusted` 包裹后进父上下文（见模块头「多 Agent 边界」）。
RUN_SUBAGENT = "run.subagent"
RUN_FINISHED = "run.finished"
RUN_FAILED = "run.failed"
RUN_INTERRUPTED = "run.interrupted"
# 测试树（K/P2-14）。刻意与 `run.*` 分开：跑测试是**用户本人**发起的动作，
# 不是 Agent 的一轮任务，混进 run 事件流会让会话视图里多出一段不属于它的输出。
TEST_STARTED = "test.started"
TEST_FINISHED = "test.finished"
# 交互式终端（M/P1-7）。同样刻意与 `run.*` 分开：这是用户在 IDE 里直接开的
# 一条独立通道，不是 Agent 的一轮任务（详见模块头的「交互式终端」边界）。
TERM_DATA = "term.data"
TERM_EXIT = "term.exit"

# ---- 审批决定 --------------------------------------------------------------
DECISIONS = ("approve", "approve_all", "deny", "deny_all")

# ---- 运行状态 --------------------------------------------------------------
STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_AWAITING_APPROVAL = "awaiting_approval"


def event(type_: str, **payload: Any) -> dict[str, Any]:
    """构造一个待广播的事件。`id` 由 EventBus 填充。"""
    return {"type": type_, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **payload}
