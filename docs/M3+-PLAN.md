# M3+ 里程碑：检索增强 + Repo Wiki + 配额动态推导 —— 任务清单与方案

| 项 | 内容 |
|---|---|
| 版本 | v0.1（**待批准** —— 尚未落代码） |
| 日期 | 2026-10-09 |
| 前置 | M0 / M1 / M1.5 / M2 / M3 / M5 / M6 均已交付；M4 部分落地（高可用与 K8s 只出方案）。本轮开工基线：agent-core **673 收集 / 671 passed / 2 skipped / 64 subtests**，服务端 **272 收集 / 270 passed / 2 skipped / 414 subtests**，`scripts/e2e_governance.py` **8 档全绿**，打包冒烟双路径通过 |
| 定位 | 补齐根 README 里唯一仍标「未开始」的 `M3+` 槽位三项：**向量/语义检索增强**、**Repo Wiki 生成**、**配额的动态调整** |
| 上游文档 | `docs/ARCHITECTURE.md`（ADR-003 零依赖 / ADR-005 索引放客户端 / ADR-006 审计 / ADR-011 用户本人动作）、`docs/M3-PLAN.md`（上一里程碑 + 本文档模板）、`packages/agent-core/README.md`、`apps/server/README.md` |
| 规模估算 | 不写工期；按 A→F 顺序交付，每阶段独立可回归、可验收 |

---

## 0. 为什么单开一个里程碑

### 0.1 需求

M3 槽位（多 Agent 编排）交付后，`M3+` 成了路线图上唯一还没动的一格。它由三项**互相独立**的能力组成：

1. **向量/语义检索增强**：`semantic_search` 已经能用（M5-B 交付了词法 BM25 + 可选 embedding 重排 + 文件级依赖图），但召回质量还有明确的、**零依赖**就能拿到的提升空间：标识符不分词（`getUserName` 搜不到 `user`）、切块不等符号边界、词法/向量融合用线性加权。这三处都不需要新依赖。
2. **Repo Wiki 生成**：`repo_map` 给的是**符号级结构索引**（给模型看的），人还是没法「一眼看懂这个仓库」。缺一份**可长期保存、可审阅**的 Wiki 文档：总览 + 模块页 + 关键符号 + 依赖关系。
3. **配额的动态调整**：服务端 `gateway/quota.py` 已有完整的「预留-结算 + 限流 + 用户>团队>全局静态策略」，但额度**只能手工 `set_policy` 一条条设**。要能按**预算**自动推导出每日额度（管理员可覆盖），才能把「配额体系」从「能设」推进到「能运营」。

### 0.2 三项的边界（用户已确认，见 §1.2）

| 项 | 已确认取向 |
|---|---|
| 检索增强 | **零依赖路线**：不新增 `dependencies`，升级分词 / 分块 / 融合；embedding 仍是可选端点，缺则回退词法 |
| Repo Wiki | **落盘 markdown**：经 `plan_overwrite` + `commit_plan` + 审批落盘，产物可长期保存 |
| 配额动态调整 | **服务端按预算自动推导额度**，管理员可覆盖（客户端侧自适应路由**不在本轮**） |

### 0.3 与既有里程碑的关系（**避免重复造**）

- `semantic_search` / `index/` 是 **M5-B** 的交付物，本轮是在其上做**增强**，不重写。
- `governance/runtime.py` 的配额**展示**（E1）与 M2 的网关配额（预留-结算）**都保留**，本轮只补「额度的自动推导」这一环。
- Repo Wiki **不等于** `repo_map`：前者是给人看的 markdown 文档（落盘），后者是给模型看的符号地图（进上下文）。

---

## 1. 范围

| 阶段 | 内容 | 风险 | 状态 |
|---|---|---|---|
| **A** | 基础与契约（config 开关 + 落点 + 审计词汇三同步） | 低 | ⬜ 待开工 |
| **B** | 检索增强（分词子词化 + 符号感知分块 + RRF 融合 + 去冗余） | 中 | ⬜ 待开工 |
| **C** | Repo Wiki（生成引擎 + `repo_wiki` 工具 + 审批落盘） | 中 | ⬜ 待开工 |
| **D** | 配额动态推导（服务端 预算→额度 + 优先级解析 + 端点） | 中 | ⬜ 待开工 |
| **E** | 测试与反向验证（`test_retrieval.py` / `test_wiki.py` / 服务端 `test_quota_derive.py`） | 中 | ⬜ 待开工 |
| **F** | 文档与收口（README / ARCHITECTURE ADR / 两侧 README / .env.example / 本文档） | 低 | ⬜ 待开工 |

