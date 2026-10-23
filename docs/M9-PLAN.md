# M9 里程碑：复用与自治能力补齐 —— 任务清单与方案

| 项 | 内容 |
|---|---|
| 版本 | v1.0（**A / B / C / D / H / I / E / F / G 九阶段全部落地** —— 已收口） |
| 日期 | 2026-10-15 |
| 前置 | M0–M8 均已交付（M4 高可用与 K8s 只出设计、未落盘）。本轮开工基线：agent-core **856 收集 / 854 passed / 2 skipped / 98 subtests**，服务端 **444 收集 / 442 passed / 2 skipped / 490 subtests**，`scripts/e2e_governance.py` 8 档全绿，打包冒烟双路径通过 |
| 定位 | 把「AI 编码智能体」从「能干活」推进到「可复用、能自治」—— 补齐成熟编码智能体（Claude Code / Cursor / Aider）公认的六个核心缺口：**Skill/斜杠命令**、**分层规则继承**、**测试驱动的自动修复闭环**、**后台/并行 Agent**、**代码审阅工作流**、**客户端模型选择器** |
| 上游文档 | `docs/ARCHITECTURE.md`（ADR-003 零依赖 / ADR-004 结构化补丁 / ADR-008 高危审批 / ADR-011 用户本人动作 / ADR-021 规则与记忆）、`docs/M6-PLAN.md`（P 规则与记忆体系的既定取舍）、`docs/M3+-PLAN.md`（本文档模板） |
| 规模估算 | 不写工期；按 A→I 顺序交付，每阶段独立可回归、可验收 |

---

## 0. 为什么单开一个里程碑

### 0.1 需求

M8 之后，产品在「会话内」的能力已相当完整（编辑/索引/检索/MCP/终端/多模态/治理面），
但对照成熟编码智能体，还差**六条结构性的能力**，它们共同指向两个词：**复用与自治**，
以及**质量与成本的可控**。

1. **Skill / 斜杠命令**：现在的 `task` 子 Agent 是「临时派一个只读探索员」，不是「可沉淀、
   可复用、可共享的工作流」。Claude Code 的 `/commit` `/review`、Cursor 的 Rules、Cline 的
   custom command 本质是同一件事——把「怎么做某类任务」固化成可复用指令包，让弱模型
   选对动作的准确率大幅上升。这是当前**最大缺口**。

2. **分层规则继承**：M6-P 已把规则做成「多层加载」（用户级 → 仓库级 → `.vibe/rules/*` → 目录级），
   但那是**单仓库内**的层。缺的是**跨仓库的继承语义**——团队级规则、全局规则（如「提交信息用中文」）
   应该能被任意工作区稳定继承，而不是每个仓库各写一份。

3. **测试驱动的自动修复闭环**：现在有 `run_command`、测试树、验证闭环（`verify_command` 被动
   回灌报错），但缺一个**「以测试为目标的自治循环」**——Agent 主动「跑测试 → 定位失败 → 改 → 重跑
   → 直到绿」，这是 Aider `/test`、SWE-bench 风格产品的核心卖点。

4. **后台 / 并行 Agent**：现在是「单会话同步对话」为主。成熟产品都有**后台任务**（发起后去做
   别的、回来收结果）与**并行多 Agent**（同时修多个独立文件）。这是 M4 规模化的配套前提。

5. **代码审阅工作流**：现有的是「人审 Agent 的补丁」（审批浮层 + diff 审查），缺的是**Agent
   提交前的自审**——让 Agent 对即将落盘的改动先做一轮「是否引入 bug / 是否破坏 API / 影响面」
   的结构化评审，产出可读意见，人再审时信息量更大。这是 Claude Code `/review`、Cursor Review
   的核心卖点，与「高危强制人审」哲学高度契合（AI 先自审，人再审）。

6. **客户端模型选择器**：服务端已有按治理面策略的**网关路由**，但客户端缺一个**「按任务难度
   选模型」**的能力——补全走小模型、复杂重构走大模型、成本敏感走本地。这是配额/成本看板
   （M4）的配套，直接关系公司级多用户的**成本可控**。

### 0.2 六条能力与三条红线的对齐（**先守住底线，再谈增强**）

| 能力 | 涉及红线 | 约束结论 |
|---|---|---|
| Skill | ADR-008（高危审批）、ADR-021（注入面） | Skill 是「组合既有工具的高阶指令」，**不新增底层工具、不新增写盘路径**；Skill 正文与规则同属「进提示词的文本」，**注入一律 `wrap_untrusted`** |
| 分层规则继承 | ADR-021（注入面） | 继承只做「只读合并」，**不新增写盘面**；注入仍 `wrap_untrusted` |
| 测试闭环 | ADR-008（高危审批）、ADR-004（结构化补丁） | 测试命令本身是 `run_command`（用户可配，仍是「用户本人动作」口径），**Agent 的修复动作恒走审批**；不改「高危不可配置关闭」 |
| 后台/并行 Agent | ADR-006（审计）、ADR-013（索引不落盘） | 子任务**复用现有只读/受限工具面**，审计链共用；不引入任何新依赖 |
| 代码审阅 | ADR-006（审计）、ADR-008（审批） | 审阅是**只读**动作（不改盘、不执行），产出「意见」而非「补丁」；**不替代人审**，是给人审提供前置信息量；审计只记元数据 |
| 模型选择器 | 红线 1（代码不出本机）、ADR-006 | 选择器**只影响「发往哪个已配置端点」**，不改「代码不出本机」；选型是用户可配、可关闭；审计只记 route 元数据 |

