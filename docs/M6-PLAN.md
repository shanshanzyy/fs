# M6 里程碑：P1 + P2 能力补齐（12 条）—— 任务清单与收口

| 项 | 内容 |
|---|---|
| 版本 | v0.6（**F、G、H、I、J、K、L、M、N、O、P、Q 已交付** —— 12 阶段全部闭合） |
| 日期 | 2026-09-29 |
| 前置 | M0–M5 均已交付（A–E 五条 P0 能力闭合）；本轮开工基线：agent-core **468 收集 / 467 passed / 1 skipped**，服务端 **262 收集 / 260 passed / 2 skipped / 338 subtests**，`e2e_governance.py` **8 档全绿** |
| 定位 | 把 P1/P2 路线图剩下的 **P1 六条（⑥–⑪）+ P2 六条（⑫–⑰）** 从「没有」或「只有设计」做到「可用且带验收」 |
| 上游文档 | `.trae/documents/P1-P2-FEATURES-PLAN.md`（**权威计划**：12 阶段任务表 / 验收 / 红线 / 明确不做）、`docs/ARCHITECTURE.md`（ADR-001~014；本轮新增 ADR-015~021） |
| 规模估算 | 不写工期；按 F→Q 顺序交付，每阶段独立可回归、可验收 |

---

## 0. 为什么单开一个里程碑

A–E（M5）补齐的是「和 IDE 比最直观能感知到的缺能力」。把剩下的欠账盘一遍，
得到的是 12 条**结构上互相独立**、但都与「本地智能体本体」直接相关的能力：
命令面板、计划视图、上下文窗口管理、多文件原子编辑、增量索引、测试树、MCP、
真实终端、多模态、预览自检、规则与记忆、多租户运营。它们既不是「效果调优」，
也不能塞进前五个里程碑的任一主题，因此单开 M6。

其中「多模态会外发」「记忆是持久注入面」「PTY 跨平台/进程泄漏」「事务回滚不彻底」
「MCP 外部工具越权」五处是**高风险面**，计划已逐条给了边界与缓解（见
`P1-P2-FEATURES-PLAN.md` §18）。

---

## 1. 范围

| 阶段 | 对应 | 内容 | 风险 | 状态 |
|---|---|---|---|---|
| **F** | P2-13 | 快捷键体系 / 命令面板 | 低 | ✅ 已交付 |
| G | P2-12 | 计划视图（计划-执行可视化） | 低 | ✅ 已交付 |
| **H** | P2-17 | 上下文窗口管理（token 预算可视化 + prompt caching） | 中 | ✅ 已交付 |
| **I** | P1-8 | 多文件原子编辑（事务） | 中 | ✅ 已交付 |
| J | P1-9 | 增量索引 / 文件监听 | 中 | ✅ 已交付 |
| K | P2-14 | IDE 内测试树 | 中 | ✅ 已交付 |
| L | P1-6 | MCP 客户端 | 中高 | ✅ 已交付 |
| M | P1-7 | 真实交互式终端（PTY） | 高 | ✅ 已交付 |
| N | P1-10 | 多模态输入（图片 / 截图 → 代码） | 高 | ✅ 已交付 |
| O | P1-11 | 浏览器预览 / 网页自检 | 中 | ✅ 已交付 |
| P | P2-15 | 规则与记忆体系 | 高 | ✅ 已交付 |
| Q | P2-16 | 多租户运营落地（登录页 + 部署形态） | 中 | ✅ 已交付 |

### 明确不做（本轮，逐条见计划 §各阶段「明确不做」）

- ❌ 用户自定义键位、快捷键速查表独立面板（F）
- ❌ 计划持久化进 SQLite、按计划自动逐步执行的编排器（G）
- ❌ 本地 tokenizer 词表打包、按语义分块压缩历史的新摘要算法（H）
- ❌ 跨工具调用的长事务、文件系统级 fsync 崩溃一致性（I）
- ❌ 引入 `watchdog`、索引落盘（J）
- ❌ 非 pytest 框架适配层、覆盖率可视化（K）
- ❌ MCP 官方 SDK（L）
- ❌ 终端给 Agent 开通道（M）
- ❌ Figma API 直连（N）
- ❌ 浏览器自动化框架（Playwright 等）（O）
- ❌ 静默自动学习记忆（P）
- ❌ 前端框架 / 构建链、自助注册 UI、K8s 清单落盘（Q）

---

## 2. 任务分解

> 通用约束（12 条全适用，原文见 `P1-P2-FEATURES-PLAN.md` §2）：核心链路零强制依赖
> （`dependencies` 保持 `[]`、前端零第三方库）；落盘恒走 `plan_patch`/`plan_overwrite`
> + `commit_plan`（ADR-004）；Agent 发起恒走审批（ADR-008）；「用户本人动作」不走审批
> 浮层但须 409/收检查点/记审计（ADR-011）；审计只记元数据（ADR-006）；新审计事件三处
> 同批同步（vocabulary / redaction / test_chain_compat）；PathGuard 保护 `.vibe/`；
> 不配 `VIBE_SERVER_URL` 行为一行不变；Config 是 frozen dataclass，改字段用 `replace()`。

### F 快捷键体系 / 命令面板（✅ 已交付）

| 子任务 | 内容 |
|---|---|
| F1 | `host/ui/app.js`：新增 `const KEYMAP`（`{chord, id, label, when?, run}`）+ `PALETTE_ACTIONS`；把旧 `document.keydown` 里的 Ctrl+1..6 / Ctrl+` / Ctrl+K / Ctrl+S / F12 / F2 / Esc / Ctrl+Enter **逐条搬进键位表** |
| F2 | `matchCommand(event)` + 单一 keydown 改造：只做「查表 → 命中即 preventDefault + run；未命中即 return（不吞键）」。`chordOf()` 把 Ctrl/Meta 归一为 `Ctrl`，字母键转小写、`Escape→Esc`；对旧实现 Shift 不敏感的两处（Ctrl+K / Ctrl+S）额外登记 `Ctrl+Shift+X` 变体，保证行为不变。**KEYMAP 顺序即优先级**，Esc 的匹配顺序保持「重命名条 → 工作区弹窗 → 回滚浮层 → 审批拒绝」 |
| F3 | `buildCommandList()`：命令清单 = KEYMAP（去重，`palette:false` 的纯语境键不入列表）∪ 动态动作（切换工作区 / 生成提交信息 / 回滚 / 新建会话 / 中断 / 刷新树·改动·Git） |
| F4 | `index.html` + `styles.css`：命令面板浮层 `#palette`（输入框 + 分组列表）；↑↓ 移动、Enter 执行、Esc 关闭、点击遮罩关闭；字号对齐既有浮层（列表 13.5px / 快捷键提示 12px） |
| F5 | `openPalette()/closePalette()/filterCommands(q)`：过滤用**子序列模糊匹配**（`label` 与 `id`，不引第三方库）；执行前先关浮层再调 `run()`；面板开着时审批态快捷键优先（面板输入框在 `S.awaiting` 时让键冒泡给 document） |
| F6 | `index.html`：标题栏加「⌘K」按钮（`btn-palette`），让鼠标用户也能发现该能力 |
| F7 | `packages/agent-core/tests/test_host.py`：新增 `test_command_palette_contract`，钉住面板 DOM 契约、旧快捷键逐条登记、过滤是**真子序列匹配**而非恒匹配 |

**验收**：旧快捷键行为与改造前**完全一致**（回归全绿）；`Ctrl/Cmd+Shift+P` 弹出面板，
输入「git」筛出「打开：Git 面板」与「生成提交信息」，Enter 执行；面板开着时审批态
快捷键（Esc 拒绝 / Ctrl+Enter 批准）仍优先。专项测试：`test_host.py`。

**红线核对**：纯前端；不动后端；不新增依赖；不改任何写盘与审批路径。

---

### G 计划视图（✅ 已交付）

