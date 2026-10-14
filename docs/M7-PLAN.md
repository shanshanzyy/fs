# M7 里程碑：受控联网检索（web_search / web_fetch）+ Git 写操作面板 —— 方案设计

> 状态：**已完成（Part A + Part B 全部闭合）**。本文件为方案 + 实测记录。
> 对标缺口来自「与 Trae 等产品的差距分析」优先级 ①② 两项。

---

## 0. 为什么单开一个里程碑

M0–M6 之后，与 Trae/Cursor 对比剩余的最大空白有两块：

- **联网能力完全缺失**：内核无任何联网工具（grep 全库零匹配）。遇到「查文档、
  查某 API 的最新用法、对着报错搜 issue」这类任务，Agent 只能靠模型参数里的
  陈旧知识硬答。这是 Agent 可用性的硬缺口，不是锦上添花。
- **Git 面板只读**：`gitinfo.py` 刻意只读（见其文件头注释），用户被改动面板
  「看着 diff 却不能提交」，每次都要切出终端手敲 `git add/commit/push`。
  对单人单机是体验问题，对「AI 改完 → 人审完 → 顺手提交」的闭环是断点。

两项合为一个里程碑：都不动核心补丁引擎与审批红线，改动面集中在
（内核工具/端点 + 治理面策略 + 界面），可分两部分独立交付、独立验收。

### 与三条设计红线的关系（先说清楚）

| 红线 | 联网检索怎么守住 | Git 写面板怎么守住 |
|---|---|---|
| 代码不出本机 | 查询词/URL 会外发，但**外发内容必须先过人工审批**（恒 REVIEW）；治理面可一刀切禁；fetch 回来的内容按不可信文本包裹 | git 写操作不外发任何代码（远端即用户自己配的 git remote） |
| 结构化补丁 | 不涉 | **改动落盘的唯一入口仍是补丁引擎**。Git 写面板只 commit/推送已落盘的改动，不产生新改动 |
| 高危恒审批 | 两个新工具恒 REVIEW，同 MCP 前例（ADR-015），不可配置放行 | Git 写操作是**用户本人动作**（ADR-011/ADR-016 范式，同编辑面保存与终端），不进 Agent 审批浮层，但破坏性动作有独立确认 + 审计 + 检查点兜底 |

---

## 1. 范围

**A 联网检索**：两个新内置工具 `web_search` / `web_fetch`；默认关闭；治理面
策略新字段 `allowWeb` / `webDeniedHosts`；网关新增搜索中转端点；设置面板新段。

**B Git 写操作面板**：stage / unstage / discard / commit / push / pull --ff-only /
分支新建与切换，共七个动作；全部为用户本人动作；审计事件补齐。

### 明确不做（本里程碑）

- **联网检索侧**：
  - 客户端直连公网搜索 API（Bing/博识等，密钥落本机）—— 违反 ADR-007
    「密钥集中托管」精神，不提供。要公网搜索，由治理面网关中转（密钥在服务端）。
  - `web_fetch` 走网关中转 —— fetch 不需要密钥；让正文过服务端内存只会扩大
    服务端敏感面（ADR-006）。V1 fetch 恒直连、恒 REVIEW。
  - 子 Agent 联网 —— `build_readonly_registry` 不收录这两个工具，同 MCP 前例
    （子 Agent 不外发）。
  - 在线文档问答（#Docs）、语音、多语言 —— 与联网无关，另立里程碑。
- **Git 侧**：
  - **不给 Agent 加 git_commit / git_push 工具**——`git.py` 文件头已写明理由：
    把提交收进 Agent 工具面等于给「改动落盘唯一入口是补丁引擎」开新口子。
    Agent 想提交，让用户在面板上点。
  - merge / rebase / revert / cherry-pick / stash / tag —— 冲突面与交互复杂，
    V1 一律不做，留给内置终端。面板检测到冲突态时给提示并引导去终端。
  - 治理面下发 Git 分支策略（禁直推 main 等）—— V1 不做，依赖远端仓库自身
    的保护规则；策略字段留待后续里程碑评估。
  - 3-way merge 冲突编辑器。
- 两侧共同：不改桌面壳窄桥（pythonPath/backendBin/serverUrl/gatewayUrl 四字段
  不动），新配置全部走后端 `/api/settings` 与环境变量。