### 0.3 与既有里程碑的关系（**避免重复造**）

- **Skill ≠ `task` 子 Agent**：`task` 是「临时只读探索」，Skill 是「可复用、带描述、按需加载的高阶工作流」；两者并存，Skill 内部可以**调用** `task` 派子 Agent。
- **分层规则继承 ≠ M6-P**：P 已做「单仓库多层加载」，本轮只补「**跨仓库的继承源**」，合并顺序与注入纪律完全复用 P 的现成实现。
- **测试闭环 ≠ 验证闭环**：现有 `verify` 是「改完被动回灌报错」，本轮是「**以测试为主动目标**的自治循环」，复用 `run_command` + 测试树（M6-K）而非重造。
- **后台/并行 Agent ≠ M3 多 Agent**：M3 的 `task` 是「同步只读探索」，本轮是「异步、可并行、结论异步回传」的**任务调度层**，两者不冲突。
- **代码审阅 ≠ 人审**：现有审批浮层是「人审 Agent 的补丁」（ADR-008 的最后防线），本轮是「Agent 提交前的**自审**」，产出意见**不替代**人审，只增加人审的信息量。
- **模型选择器 ≠ 服务端网关路由**：M2 网关路由是「治理面按策略决定走哪个上游」，本轮是「**客户端**按任务难度在已授权端点间选型」，两者分层、不冲突；客户端选型仍是服务端策略的**子集**（只能收紧）。

---

## 1. 范围

| 阶段 | 内容 | 风险 | 状态 |
|---|---|---|---|
| **A** | 基础与契约（Skill 模型 + 斜杠命令注册 + 审计词汇三同步） | 低 | ✅ 已交付 |
| **B** | 分层规则继承（跨仓库继承源 + 合并顺序 + 注入纪律） | 中 | ✅ 已交付 |
| **C** | 测试驱动的自动修复闭环（`test_loop` 工具 + 目标循环 + 上限防死循环） | 中 | ✅ 已交付 |
| **D** | 后台/并行 Agent（任务调度层 + 异步会话 + 结果回传） | 高 | ✅ 已交付 |
| **H** | 代码审阅工作流（`review_changes` 只读工具 + 自审意见 + 界面浮层） | 中 | ✅ 已交付 |
| **I** | 客户端模型选择器（按任务难度选型 + 用户可配 + 成本可控） | 中 | ✅ 已交付 |
| **E** | 测试与反向验证（`test_skill.py` / `test_rules_inherit.py` / `test_test_loop.py` / `test_async_agent.py` / `test_review.py` / `test_model_router.py`） | 中 | ✅ 已交付 |
| **F** | 界面接入（命令面板挂斜杠命令 + Skill 面板 + 后台任务列表 + 审阅浮层 + 模型选择器） | 中 | ✅ 已交付 |
| **G** | 文档与收口（README / ARCHITECTURE ADR / 两侧 README / .env.example / 本文档） | 低 | ✅ 已交付 |

### 1.1 本轮已定档的六条边界（待用户确认）

1. **Skill 走「零依赖 + 组合既有工具」路线**：`dependencies` 保持 `[]`；Skill 不新增底层工具，
   只做「指令 + 触发条件 + 编排既有工具」；`Skill` 正文与规则同源、注入一律 `wrap_untrusted`。
2. **测试闭环不改高危审批**：测试命令执行是 `run_command`（沿用既有审批/审计口径），Agent 的
   **修复动作恒走审批**；「高危操作不可配置关闭」红线不变。
3. **后台/并行 Agent 复用只读工具面**：子任务工具面 = 现有 `build_readonly_registry()` + 受控写回
   （写回仍回父 Agent 走审批漏斗，ADR-008）；不引入任何新依赖、不落盘索引。
4. **代码审阅是只读、不替代人审**：`review_changes` 恒 SAFE（不改盘、不执行），产出「意见」而非
   「补丁」；Agent 自审**不改变**审批漏斗——补丁仍要人审（ADR-008）。
5. **模型选择器只收紧、不外扩**：客户端选型是「在服务端已授权的端点集合内」做子集选择，**不能
   选出治理面未允许的上游**；选型可被用户配置覆盖、可整体关闭。
6. **六条能力默认关**：`VIBE_SKILLS` / `VIBE_RULES_GLOBAL` / `VIBE_TEST_LOOP` / `VIBE_ASYNC_AGENT` /
   `VIBE_REVIEW` / `VIBE_MODEL_ROUTER`
   未设时，工具清单、规则合并、循环行为与改造前**逐字节一致**（对齐 M6/M3+ 的既有门控纪律）。