| 子任务 | 内容 |
|---|---|
| G1 | 新增 `agent_core/tools/plan.py`：`UPDATE_PLAN_SPEC`（`Risk.SAFE`）。`normalize_steps()` 校验并归一化 `steps: [{id, text, status}]`（**整批拒绝**：任一非法即抛 `ToolError`），上限 `MAX_PLAN_STEPS=20` / `MAX_STEP_TEXT=200` / `MAX_STEP_ID=40`，缺省 `id` 补 `s{index}`、缺省 `status` 补 `pending`；handler 经 `ctx.plan_sink` 全量替换会话计划（**幂等**），返回一句话确认 + 步骤清单。**纯内存，不落盘** |
| G2 | `tools/__init__.py`：`specs` 列表在 `WRITE_FILE_SPEC` 后注册 `UPDATE_PLAN_SPEC`，docstring「核心 10 个」→「核心 11 个」 |
| G3 | `loop/agent.py`：`ToolContext` 新增 `plan_sink`，`Agent.__init__` 注入 `self._replace_plan`；新增 `AgentHooks.on_plan(steps, revision)` 钩子；`Agent.plan` / `_plan_revision` 落成，`reset()` 清空；`_replace_plan`（防御性拷贝 + revision++）、`plan_snapshot()`、`set_plan_step(id, status)`（**刻意不广播**，由会话层先审计后广播） |
| G4 | `host/protocol.py`：新增 `RUN_PLAN = "run.plan"`，payload `{runId, steps[], revision}`；同步事件表、HTTP 表，并补「计划视图边界」三条约束（只存内存 / 模型驱动状态人可改标注 / 运行中不 409） |
| G5 | `host/session.py`：`describe()` 带 `plan` 快照（刷新不丢）；`_SessionHooks.on_plan` 广播 `run.plan`（不节流）；新增 `plan_toggle(step_id, status)` —— 校验 → 锁内改状态 → `audit.record("plan.toggle", ...)` → 广播 |
| G6 | `host/ui/index.html` + `styles.css` + `app.js`：composer 内、审批条上方新增可折叠计划卡片 `#plan-card`（`#plan-head` + `#plan-list`）；状态图标 ○/◐/●/◌；随 `run.plan` 事件实时更新（`applyPlan` 按 `revision` 丢弃乱序旧版本） |
| G7 | `host/ui/app.js`：点某一步在 `done↔pending` 间切换 → `POST /api/plan/toggle {id, status}`（用户本人动作，不审批、不写盘、记审计）；`togglePlanCollapsed()` 折叠 |
| G8 | `host/server.py`：`POST /api/plan/toggle` → `session.plan_toggle(id, status)`，`ValueError` 转 400；运行中不 409 |
| G9 | `packages/agent-core/tests/test_plan.py`（新增 18 例）：工具层（注册为 SAFE / 五类校验 / 幂等替换 / 空计划清空 / 无 sink 不炸 / 不落盘）+ 会话层（工具→事件→快照 / 不用则不产生事件 / 勾选记审计并广播 / 运行中不冲突 / 非法输入 / reset 清空）+ HTTP 层（toggle 往返 / 四类坏请求 400） |
| 审计三重同步 | `governance/vocabulary.py` `_RULES`、`apps/server/vibe_server/redaction.py` `_SCHEMA`、`apps/server/tests/test_chain_compat.py` `_sample_locals()` 同批新增 `plan.toggle`（字段 `id`/`status`/`steps_count`/`revision`，均为短标识或计数，不含计划正文） |

**验收**：让 Agent「先列出计划再动手」，界面出现计划卡片并随执行推进更新状态；
点某一步可手动标注完成，刷新页面后计划仍在（来自快照）；不使用该工具时，
界面与今天**逐字节一致**（无计划卡片）。专项测试：`test_plan.py`。

**红线核对**：`update_plan` 是 SAFE + 纯内存，不触审批、不写盘；审计只记计数与短标识（ADR-006）。

---

### H 上下文窗口管理（✅ 已交付）

| 子任务 | 内容 |
|---|---|
| H1 | 新增 `agent_core/context/tokens.py`：`heuristic_tokens()`（CJK/全角按 1 token/字、其余按 1/4、向上取整）、`estimate_tokens()` / `estimate_messages()`（含 `tool_calls` 的 name+arguments 与每消息固定开销 `PER_MESSAGE_TOKENS=4`）、图片 part（`image_url`/`image`/`input_image`）固定 `IMAGE_TOKENS=85`；`_tiktoken()` 用 `importlib.util.find_spec` **只探测不打包**、装坏也不拖垮；`estimator_name()` 返回 `tiktoken`/`heuristic`；**估算永不抛异常** |
| H2 | `config.py`：新增 `context_budget_tokens: int = 0`（0 = 未启用，沿用字符口径）与 `prompt_cache: str = "auto"`；新增 `_env_choice()` 助手（`_env_lsp_mode` 改为委托它）；接线 `VIBE_CONTEXT_BUDGET_TOKENS` / `VIBE_PROMPT_CACHE`（`auto｜on｜off`） |
| H3 | `loop/agent.py`：新增 `_token_budget()` / `_context_budget()`（**预算数值与长度单位同源**，消除静默失配）/ `_total_units()` / `_units_of()`，替换旧 `_total_chars()`/`_chars_of()`；`_plan_trim(start)` 不再接收 budget，内部自取；`_trim_context()` 的 notice 文案按口径切换（token → `VIBE_CONTEXT_BUDGET_TOKENS`，字符 → `VIBE_MAX_CONTEXT_CHARS`，默认路径逐字节不变） |
| H4 | `llm/openai_compat.py`：`prompt_cache` 参数（默认 `off`）+ `cache_key`；`_post_with_cache()` / `_open_stream()` 注入 `prompt_cache_key`；`auto` 走 **inline 被动探测**：上游 400/422 **且**报错文本含字段名 → 永久 `_cache_support=False` 并去掉键重发一次；`cache_state` 三态 `on｜off｜unsupported` |
| H5 | `loop/agent.py` 新增 `context_stats()`（只读无副作用：`budgetTokens`/`usedTokens`/`segments{system,rules,history,toolOutputs}`/`estimator`/`trimmedLastRun`/`cache`，**分段之和恒等于总量**、规则从系统段扣除）；`AgentHooks.on_context()` 钩子**在裁剪之后**发；`host/session.py` 的 `describe()`/`context_stats()` 透传；`host/server.py` 新增 `GET /api/context/stats` |
| H6 | `host/protocol.py` 新增 `RUN_CONTEXT = "run.context"`（payload 六字段）+ 事件表 + HTTP 表 + 「上下文窗口管理边界」两条约束；`host/session.py` 的 `_SessionHooks.on_context` 每轮广播、`_worker` 收尾补播一次；`host/ui/index.html`+`styles.css`+`app.js` 新增 `#ctx-bar` 预算条（四分段色、near/over 两档告警、`上下文 X / Y · 本轮已裁剪`、cache 三态） |
| H7 | `packages/agent-core/tests/test_tokens.py`（新增 13 例）：启发式量级、多模态固定开销、**字符/ token 口径切换**、`context_stats` 不变式、`run.context` 每轮+收尾、`GET /api/context/stats` 只读、快照一致性 |
| H8 | `packages/agent-core/tests/test_llm.py`（增量 6 例）：`off` 不注入、`on` 注入稳定键、`auto` 被拒后一次性降级、`auto` 成功报 `on`、**普通 400 不被误判为不支持**、无 key 时 `off` |
| 审计三重同步 | H **未新增任何审计事件**（预算/caching 只记元数据于既有事件与快照中），故 `vocabulary._RULES` / `redaction._SCHEMA` / `test_chain_compat._sample_locals()` 三处**无需改动** |

**验收**：`context_budget_tokens=0` 时行为与改造前**逐字节一致**（回归全绿）；开启 token 预算后界面预算条按 token 显示、超 80% 变黄、超限变红；上游不支持 caching 时自动降级一次且不报错。专项测试：`test_tokens.py` + `test_llm.py`。

**红线核对**：tiktoken 只探测不打包（核心链路零强制依赖）；caching 只在外发供应商专有字段（默认 off 不改变请求体）；估算永不抛异常。

---

### I 多文件原子编辑（✅ 已交付）