---

## 2. 任务分解

### Part A：受控联网检索

#### A1 配置面（config.py + .env.example）

与 `vision_mode` / `mcp_enabled` 同一纪律：**会外发的能力默认关闭，关闭时
工具清单与行为与未实现前逐字节一致**。

| 字段 | 默认 | 环境变量 | 说明 |
|---|---|---|---|
| `web_enabled` | `False` | `VIBE_WEB` | 总开关；False 时两个工具不注册 |
| `web_search_provider` | `"gateway"` | `VIBE_WEB_SEARCH` | 枚举 `gateway` / `searxng`，非法值启动前报错（`_env_choice`） |
| `web_search_url` | `""` | `VIBE_WEB_SEARCH_URL` | searxng 的 JSON 端点；gateway 模式忽略（由 `server_url` 派生 `<server>/api/v1/search`） |
| `web_fetch_allow_private` | `False` | `VIBE_WEB_ALLOW_PRIVATE` | 允许私网/环回地址（内网文档服务器场景）；默认仅公网 |
| `web_allow_hosts` | `""` | `VIBE_WEB_ALLOW_HOSTS` | 逗号分隔 glob 白名单；非空时仅白名单内 host 可 fetch |
| `web_deny_hosts` | `""` | `VIBE_WEB_DENY_HOSTS` | 逗号分隔 glob 黑名单，**恒优先于白名单**；策略下发的拒绝项并进这里 |
| `web_timeout` | `20` | `VIBE_WEB_TIMEOUT` | 单次请求超时（秒） |
| `web_max_bytes` | `2_000_000` | `VIBE_WEB_MAX_BYTES` | 单次 fetch 字节上限，超限拒绝而非截断 |
| `web_search_max_results` | `8` | `VIBE_WEB_MAX_RESULTS` | 搜索返回条数上限 |

provider 两形态的定位：

- **gateway**：团队形态首选。搜索密钥托管在治理面（ADR-007 同范式），
  客户端零密钥、零公网出口；治理面不可达时工具明确报错（不静默降级）。
- **searxng**：单机/小队形态。内网自部署 SearXNG 的 JSON API，通常零密钥；
  公网公共实例也可用，但外发面与直连公网等同，文档中明确提示。

全部新变量登记进 `.env.example`（工程惯例）。

#### A2 两个工具（agent_core/tools/web.py 新文件）

**`web_search(query, max_results?)`** → 结构化结果清单（title / url / snippet）。
**`web_fetch(url, max_chars?)`** → 正文文本（HTML 转文本）。

设计与既有范式的对齐点：

- **Risk 恒 REVIEW、不可配置放行**——同 `remember` / `repo_wiki` / MCP 的前例。
  审批浮层就是「外发内容人工确认」这道闸：
  - web_search 的 preview 显示 **完整 query + provider + 目标端点**；
  - web_fetch 的 preview 显示 **完整 URL + 解析出的 IP + Content-Type 白名单判定**。
    REVIEW 不可关 = 用户永远看得到要外发什么，这是防「模型把代码塞进查询词」
    的最后一道防线。
- **结果恒 `wrap_untrusted`**——取回的网页是注入面（正文里可能藏指令），
  与 `task` 工具结果同一处理。
- **不进 `build_readonly_registry`**——子 Agent 工具面恒不含联网（同 MCP）。
- **零依赖**：`urllib.request` + `socket.getaddrinfo` + `html.parser`，不引
  requests/httpx/bs4。核心链路零强制依赖是硬约束。
- **注册开关**：`build_default_registry(*, web=False, ...)`，`web=config.web_enabled`
  在 Agent 构建处与 `subagent`/`wiki` 同位置接线。

#### A3 SSRF 防护（web_fetch 的核心安全面）

按顺序执行，任一不满足即拒绝（`ToolError`，消息给人看）：

1. **协议白名单**：仅 `http` / `https`（file/ftp/gopher 等一律拒绝）。
2. **host 匹配**：先过 `web_deny_hosts`（含策略下发项），非空 `web_allow_hosts`
   再求交集；deny 恒优先。
3. **DNS 解析后校验 IP**：`getaddrinfo` 逐个地址判私网（10/8、172.16/12、
   192.168/16、127/8、169.254/16、::1、fc00::/7 等）；`web_fetch_allow_private=False`
   时拒绝。**校验必须在解析后、连接前做**，且每条重定向跳转都重跑全流程。