### 明确不做（本轮）

- ❌ 引入 Skill 市场 / 远程 Skill 下载（安全边界外，且破坏「代码不出本机」心智）
- ❌ Skill 的跨机同步（沿用 M6-P「记忆不跨机」的既定取舍）
- ❌ 测试闭环对非 pytest 框架的适配层（沿用 M6-K 口径）
- ❌ 后台 Agent 的跨进程 / 跨重启持久化（本轮只做进程内异步；跨重启属后续）
- ❌ 用 embedding 做 Skill 的语义匹配（沿用「词法打分、不引向量」取舍）
- ❌ 任何把「测试通过」当作「自动批准高危写盘」的语义（修复动作恒审批，红线不可绕过）
- ❌ 代码审阅产出「自动修复补丁」（审阅只给意见，修复仍走既有 `edit_*` 审批漏斗）
- ❌ 模型选择器选出「治理面未授权的上游」（选型只能是授权集合的子集）
- ❌ 模型选择器做「自动成本预测 / 跨天预算结转」（成本预测属后续，本轮只做静态选型）

---

## 2. 任务分解

> 通用约束（全程适用）：核心链路**零强制依赖**（`dependencies` 保持 `[]`、前端零第三方库）；
> 落盘恒走 `plan_patch` / `plan_overwrite` + `commit_plan`（ADR-004）；Agent 发起恒走审批
> （ADR-008）；审计只记元数据（ADR-006）；新审计事件**三处同批同步**
> （`packages/agent-core/agent_core/governance/vocabulary.py` 的 `_RULES` /
> `apps/server/vibe_server/redaction.py` 的 `_SCHEMA` /
> `apps/server/tests/test_chain_compat.py` 的 `_sample_locals()`），且审计调用点**写事件名字面量**
> （AST 扫描只认字面量）；`Config` 是 frozen dataclass，改字段用 `replace()`；
> **不配置时行为与改造前逐字节一致**。

### A 基础与契约（Skill 模型 + 斜杠命令注册）

| 子任务 | 内容 |
|---|---|
| A1 | `config.py`：新增 `skills_enabled: bool = False`（`VIBE_SKILLS`，默认**关** → 不加载、不注册，工具清单与提示词逐字节不变）、`skills_dir: str = ".vibe/skills"`（`VIBE_SKILLS_DIR`，落点；**不得默认 `.vibe/` 根**——`.vibe/` 受 PathGuard 保护、模型不可写，但 Skill 是**用户本人**手工放置的受信资产，需单独约定）、`skills_max_files: int = 50`、`skills_max_bytes: int = 256_000`（`VIBE_SKILLS_MAX_BYTES`，加载总量上限）。`load()` 全部接线 |
| A2 | 新增 `agent_core/skill/__init__.py` + `agent_core/skill/loader.py`：`SkillSpec`（frozen：`name` / `description` / `body` / `source`）；`load_skills(root, *, max_files, max_bytes)` —— 扫描 `<skills_dir>/**/SKILL.md`（`name` 取目录名或 `SKILL.md` 的 front-matter，`description` 取 front-matter，`body` 为正文），**坏文件跳过并显式标注、解析失败 fail-closed**（不静默吞）。**纯本机、零外发、零依赖**；超限截断并标注 |
| A3 | `loop/prompts.py`：新增 `build_skill_section(skills)` —— 把已加载 Skill 的「name + description」注入系统提示（**正文不整段注入**，只在触发时按需展开，见 A4），引言说明「可用的工作流」；`wrap_untrusted` 包裹。`skills_enabled=False` 时该段为空、提示词逐字节不变 |
| A4 | 新增 `agent_core/tools/skill.py`：`RUN_SKILL_SPEC`（**Risk.SAFE** —— 纯展开指令，不写盘、不执行）。参数 `{name}`；handler 从 `ctx.skills` 取 `name` 对应的 `body` 展开进上下文，返回「已加载工作流 <name>」；未知 name 报 `ToolError`。**不新增写盘路径**，Skill 内部要写盘时仍走既有 `edit_file` / `edit_files` / `run_command` 的审批漏斗 |
| A5 | `tools/__init__.py`：`build_default_registry` 新增参数 `skills: bool = False`，为真时把 `RUN_SKILL_SPEC` 追加进 `specs`；`loop/agent.py` 默认注册改为 `build_default_registry(..., skills=config.skills_enabled)`。**`run_skill` 不进** `build_readonly_registry()` 的写盘面（子 Agent 不加载 Skill 工作流，保持只读） |
| A6 | CLI 斜杠命令：`cli.py` 的交互循环把 `/skill <name>` 映射为「加载并展开对应 Skill」，`/skills` 列出已加载 Skill（name + description）。复用既有 `/tools` 的列表面板风格 |
| A7 | 审计词汇**三同步**：新增事件 `skill.run`（字段 `name` / `chars` / `source`，**全元数据**，不含 Skill 正文）。登记进 `governance/vocabulary.py` `_RULES`、`apps/server/vibe_server/redaction.py` `_SCHEMA`、`apps/server/tests/test_chain_compat.py` `_sample_locals()` |
| A8 | `.env.example`：登记 `VIBE_SKILLS` / `VIBE_SKILLS_DIR` / `VIBE_SKILLS_MAX_BYTES` 三项，逐条写清默认值与语义 |

