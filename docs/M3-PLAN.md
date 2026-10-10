# M3 里程碑：多 Agent 编排（主从只读子 Agent）—— 任务清单与方案

| 项 | 内容 |
|---|---|
| 版本 | v1.0（**已交付** —— A–F 六阶段全部完成并回归验证） |
| 日期 | 2026-10-09 |
| 前置 | M0 / M1 / M1.5 / M2 / M4 / M5 / M6 均已交付。本轮开工基线：agent-core **653 收集 / 652 passed / 1 skipped / 64 subtests**，服务端 **272 收集 / 270 passed / 2 skipped / 409 subtests**，`e2e_governance.py` **8 档全绿**，打包冒烟双路径通过 |
| 定位 | 让父 Agent 能把「**只读的探索 / 分析 / 检索**」委派给子 Agent，拿到结构化摘要后继续决策；子 Agent **不写盘、不进审批、不递归** |
| 上游文档 | `docs/ARCHITECTURE.md`（ADR-003 的切换触发补记、本轮新增 **ADR-022**）、`docs/M6-PLAN.md`（上一里程碑 + 本文档模板）、`packages/agent-core/README.md` |
| 规模估算 | 不写工期；按 A→F 顺序交付，每阶段独立可回归、可验收 |

---

## 0. 为什么单开一个里程碑

### 0.1 需求

对标 Claude Code / Qoder 一类工具，「一个 Agent 从头干到尾」在两类场景下会失焦：

1. **探索型子任务**：父 Agent 为了改一个函数，需要先扫一遍仓库（找调用点、看依赖、
   读若干文件）。这些内容大多**用完即弃**，却全部堆进父 Agent 的上下文，把真正
   该改的那几行挤到一边，还推高裁剪概率。
2. **并行/受限视角**：某些子任务只该看只读信息（「这个函数还有哪些调用方」），
   让主 Agent 亲自读反而容易顺手去改。

多 Agent 编排把这类子任务**下沉**：父 Agent 通过一个工具派生一个**只读子 Agent**，
子 Agent 在自己的小上下文里探索，只把**压成要点的结论**交回父 Agent。父 Agent 的
上下文因此变干净，决策更聚焦。

### 0.2 「M3」槽位的历史漂移（**必须先澄清，否则文档会自相矛盾**）

仓库里 "M3" 有三处互不一致的用法：

| 出处 | 对 M3 的定义 |
|---|---|
| 根 `README.md` 路线图 | 向量检索、Repo Wiki、配额的动态调整（标「未开始」） |
| `docs/ARCHITECTURE.md` 演进表 | Repo 向量检索、云端路由、配额体系、Repo Wiki |
| **大量代码注释** | 把**已在 M2 完成**的治理面接线标为 `(M3)`（如 `host/session.py`、`cli.py`） |
| `docs/M5-PLAN.md` §1 | 「多 Agent / Repo Wiki（**仍归 M3**）」 |

**本里程碑的裁定**：M3 槽位**改指多 Agent 编排**（用户方向）。原 M3 的
「向量检索增强 / Repo Wiki / 配额动态调整」三项**移入后续槽位**（记为 `M3+`，本轮不做）。
代码注释里 `(M3)=治理面接线` 属历史标注，**不改动**（改注释噪音大、收益低），
但本文档与 README / ARCHITECTURE 明确以「多 Agent」为 M3 的**当前**含义。

---

## 1. 范围

| 阶段 | 内容 | 风险 | 状态 |
|---|---|---|---|
| **A** | 基础与契约（config 开关 + 只读工具注册表 + 审计词汇三同步） | 低 | ✅ 已完成 |
| **B** | 子 Agent 引擎（`loop/subagent.py` + `Agent` 支持注入审计实例） | 中 | ✅ 已完成 |
| **C** | `task` 工具（`tools/task.py` + `ToolContext.subagent_sink` + 注册） | 中 | ✅ 已完成 |
| **D** | 事件与 UI（`run.subagent` 事件 + 会话接线 + 前端内嵌子 Agent 卡片） | 中 | ✅ 已完成 |
| **E** | 测试与反向验证（`tests/test_subagent.py`） | 中 | ✅ 已完成 |
| **F** | 文档与收口（README 路线图 / ARCHITECTURE ADR-022 / agent-core README / .env.example） | 低 | ✅ 已完成 |