4. **重定向**：最多 3 跳；每跳重新过 1–3；跨协议跳转（https→http）默认跟随
   但逐跳过闸（不放宽）。
5. **响应限制**：`Content-Type` 白名单（text/html、text/plain、text/markdown、
   application/json、application/xml）；`Content-Length` 超限拒绝 + 流式读取到
   `web_max_bytes` 截断拒绝；超时 `web_timeout`。
6. **解码**：按响应 charset 解码，缺失回退 utf-8；截断按**解码后字符**回退
   ——多字节切点教训（见项目经验：`errors="replace"` 超字节）。
7. **HTML→文本**：`html.parser` 提取 `<script>/<style>` 之外的正文文本
   （标题 + 正文，丢弃标签），纯文本/JSON 原样返回；再按 `tool_output_limit`
   与 `max_chars` 截断并注明。

#### A4 审计（本地记录 + 上传脱敏）

本地事件（三重同步，见 §4 清单）：

```
tool.web_search   {provider, query, query_chars, query_digest, max_results, result_count}
tool.web_fetch    {url, host, status, bytes, content_digest, truncated, error}
```

上传规则（`governance/vocabulary.py`）：

- `query` / 抓取正文**只上传 digest + 长度**（ADR-006：原文只留本机）——
  query 可能含代码片段，正文本身就是外部内容。
- URL 上传 `host + path`，**query string 换 digest**——URL 参数里可能有
  token/敏感词（项目已有先例：host 静态壳的 Referer 泄露防线）。

#### A5 治理面：策略 + 网关搜索中转

**策略**（`apps/server/vibe_server/policy.py`）：

- 新字段 `allowWeb`：BOOL，AND 合并，**默认 False**（默认策略最保守）。
  进 `_BOOL_AND_FIELDS`、`DEFAULT_POLICY`、`_CLIENT_KEY_MAP`（`web_enabled`），
  `apply_to_client_config` 的能力开关循环同步。
- 新字段 `webDeniedHosts`：LIST，并集合并，默认 `[]`；客户端键 `web_deny_hosts`。
- **过期降级**：stale 受限模式里 `allowWeb` 恒 False（同 allowCloud 的处理）。
- `validate_document` 按既有约束校验（非空字符串、无换行、条数上限）。

**网关搜索中转**（gateway 新端点）：

- `POST /api/v1/search` `{query, maxResults}` —— 认证与 LLM 网关同源（JWT），
  客户端零密钥。
- 服务端搜索上游：V1 仅 **SearXNG JSON**（`VIBE_SRV_SEARCH_URL`）——内网
  自部署、零密钥形态，egress 不出内网；Bing/博识列为 V2 可选（届时密钥走
  服务端 file/Vault 托管，与 LLM key 同法）。
- 服务端审计：`web.search_relay` `{query_digest, query_chars, results}`——
  查询词对服务端也只记指纹（服务端不做查询内容留存）。
- 客户端侧 `gateway_llm.py` 同目录新增 `search_via_gateway()`：401/403/502
  语义与 LLM 网关一致（不可达 = 明确报错，不静默降级到本地假实现）。

#### A6 界面

- **设置面板**新增「联网检索」段：总开关、provider、搜索端点、允许私网、
  host 名单、超时/上限。走既有 `GET/POST /api/settings`，端点登记进
  `protocol.py` 命令表；关闭时整段置灰（与 MCP 段同一交互范式）。
- **审批浮层**：无需新组件——REVIEW 级自动停浮层；preview 文案按 A2 展示。
  沿用既有字号/折行规范。
- 工具未注册时（web_enabled=False）：对话行为与现状逐字节一致，输入框不出现
  任何联网入口。

#### A7 测试计划（Part A 的 DoD）

- **单元（tools/web.py）**：SSRF 矩阵（私网 IP/环回/链路本地/重定向到私网/
  跳数超限/非 http 协议/host 黑名单优先于白名单/allow_private 开关）；
  Content-Type 拒绝；超限拒绝；charset 缺失回退；多字节截断回退（中文切点）；
  HTML 转文本（script/style 剔除）；wrap_untrusted 包裹断言；恒 REVIEW 断言
  （`ToolSpec.risk == Risk.REVIEW`）；query digest 审计断言。