| 子任务 | 内容 |
|---|---|
| I1 | 新增 `agent_core/edit/transaction.py`：`EditTransaction`。阶段一（准备）：对每个目标文件走 `plan_patch`/`plan_overwrite` 生成 `PatchPlan`（**只算不写**），并完成路径沙箱 / search 命中 / 乐观锁（`expected_digest`）全部校验；任一失败即抛错，**一个文件都不落盘**。阶段二（提交）：先逐个收检查点快照，再逐个 `commit_plan`；任一写失败 → 逆序用计划里的 `old_text` 通过 `plan_overwrite`+`commit_plan` **回滚已写文件**（新建文件则删除）。上限 `MAX_EDIT_FILES=20`。`TransactionError.rolled_back` 如实报告回滚是否完成 |
| I2 | 检查点复用：**不新增写盘/快照实现** —— 快照仍走 `CheckpointManager.snapshot_before_write`（经构造传入的回调），回滚素材直接用 `PatchPlan.old_text`（阶段一已算好、阶段二无其它写者，即提交那刻的磁盘内容） |
| I3 | 新增 `agent_core/tools/multi_edit.py`：`EDIT_FILES_SPEC`（**`Risk.REVIEW`**）。参数 `edits: [{path, search, replace}｜{path, content, overwrite?}]`（可选 `replace_all`/`expected_digest`）。**整批拒绝**：非数组/空/缺 path/重复 path/两种模式同时给/超上限 → 抛 `ToolError`。`previewer` 汇总各文件 unified diff（**一次审批看全部**，命中失败标 `blocking`）；handler 走 `EditTransaction` |
| I4 | `tools/__init__.py`：注册 `EDIT_FILES_SPEC`，docstring「核心 11 个」→「核心 12 个」（`edit_file`/`write_file` 保留：单文件场景语义更清晰、审批更聚焦） |
| I5 | `host/session.py`：`lsp_rename_apply`（已多文件）改走 `EditTransaction`，获得「整批校验—整批落盘—失败整批回滚」；`rewrite/apply`、`edit/save` 仍为单文件，保持原样。前端契约不变（失败时额外补一条 `edit.transaction` 审计） |
| I6 | 新增审计事件 `edit.transaction`：`{files_count, changed_count, added, removed, strategy:"atomic", rolled_back:bool}`。**只记计数与结果标志**（各文件 diff 正文只进审批预览，不进审计）。三重同步：`vocabulary._RULES` / `redaction._SCHEMA` / `test_chain_compat._sample_locals()` |
| I7 | 新增 `packages/agent-core/tests/test_transaction.py`（17 例）：① 全成（3 文件都写、无改动为 no-op）；② **阶段一失败整体不写**（search 失配 / 乐观锁不符 / 超上限 / 覆盖未显式 overwrite）；③ **阶段二失败回滚**（已写文件恢复原内容、新建文件被删掉）+ 落盘前逐文件收快照；④ 工具层（注册 REVIEW / 五类坏输入整批拒绝 / 审计只含计数 / 预演阻塞与汇总） |
| 审计三重同步 | 新增 `edit.transaction` 于 `governance/vocabulary.py` `_RULES`、`apps/server/vibe_server/redaction.py` `_SCHEMA`、`apps/server/tests/test_chain_compat.py` `_sample_locals()`（字段均为计数/短枚举/布尔，不含正文） |

**验收**：Agent 一次改 3 个文件，界面**一次审批**（汇总 diff），提交后 3 个都变；构造第 2 个文件 search 失配：审批后**一个都不落盘**；第 2 个文件写盘失败：已写文件被回滚为原内容（新建文件被删）。现有 `edit_file`/`write_file` 行为不变（回归全绿）。专项测试：`test_transaction.py`。

**红线核对**：ADR-004（事务是同一引擎的编排，未新增写盘路径）；ADR-008（`EDIT_FILES_SPEC` 是 REVIEW，Agent 发起恒审批）；ADR-006（审计只记计数）。

---

### J 增量索引 / 文件监听（✅ 已交付）

| 子任务 | 内容 |
|---|---|
| J1 | `agent_core/index/lexical.py`：`_lengths` 由 `list` 改 `dict[int,int]`（支持稀疏 doc_id），新增 `_doc_terms: dict[int,set[str]]` 记录每个文档的 token 集合；新增 `remove(doc_id)`（摘 `_lengths`、按 `_doc_terms` 逐 token 摘 postings，空桶即删）；`add` 对同一 id 先 `remove` 保证幂等；`search` 的 `sum(self._lengths.values())` 让 `total/avgdl` 只统计存活文档 |
| J2 | `agent_core/index/store.py`（核心）：`chunks` 由 `list` 改 `dict[int,Chunk]`；`build()` 改为循环调 `_ingest(rel,path)`；新增 `_drop_file(rel)`（摘块+倒排+依赖+files）、`_ingest`（返回 bool 且**幂等**：已在索引里先 `_drop_file` 再收）；新增 `update(added,changed,removed)` —— 先摘 `removed∪changed` 再收 `added∪changed`，末尾 `DepGraph.build`，返回 `{added,changed,removed,files,chunks,edges}`（**计数在摘除前取**） |
| J3 | `config.py`：新增 `index_watch: bool = False`（默认关，行为一行不变）、`index_watch_interval: int = 5`；`load()` 接 `VIBE_INDEX_WATCH` / `VIBE_INDEX_WATCH_INTERVAL`（`max(MIN_WATCH_INTERVAL, …)` 强制下限 2s） |
| J4 | `agent_core/index/watch.py`（新增）：`IndexWatcher(root, rel_of, interval, max_files, window, overlap, index, audit)`，**纯 stdlib 轮询**（零强制依赖红线，对齐 `lsp/manager.py` 范式）。`(mtime_ns,size)` 快照 diff → `update(added/changed/removed)`；单后台线程 + `threading.Event`；`stop()` 幂等 + `atexit` 双保险回收；`poll_once()`（测试可注入）、`status()` 返回 `{watching,interval,files,chunks,edges,lastUpdate,pending}` |
| J5 | `host/session.py`：`__init__` 末尾按 `config.index_watch` 拉起 watcher（`rel_of` 用 `ctx.paths.relative`，与 `semantic_search` 口径逐字符一致）；新增 `close()`（停 watcher）与 `index_status()`（watch 关时走 `peek_index` **只读**，绝不因查询而建索引）。`host/server.py` 新增 `GET /api/index/status`；`switch_workspace()` 在 `end_session` 后补 `current.close()` |
| J6 | 缓存与 watch 协作：新增 `_PINNED` 集合与 `get_index(..., pin=False)`（pin 命中的缓存实例补设 `pinned=True`）、`peek_index()`（只读不建）、`release_index(index)`；LRU 淘汰**跳过 pinned**，`clear_cache()` 同步清 `_PINNED`。保证被 watcher 持有的索引不会被淘汰、且同 root 调用恒返回**同一个实例**（避免「watcher 更新 A、检索读 B」） |
| J7 | 新增审计事件 `index.rebuild`：`{added, changed, removed, files_count, ms}`（**只记计数**，不含路径与正文）。三重同步：`vocabulary._RULES` / `redaction._SCHEMA` / `test_chain_compat._sample_locals()`。watcher 调用点必须是 `self._audit.record("index.rebuild", …)` **字面量**形式（AST 测试只认字面量调用） |
| J8 | 新增 `packages/agent-core/tests/test_watch.py`（**18 例 + 4 subtests**）：增量更新（added 可检索 / removed 检索不到 / changed 替换旧块 / **update 与全量重建等价**（chunks/files/doc_count/term_count/4 query 逐项一致）/ 空词桶清理 / stats / 缺文件与二进制容忍 / 依赖边随 update 变 / remove→re-add 单条）+ watcher（无变化返回 None / `poll_once` 应用并记审计且审计不含路径与正文 / `stop` 回收线程 / `stop` 幂等 / 与检索共享同一实例 / pinned 不被 LRU 淘汰）+ 会话层（watch 关行为不变且不起线程 / watch 开报健康度 / interval 下限 2） |
| J9 | 反向验证：临时把 `update()` 的摘除循环从 `dict.fromkeys([*removed_rels, *changed])` 改为 `dict.fromkeys([*changed])`（退化为「只加不删」）→ 恰 **3 例变红**，已还原 |
| 审计三重同步 | 新增 `index.rebuild` 于 `governance/vocabulary.py` `_RULES`、`apps/server/vibe_server/redaction.py` `_SCHEMA`、`apps/server/tests/test_chain_compat.py` `_sample_locals()`（字段均为计数，不含路径与正文） |

**验收**：改动工作区文件后，`semantic_search` 结论随文件变化（新增即可检索、删除即消失、修改即替换）；
`GET /api/index/status` 如实报告健康度；**`index_watch` 关时行为与今天逐字节一致**（不启线程、
不起额外索引）；watcher 停止后无残留线程。等价性由「update 与全量重建逐项相等」钉住。专项测试：`test_watch.py`。

**红线核对**：不引入 `watchdog`（纯 stdlib 轮询，核心链路零强制依赖）；**索引不落盘**（ADR-013：只驻内存）；
审计只记计数（ADR-006：无路径、无正文）；`index_watch` 默认 `False` 时行为一行不变。
**J 无新增 ADR**（ADR-015~020 不含 J）。