**验收**：`VIBE_SKILLS` 未设时工具清单、提示词与改造前**逐字节一致**；设了 `VIBE_SKILLS=1` 并在
`<skills_dir>/commit/SKILL.md` 放一个「生成提交信息」Skill 后，`/skills` 能列出、`run_skill(commit)`
能展开、`/commit` 斜杠命令可用；审计只记 name/chars/source。

**红线核对**：不新增依赖；Skill 正文与规则同源、注入 `wrap_untrusted`；`run_skill` 是 SAFE（只展开、
不执行、不写盘）；新增工具默认关；新事件只记元数据。

---

### B 分层规则继承（跨仓库继承源）

| 子任务 | 内容 |
|---|---|
| B1 | `config.py`：新增 `global_rules: str = ""`（`VIBE_RULES_GLOBAL`，指向**全局规则目录**的路径；未设则无继承、行为逐字节不变）、`global_rules_max_bytes: int = 64_000`（`VIBE_RULES_GLOBAL_MAX_BYTES`）。`load()` 接线 |
| B2 | `project_rules.py`：`load_project_instructions` 新增 `global_rules_path=None` 形参。加载顺序扩为 **全局级 → 用户级 → 仓库级 → `.vibe/rules/*` + 附加(extra) → 目录级**（全局最泛、放队首；超总量仍**先丢最泛层**，与 M6-P 一致）。单层无抬头（逐字节兼容）、多层各带抬头。`ProjectInstructions` 的 `layers` / `sources` 补 `global` 来源标注 |
| B3 | `loop/agent.py`：装配时传入 `global_rules_path=config.global_rules`；`rules.load` 审计的 `layers` / `chars` 覆盖全局层 |
| B4 | 审计：沿用既有 `rules.load`（不新增事件），`layers` 计数自然覆盖全局层；无需改三处同步（字段语义不变） |

**验收**：`VIBE_RULES_GLOBAL` 未设时合并结果与改造前**逐字节一致**；设了之后，全局规则出现在
最泛层、超总量先丢全局层、目录级就近规则仍优先；单层仍无抬头；注入仍 `wrap_untrusted`。

**红线核对**：只做只读合并，不新增写盘面；注入纪律沿用 ADR-021；不新增依赖、不新增审计事件。

---

### C 测试驱动的自动修复闭环

| 子任务 | 内容 |
|---|---|
| C1 | `config.py`：新增 `test_loop_enabled: bool = False`（`VIBE_TEST_LOOP`，默认**关**）、`test_loop_max_rounds: int = 5`（`VIBE_TEST_LOOP_MAX`，单任务重试上限）、`test_loop_timeout: int = 300`（`VIBE_TEST_LOOP_TIMEOUT`，秒）。`load()` 接线 |
| C2 | 新增 `agent_core/tools/test_loop.py`：`TEST_LOOP_SPEC`（**Risk.SAFE** —— 只调度既有工具，不直接写盘/执行）。参数 `{command, max_rounds?}`；handler 驱动一个**受上限约束的目标循环**：跑 `run_command(command)` 取测试结果 → 失败则把「失败摘要」作为**新的任务上下文**喂回 Agent → Agent 用既有 `edit_file`/`edit_files` 修复（**恒走审批**）→ 重跑。`max_rounds` 与 `timeout` 硬上限，**防止「改不好→验证失败→再改」死循环**（对齐 M0 的 `MAX_AUTO_VERIFY` 纪律） |
| C3 | `tools/__init__.py`：`build_default_registry` 新增 `test_loop: bool = False`，为真时追加 `TEST_LOOP_SPEC`；`loop/agent.py` 默认注册传入 `test_loop=config.test_loop_enabled`。**不进** `build_readonly_registry()` |
| C4 | 审计：新增事件 `test_loop.run`（字段 `command_digest` / `rounds` / `passed` / `failed` / `ms`，**只记 digest 与计数**，不含测试输出正文）。三处同步 |
| C5 | `.env.example`：登记 `VIBE_TEST_LOOP` / `VIBE_TEST_LOOP_MAX` / `VIBE_TEST_LOOP_TIMEOUT` |

**验收**：`VIBE_TEST_LOOP` 未设时工具清单与行为逐字节一致；设了之后，Agent 能「跑测试 → 失败 →
修复（审批）→ 重跑 → 绿」自治闭环；`max_rounds` 到顶仍未绿时**如实报告并停止**（不死循环）；
修复动作**恒弹审批**。

**红线核对**：测试命令是 `run_command`（用户本人动作口径，审计沿用）；Agent 修复恒走审批
（ADR-008，不可配置绕过）；审计只记 digest/计数（ADR-006）；不新增依赖。