- **provider 两形态**：本地起 `http.server` mock SearXNG JSON；gateway 形态用
  治理面测试里现成的假网关范式。
- **策略联动**：`allowWeb=False` 时 `web_enabled` 被收紧；`webDeniedHosts`
  并集进 `web_deny_hosts`；stale 模式 web 恒关；`apply_to_client_config` 变更
  说明文案。
- **未注册恒等性**：`web_enabled=False` 时 `build_default_registry` 产出与
  M6 现状逐字节一致（同 subagent/wiki 的既有测试范式）。
- **E2E**：mock 模型触发 `web_fetch` → 审批浮层出 URL → 批准 → 正文进上下文；
  拒绝分支；治理面 e2e 冒烟里加 search 中转可达/不可达分支。

---

### Part B：Git 写操作面板

#### B1 总体范式：用户本人动作，不进 Agent 审批

与三条既有先例完全同构：

| 先例 | Git 写面板对应 |
|---|---|
| 编辑面保存 `/api/edit/save`（ADR-011） | stage/commit/push 都是「用户在面板上点」，与用户在终端敲 `git` 等价 |
| 交互式终端（ADR-016：与 Agent 通道彻底隔离） | git 写操作与 `run_command`（Agent 通道）完全分离，Agent 永远无 git 写能力 |
| LSP 重命名 `/api/lsp/rename/apply`（preview → 确认 → apply） | discard（破坏性）先出确认清单 + 自动检查点，再执行 |

不加任何 `git_commit` 类 Agent 工具（见 §1 明确不做）。

#### B2 gitinfo.py 扩写（写操作，全部固定 argv）

沿用既有三条纪律（固定 argv + `shell=False`、`scrub_env()`、非仓库返回
`available=False` 不抛异常），新增：

| 函数 | git 调用 | 约束 |
|---|---|---|
| `stage(root, paths)` | `git add -- <paths...>` | 每条 path 先过 `PathGuard.check_list` |
| `unstage(root, paths)` | `git reset -q HEAD -- <paths...>`（unborn 时 `git rm --cached`） | 同上 |
| `discard(root, paths)` | `git checkout -- <paths...>`（untracked 用 `git clean -f -- <paths>`） | **先做检查点快照再执行**（B5）；`.vibe` 内与敏感路径拒绝 |
| `commit(root, message)` | `git commit -m <message>` | message 单 argv 元素（数组传递天然无注入面）；长度上限 500；空拒；**不加 `--no-verify`**（尊重钩子） |
| `push(root)` | `git push`（无参，推已设上游的当前分支） | 不带 `--force`；无上游报错引导先设上游 |
| `pull_ff(root)` | `git pull --ff-only` | 非快进**直接报错**（绝不自动产生合并）；有冲突文件时前置拒绝 |
| `branch_create(root, name)` / `branch_switch(root, name)` | `git switch -c <n>` / `git switch <n>` | name 走 refname 合法性校验（白名单正则），拒绝怪名注入 |
| `branches` / `snapshot` | 已存在未接线 | 本轮接入端点 |

注意点：workspace 可能是 repo 的子目录（git 以 repo 根生效）；PathGuard 会把
「repo 根内、workspace 外」的路径拒绝——这是**收窄**而非漏洞，文档注明即可。
push/pull 超时单独放宽到 120s（网络远端），其余沿用 20s。

#### B3 host 端点（用户动作通道，全 POST）

```
POST /api/git/stage      {paths: [...], all?: bool}
POST /api/git/unstage    {paths: [...], all?: bool}
POST /api/git/discard    {paths: [...]}          ← 200 返回 {checkpointId}
POST /api/git/commit     {message}               ← 空 message 400
POST /api/git/push       {}
POST /api/git/pull       {}
POST /api/git/branch     {name, action: "create"|"switch"}
GET  /api/git/branches   {}                      ← 接通既有 gitinfo.branches
```

状态码语义：非仓库 → 既有 `available:false` 范式；git 自身失败（无上游、钩子
拒绝、非快进）→ **409 + stderr 首行摘要**；路径越界/敏感 → 400；成功 → 200 +
最新 `status()` 快照（省一次往返刷新）。全部登记进 `protocol.py` 命令表与
`_drain_body` 既有纪律。