---

### L MCP 客户端（✅ 已交付）

| 子任务 | 内容 |
|---|---|
| L1 | 新增 `agent_core/mcp/client.py`：标准库 JSON-RPC over stdio（`initialize` → `notifications/initialized` → `tools/list` → `tools/call`），**NDJSON 与 `Content-Length` 双分帧**；后台读线程 + 请求-响应关联；`shell=False` + `cwd=workspace` + `env=scrub_env()` + 超时；**可选 HTTP/SSE**（`urllib`，本机地址绕代理） |
| L2 | 新增 `agent_core/mcp/registry.py`：`McpServerConfig`（frozen）/ `load_servers(path)`（坏条目跳过、解析失败抛 `McpError`）/ schema **白名单清洗** / 生成 `ToolSpec`（名 `mcp__<server>__<tool>`，**恒 `Risk.REVIEW`**），handler 调 `tools/call` 且结果 `wrap_untrusted`；`previewer` 展示 server/tool/参数 JSON |
| L3 | `tools/__init__.py`：`build_default_registry(*, lsp=None, mcp=None, audit=None)` 新增 `mcp`/`audit` 形参；**MCP 工具只在 `merge_mcp` 一处并入**同一个 registry（同一审批漏斗）；与内置工具或彼此**名字冲突时拒绝启动该 server** 并给原因 |
| L4 | `config.py`：`mcp_enabled=False` / `mcp_config=""` / `mcp_timeout=30`；`load()` 接 `VIBE_MCP` / `VIBE_MCP_CONFIG` / `VIBE_MCP_TIMEOUT`；`mcp_config_path` property（显式路径优先，否则 `<工作区>/.vibe/mcp.json`） |
| L5 | 新增 `agent_core/mcp/manager.py`：按 server 启动并缓存 tools（**发现即启动、常驻复用**），崩溃重启一次、连续失败标不可用；`/api/mcp/status` 报 `available:false + reason`；`at_exit` 兜底回收全部子进程 |
| L6 | `host/server.py` + `host/session.py`：`GET /api/mcp/status`、`POST /api/mcp/reload`（重读配置、**不落盘不 409**，摘旧 `mcp__*` 后重建）；`host/protocol.py` 登记两端点 + 「外部 MCP server（P1-6）的边界」三条 |
| L7 | 审批预览：`previewer` 展示 server / tool / 参数 JSON；外部工具**恒弹审批**（REVIEW），与会话内 `approve_all` 的**显式**放行语义一致 |
| L8 | 审计：`mcp.call` / `mcp.server_start`：`{server, tool, ok, argc, args_digest, ms}`。**不记参数正文**（只 digest + 计数）。三重同步 |
| L9 | 新增 `packages/agent-core/tests/test_mcp.py`（**12 例 + 2 subtests**）：假 MCP server（标准库脚本）钉住 ① 恒 REVIEW + 双分帧 + schema 清洗；② 结果 `wrap_untrusted`；③ crash → 降级不崩会话；④ 名字冲突被拒；⑤ 未启用不起子进程；另含配置默认值与 `VIBE_MCP*` 接线 |
| L10 | 反向验证：临时把 MCP 工具风险改 `Risk.SAFE` → 「恒 REVIEW」用例变红（已还原） |
| 审计三重同步 | `governance/vocabulary.py` `_RULES`、`apps/server/vibe_server/redaction.py` `_SCHEMA`、`apps/server/tests/test_chain_compat.py` `_sample_locals()` 同批新增 `mcp.call` / `mcp.server_start`（字段为短标识 / 计数 / 布尔，不含参数正文） |

**验收**：配一个 stdio MCP server 后工具清单出现 `mcp__*`，调用恒弹审批、放行后拿到结果；
server 跑不起来 → `/api/mcp/status` 显示 `available:false + 原因`、会话不受影响；
`mcp_enabled=false` 行为与今天逐字节一致（回归全绿）。专项测试：`test_mcp.py`。

**红线核对**：ADR-015（外部工具恒 REVIEW、前缀隔离、argv 只来自受信配置、结果按不可信处理）；
ADR-008（不可配置放行）；ADR-006（审计只记 digest/计数）；ADR-012（不引官方 `mcp` SDK）。

---

### M 真实交互式终端（✅ 已交付）

| 子任务 | 内容 |
|---|---|
| M1 | 新增 `agent_core/terminal/pty_unix.py`：Unix 伪终端后端（**纯标准库** `os.openpty` + `subprocess.Popen(start_new_session=True)` + `fcntl.TIOCSWINSZ` + `select`）。`UnixPtySession`（`mode="pty"`）实现统一鸭子接口 `pid/mode/alive/eof/read(timeout)/write(bytes)/resize(cols,rows)/close()/exit_code`；`_terminate_tree` 用 `os.killpg` 收**整个进程组**（只杀引导进程会留孤儿）；`PtyError` 表失败 |
| M2 | 新增 `agent_core/terminal/pty_windows.py`：Windows ConPTY 后端（`ctypes` 直调 Win32，**不引 `pywinpty`/`winpty`**）。`_load_kernel32()` 探测 12 个 API 并设 argtypes；`CreatePipe`→`CreatePseudoConsole`→`InitializeProcThreadAttributeList`/`UpdateProcThreadAttribute`→`CreateProcessW`（`EXTENDED_STARTUPINFO_PRESENT + PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE`）；后台读线程 `_read_loop`。**任一步失败即抛 `TerminalUnsupported`（绝不半启动）**，由 manager 降级 |
| M3 | 新增 `agent_core/terminal/manager.py`：`TerminalManager`（`max_sessions=3` / `idle_seconds=1800` / `READ_TIMEOUT=0.2` / `READ_CHUNK=65536`）。`_create` **先试 PTY、失败降级 `_PipeSession`（`mode="pipe"`）**并记 `last_mode`/`fallback_reason`；`_pump` 读线程推 `on_data`；`get/write/resize/close/_reap/status/shutdown`；`atexit` 兜底回收，Windows 用 `taskkill /T /F` 按进程树终止。进程内单例 `get_manager()/set_manager()`（对齐 lsp/mcp manager 范式）。新增 `terminal/__init__.py` 导出 |
| M4 | `config.py`：新增 `terminal_enabled=True` / `terminal_shell=""` / `terminal_max_sessions=3`；`load()` 接 `VIBE_TERM` / `VIBE_TERM_SHELL` / `VIBE_TERM_MAX` |
| M5 | `host/session.py`：`_term_ids` 集合；`term_open(cols,rows)`（校验启用 → `set_max_sessions` → `default_shell_argv` → `manager.open(on_data=self._term_data, on_exit=self._term_exit)` → 记 `term.open` 审计 → 返回 `{termId,mode,cols,rows,pid,fallbackReason}`）、`term_input`（空输入**不记审计**）、`term_resize`、`term_close`（记 `term.close` 审计 + 广播 `TERM_EXIT`）、`_terminal_env()`（继承 `os.environ` + `TERM=xterm-256color`/`COLORTERM=truecolor`，**不脱敏** —— 是用户本人的 shell）；`_term_data`/`_term_exit` 发 SSE；`close()` 遍历 `_term_ids` 回收；`describe()` 带 `terminalEnabled`。`host/server.py` 新增 **4 个 POST 端点** `/api/term/open|input|resize|close` |
| M6 | `host/protocol.py`：新增事件 `TERM_DATA="term.data"`（`termId`+`data`(base64)）/ `TERM_EXIT="term.exit"`（`termId`+`exitCode`）与 4 条命令；补「交互式终端（P1-7）：一条与 Agent 彻底隔离的独立通道」四条边界（**隔离 / 独立事件流 / 审计只记 open-close / 零依赖降级**） |
| M7 | `host/ui/index.html`+`styles.css`+`app.js`：`#term` 由被动视图改为**可输入交互终端**。前端自实现**最小 ANSI 子集**（状态机跨 chunk 保持 + `TextDecoder` 流式解码 + SGR 前景/背景/加粗 + `\r`/`\n`/`\b`/`\t` + 清屏 `J`/清行 `K` + 光标定位 `H/A/B/C/D/G/d`），以 `{t, st}` 单元格网格逐行合并 span 渲染；键盘→`/api/term/input`（含 Ctrl+字母、方向键、Home/End/Delete/PgUp/PgDn）、粘贴→`termPaste`；`ResizeObserver` + 等宽字符探针换算 cols/rows→`/api/term/resize`。**Agent 命令输出改到「输出」面板**（`termLine` 目标改 `output`），两条通道在界面上也不混；`VIBE_TERM=0` 时隐藏终端页签并切到「输出」 |
| M8 | 新增审计事件 `term.open` / `term.close`：`term.open` 记 `{termId, argc, argv_digest, cols, rows, mode, ms}`，`term.close` 记 `{termId, argc=0, argv_digest, cols, rows, mode, exit_code, ms}`。**只记短标识 / 计数 / digest**（不记命令正文）。三重同步：`governance/vocabulary.py` `_RULES` / `apps/server/vibe_server/redaction.py` `_SCHEMA` / `apps/server/tests/test_chain_compat.py` `_sample_locals()` |
| M9 | 新增 `packages/agent-core/tests/test_terminal.py`（**7 例 + 1 skip**）：① 起/写/读回显（真 PTY 才有）；② resize 不崩且尺寸如实；③ **关掉后子进程不残留**（跨平台 `_process_alive`，Windows 用 `tasklist`）；④ 关不存在的 id 报 `TerminalError`；⑤ **PTY 不可用时降级管道模式**（注入必抛 `boom` spawn）且仍能写读；⑥ 会话数上限拒绝；⑦ `default_shell_argv` 读配置/随平台 |
| M10 | 反向验证 + 全量回归 + e2e + 文档同步 |
| 审计三重同步 | `governance/vocabulary.py` `_RULES`、`apps/server/vibe_server/redaction.py` `_SCHEMA`、`apps/server/tests/test_chain_compat.py` `_sample_locals()` 同批新增 `term.open` / `term.close`（字段为短标识 / 计数 / digest，不含命令正文） |