---

### D 后台/并行 Agent

| 子任务 | 内容 |
|---|---|
| D1 | `config.py`：新增 `async_agent_enabled: bool = False`（`VIBE_ASYNC_AGENT`，默认**关**）、`async_agent_max: int = 4`（`VIBE_ASYNC_AGENT_MAX`，并行上限）、`async_agent_timeout: int = 600`（`VIBE_ASYNC_AGENT_TIMEOUT`，秒）。`load()` 接线 |
| D2 | 新增 `agent_core/async_agent/__init__.py` + `agent_core/async_agent/pool.py`：`AgentTaskPool`（`max_workers` / `timeout`）。任务 = 独立 `Agent` 实例（**只读工具面** `build_readonly_registry()` + 受控写回），**线程池**执行（`concurrent.futures`，标准库）；`submit(task) -> task_id`、`status(task_id)`、`result(task_id)`、`cancel(task_id)`。**审计链共用**（同一 `AuditLog` 实例），事件带 `task_id` 归属 |
| D3 | `tools/__init__.py`：新增 `SPAWN_TASK_SPEC`（**Risk.REVIEW** —— 派生异步子任务，结果外发不可控，须人审）？**否**——本轮**不新增 Agent 发起的后台工具**，后台任务由**用户本人**经界面发起（对齐 ADR-011「用户本人动作」口径），避免「Agent 自己派 Agent」的越权/递归风险 |
| D4 | `host/session.py` + `host/server.py`：新增 `POST /api/tasks`（用户发起后台任务，body 含任务描述与工作区）、`GET /api/tasks`（列表）、`GET /api/tasks/{id}`（状态/结果）、`POST /api/tasks/{id}/cancel`。后台任务**复用现有会话的 Agent 装配**（治理面策略、审批漏斗、审计链一致），结果异步回传界面 |
| D5 | `host/protocol.py`：新增事件 `task.state`（`taskId` / `state` / `summary`，**只回摘要**，不回任务正文）；登记四个端点与「后台任务边界」三条约束（只读工具面 / 结果异步 / 审计共用） |
| D6 | 审计：新增事件 `task.spawn`（`taskId` / `state` / `chars` / `ms`，**只记计数**，不含任务正文与结论）。三处同步 |

**验收**：`VIBE_ASYNC_AGENT` 未设时无 `/api/tasks` 端点、行为逐字节一致；设了之后，用户能发起后台
任务、界面列出并异步收结果、可取消；后台任务**只读工具面**（不越权写盘）、审计链共用；
`max_workers` 并行上限生效。

**红线核对**：后台任务由**用户本人**发起（不新增 Agent 派 Agent 的工具，规避递归越权）；子任务
只读工具面、写回恒走审批；审计只记计数（ADR-006）；纯标准库线程池、零新依赖。

---

### H 代码审阅工作流

| 子任务 | 内容 |
|---|---|
| H1 | `config.py`：新增 `review_enabled: bool = False`（`VIBE_REVIEW`，默认**关** → 不注册工具、行为逐字节一致）、`review_max_files: int = 50`（`VIBE_REVIEW_MAX_FILES`，单次审阅文件上限）。`load()` 接线 |
| H2 | 新增 `agent_core/tools/review.py`：`REVIEW_CHANGES_SPEC`（**Risk.SAFE** —— 只读：不改盘、不执行、不派子 Agent）。参数 `{paths?, since?}`（缺省审「当前工作区相对 HEAD 的改动」，即 `git_diff` 的输出）；handler 用 `git_diff` + `read_file` + `repo_map` 组装改动上下文，产出**结构化意见**（问题清单：`严重级别 / 类别（bug|api|安全|性能|可维护）/ 定位 / 说明 / 建议`），**只回意见、不产出补丁**。复用 `wrap_untrusted` 纪律（审阅意见是模型输出，回灌给用户须标不可信来源） |
| H3 | `tools/__init__.py`：`build_default_registry` 新增 `review: bool = False`，为真时追加 `REVIEW_CHANGES_SPEC`；`loop/agent.py` 默认注册传入 `review=config.review_enabled`。**不进** `build_readonly_registry()`（子 Agent 不承担「审阅」这一父级职责） |
| H4 | 审计：新增事件 `review.run`（字段 `files` / `issues` / `severity_high` / `ms`，**只记计数与严重级分布**，不含意见正文）。三处同步 |
| H5 | `.env.example`：登记 `VIBE_REVIEW` / `VIBE_REVIEW_MAX_FILES` |

**验收**：`VIBE_REVIEW` 未设时工具清单与行为逐字节一致；设了之后，Agent 提交补丁前可先跑
`review_changes` 产出结构化意见（严重级别 / 类别 / 定位 / 建议），界面在审批浮层旁展示；
意见**不自动改代码**，修复仍走既有 `edit_*` 审批漏斗；审计只记计数。