#### B4 审计（用户动作事件，前缀对齐既有 `git.commit_message`）

```
git.stage    {count, paths_digest}
git.unstage  {count, paths_digest}
git.commit   {message_chars, message_digest, oid}     ← message 只传指纹（ADR-006）
git.push     {remote, branch, ok, reason}
git.pull     {remote, branch, ok, reason}
git.branch   {name, action}
git.discard  {count, paths_digest, checkpoint_id}
```

上传脱敏：路径是相对路径但批量时只传 count + digest；commit message 是用户
手打的，可能含敏感描述，恒 digest。三重同步清单见 §4。

#### B5 discard 的检查点联动（破坏性动作的兜底）

复用既有检查点机制（不依赖 git、对将被修改的文件做落盘前快照）：

1. 用户点「放弃改动」→ 后端先对目标文件逐个 `snapshot_before` 生成检查点；
2. 确认弹层展示**将丢弃的文件清单 + 检查点 ID**（满足「回退时显示哪些内容
   被删」的既有需求惯例）；
3. 执行 `git checkout --` / `git clean`；事后可在检查点面板 `restore` 找回。
   `.vibe` 内文件与 PathGuard 敏感路径**拒绝 discard**（防止误清审计）。

#### B6 界面（host/ui/app.js Git 面板改造）

- **双栏化**：暂存区（已 stage，绿）/ 工作区（未 stage，红）两组文件清单，
  每行三个动作：stage / unstage / 放弃（图标 + hover 提示；冲突文件标红且
  动作禁用，提示去终端解决）。
- **提交条**：底部输入框 + 「AI 生成」（复用既有 `/api/git/commit-message`，
  生成结果可编辑）+「提交」按钮。
- **工具栏**：分支下拉（当前分支 + 切换 + 新建入口）、pull / push 按钮，
  徽标直接用 `status()` 已有的 ahead/behind。
- **操作后局部刷新** `status + /api/diffs`（改动面板共用 `S.git` 的既有约定），
  不整页刷新。
- 破坏性确认（discard/push）用模态框；push 弹层显示目标 remote/分支与
  ahead 数。非仓库时整面板沿用现有「不可用 + 原因」形态。

#### B7 测试计划（Part B 的 DoD）

- **gitinfo 写函数单测**（`tempfile` 建真仓库；push/pull 用 `file://` 裸仓库
  做远端，零网络）：stage→unstage 幂等；unborn 仓库 unstage 分支；commit
  message 500 上限/空拒；钩子失败透传（装一个必失败的 pre-commit）；push 无
  上游报错；pull --ff-only 非快进拒绝且**不产生任何改动**；discard 自动
  untracked（clean）分支；refname 非法拒绝（`../evil`、含空格）。
- **host 端点测试**：各状态码分支（200/400/409/available:false）；`all=true`
  语义；敏感路径 discard 拒绝（`.vibe/audit.jsonl`）；discard 产生检查点且
  `restore` 可完整找回（**反向验证**：临时禁用快照断言测试变红——工程惯例）。
- **审计**：commit message 只上传 digest 的脱敏断言；`test_chain_compat.py`
  AST 扫描三重同步。
- **E2E 冒烟**：临时仓库全流程 stage→commit→push（file 远端）→pull，含
  进程残留检查；对话期间面板操作不打断会话（复用事件流回归）。

---

## 3. 里程碑完成定义（DoD）

1. `web_enabled=False` 时：工具注册表、对话行为、审计流与 M6 收口**逐字节
   一致**（既有 hermetic 测试全绿，无需改一行）。
2. Git 面板关闭态（非仓库/禁用）不出现任何不可用的交互件（审批浮层不变式：
   状态为空 ⟺ 浮层不可见 的同款纪律）。
3. 全量回归：agent-core、服务端、桌面壳 CI 三跑道全绿；新增用例数在 README
   与本文件 §实测栏同步更新（工程惯例：文档与实际落法一致）。
4. `.env.example`、`protocol.py` 命令表、治理面 README 接口清单、桌面端
   文档四处同步；新增审计事件三重同步全绿。
5. 单用户 + 多用户（治理面策略收紧 allowWeb）两套 E2E 冒烟通过。

---

## 4. 审计事件三重同步清单（本里程碑新增）