**验收**：Unix 下部终端可直接跑 `top` / `python` REPL 类交互程序、彩色输出、缩放换行正确；
Windows 能起终端，ConPTY 不可用时**降级管道模式并在界面如实说明**、功能不崩；
终端命令**不弹审批浮层**但 `term.open` / `term.close` 有审计；`VIBE_TERM=0` 时终端入口隐藏。
专项测试：`test_terminal.py`。

**红线核对**：ADR-016（交互式终端是「用户本人动作」，与 Agent 通道**彻底隔离**：Agent 仍只能走受审批的
`run_command`）；ADR-006（审计只记 `*_digest` / 计数）；核心链路零强制依赖（`dependencies` 保持 `[]`，
ConPTY 用 `ctypes`、Unix 用标准库 `pty`/`fcntl`）。

**环境说明**：本机 Windows 上 ConPTY 调用失败（诊断阶段见 `CreatePseudoConsole` 返回 `HRESULT 0x80070006`
`E_HANDLE` / `GetLastError=5`；浏览器实测阶段失败点又落在 `UpdateProcThreadAttribute`），即**该环境 ConPTY
不可用**——按计划设计，失败即抛「不支持」→ 降级管道模式，屏幕/回显用例①显式 skip、降级路径用例⑤覆盖；
Unix 上 PTY 正常。这是环境限制而非代码缺陷（`available()` 只探测 API 存在性，实际可用性只能靠 `spawn` 验证，
故失败点会随运行在 `CreatePseudoConsole` / `UpdateProcThreadAttribute` 之间浮动，但都归入同一降级路径）。

---

### N 多模态输入（图片 / 截图 → 代码）（✅ 已交付）

| 子任务 | 内容 |
|---|---|
| N1 | `config.py`：新增 `vision_mode="off"`（`off`/`auto`/`on`，**默认 off** —— 会外发，必须显式开）/ `vision_max_image_bytes=4_000_000`；`load()` 接 `VIBE_VISION` / `VIBE_VISION_MAX_BYTES` |
| N2 | 新增 `agent_core/context/images.py`：纯函数 `identify_image`（**按魔数**判类型，不信后缀）/ `load_image`（`check_read` → 后缀白名单 → 读字节 → 大小上限 → 魔数识别 → base64 data URL）/ `to_message_parts`（先文字、后图片）/ `message_text`（从 parts 取纯文本，图片 base64 不进指纹） |
| N3 | `context/references.py`：`_TYPES` 增 `image`；`RefResolution` 增 `images` 并列产物；`resolve_refs(allow_images=False,…)` 的 image 分支**不占字符预算**，`off` 时逐条降级为提示；`build_refs_prompt` 透传参数。图片**不进** `format_refs_block` 文本块 |
| N4 | `host/session.py` `submit`：按 `vision_mode` 解析图片，`resolution.images` 非空才把首条 user 消息 `content` 组装成 parts；**无图片保持纯字符串**（逐字节兼容）；`off` 时 image ref 降级为 `run.notice`。`loop/agent.py` `run` 接受 `str \| list[parts]`，审计/检查点只取**纯文本**算长度与指纹 |
| N5 | `host/server.py`：新增 `POST /api/upload-image`（放宽 body 上限以容 base64）→ `session.upload_image(name,data,mediaType)`：`off` 拒绝 → base64 解码 → 大小上限 → `identify_image` → 落 `<workspace>/.vibe/images/<uuid>.<ext>` → 审计 `image.upload`（`{bytes,ext,digest}`）→ 返回 `{ref{type,value},bytes,ext,digest}` |
| N6 | `host/protocol.py`：命令表补 `POST /api/upload-image`；`refs` 文档补 `image` 类型（value 为上传返回的本机图片路径） |
| N7 | `host/ui/index.html`+`styles.css`+`app.js`：composer 加「选图」按钮 + 粘贴图片 → `/api/upload-image` → 复用 refs-bar 的**图片 chip**（`.ref-chip.is-image`）；`vision_mode=off` 时**隐藏入口**（与终端 `VIBE_TERM=0` 同一纪律），粘贴不拦截 |
| N8 | 能力探测（`llm/openai_compat.py`）：auto 模式下首次带图若上游 **400/422 且报错含图片相关词**（状态码 + 关键词**双条件**）→ 永久回退为纯文本（图片换成一行占位说明）并**重发一次**；攒一条提示经 `pop_vision_notice` 由 Agent 转 `run.notice`。`on` 不探测；`off` 不剥图 |
| N9 | 新增 `packages/agent-core/tests/test_images.py`（24 例）：① 夹具识别；② 白名单外 / 超大 / 魔数不符 / 越权被拒；③ parts 形状（含 `data:` 前缀、正文进审计但不含 base64）；④ `allow_images` off 降级、on 产 part 且不占预算；⑤ 会话层 off 降级 notice 且消息仍是纯字符串、on 组 parts、无图逐字节一致；⑥ 上传端点（落盘 / data URL / off 拒 / 非法 base64 / 非图 / 超大 / **审计只记元数据**） |
| N10 | 反向验证 + 全量回归 + e2e + 文档同步 |
| 审计三重同步 | `governance/vocabulary.py` `_RULES`、`apps/server/vibe_server/redaction.py` `_SCHEMA`、`apps/server/tests/test_chain_compat.py` `_sample_locals()` 同批新增 `image.upload`（字段 `bytes`(int) / `ext`(str) / `digest`(digest)，**不记图片正文**） |

**验收**：`VIBE_VISION=on` + 支持模型 → 贴图能据图改代码；不支持 → 明确提示 + 自动回退不崩；
`off`（默认）→ composer 无图片入口、行为与今天逐字节一致。专项测试：`test_images.py`。

**红线核对**：图片会随请求发往**用户配置的模型端点**（非本地则离开本机）→ 故默认 `off`、必须显式开；
图片只落 host 侧 `.vibe/images/`（PathGuard 保护，模型写不进）；审计只记字节数 / 扩展名 / 摘要（ADR-006，未动）。
**明确不做**：❌ Figma API 直连（Figma 走「导出图片」同一路径）❌ 图片 OCR 预处理 ❌ 视频 / 音频输入。
核心链路零强制依赖（`dependencies` 保持 `[]`，base64 / 魔数识别均为标准库）。

---

### P 规则与记忆体系（✅ 已交付）