### 本轮已定档的四条边界（用户已确认）

1. **范围**：只做多 Agent 编排；向量检索增强 / Repo Wiki / 配额动态调整**不在本轮**。
2. **编排形态**：**主从子 Agent**（单一 orchestrator，无 DAG、无多角色协作）。
3. **权限**：子 Agent **只读**（探索 / 分析 / 检索），任何写盘一律回到父 Agent 走既有审批漏斗。
4. **UI**：子 Agent 作为**内嵌工具卡片**（`task` 卡片可展开看子 Agent 内部步骤 + 最终摘要）。

### 明确不做（本轮）

- ❌ 子 Agent 写盘 / 子 Agent 独立审批浮层（子 Agent 恒只读）
- ❌ 递归派生（子 Agent 的注册表**不含** `task`，结构上不可能再往下派生）
- ❌ 并行 fan-out（一次只派生一个子 Agent，串行委派）
- ❌ 多角色协作（planner / coder / reviewer）、可配置 DAG 工作流（ADR-003 的切换触发仍不满足）
- ❌ 引入 LangGraph / AutoGen（沿用 ADR-003：手写编排）
- ❌ 子 Agent 消息落会话库 / 子 Agent 计划进父计划视图
- ❌ 子 Agent 走网关以外的独立模型（沿用父 Agent 的 client）

---

## 2. 任务分解

> 通用约束（全程适用）：核心链路**零强制依赖**（`dependencies` 保持 `[]`、前端零第三方库）；
> 落盘恒走 `plan_patch` / `plan_overwrite` + `commit_plan`（ADR-004）；Agent 发起恒走审批
> （ADR-008）；审计只记元数据（ADR-006）；新审计事件**三处同批同步**
> （`governance/vocabulary.py` `_RULES` / `apps/server/vibe_server/redaction.py` `_SCHEMA` /
> `apps/server/tests/test_chain_compat.py` `_sample_locals()`），且审计调用点**写事件名字面量**
> （AST 扫描只认字面量）；`Config` 是 frozen dataclass，改字段用 `replace()`；
> **不设 `VIBE_SUBAGENT` 时行为与改造前逐字节一致**。

### A 基础与契约

| 子任务 | 内容 |
|---|---|
| A1 | `config.py`：新增 `subagent_enabled: bool = False`（`VIBE_SUBAGENT`，默认**关** —— 与 `mcp_enabled` / `vision_mode` 同范式，不改工具清单即不改行为）、`subagent_max_iterations: int = 8`（`VIBE_SUBAGENT_MAX_ITERATIONS`，子 Agent 独立的迭代上限）。`load()` 接线 |
| A2 | `tools/__init__.py`：新增 `build_readonly_registry(*, lsp: bool \| None = None) -> ToolRegistry` —— **白名单**只注册只读 SAFE 工具：`repo_map` / `list_dir` / `read_file` / `grep` / `find_files` / `semantic_search` / `git_diff`（LSP 可用时附 `lsp_diagnostics`）。**显式排除**：`edit_file` / `write_file` / `edit_files` / `update_plan` / `run_command` / `remember` / `task` / `mcp__*` |
| A3 | 审计词汇**三同步**：新增事件 `subagent.run`（字段 `ok` / `iterations` / `tool_calls` / `answer_chars` / `task_digest`，**全部元数据**）登记进 `governance/vocabulary.py` `_RULES`、`apps/server/vibe_server/redaction.py` `_SCHEMA`、`apps/server/tests/test_chain_compat.py` `_sample_locals()` |