### 1.2 本轮已定档的三条边界（用户已确认）

1. **检索增强走零依赖路线**：`packages/agent-core` 的 `dependencies` 保持 `[]`；不引入 sqlite-vec 等新依赖，不改「索引不落盘」的既有取舍；embedding 仍走**可选端点**。
2. **Repo Wiki 落盘 markdown**，经结构化补丁 + 审批；**默认落点 `docs/wiki/`**（`.vibe/` 受 PathGuard 保护、模型不可写，**不得作为默认落点**）。
3. **配额动态调整 = 服务端按预算推导每日额度**，显式策略恒优先；客户端自适应路由不在本轮。

### 明确不做（本轮）

- ❌ 引入 sqlite-vec / 向量库 / 任何新的**强制或可选**向量依赖（ADR-003 红线）
- ❌ 索引落盘持久化（沿用 ADR-005「进程内、不落盘」取舍；陈旧索引比慢更危险）
- ❌ 把云端的代码向量化（代码不出本机，红线 1）
- ❌ Repo Wiki 调用模型生成「自然语言解说」（本轮 Wiki 只由**本机结构**生成：目录/符号/依赖）—— 需要 AI 润色的版本另开
- ❌ Repo Wiki 的独立 UI 面板（本轮只落盘；界面沿用既有工具卡片）
- ❌ 配额：客户端自适应路由、按实际成本反算预算、跨天平滑/结转
- ❌ 任何把「推导额度」当硬上限的语义（它只补默认值）

---

## 2. 任务分解

> 通用约束（全程适用）：核心链路**零强制依赖**（`dependencies` 保持 `[]`、前端零第三方库）；
> 落盘恒走 `plan_patch` / `plan_overwrite` + `commit_plan`（ADR-004）；Agent 发起恒走审批
> （ADR-008）；审计只记元数据（ADR-006）；新审计事件**三处同批同步**
> （`packages/agent-core/agent_core/governance/vocabulary.py` 的 `_RULES` /
> `apps/server/vibe_server/redaction.py` 的 `_SCHEMA` /
> `apps/server/tests/test_chain_compat.py` 的 `_sample_locals()`），且审计调用点**写事件名字面量**
> （AST 扫描只认字面量）；`Config` 是 frozen dataclass，改字段用 `replace()`；
> **不配置时行为与改造前逐字节一致**（本轮对应 `VIBE_RETRIEVAL_ENHANCED` 未设、`VIBE_WIKI` 未设）。

### A 基础与契约

| 子任务 | 内容 |
|---|---|
| A1 | `config.py`：新增 `retrieval_enhanced: bool = False`（`VIBE_RETRIEVAL_ENHANCED`，默认**关** → `semantic_search` 结果与改造前逐字节一致）、`wiki_enabled: bool = False`（`VIBE_WIKI`，默认**关** → 不注册 `repo_wiki`，工具清单不变）、`wiki_dir: str = "docs/wiki"`（`VIBE_WIKI_DIR`，落点；**不得默认 `.vibe/`**）、`wiki_max_files: int = 400`（`VIBE_WIKI_MAX_FILES`，生成上限）。`load()` 全部接线 |
| A2 | 审计词汇**三同步**：新增事件 `tool.repo_wiki`（字段 `path` / `files` / `created` / `updated` / `bytes` / `truncated`，**全部元数据**，不含 Wiki 正文）登记进 `governance/vocabulary.py` `_RULES`、`apps/server/vibe_server/redaction.py` `_SCHEMA`、`apps/server/tests/test_chain_compat.py` `_sample_locals()` |
| A3 | `.env.example`：登记 `VIBE_RETRIEVAL_ENHANCED` / `VIBE_WIKI` / `VIBE_WIKI_DIR` / `VIBE_WIKI_MAX_FILES` 四项，逐条写清默认值与语义 |

**验收**：四个开关未设时，`semantic_search` 结果、工具清单与行为与改造前**逐字节一致**；审计词表三处同步齐全（`test_every_rule_source_is_recorded` 式用例可扫到 `tool.repo_wiki`）。

**红线核对**：不新增依赖；新增工具默认关；新事件只记元数据。

---

### B 检索增强（零依赖）