| 子任务 | 内容 |
|---|---|
| P1 | `project_rules.py` 重写为**多层加载**：常量 `MAX_RULES_BYTES=32KB` / `TOTAL_RULES_BYTES=64KB` / `_CANDIDATES=("VIBE.md", .vibe/VIBE.md)` / `_EXTRA_DIR=.vibe/rules` / `USER_RULES_REL=.vibe/rules.md` / `LAYER_HEADER="### 规则来源：{source}"`。`ProjectInstructions` 增 `layers/truncated_count/sources`。合并顺序 **用户级 → 仓库级 → `.vibe/rules/*.md` + 附加(extra) → 目录级**；**单层无抬头**（逐字节兼容改造前），多层各带抬头；超总量先丢队首（最泛层）。签名 `load_project_instructions(workspace, *, user_rules_path=None, extra="", current_files=None)` |
| P2 | `loop/prompts.py`：`build_system_prompt` 规则段引言说明**多层来源**；保留 `RULE_MARK`/`SAFETY_MARK`/`"绕过审批"` 不变 |
| P3 | 新增 `agent_core/memory/store.py` + `memory/__init__.py`：常量 `MAX_TEXT_CHARS=2000`/`MAX_TAGS=8`/`MAX_TAG_CHARS=32`；表 `memories(id,scope,text,tags,created_at,source_digest)`；`MemoryLimitExceeded(ValueError)`、`MemoryRecord`（frozen + `to_dict()`）、`default_memory_db_path()`、`_TOKEN_RE`；`MemoryStore(path, *, max_items=200)` 提供 `add/remove/count/list/search/close`，`search` 按 token 交集打分 `sort(key=lambda item:(-item[0],-item[1]))`（**词法相关度，不引 embedding**） |
| P4 | 新增 `agent_core/tools/memory.py`：`REMEMBER_SPEC`（**恒 `Risk.REVIEW`**）+ `_remember`（从 `ctx.memory_store` 取库，None 时报「记忆功能未启用（VIBE_MEMORY=0）」；写 `store.add(text,tags,scope="agent",source_digest=digest(text))`；审计 `memory.add` 只记 scope/chars/digest）。`tools/base.py` 的 `ToolContext` 增 `memory_store`；`tools/__init__.py` 导出并 `build_default_registry(..., memory=False)` 按开关注册 |
| P5 | `host/session.py`：`self.memory = self.agent.memory`；`close()` 关 memory；新增 `list_memories()/add_memory(text,tags)/delete_memory(mem_id)`（add 记 `memory.add` scope="user"）；`describe()` 带 `memoryEnabled`。`host/server.py` 新增 `GET /api/memories`、`POST /api/memories`、`POST /api/memories/delete`（`except ValueError` → 400）。`host/protocol.py` 命令表登记三个端点。`host/ui/*`：活动栏加「记忆」页签（`Ctrl+8`）+ 记忆面板（输入/标签/列表/删除），回答气泡追加「记住这条」按钮；`memoryEnabled=false` 时隐藏入口 |
| P6 | `loop/agent.py`：`Agent(..., memory=None)`；`self.registry = registry or build_default_registry(memory=config.memory_enabled)`；记忆装配（注入优先，否则按 `config.memory_db` 或 `default_memory_db_path()` 建库）；`ToolContext(..., memory_store=...)`；规则多层加载 + `rules.load` 审计（layers/chars/truncated_count）；`self._base_system_prompt` + `messages[0]` 初始化。新增 `_memory_section()`（**top-N 检索 + `wrap_untrusted` 包裹** + 记 `memory.inject`）与 `_inject_memories()`（每轮任务前刷新记忆段），`reset()` 复位为基础提示 |
| P7 | `config.py`：新增 `rules_extra=""` / `memory_enabled=True` / `memory_max_items=200` / `memory_inject_limit=5` / `memory_db=""`；`load()` 接 `VIBE_RULES_EXTRA` / `VIBE_MEMORY` / `VIBE_MEMORY_MAX` / `VIBE_MEMORY_INJECT` / `VIBE_MEMORY_DB` |
| P8 | 新增审计事件 `rules.load`（layers/chars/truncated_count） / `memory.add`（scope/chars/digest） / `memory.inject`（count/chars）。三重同步：`governance/vocabulary.py` `_RULES` / `apps/server/vibe_server/redaction.py` `_SCHEMA` / `apps/server/tests/test_chain_compat.py` `_sample_locals()`。审计调用点必须写字面量事件名（AST 扫描只认字面量） |
| P9 | 新增 `packages/agent-core/tests/test_rules_layers.py`（**8 例**）：多层合并顺序 / 根源优先不叠加 / 单层无抬头 / `rules_extra` glob / 目录级就近 / 目录级只匹配路径 / 工作区外忽略 / 总预算丢最泛层。新增 `packages/agent-core/tests/test_memory.py`（**16 例**）：`MemoryStoreTests`(7) + `RememberToolTests`(4) + `AgentMemoryTests`(5)，钉住「恒 REVIEW / 注入被包裹 / top-N 上限 / 关闭时不读不写」四条纪律 |
| P10 | 反向验证 + 全量回归 + e2e + 文档同步 |
| 测试基线隔离 | 新增 `packages/agent-core/tests/conftest.py` 与 `apps/server/tests/conftest.py`，均 `os.environ.setdefault("VIBE_MEMORY", "0")` —— 记忆库默认落 `~/.vibe/memories.db`，CI / 沙箱家目录不可写，若不隔离则「默认建 Agent」的用例会在与被测逻辑无关处抛 `PermissionError`。单例要验记忆时显式 `Config.load(ws, memory_enabled=True, memory_db=<tmp>)` 打开（`setdefault` 尊重外部显式设置） |

**验收**：规则按多层合并（用户级 → 仓库级 → 附加 → 目录级），**单层无抬头**（逐字节兼容改造前）、
多层各带抬头、超总量**先丢最泛层**；记忆写入**必经审批**（`remember` 恒 REVIEW，模型只能提议）、
检索注入一律按不可信内容包裹、`VIBE_MEMORY=0` 时行为与改造前逐字节一致（不建库 / 不注入 /
不注册工具）。专项测试：`test_rules_layers.py` + `test_memory.py`。

**红线核对**（本阶段风险最高，两条硬纪律）：① **注入一律 `wrap_untrusted`**（规则与记忆都是
进提示词的文本，天然 Prompt Injection 面）；② **写入必经用户确认或审批，禁止静默自动学习**
（`REMEMBER_SPEC` 恒 `Risk.REVIEW`，不可配置关闭，ADR-008）。ADR-006（审计只记计数 / digest，
**不记规则与记忆正文**）；`.vibe/` 仅 host 侧可写（PathGuard）；核心链路零强制依赖
（`dependencies` 保持 `[]`，记忆检索为词法打分、不引 embedding）。
**明确不做**：❌ 静默自动学习记忆 ❌ 引入 embedding 向量检索 ❌ 记忆跨机同步。

---

### Q 多租户运营落地（✅ 已交付）

**目标**：补上路线图点名的两块 —— ① **前端登录页**（账号 API 早已就绪，但界面用户没有登录入口，只能靠 CLI）；
② 明确**部署形态**（`infra/` 是否落盘）。

**决策**：选 **Q-默认** —— 沿用 M4 定档，`infra/docker-compose.yml` **不落盘**（K8s 仍只出设计），
本轮只补登录页；「compose 未落盘」记为**有意为之**，理由与触发条件写进 `apps/server/README.md`。

| 子任务 | 内容 |
|---|---|
| Q1 | 新增 `apps/server/vibe_server/api/static.py`：极简静态页服务（**零第三方依赖、不引模板引擎**，手写渲染）。`STATIC_DIR=api/static/`；`CSP` 收紧（`default-src 'self'` / `script-src 'self'` / `style-src 'self' 'unsafe-inline'` / `connect-src 'self'` / `frame-src 'none'` / `form-action 'none'` / `frame-ancestors 'none'`）；`GET /login` 回 `text/html`、`GET /assets/{name}` 回同目录资源（MIME 白名单 + 三道路径穿越防护，含编码过的 `%2e%2e%2f`）。`Referrer-Policy: no-referrer` 由 `httpd._send()` 统一附加（不在 handler 里重复设置） |
| Q2 | 新增 `apps/server/vibe_server/api/static/login.html` + `login.js`：登录表单（邮箱/密码 → `POST /api/v1/auth/login`，成功把 access/refresh 存 `sessionStorage` 并显示成功态；错误凭据原样透出服务端文案）；OIDC 场景给「用 SSO 登录」按钮 → `GET /api/v1/auth/oidc/begin`。**不含任何业务逻辑**（接口路径写字面量，防漂移） |
| Q3 | `api/routes.py`：`register_static(router)` 注册 `/login` 与 `/assets/{name}`（**公开、不需 token**），`build_router` 中排在 `register_auth` 之前；与 `/api/v1/auth/*` 无路由冲突（顶层路径 vs 前缀路径） |
| Q4 | 桌面端接登录（**薄壳**）：`governance/runtime.py` 的 `describe()` 在配置了治理面时暴露 `serverUrl`（未配则不出现）；`host/ui/app.js` 的治理面徽标在 `unauthenticated`/`degraded` 时追加「去登录」入口（`<serverUrl>/login`，`target=_blank` 交给系统浏览器），`styles.css` 加 `.chip-link`。**Electron 壳里不实现任何认证逻辑** |
| Q5 | 新增 `apps/server/tests/test_static.py`（**10 例 + 5 subtests**）：① `/login` 200 + `text/html` + `Referrer-Policy: no-referrer`；② 路径穿越（含 `%2e%2e%2f`）被拒；③ 未认证可访问；④ CSP 头存在；外加与 `/api/v1/auth/*` 无冲突、`login.js` 接口路径与 `routes.API` 防漂移 |
| Q6 | 决策点：`infra/` 落盘与否（结论见上，**Q-默认**） |