**验收**：`subagent_enabled=False` 时工具清单与行为与改造前**逐字节一致**；
`build_readonly_registry()` 的输出**不含任何写盘 / 执行 / 递归工具**。

**红线核对**：不新增依赖；只读表不含写盘工具；审计只记元数据（`task_digest` 而非正文）。

---

### B 子 Agent 引擎

| 子任务 | 内容 |
|---|---|
| B1 | `loop/agent.py`：`Agent.__init__` 新增可选参数 `audit: AuditLog \| None = None`，`self.audit = audit or AuditLog(...)`。**理由**：`AuditLog._last_hash` 是**内存态**（见 `audit.py` 的「写入必须串行」说明）—— 两个实例各持一份 prev，交错写入会让 `verify_chain` 判定整条链被篡改。父/子 Agent **必须共用同一实例** |
| B2 | 新增 `loop/subagent.py`：`SubAgentResult`（dataclass：`ok` / `answer` / `iterations` / `tool_calls` / `error`）+ `run_subagent(parent, task_text, *, call_id, on_event) -> SubAgentResult`。构造只读子 Agent：`replace(parent.config, session_id=<同父>, max_iterations=config.subagent_max_iterations, allow_write=False, allow_shell=False, verify_command="", stream_enabled=False, memory_enabled=False)`；`registry=build_readonly_registry(...)`；`client=parent.client`；`audit=parent.audit`（**共用**）；`approver=<恒 deny>`（只读不触发）；`should_stop=parent.should_stop`（中断**透传**）；`hooks=_SubAgentHooks(on_event, call_id)`；`initial_messages=None` |
| B3 | `loop/subagent.py`：子 Agent 专用系统提示（探索/分析取向 —— 明确「你只读、不写盘；把发现压成要点返回，不要复述全文」）；`_SubAgentHooks(AgentHooks)` 只转发 `on_iteration` / `on_tool_call` / `on_tool_result` / `on_notice` 为 `on_event`；**不转发** `on_message`（不落会话库）/ `on_context` / `on_plan` / `on_assistant_delta`（不污染父视图）。任何异常（`LLMError` 等）捕获为 `SubAgentResult(ok=False, error=...)`，**绝不抛出** |
| B4 | `loop/agent.py`：`AgentHooks` 新增 `on_subagent(event: dict[str, Any]) -> None` 空实现（子 Agent 事件的父侧出口，见 D1/D2） |

**验收**：用一个父 Agent + `FakeClient` 直接跑 `run_subagent`，返回结构化摘要；
子注册表**看不到**任何写工具；`task` 不在子注册表（**无递归**）；子 Agent 异常降级为 `ok=False`。

**红线核对**：子 Agent 复用父的 `PathGuard` / `CommandGuard` / **审计实例**；
`allow_write=False` 作为纵深防御；子 Agent 不新增写盘路径。

---

### C `task` 工具

| 子任务 | 内容 |
|---|---|
| C1 | `tools/base.py`：`ToolContext` 新增 `subagent_sink: Callable[[str], ToolResult] \| None = None`（与既有 `plan_sink` 同范式 —— 工具不依赖 `Agent` 类型，只调一个回调） |
| C2 | 新增 `tools/task.py`：`TASK_SPEC`（**`Risk.SAFE`** —— 只读委派，不写盘、不下命令）。参数 `{task: string, description?: string}`；校验：`task` 非空且 ≤ `MAX_TASK_CHARS`（4000）、`description` ≤ 200；内容**整批拒绝**（非法即 `ToolError`）。handler：`ctx.subagent_sink is None` → 明确失败（「子 Agent 未启用」）；否则调用并以 `wrap_untrusted("subagent", answer)` 包裹返回（子 Agent 读到的是工作区/外部文本，**天然是注入面**） |
| C3 | `tools/__init__.py`：`build_default_registry` 新增参数 `subagent: bool = False`；为真时把 `TASK_SPEC` 追加进 `specs`。`loop/agent.py` 的默认注册调用改为 `build_default_registry(memory=..., subagent=config.subagent_enabled)` |
| C4 | `loop/agent.py`：`_execute_call` 在 `registry.execute` **之前**把 `self.ctx.subagent_sink` 设为绑定当前 `call` 的闭包（`lambda task: self._delegate(call, task)`），执行后复原（工具批在循环里是**串行**执行，单次赋值即安全）；新增 `Agent._delegate(call, task)` —— 发 `started` 子事件 → 调 `run_subagent` → 记 `subagent.run` 审计 → 发 `finished`/`failed` 子事件 → 返回 `ToolResult` |