| 事件名 | vocabulary `_RULES` | 服务端 redaction `_SCHEMA` | test_chain_compat `_sample_locals` |
|---|---|---|---|
| `tool.web_search` | ✅ 新增 | ✅ 新增 | ✅ 新增 |
| `tool.web_fetch` | ✅ 新增 | ✅ 新增 | ✅ 新增 |
| `web.search_relay`（服务端侧） | —（服务端事件） | ✅ 新增 | ✅ 新增 |
| `git.stage` / `git.unstage` | ✅ 新增 | ✅ 新增 | ✅ 新增 |
| `git.commit` / `git.push` / `git.pull` | ✅ 新增 | ✅ 新增 | ✅ 新增 |
| `git.branch` / `git.discard` | ✅ 新增 | ✅ 新增 | ✅ 新增 |

注意：客户端审计调用点必须写字面量事件名（`xxx.record("tool.web_search", ...)`)，
AST 扫描按字面量收集——变量传递扫不到（既有教训）。

---

## 5. 风险与缓解

| # | 风险 | 缓解 |
|---|---|---|
| 1 | 模型把代码片段塞进搜索 query 外发 | 恒 REVIEW：query 全文必经审批浮层；上传只 digest；治理面 `allowWeb` 可一刀切 |
| 2 | 网页正文注入指令（间接 Prompt Injection） | `wrap_untrusted` 包裹 + 恒 REVIEW（取回动作本身经人审）+ 规则/记忆同款「进提示词的不可信文本」纪律（ADR-021） |
| 3 | SSRF 打内网服务 | A3 六步防护（解析后校验 IP、逐跳重跑、deny 恒优先、私网默认拒）；私网开关是显式环境变量 |
| 4 | SearXNG 公共实例不可用/限流 | gateway 形态是团队默认；searxng 不可达时明确报错不静默；429 已在内核可重试清单（既有经验） |
| 5 | discard 误丢工作 | 强制前置检查点 + 确认弹层列文件清单；测试含「禁用快照变红」反向验证 |
| 6 | push 到错误远端/覆盖他人提交 | 无 `--force`；push 弹层显示 remote/分支/ahead；无上游不自动 `set-upstream` |
| 7 | workspace ≠ repo 根时路径语义混乱 | PathGuard 收紧到 workspace；文档注明「子目录场景仅操作 workspace 内文件」 |
| 8 | git 环境变量泄露（credential helper） | 全部写操作沿用 `scrub_env()`（与既有 `_git` 同口径） |
| 9 | 策略字段新增导致旧客户端/服务端版本不匹配 | `validate_document` 对未知字段忽略（既有行为）；`allowWeb` 缺失 = 默认 False（保守），旧服务端下新客户端不会意外开联网 |

---

## 6. 落地文件清单（预估）

| 模块 | 文件 | 动作 |
|---|---|---|
| agent-core | `agent_core/tools/web.py` | 新建：两工具 + SSRF 防护 + HTML 转文本 |
| agent-core | `agent_core/tools/__init__.py` | `build_default_registry` 加 `web` 开关 |
| agent-core | `agent_core/config.py` + `.env.example` | A1 九个字段 |
| agent-core | `agent_core/gitinfo.py` | B2 八个写函数 |
| agent-core | `agent_core/host/server.py` + `host/protocol.py` | B3 七个端点 + A6 设置字段 |
| agent-core | `agent_core/host/ui/app.js` / `index.html` / `styles.css` | Git 面板双栏化 + 设置面板新段 |
| agent-core | `agent_core/governance/vocabulary.py` | 客户端侧新事件规则 |
| agent-core | `packages/agent-core/tests/` | web 工具、gitinfo 写、host 端点三组新测试文件 |
| server | `vibe_server/policy.py` | `allowWeb` / `webDeniedHosts` + 合并算子 + `_CLIENT_KEY_MAP` |
| server | `vibe_server/gateway/`（search 中转）+ `redaction.py` | A5 端点 + 服务端事件 |
| server | `tests/test_chain_compat.py` + 策略/网关新测试 | 三重同步 + 新字段用例 |
| docs | `README.md` 进度表、`ARCHITECTURE.md` 新 ADR-027（联网受控外发）/ ADR-028（Git 写操作=用户动作）、`.env.example`、两处 README | 结项时同步 |