**验收**：浏览器打开 `<server>:8787/login` 能登录并拿到 token；错误凭据明确提示；
静态页无路径穿越、带安全头；桌面端「已配置未登录」有登录入口且不阻塞本地功能。

**红线核对**：登录页只承载认证 UI（无业务逻辑）；静态服务零依赖、手写渲染；
安全头齐全（`no-referrer` + CSP）；**不改变「服务端不存代码」的红线（ADR-006）** ——
服务的是应用自带登录页文件，目录固定、只读，与用户代码 / prompt / 模型输出无关。

**明确不做**：❌ 前端框架 / 构建链（保持原生 HTML/JS） ❌ 自助注册 UI（内网口径由管理员开号）
❌ K8s 清单落盘（无论哪个选项都只出设计） ❌ `infra/docker-compose.yml` 落盘（Q-默认）。

---

## 3. 里程碑完成定义（DoD）

- [x] F 交付，专项测试 `test_host.py::test_command_palette_contract`
- [x] G 交付，专项测试 `tests/test_plan.py`（18 例）
- [x] 测试数**只增不减**：F 收口 agent-core **468 收集 / 467 passed / 1 skipped / 54 subtests**；
      服务端 **262 收集 / 260 passed / 2 skipped / 338 subtests**；
      `e2e_governance.py` **8 档全绿**；既有用例无变红
- [x] 反向验证已做（F）：临时把 `filterCommands` 改成恒匹配 → `test_command_palette_contract`
      **变红**（已还原）
- [x] 反向验证已做（G）：临时停用 `MAX_PLAN_STEPS` 条数校验 → `test_rejects_too_many_steps`
      **变红**（已还原）
- [x] H 交付，专项测试 `tests/test_tokens.py`（13 例，新增文件）+ `test_llm.py` 增量 6 例
- [x] 反向验证已做（H）：① 临时去掉 `_cache_rejected` 的「报错文本含字段名」条件 →
      `test_plain_400_is_not_mistaken_for_unsupported` **变红**（已还原）；
      ② 临时把 `_trim_context` 的 budget 写死为字符预算 → 暴露出「数值/单位错配」静默失配，
      遂把预算「数值+单位」收敛为单一来源 `_context_budget()`（设计改进，非仅测试修补）
- [x] I 交付，专项测试 `tests/test_transaction.py`（17 例，新增文件）
- [x] 反向验证已做（I）：临时在 `EditTransaction._add` 的 `self.plans.append(plan)` 之后插入
      `commit_plan(plan)`（退化为「逐文件即写」）→ ②③ 共 **4 例变红**
      （含 `test_unmatched_search_writes_nothing` 与两个回滚用例），已还原
- [x] J 交付，专项测试 `tests/test_watch.py`（18 例 + 4 subtests，新增文件）
- [x] 反向验证已做（J）：临时让 `update()` 的摘除循环退化为「只加不删」→
      `test_removed_file_disappears_from_results` / `test_removed_term_leaves_no_stale_bucket` /
      `test_update_matches_full_rebuild` 共 **3 例变红**（已还原）
- [x] K 交付，专项测试 `tests/test_testrunner.py`（20 例，新增文件）
- [x] 反向验证已做（K）：① 临时让 junit 解析忽略 `failure` 元素 → `test_testrunner` 中
      「失败定位到行」相关 **4 例变红**（已还原）；② 临时让 `openTestLocation` 不落行号 →
      `test_host.py::test_tests_panel_contract` 变红（已还原）
- [x] L 交付，专项测试 `tests/test_mcp.py`（12 例 + 2 subtests，新增文件）
- [x] 反向验证已做（L）：临时把 MCP 工具风险改 `Risk.SAFE` →
      `test_mcp.py::ToolDiscoveryTests::test_tools_are_registered_as_review`（ndjson/framed 两个
      subtest）**变红**（已还原）
- [x] M 交付，专项测试 `tests/test_terminal.py`（7 例 + 1 skip，新增文件）
- [x] 反向验证已做（M）：临时把 `TerminalManager.close()` 里的 `backend.close()` 注释掉（退化为
      「关会话但杀不了子进程」）→ `test_close_leaves_no_process` **变红**（`关闭后子进程仍残留`，已还原）
- [x] N 交付，专项测试 `tests/test_images.py`（24 例，新增文件）
- [x] 反向验证已做（N）：临时把 `session.submit` 的 `allow_images=cfg.vision_mode != "off"`
      改成恒 `True`（退化为「off 也处理图片」）→ `test_submit_off_degrades_to_notice` **变红**
      （`message` 变成 parts 列表而非纯字符串，已还原）
- [x] N 多模态输入（图片 / 截图 → 代码）—— 已交付
- [x] O 浏览器预览 / 网页自检 —— 已交付
- [x] O 交付，专项测试 `tests/test_preview.py`（22 例，新增文件）
- [x] 反向验证已做（O）：临时让 `/preview/` 跳过 PathGuard 与 `.vibe` 检查
      （`target = paths.root_resolved / raw`）→ `test_traversal_rejected` 与
      `test_vibe_dir_rejected` **变红**（越权文件与 `.vibe/` 私产被正常送出，已还原）
- [x] P 规则与记忆体系 —— 已交付
- [x] P 交付，专项测试 `tests/test_rules_layers.py`（8 例）+ `tests/test_memory.py`（16 例，新增文件）
- [x] 反向验证已做（P）：临时把 `REMEMBER_SPEC` 的 `risk` 由 `Risk.REVIEW` 改 `Risk.SAFE`
      → `test_memory.py::RememberToolTests::test_remember_requires_review` **变红**（已还原）
- [x] Q 多租户运营落地 —— 已交付（登录页 `api/static.py` + `login.html` + `login.js`；桌面端「去登录」入口；决策 **Q-默认**：compose 不落盘）
- [x] Q 交付，专项测试 `apps/server/tests/test_static.py`（10 例 + 5 subtests，新增文件）
- [x] 反向验证已做（Q）：临时移除 `httpd._send()` 的 `Referrer-Policy: no-referrer` 头
      → `test_static.py::StaticPageTests::test_login_page_is_html_with_referrer_policy` **变红**
      （`None != 'no-referrer'`，已还原）
- [x] `docs/ARCHITECTURE.md` 新增 ADR-015~020，并补记 §6「P1/P2 能力补齐实际落地情况」—— 随各阶段增量补记（**ADR-015 已随 L、ADR-016 已随 M、ADR-017 已随 I、ADR-018 已随 N、ADR-019 已随 O、ADR-020 已随 H 落地**；本轮 P 新增 **ADR-021**；**Q 无新 ADR，仅补记 §6「Q」行**）
- [x] 两份 README / `.env.example` 补新端点与新环境变量对应表 —— 随各阶段增量补记（**`VIBE_MCP*` 已随 L、`VIBE_TERM*` 已随 M、`VIBE_VISION*` 已随 N、`VIBE_RULES_EXTRA` / `VIBE_MEMORY*` 已随 P 落地**；**Q 新增 `/login` + `/assets/{name}` 端点（无新环境变量），已落 `apps/server/README.md`**）

---

## 4. 实测测试数（随阶段更新）