**验收**：启用后 Agent 的工具清单里出现 `task` 并可委派；**未启用时工具清单逐字节一致**；
子 Agent 的答案被 `wrap_untrusted` 包裹；委派失败返回 `ok=False` 且父循环**继续**。

**红线核对**：`task` 是 SAFE + 只读；结果恒包裹；审计只记 digest / 计数。

---

### D 事件与 UI

| 子任务 | 内容 |
|---|---|
| D1 | `host/protocol.py`：新增 `RUN_SUBAGENT = "run.subagent"`，payload `{runId, callId, phase, ...}`，`phase ∈ started｜iteration｜tool｜finished｜failed`；同步**事件表 + HTTP 表**，并补「多 Agent 边界」三条约束（子 Agent 只读 / 无递归 / 事件只带元数据） |
| D2 | `host/session.py`：`_SessionHooks.on_subagent(event)` → `bus.publish(event(RUN_SUBAGENT, runId=..., **event))`（`callId` 关联父侧 `task` 工具卡片）。`Agent._delegate` 经 `hooks.on_subagent` 送出子事件 |
| D3 | `host/ui/index.html` + `styles.css` + `app.js`：`task` 工具卡片内嵌**可展开**的「子 Agent 步骤」子列表（复用既有工具卡片样式，字号对齐既有浮层：列表 13.5px / 提示 12px）；`applySubAgent(ev)` 按 `callId` 归位到对应卡片；`finished` 后显示「N 轮 · M 次只读工具 · 摘要 K 字」（**不展示子 Agent 全文**） |

**验收**：委派时出现 `task` 卡片，展开可见子 Agent 的迭代与只读工具调用；**不启用时界面逐字节一致**。

**红线核对**：事件**不含子 Agent 正文**（只 digest / 长度 / 计数）；前端零第三方库。

---

### E 测试与反向验证

| 子任务 | 内容 |
|---|---|
| E1 | 新增 `packages/agent-core/tests/test_subagent.py`：<br>· **工具层**：`task` 仅在 `subagent=True` 时注册；参数校验（空 `task` / 超长）；无 sink 明确失败；结果被 `wrap_untrusted` 包裹<br>· **注册表层**：`build_readonly_registry()` 不含写/执行/`task`/`mcp__*`，含全部只读工具<br>· **引擎层**：`run_subagent` 用 `FakeClient` 返回摘要；无递归；异常降级 `ok=False`；`should_stop` 透传；`subagent_max_iterations` 生效<br>· **审计层**：`subagent.run` 只含元数据（无正文）；父/子**共用同一** `AuditLog` 实例（`verify_chain` 通过）<br>· **会话/事件层**：`run.subagent` 各 phase 齐全且 `callId` 关联；`subagent_enabled=False` 时**无 `task` 工具、无 `run.subagent` 事件** |
| E2 | **反向验证**：临时在只读注册表里放回 `write_file`（或让 `_delegate` 绕过 `wrap_untrusted`）→ 对应用例**变红**（随后**还原**） |

**验收**：`test_subagent.py` 全绿；反向验证变红并已还原；agent-core 全量回归**既有用例不变红、总数只增**。

---

### F 文档与收口