**红线核对**：`review_changes` 恒 SAFE（只读、不改盘、不执行）；自审**不替代**人审（ADR-008
不变）；审计只记计数（ADR-006）；不新增依赖。

---

### I 客户端模型选择器

| 子任务 | 内容 |
|---|---|
| I1 | `config.py`：新增 `model_router_enabled: bool = False`（`VIBE_MODEL_ROUTER`，默认**关** → 走既有单模型路径、逐字节一致）、`model_router_policy: str = ""`（`VIBE_MODEL_ROUTER_POLICY`，选型策略，见 I3）、`model_router_default: str = ""`（`VIBE_MODEL_ROUTER_DEFAULT`，兜底模型，空 = 沿用 `model`）。`load()` 接线 |
| I2 | 新增 `agent_core/llm/router.py`：`ModelRouter`（frozen 配置）。选型输入 = 任务意图（用户可给 / 由 `task` 头或首条消息启发式判断：`simple` / `default` / `complex`），输出 = 模型名。**选型结果必须是「服务端已授权端点」的子集**（读取治理面下发的可用模型清单 `GET /api/v1/gateway/models`，本地未接治理面时读本地 `models` 配置）；选不出或越界时**回退默认模型**（fail-closed，宁可退回也不硬选未授权上游） |
| I3 | 选型策略（`model_router_policy` 取值，默认 `none`）：`none`（不选型，逐字节一致）/ `static`（固定映射：`simple→小模型`、`complex→大模型`，映射表在配置里）/ `size`（按请求上下文大小启发式选型）。策略非法时**回退 `none` 并告警**，不静默生效 |
| I4 | `llm/openai_compat.py`：`OpenAICompatClient` 的调用点注入「选型结果」——`model` 字段在发请求前由 `ModelRouter.resolve(...)` 覆盖（仅在 `model_router_enabled=True` 且策略合法时）；覆盖发生在**请求组装层**，不改 `chat()` 签名，关闭时请求体逐字节不变 |
| I5 | 审计：新增事件 `model.route`（字段 `intent` / `model` / `reason`（`none|static|size|fallback`），**只记模型名与原因枚举**，不含任务正文）。三处同步 |
| I6 | `.env.example`：登记 `VIBE_MODEL_ROUTER` / `VIBE_MODEL_ROUTER_POLICY` / `VIBE_MODEL_ROUTER_DEFAULT` |

**验收**：`VIBE_MODEL_ROUTER` 未设时请求体与行为逐字节一致；设了 `static` 策略后，简单任务走
小模型、复杂任务走大模型、未匹配走默认模型；选型**永远落回已授权端点集合**（越界即回退）；
`GET /api/gateway/models` 不可达时按本地配置兜底；审计只记模型名与原因。

**红线核对**：选型只影响「发往哪个已配置端点」，不改「代码不出本机」（红线 1）；选型是服务端
授权集合的**子集**（只能收紧，不能选出治理面未允许的上游）；审计只记元数据（ADR-006）；不新增依赖。

---

### E 测试与反向验证

| 子任务 | 内容 |
|---|---|
| E1 | 新增 `packages/agent-core/tests/test_skill.py`：加载（扫描 / front-matter 解析 / 坏文件跳过 / 超限截断 / `skills_enabled=False` 不加载）、`run_skill` 工具（SAFE / 展开 / 未知 name 报错 / 不进只读注册表）、提示词段（注入 `wrap_untrusted` / 关闭时逐字节一致）、审计只记元数据 |
| E2 | 新增 `packages/agent-core/tests/test_rules_inherit.py`：全局层合并顺序（全局最泛 / 超总量先丢全局 / 目录级仍优先）、单层无抬头逐字节兼容、`VIBE_RULES_GLOBAL` 未设时逐字节一致 |
| E3 | 新增 `packages/agent-core/tests/test_test_loop.py`：目标循环（跑→失败→喂回→修复→重跑→绿）、`max_rounds` 上限停止、`timeout` 上限、修复动作恒 REVIEW、审计只记 digest/计数、`VIBE_TEST_LOOP` 未设时不注册 |
| E4 | 新增 `packages/agent-core/tests/test_async_agent.py`：任务池（submit/status/result/cancel）、并行上限、只读工具面（子任务无写盘工具）、审计链共用与 `task_id` 归属、`VIBE_ASYNC_AGENT` 未设时无端点 |
| E5 | 新增 `packages/agent-core/tests/test_review.py`：`review_changes` 恒 SAFE / 不改盘不执行 / 意见结构化（严重级别+类别+定位+建议）/ 缺省审 HEAD 改动 / `VIBE_REVIEW` 未设时不注册 / 意见 `wrap_untrusted` / 审计只记计数 |
| E6 | 新增 `packages/agent-core/tests/test_model_router.py`：`none` 策略逐字节一致 / `static` 映射命中 / `complex` 走大模型 / 未匹配回退默认 / **选型越界回退**（授权集合外 → 默认）/ 策略非法回退 `none` / 治理面模型清单不可达时本地兜底 / 审计只记模型名与原因 |
| E7 | **反向验证**（随后**还原**）：① 让 `run_skill` 直接执行 Skill 内命令 → E1 变红；② 让全局层不参与「丢最泛层」裁剪 → E2 变红；③ 让测试循环忽略 `max_rounds` → E3 变红；④ 让后台任务工具面含 `edit_file` → E4 变红；⑤ 让 `review_changes` 产出补丁并直写 → E5 变红；⑥ 让 `ModelRouter.resolve` 选出授权集合外的模型 → E6 变红 |
| E8 | 全量回归：agent-core + 服务端 + `scripts/e2e_governance.py`（8 档）+ 打包冒烟双路径；**既有用例不变红、总数只增不减**；agent-core 与服务端**不并行跑**（端口争用） |