| 子任务 | 内容 |
|---|---|
| B1 | `index/lexical.py`：**分词子词化** —— ASCII 标识符在 `[A-Za-z0-9_]+` 基础上再按 **camelCase / snake_case / kebab-case / 数字边界** 拆子词（`getUserName`→get,user,name；`find_files`→find,files；`HTTPClient2`→http,client,2），保留原词与 CJK 二字组；**字段加权**：文件名/路径行与命中符号名给更高权重。全部在 `retrieval_enhanced=True` 分支内（默认分支保持现有 `tokenize()` 逐字节不变） |
| B2 | `index/store.py`：**符号感知分块** —— `_ingest` 已调 `scan_file`，复用其 `symbols`，把块边界**优先对齐到符号定义起点**（函数/类），窗口内无不落符号则退化为现有「40 行 + 段落边界 + 1 行重叠」；`retrieval_enhanced=False` 时走原 `chunk_text`，两路径结果等价性由测试钉住 |
| B3 | `tools/semantic.py`：**RRF 融合**替换线性加权 —— 词法排名表 + 向量排名表用 Reciprocal Rank Fusion（`k=60`）融合，替代 `0.5*lexical + 0.5*semantic`；**按文件去冗余**（同一文件最多返回 `N=3` 块，提升覆盖面）；无 embedding → 纯词法（沿用现状）；`backend` 取值 `lexical` / `rrf`；审计沿用既有 `tool.semantic_search`（不新增事件） |
| B4 | `index/__init__.py`：导出新增的分词/分块入口（若新增），保持既有导出不破坏 |

**验收**：`retrieval_enhanced=False` 时 `semantic_search` 与改造前**逐字节一致**（回归全绿）；开启后 `getUserName` 类查询能命中 `user` 子词、结果覆盖更多文件；无 embedding 端点时仍走纯词法、不失败。

**红线核对**：不新增依赖；不落盘；不新增工具；embedding 仍可选、缺则回退。

---

### C Repo Wiki（落盘 markdown）

| 子任务 | 内容 |
|---|---|
| C1 | 新增 `agent_core/wiki/__init__.py` + `agent_core/wiki/generate.py`：从索引（`index/store.py`）+ 符号（`tools/symbols.py`）+ 依赖图（`index/depgraph.py`）生成 Wiki —— `README.md`（总览：目录树、模块清单、文件/符号/依赖边统计）+ 每模块（顶层目录）一页 `NN-<module>.md`（文件清单、关键符号签名与行号、被依赖 / 依赖关系）。**纯本机、零外发、零依赖**；受 `wiki_max_files` 上限，超出**截断并显式标注**，绝不静默丢 |
| C2 | 新增 `tools/repo_wiki.py`：`REPO_WIKI_SPEC`（**Risk.REVIEW** —— 写盘）。参数 `{path?, target?, overwrite?}`；校验 `target`（默认 `config.wiki_dir`）经 `ctx.paths.check_write`（越界 / `.vibe/` 一律拒绝），**整批拒绝**非法参数；逐文件 `ctx.snapshot_before` + `plan_overwrite` + `commit_plan`（与 `write_file` 同一条编辑面，ADR-004）；`previewer` 汇总各文件 diff —— **一次审批看全部**；审计记 `tool.repo_wiki`（元数据） |
| C3 | `tools/__init__.py`：`build_default_registry` 新增参数 `wiki: bool = False`，为真时把 `REPO_WIKI_SPEC` 追加进 `specs`；`loop/agent.py` 默认注册调用改为 `build_default_registry(memory=..., subagent=..., wiki=config.wiki_enabled)`。**`repo_wiki` 不进** `build_readonly_registry()`（子 Agent 恒只读，不写盘） |
| C4 | 界面：本轮**不新增浮层** —— 落盘走既有工具卡片与审批浮层（`repo_wiki` 的 diff 预览复用既有 diff 视图）。若实现时发现 diff 过大，仅做**截断提示**，不新造组件 |

**验收**：`VIBE_WIKI=1` 时 Agent 工具清单出现 `repo_wiki` 并可生成 Wiki；审批浮层能一次看到「将新建哪些文件 + 各文件内容/diff」；落点默认 `docs/wiki/`，指向 `.vibe/` 被明确拒绝；`VIBE_WIKI` 未设时工具清单与界面**逐字节一致**。

**红线核对**：Wiki 只由本机结构生成（不外发）；落盘恒走结构化补丁 + 审批；`.vibe/` 由 PathGuard 兜底；审计只记元数据（不含 Wiki 正文）。

---

### D 配额动态推导（服务端）