| 子任务 | 内容 |
|---|---|
| F1 | 根 `README.md`：路线图 M3 行改为「多 Agent 编排（主从只读子 Agent）」；原「向量检索 / Repo Wiki / 配额动态调整」移入 `M3+`（未开始）备注 |
| F2 | `docs/ARCHITECTURE.md`：新增 **ADR-022**（多 Agent 编排：主从只读子 Agent、无 DAG、共用审批与审计链）；补记 ADR-003 的「切换触发」（本轮以轻量编排器满足需求，**仍未**引入 LangGraph） |
| F3 | `packages/agent-core/README.md`：新增多 Agent 小节（能力 / 边界 / 开关 / 事件 / 只读工具面） |
| F4 | `.env.example`：登记 `VIBE_SUBAGENT` / `VIBE_SUBAGENT_MAX_ITERATIONS` |
| F5 | 本文档：收口实测结论 + 勾 DoD + 补 §4 测试数演进 |

---

## 3. 里程碑完成定义（DoD）

- [x] A–F 六阶段全部交付，专项测试 `tests/test_subagent.py`（19 用例）
- [x] 全量测试数**只增不减**：agent-core 653 → 672 收集；服务端 272 → 272（既有用例**无变红**，服务端仅词表用例新增子测试）
- [x] 反向验证已做（E2）并已还原
- [x] 审计三重同步（`subagent.run`）三处齐全，`test_every_rule_source_is_recorded_somewhere` 通过
- [x] 治理面 E2E：`scripts/e2e_governance.py` **8 档全绿**、退出无残留进程
- [x] 打包冒烟：`scripts/build_desktop.py --dist` + `scripts/e2e_package.py` **双路径通过**
- [x] 文档同步（README 路线图 / ARCHITECTURE ADR-022 / agent-core README / .env.example）
- [x] **未启用（`VIBE_SUBAGENT=0`）时行为与改造前逐字节一致**

---

## 4. 实测测试数

| 套件 | 开工基线（M6 收口实测） | 收口实测 |
|---|---|---|
| agent-core | **653 收集 = 652 passed / 1 skipped / 64 subtests** | **673 收集 = 671 passed / 2 skipped / 64 subtests**（+20：`test_subagent.py` 19 + 排空回归 1；另 1 个 skip 为 `test_sandbox.py` 符号链接用例，**环境相关**、与 M3 无关） |
| 服务端 | **272 收集 = 270 passed / 2 skipped / 409 subtests** | **272 收集 = 270 passed / 2 skipped / 414 subtests**（`test_chain_compat.py` 词表新增 `subagent.run`，子测试 +5） |
| e2e_governance | 8 档全绿 | 8 档全绿、退出无残留进程 |

> agent-core skip：`test_terminal.py`（本机 ConPTY 不可用）+ `test_sandbox.py`（本机不允许创建符号链接）——
> 两者都是**环境相关**的既有 skip，非 M3 引入。
> 服务端收集数不变（新增词表项落在既有用例的子测试内），故「只增不减」以 **passed + subtests** 计。
>
> **既有 flaky 已修复**：`test_store.py::SessionApiTests::test_resume_from_other_workspace_rejected`
> 过去偶报 `ConnectionAbortedError [WinError 10053]`。根因是 `host/server.py` 里「直接回 400/403
> 而不读请求体」的分支**未排空请求体** —— keep-alive 下残留字节使连接关闭时发出 TCP RST，
> 客户端读响应体即被中止。修复为在 `_dispatch` 收尾（`finally`）**中心化排空**未消费的请求体
> （`_read_json` 扣减已读、`_drain_body` 兜底），并加确定性回归用例
> `test_rejected_body_is_drained_without_reset`（512 KiB 请求体放大复现；反向验证变红后已还原）。

---