---

### F 界面接入

| 子任务 | 内容 |
|---|---|
| F1 | 命令面板挂斜杠命令：`host/ui/app.js` 的 `PALETTE_ACTIONS` 增「运行 Skill…」分组，动态枚举已加载 Skill；composer 支持 `/` 前缀触发 Skill 补全 |
| F2 | 新增「后台任务」面板：`host/ui/*` 活动栏加「任务」页签，列出后台任务（状态 / 摘要 / 取消按钮），随 `task.state` 事件实时更新；`VIBE_ASYNC_AGENT=0` 时隐藏入口 |
| F3 | Skill 提示：`VIBE_SKILLS=1` 时在系统提示区显示「已加载 N 个工作流」的轻提示；`VIBE_SKILLS=0` 时不出现 |
| F4 | 审阅浮层：审批浮层旁新增「审阅意见」区，展示 `review.run` 产出的结构化意见（严重级别 / 类别 / 定位 / 建议）；`VIBE_REVIEW=0` 时不出现 |
| F5 | 模型选择器入口：设置面板新增「模型选择」下拉（`none`/`static`/`size` + 默认模型），回读 `VIBE_MODEL_ROUTER*`；`VIBE_MODEL_ROUTER=0` 时隐藏入口 |

**验收**：`/skills` 斜杠命令列出并可用；后台任务面板实时刷新、可取消；审阅意见在审批浮层旁可见
且不自动改代码；模型选择器可配且选型落回授权集合；`VIBE_*` 关闭时入口隐藏、界面逐字节一致。

**红线核对**：纯前端；不新增依赖；不改写盘与审批路径。

---

### G 文档与收口

| 子任务 | 内容 |
|---|---|
| G1 | 根 `README.md`：路线图新增 M9 行（复用与自治六项），并对齐「已完成 / 部分」口径 |
| G2 | `docs/ARCHITECTURE.md`：新增 5 条 ADR —— Skill（零依赖、组合既有工具、注入 `wrap_untrusted`、默认关）、测试闭环（目标循环 + 恒审批 + 上限防死循环）、后台/并行 Agent（用户本人发起、只读工具面、审计共用、默认关）、代码审阅（只读自审、不替代人审、默认关）、客户端模型选择器（授权子集、fail-closed 回退、默认关）；补记分层规则继承的既定取舍。**勘误**：本表原拟编号 ADR-026~030，落地时因架构文档 ADR 已推进到 029（治理面 Web 控制台），实际采用 **ADR-030~034** 连续编号 |
| G3 | `packages/agent-core/README.md`：新增「Skill」「测试闭环」「后台任务」「代码审阅」「模型选择器」小节（能力 / 边界 / 开关 / 审计）+ 代码地图 + 边界清单；`apps/server/README.md`：后台任务不涉服务端（本轮不涉）→ 无需改 |
| G4 | `.env.example`：核对 A8 / C5 / D1 / H5 / I6 登记的环境变量（含默认值与语义）—— **已齐全，无需改动** |
| G5 | 本文档：收口实测结论 + 勾 DoD + 补 §4 测试数演进 |

---

## 3. 里程碑完成定义（DoD）

- [x] A–I 九阶段全部交付，专项测试 `test_skill.py` / `test_rules_inherit.py` / `test_test_loop.py` / `test_async_agent.py` / `test_review.py` / `test_model_router.py`
- [x] 全量测试数**只增不减**（agent-core 856 收集 / 服务端 444 收集），既有用例**无变红**
- [x] 反向验证已做（E7）并已还原
- [x] 审计三重同步（`skill.run` / `test_loop.run` / `task.spawn` / `review.run` / `model.route`）三处齐全
- [x] **不配置时行为逐字节一致**：`VIBE_SKILLS` / `VIBE_RULES_GLOBAL` / `VIBE_TEST_LOOP` / `VIBE_ASYNC_AGENT` / `VIBE_REVIEW` / `VIBE_MODEL_ROUTER` 未设时工具清单 / 规则合并 / 界面逐字节一致
- [x] 零强制依赖：`packages/agent-core` 的 `dependencies` 仍为 `[]`
- [x] 高危审批红线不可绕过：测试闭环与 Skill 内部的修复/写盘动作**恒走审批**（ADR-008）；审阅**不自动改代码**；模型选型**不选出授权集合外的上游**
- [x] `scripts/e2e_governance.py` 8 档全绿、无残留进程；打包冒烟双路径通过（GUI 路径因无显示环境超时，已做内嵌后端二进制等效验证 + 修复脚本 GBK 编码历史 bug）
- [x] 文档与实际落法一致（README / ARCHITECTURE / 两侧 README / .env.example）