| 套件 | 开工基线 | F 收口实测 | G 收口实测 | H 收口实测 | I 收口实测 | J 收口实测 | K 收口实测 | L 收口实测 | M 收口实测 | N 收口实测 | O 收口实测 | P 收口实测 | Q 收口实测 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| agent-core | 468 收集 / 467 passed / 1 skipped | **468 收集 / 467 passed / 1 skipped / 54 subtests** | **486 收集 / 485 passed / 1 skipped / 54 subtests** | **505 收集 / 504 passed / 1 skipped / 54 subtests** | **522 收集 / 521 passed / 1 skipped / 54 subtests** | **540 收集 / 539 passed / 1 skipped / 58 subtests** | **561 收集 / 560 passed / 1 skipped / 58 subtests** | **573 收集 / 572 passed / 1 skipped / 60 subtests** | **581 收集 / 579 passed / 2 skipped / 60 subtests** | **605 收集 / 603 passed / 2 skipped / 60 subtests** | **627 收集 / 625 passed / 2 skipped / 60 subtests** | **651 收集 / 649 passed / 2 skipped / 60 subtests** | **653 收集 / 651 passed / 2 skipped / 64 subtests** |
| 服务端 | 262 收集 / 260 passed / 2 skipped / 338 subtests | **262 收集 / 260 passed / 2 skipped / 338 subtests** | **262 收集 / 260 passed / 2 skipped / 344 subtests** | **262 收集 / 260 passed / 2 skipped / 344 subtests** | **262 收集 / 260 passed / 2 skipped / 349 subtests** | **262 收集 / 260 passed / 2 skipped / 353 subtests** | **262 收集 / 260 passed / 2 skipped / 359 subtests** | **262 收集 / 260 passed / 2 skipped / 368 subtests** | **262 收集 / 260 passed / 2 skipped / 379 subtests** | **262 收集 / 260 passed / 2 skipped / 383 subtests** | **262 收集 / 260 passed / 2 skipped / 389 subtests** | **262 收集 / 260 passed / 2 skipped / 404 subtests** | **272 收集 / 270 passed / 2 skipped / 409 subtests** |
| e2e_governance | 8 档全绿 | **8 档全绿** | **8 档全绿** | **8 档全绿** | **8 档全绿（退出码 0、无残留进程）** | **8 档全绿（退出码 0、无残留进程）** | **8 档全绿（退出码 0、无残留进程）** | **8 档全绿（退出码 0、无残留进程）** | **8 档全绿（退出码 0、无残留进程）** | **8 档全绿（退出码 0、无残留进程）** | **8 档全绿（退出码 0、无残留进程）** | **8 档全绿（退出码 0、无残留进程）** | **8 档全绿（退出码 0、无残留进程）** |

新增专项用例：F 为 `test_host.py::test_command_palette_contract`（1 例，增量）；
G 为 `tests/test_plan.py`（18 例，新增文件）；
H 为 `tests/test_tokens.py`（13 例，新增文件）+ `tests/test_llm.py`（6 例，增量）；
I 为 `tests/test_transaction.py`（17 例，新增文件）；
J 为 `tests/test_watch.py`（18 例 + 4 subtests，新增文件）；
K 为 `tests/test_testrunner.py`（20 例，新增文件）+ `test_host.py::test_tests_panel_contract`（1 例，增量）；
L 为 `tests/test_mcp.py`（12 例 + 2 subtests，新增文件）；
M 为 `tests/test_terminal.py`（7 例 + 1 skip，新增文件；skip 为本机无 ConPTY 的回显用例）；
N 为 `tests/test_images.py`（24 例，新增文件）；
O 为 `tests/test_preview.py`（22 例，新增文件；含 `visual` 纯函数解码/差异 4 例）；
P 为 `tests/test_rules_layers.py`（8 例，新增文件）+ `tests/test_memory.py`（16 例，新增文件）；
Q 为 `apps/server/tests/test_static.py`（10 例 + 5 subtests，新增文件）+ `tests/test_governance_wiring.py` 增量 2 例
（`describe()` 暴露 `serverUrl`、界面「去登录」入口契约）；
另新增 `packages/agent-core/tests/conftest.py` 与 `apps/server/tests/conftest.py`（`VIBE_MEMORY=0` 基线隔离，
服务端 subtests 389 → 404 即 `test_chain_compat` 新增三个事件样本各 5 个 subtest 所致；Q 收口上升至 409 为静态页新增 subtests）。
`test_host.py` 的 `CORE_TOOLS` 补入 `edit_files` 与 `update_plan`（工具总数 10 → 12）。

---

## 5. 风险与缓解（M6 收口实测结论）

逐条风险与缓解见 `.trae/documents/P1-P2-FEATURES-PLAN.md` §18。本章汇总开工时点名的
**五处高风险面**的落地边界与实测结论，并给出结项前的全量回归记录。

### 5.1 五处高风险面（开工时点名）

| 高风险面 | 计划给的边界 | 落点 | 实测结论 |
|---|---|---|---|
| 多模态会外发（N） | 默认 `off`、显式开、图片只落 host 侧 `.vibe/images/`、审计只记元数据 | `context/images.py` / `config.vision_mode` / `session.upload_image` | `VIBE_VISION=off`（默认）时行为逐字节一致；`on` 时组 parts、`off` 时逐条降级 `run.notice`；`image.upload` 只记 `bytes`/`ext`/`digest` |
| 记忆是持久注入面（P） | 注入一律 `wrap_untrusted`、写入恒 `Risk.REVIEW`、关闭时不读不写 | `memory/store.py` + `tools/memory.py` + `agent._memory_section` | `remember` 恒 REVIEW（不可配置放行）；注入经 `wrap_untrusted`；`VIBE_MEMORY=0` 时不建库 / 不注入 / 不注册工具 |
| PTY 跨平台 / 进程泄漏（M） | 失败即降级 pipe、按**进程树**终止、会话上限 + 空闲回收 + `atexit` 兜底 | `terminal/{pty_unix,pty_windows,manager}.py` | 关会话**无残留子进程**；ConPTY 不可用时降级 pipe 并如实回显（本机即此情形）；会话上限拒绝；退出按进程树终止 |
| 事务回滚不彻底（I） | 两阶段：先整批校验、再逐个落盘；失败**逆序回滚**已写文件 | `edit/transaction.py` + `tools/multi_edit.py` | 阶段一失败**一个都不落盘**；阶段二失败已写文件恢复原内容、新建文件被删；`edit.transaction` 只记计数与 `rolled_back` |
| MCP 外部工具越权（L） | 恒 REVIEW、前缀隔离、argv 只来自受信配置、结果 `wrap_untrusted` | `mcp/{client,registry,manager}.py` | 恒 REVIEW（ADR-015，不可配置放行）；名字冲突**拒绝启动该 server**；崩溃降级不崩会话；`mcp.call` 只记 `args_digest`/`argc` |

### 5.2 结项前全量回归（2026-10-09）

| 验证项 | 命令 | 结果 |
|---|---|---|
| 服务端测试树 | `cd apps/server && python -m pytest -q` | **270 passed / 2 skipped / 409 subtests** |
| agent-core 测试树 | `cd packages/agent-core && python -m pytest -q` | **652 passed / 1 skipped / 64 subtests** |
| 治理面 E2E | `python scripts/e2e_governance.py` | **8 档全绿，退出码 0，无残留进程** |
| 桌面打包冒烟 | `python scripts/build_desktop.py --dist` + `python scripts/e2e_package.py` | **内嵌二进制 + 回退系统 Python 两条路径均通过，版本三处一致** |

- **收集数**：agent-core **653**、服务端 **272**，均**只增不减**（相较开工基线 468 / 262）。
- **skip 归属**：本机 skip 全部来自**环境条件探测**（可选依赖 tree-sitter / OS 符号链接权限 / ConPTY）。本轮 agent-core 恰 **1 例** skip —— `test_terminal.py`（本机 ConPTY 不可用的回显用例）；早期记录的「2 skipped」中另一例属同类环境探测，随本机环境满足转为执行，**收集总数不变、无用例变红**。
- **反向验证**：12 阶段逐条已做（清单见 §3 DoD）；Q 为「临时移除 `httpd._send()` 的 `Referrer-Policy: no-referrer` → 静态页头断言变红」。

### 5.3 结项结论

F–Q 十二条能力**全部交付**，各自带专项测试与反向验证；代码 / 测试 / E2E / 打包
四个层面均验证通过。核心链路零强制依赖红线保持（`dependencies` 保持 `[]`、前端零第三方库）；
Q（治理面）改动**不改变「服务端不存代码」的红线**（ADR-006）—— 静态页服务的是应用自带登录页文件，
目录固定、只读，与用户代码 / prompt / 模型输出无关。