## 5. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| **审计链断裂**：子 Agent 建了独立 `AuditLog` 实例 | 两条链交错 → `verify_chain` 判定整条链被篡改，审计**永久不可信** | 子 Agent **共用父的审计实例**（B1 注入 `audit=`）+ E1 用 `verify_chain` 钉住 |
| **越权写盘 / 递归**：子 Agent 拿到写工具或 `task` | 绕过父级审批落盘；无限套娃烧光预算 | 只读注册表**白名单**（A2）+ `allow_write=False` 纵深防御 + 子表**不含 `task`**（无递归） |
| **Prompt 注入**：子 Agent 摘要是工作区文本的再加工 | 注入指令经子 Agent「洗白」后进父上下文 | 结果恒 `wrap_untrusted` 包裹（C2）+ 子 Agent 只读面 |
| **成本失控**：子 Agent 反复探索烧 token | 一次委派成本不可预测 | 子 Agent **独立 `subagent_max_iterations`**（默认 8）+ 只读工具面（无写、无命令） |
| **事件污染**：子 Agent 的消息 / 计划混进父视图 | 父对话流与计划卡片被灌入子 Agent 内容 | `_SubAgentHooks` **不转发** `on_message` / `on_plan` / `on_context`（B3）+ 事件只带元数据（D1） |
| **中断不透传**：父被中断，子 Agent 仍在跑 | 用户中断后进程仍在工作、残留输出 | `should_stop` 透传（B2）；子 Agent 在**轮次边界**响应中断 |
| **默认开启改变既有行为** | 不配置的部署被静默改变工具清单/请求体 | `subagent_enabled` **默认 False**（A1）；E1 钉「未启用时无 `task`、无 `run.subagent`」 |
| **前端子卡片在大对话流里错位** | 子步骤挂到错误的工具卡片上 | 子事件带 `callId` 归位；`finished` 后冻结该卡片（D3） |

### 5.1 收口实测结论

- **审计链断裂**：`test_parent_and_child_share_one_audit_instance` 钉住父子共用同一 `AuditLog`；
  委派后 `verify_chain` **通过**，`subagent.run` 只含 `{ok, iterations, tool_calls, answer_chars, task_digest}`。
- **越权写盘 / 递归**：`ReadonlyRegistryTests` 钉住只读表**不含**写/执行/`task`/`mcp__*`；
  `test_child_cannot_write` / `test_child_cannot_recurse` 钉住子 Agent 写盘与派生**均不可达**。
- **纵深防御已实证**（反向验证 E2 的关键收获）：把 `WRITE_FILE_SPEC` 临时放回只读表后，
  `test_child_cannot_write` **变红**，但**并非写成功** —— 而是被 `allow_write=False` 的前置检查拦下
  （回灌「【调用未执行】提交前检查未通过…当前会话已关闭写权限」）。说明**白名单 + 运行时闸门两道都生效**；
  `ReadonlyRegistryTests` 也如期变红。两处均已还原。
- **Prompt 注入**：`TaskToolTests` 钉住 `task` 返回恒被 `wrap_untrusted("subagent", ...)` 包裹。
- **默认关**：`SubAgentSessionTests` 钉住 `VIBE_SUBAGENT=0` 时**无 `task` 工具、无 `run.subagent` 事件**。
- **附带修复（既有 flaky）**：`test_store.py` 的 `test_resume_from_other_workspace_rejected` 曾偶发
  `ConnectionAbortedError`。已定为「回 400/403 未排空请求体 → 关连接发 TCP RST」，改为
  **中心化排空**（`_dispatch` 的 `finally` → `_drain_body`）并加确定性回归用例；反向验证已做并还原。

---

## 6. 落地顺序（建议）

`A（契约）→ B（引擎）→ C（工具）→ E（先写工具层/注册表层/引擎层用例）→ D（事件+UI）
→ E（补会话/事件层用例 + 反向验证）→ F（文档与收口）`

即：先把**只读边界**与**审计共用**钉死（A/B/E 的工具与引擎用例），再接通工具与界面。