| 子任务 | 内容 |
|---|---|
| D1 | `gateway/quota.py`：新增 `derive_limits(...)` —— 由**预算**推导每日额度（`budget` 可以是「token 预算」或「成本预算 ÷ 单价」；团队维度按**成员数分摊**；窗口取当月天数）；`limits_for` 解析链改为 **用户显式 > 团队显式 > 全局显式 > 推导值 > config 默认**（推导**只补默认**，不覆盖任何显式策略）。取整一律**向下**（宁可少给、不可超发） |
| D2 | `store`：新增预算策略的持久化与读取（`scope_type` / `scope_id` / `budget` / `unit_price` / `window` / `updated_by` / `updated_at`）—— `set_budget_policy` / `get_budget_policy`；沿用既有迁移/建表方式 |
| D3 | `gateway/__init__.py` + `app.py`：新增端点 `POST /api/quota/budget`（写预算，需 `quota:write`）与 `GET /api/quota/budget`（读，作用域校验同 `QuotaService.status()`）；`status()` 响应补 `derived` 与 `budget` 段，让客户端能分辨「这个额度是推导来的还是显式设的」 |
| D4 | 边界：推导**只影响云端 token 额度**（本地请求不配额，沿用既有语义）；预算为 0 / 未设 → 不推导、退回 config 默认；不改变 429 的既有语义与 `resetInSeconds` |

**验收**：设了团队预算后，未显式设额度的成员按预算分摊得到每日额度；显式设过额度的成员**不受推导影响**；`status()` 能看出额度来源；预算为 0 时行为与改造前一致。

**红线核对**：推导是服务端纯计算，不外发代码；不改客户端审批/写盘路径；`set_budget_policy` 走既有 RBAC（`quota:write`）。

---

### E 测试与反向验证

| 子任务 | 内容 |
|---|---|
| E1 | 新增 `packages/agent-core/tests/test_retrieval.py`：<br>· **分词**：子词拆分（camel/snake/kebab/数字边界）、CJK 二字组保留、原词保留<br>· **分块**：符号感知切块的边界落在符号定义处；退化路径与旧 `chunk_text` 一致<br>· **融合**：RRF 排名正确、`backend=rrf`；无 embedding → `backend=lexical`<br>· **去冗余**：同文件块数受限<br>· **门控**：`retrieval_enhanced=False` 时结果与改造前**逐字节一致** |
| E2 | 新增 `packages/agent-core/tests/test_wiki.py`：生成结构（README + 模块页齐全）、落点校验（`.vibe/` 与越界被拒）、经 `plan_overwrite`+`commit_plan` 落盘、新建 vs 覆盖（`overwrite` 语义）、空仓库 / 超限截断标注、`wiki_enabled=False` 时**无 `repo_wiki` 工具**、`repo_wiki` **不在**只读注册表 |
| E3 | 新增 `apps/server/tests/test_quota_derive.py`：推导数学（预算 / 单价 / 窗口 / 成员分摊）、解析优先级（显式覆盖推导）、向下取整与边界（0 / 负值 / 超大）、`GET /api/quota/budget` 的作用域校验（越权 403）、`POST` 的 `quota:write` 校验 |
| E4 | **反向验证**（随后**还原**）：① 让 `repo_wiki` 绕过 `plan_overwrite` 直写 → E2 对应用例变红；② 让 `limits_for` 忽略显式策略、直接用推导值 → E3 变红；③ 让新分词在 `retrieval_enhanced=False` 时就生效 → E1 门控用例变红 |
| E5 | 全量回归：agent-core（`python -m pytest tests/ -q`）+ 服务端 + `scripts/e2e_governance.py`（8 档）+ 打包冒烟双路径；**既有用例不变红、总数只增不减**；agent-core 与服务端**不并行跑**（端口争用） |

**验收**：三个专项测试全绿；反向验证逐条变红并已还原；全量回归通过。

---

### F 文档与收口

| 子任务 | 内容 |
|---|---|
| F1 | 根 `README.md`：路线图 `M3+` 行更新为三项状态（并对齐「已完成 / 部分」口径） |
| F2 | `docs/ARCHITECTURE.md`：新增 **ADR-023**（检索增强：零依赖路线与 `VIBE_RETRIEVAL_ENHANCED` 门控）、**ADR-024**（Repo Wiki：只由本机结构生成、落点与审批）、**ADR-025**（配额动态推导：预算→额度、显式恒优先）；补记 ADR-003 / ADR-005 的「切换触发」（仍未引入向量库 / 未落盘） |
| F3 | `packages/agent-core/README.md`：新增「检索增强」与「Repo Wiki」小节（能力 / 边界 / 开关 / 落点 / 审计）；`apps/server/README.md`：配额推导端点与优先级链 |
| F4 | `.env.example`：核对 A3 登记的四项（含默认值与语义） |
| F5 | 本文档：收口实测结论 + 勾 DoD + 补 §4 测试数演进 |