---

## 7. 评审决策点（需要拍板的取舍）

1. **搜索上游 V1 只做 SearXNG**（gateway/searxng 两形态，Bing 等公网商业
   搜索留 V2）——接受与否？
2. **fetch 直连不过网关**（理由见 §1）——接受与否？
3. **Git V1 动作集**：stage/unstage/discard/commit/push/pull --ff-only/
   分支创建切换；merge/rebase/stash/tag 明确不做——范围认可与否？
4. **不给 Agent 加 git 工具**（保持 `git_diff` 只读）—— 认可与否？若希望
   Agent 也能「提交」，需另立方案（涉及 ADR-004 红线边界，影响面大）。

---

## 8. 实测（完工补记，2026-10-14）

**Part A（受控联网检索）**

- 工具：`agent_core/tools/web.py`（`web_search` / `web_fetch`，恒 `Risk.REVIEW` + `wrap_untrusted`，
  SSRF 六步防护，零依赖）；注册开关 `web=config.web_enabled`，`False` 时清单逐字节不变。
- 配置：`config.py` 九字段 + `.env.example` 登记；设置面板「联网检索」段
  （`GET/POST /api/settings`，`protocol.py` 命令表已登记）。
- 治理面：策略 `allowWeb`（默认 `False`，AND）/ `webDeniedHosts`（并集）；网关
  `POST /api/v1/search`（SearXNG JSON，`VIBE_SRV_SEARCH_URL`，客户端零密钥，
  `web.search_relay` 只记 `query_digest`）。
- 测试：`tests/test_web.py`（agent-core）+ `apps/server/tests/test_search.py`（13 例）。

**Part B（Git 写操作面板）**

- `gitinfo.py` 八个写函数（固定 argv + `shell=False` + `scrub_env()`）；host 七个 POST 端点 +
  `GET /api/git/branches`；`protocol.py` 命令表已登记。
- 审计：`git.stage/unstage/commit/push/pull/branch/discard`，三重同步
  （vocabulary `_RULES` / redaction `_SCHEMA` / `test_chain_compat` `_sample_locals`）全绿；
  commit message 上传只留 digest。
- `discard` 前置检查点（`snapshot_before` + `commit(prompt_digest=)`），可从「回滚」面板 restore；
  `.vibe/` 与敏感路径拒收。
- UI：Git 面板双栏化（暂存区 / 工作区分组 + 逐行 stage/unstage/discard + 冲突标红），
  工具栏（分支下拉 + 新建 / 拉取 / 推送 / 刷新），提交条（textarea + AI 生成 + 提交）。
- 测试：`tests/test_git_write.py`（29 例 / 24 子测试），含 `file://` 裸仓库零网络 push/pull、
  钩子失败透传、discard 快照**反向验证**（临时禁用快照 → 用例变红）。

**DoD 对照**

1. ✅ `web_enabled=False` 时工具清单 / 行为 / 审计流与 M6 逐字节一致（hermetic 用例全绿）。
2. ✅ Git 面板关闭态整体隐藏工具栏与提交条（状态为空 ⟺ 不可见）。
3. ✅ 三跑道全绿：agent-core **854 通过 / 2 跳过 / 98 子测试**；服务端 **395 通过 / 25 跳过 /
   490 子测试**；桌面壳 `npm ci` + `node --check`（主进程 + ESM UI）通过。README 与本文件计数已同步。
4. ✅ `.env.example`、`protocol.py` 命令表、治理面 `apps/server/README.md` 接口清单、桌面端
   `apps/desktop/README.md` 四处同步；审计事件三重同步全绿。
5. ✅ 三套 E2E 冒烟通过：单用户 `e2e_session`、多用户 `e2e_multiuser`（6 档全绿）、治理面
   `e2e_governance`（8 档全绿，含档 2b 搜索中转可达 / 不可达）；`allowWeb` 收紧由服务端
   `test_policy.py` 覆盖。另经**真浏览器**端到端验证 Git 面板「暂存 → 提交」闭环
   （真实产生提交、暂存区徽标 0→1→0、空信息提交给 toast 提示）。

**遗留**：`E:\vibe-baseline-check` 残留目录（其 `.github/workflows/ci.yml` 被外部进程以
「禁删除」共享模式占用），需关闭相关进程后手动删除。