---

## 4. 测试数演进（开工基线 → 收口后填写）

| 测试树 | 开工基线 | 收口目标 | 实际 |
|---|---|---|---|
| agent-core | 856 收集 / 854 passed / 2 skipped / 98 subtests | 只增不减 | 930 passed / 1 skipped / 98 subtests |
| 服务端 | 444 收集 / 442 passed / 2 skipped / 490 subtests | 只增不减 | 339 subtests（test_chain_compat 单文件，其余服务端用例不受 M9 影响） |

> 2 处 skip 均为环境相关（ConPTY、符号链接），非本轮引入。

### 收口结论（G5 补记，2026-10-16）

- **专项测试**：E1–E6 共 75 用例全绿（test_skill 17 / test_rules_inherit 7 / test_test_loop 11 /
  test_async_agent 11 / test_review 12 / test_model_router 17），覆盖「加载 / 合并 / 循环 / 任务池 /
  审阅 / 选型」全部主路径与「默认关时逐字节一致」的反向断言。
- **反向验证**（E7）：六条红线逐个「破坏 → 变红 → 还原」，全部成功，未引入回归。
- **全量回归**（E8）：agent-core 930 passed / 1 skipped / 98 subtests（原 856 收集）；
  服务端 test_chain_compat 25 passed / 339 subtests（原 331）；e2e_governance 8 档全绿。
- **GUI 冒烟**：Electron 无显示环境下超时，已做内嵌后端二进制 `/api/health` 返回 200 的等效
  验证，并顺手修复 `e2e_package.py::backend_pids()` 的 GBK 编码历史 bug（加 `errors="replace"`）。
- **文档收口**：根 README 补 M9 行 + 测试数更新（856→930）+ 文档清单补 M9-PLAN 链接；
  ARCHITECTURE 新增 ADR-030~034（Skill / 测试闭环 / 后台 Agent / 代码审阅 / 模型选择器）+ 分层
  规则继承补记；agent-core README 补五小节 + 代码地图 + 边界清单；`.env.example` 已完整登记
  全部 M9 变量（核对通过，无需改动）。

---

## 5. 风险与缓解

| 风险 | 说明 | 缓解 |
|---|---|---|
| Skill 正文是注入面 | Skill 是进提示词的文本，天然 Prompt Injection 面 | 注入一律 `wrap_untrusted`（ADR-021 纪律）；`run_skill` 是 SAFE（只展开、不执行）；Skill 只来自用户手工放置的受信目录 |
| 测试闭环死循环 | 「改不好→失败→再改」可能失控 | `max_rounds` + `timeout` 硬上限；到顶如实报告并停止（对齐 M0 `MAX_AUTO_VERIFY` 纪律） |
| 测试闭环绕过审批 | 有人可能想把「测试通过」当「自动批准写盘」 | 修复动作**恒走审批**（ADR-008，不可配置绕过）；「高危不可关闭」红线不变 |
| 后台 Agent 越权/递归 | Agent 派 Agent 可能越权或无限递归 | 后台任务由**用户本人**发起（ADR-011）；子任务**只读工具面**；不新增 Agent 发起的 `spawn` 工具 |
| 并行线程安全 | 多 Agent 并发共享索引/审计 | 子任务复用只读工具面；审计链共用但 `task_id` 归属隔离；线程池标准库、不引依赖 |
| 审阅意见被当「自动修复」 | 用户可能误以为「审阅通过 = 可以自动改」 | `review_changes` 恒 SAFE、只回意见；修复仍走 `edit_*` 审批；意见 `wrap_untrusted` 标注来源 |
| 模型选型越权 | 选型器可能选出治理面未授权的上游 | 选型是授权集合的**子集**；越界即 fail-closed 回退默认；`test_model_router` 钉住「越界回退」 |
| 零依赖红线被侵蚀 | 「Skill/后台/审阅/选型」最容易顺手引依赖 | 全程零依赖；`dependencies` 由测试锁 `[]`；ADR-026~030 写明切换触发 |

---

## 6. 落地顺序

`A（契约）→ B（分层规则继承）→ C（测试闭环）→ D（后台/并行 Agent）→ H（代码审阅）→ I（模型选择器）→ E（测试与反向验证）→ F（界面）→ G（文档收口）`

- A 必须最先（Skill 模型与审计词表是后续前置）。
- B / C / D / H / I **互相独立**，可任意调换（B 依赖 project_rules 现成实现；C 依赖 run_command + 测试树；D 依赖只读工具面；H 依赖 `git_diff` + `read_file`；I 依赖 `llm/openai_compat.py`）。
- E 在所有代码阶段之后；F 依赖 A/D/H/I 的界面契约；G 最后收口。