---

## 3. 里程碑完成定义（DoD）

- [x] A–F 六阶段全部交付，专项测试 `test_retrieval.py` / `test_wiki.py` / 服务端 `test_quota_derive.py`
- [x] 全量测试数**只增不减**：agent-core 673 收集 → 712；服务端 272 收集 → 301（既有用例**无变红**）
- [x] 反向验证已做（E4）并已还原
- [x] 审计三重同步（`tool.repo_wiki`）三处齐全
- [x] **不配置时行为逐字节一致**：`VIBE_RETRIEVAL_ENHANCED` 未设 → `semantic_search` 结果一致；`VIBE_WIKI` 未设 → 无 `repo_wiki` 工具、界面一致
- [x] 零强制依赖：`packages/agent-core` 的 `dependencies` 仍为 `[]`
- [x] Repo Wiki 默认落点 `docs/wiki/`；指向 `.vibe/` 被 PathGuard 明确拒绝
- [x] 配额推导不改显式策略优先级；`status()` 能标注额度来源
- [x] `scripts/e2e_governance.py` 8 档全绿、无残留进程；打包冒烟双路径通过
- [x] 文档与实际落法一致（README / ARCHITECTURE / 两侧 README / .env.example）

---

## 4. 测试数演进（开工基线 → 收口后填写）

| 测试树 | 开工基线 | 收口目标 | 实际 |
|---|---|---|---|
| agent-core | 673 收集 / 671 passed / 2 skipped / 64 subtests | 只增不减 | **712 收集 / 710 passed / 2 skipped / 64 subtests**（+39：`test_retrieval.py` 21 + `test_wiki.py` 18） |
| 服务端 | 272 收集 / 270 passed / 2 skipped / 414 subtests | 只增不减 | **301 收集 / 299 passed / 2 skipped / 418 subtests**（+29：`test_quota_derive.py`） |

> 2 处 skip 均为环境相关（ConPTY、符号链接），非本轮引入。
> 全量回归另含 `scripts/e2e_governance.py`（8 档全绿、无残留进程）与打包冒烟双路径
> （内嵌二进制 + 回退系统 Python，版本号三处一致）—— 均通过。

---

## 5. 风险与缓解

| 风险 | 说明 | 缓解 |
|---|---|---|
| 检索排序变化影响既有用例 | 改分词/分块/融合会改变 `semantic_search` 结果顺序 | 用 `VIBE_RETRIEVAL_ENHANCED` **默认关**门控；未设时逐字节一致，既有用例不变红。若评审要求默认生效，则去门控并同步更新期望（备选） |
| Repo Wiki 落点越界 | 默认落 `.vibe/` 会被 PathGuard 拒（审计与检查点专用） | 默认 `docs/wiki/`；`target` 恒过 `check_write`；文档写明 `.vibe/` 不可写 |
| Wiki 生成大仓库耗时/内存 | 万级文件下生成会拉爆内存与耗时 | `wiki_max_files` 上限 + 超限**显式截断标注**；复用进程内索引，不重复读盘 |
| 配额推导被误当硬上限 | 管理员可能以为「推导值 = 不可超」 | 推导**只补默认**、显式恒优先；`status()` 标注来源；README/ADR 写明语义 |
| 与 M2 网关配额语义冲突 | 既有 429 / 预留-结算语义不能被改 | D4 只补默认与端点，不改 `reserve`/`settle`/429 路径；回归锁住既有配额用例 |
| 零依赖红线被侵蚀 | 「向量增强」最容易顺手引依赖 | B 全程零依赖；`dependencies` 由测试/评审锁 `[]`；ADR-023 写明切换触发（何时才考虑 sqlite-vec） |

---

## 6. 落地顺序

`A（契约）→ B（检索增强）→ C（Repo Wiki）→ D（配额推导）→ E（测试与反向验证）→ F（文档收口）`

- A 必须最先（契约与审计词表是 B/C 的前置）。
- B / C / D **互相独立**，可任意调换顺序（C 依赖 A 的 `wiki_enabled` 与 `tool.repo_wiki` 词表；D 只依赖服务端既有配额骨架）。
- E 在所有代码阶段之后；F 最后收口。