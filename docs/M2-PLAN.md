# M2 治理面 —— 收尾与验收方案

| 项 | 内容 |
|---|---|
| 版本 | v1.1（E1–E4 已闭合；G4/G5 已定档） |
| 日期 | 2026-09-24（收尾更新 2026-09-28） |
| 对应 | `docs/ARCHITECTURE.md` §6「M2 治理」 |
| 覆盖 | 云端路由、出站脱敏、SSO·RBAC·配额 |
| 定位 | **收尾方案**：主体代码已在仓库，本文只覆盖尚未闭合的缺口 |
| 上游文档 | `docs/ARCHITECTURE.md`（ADR-006/007/009/010、§11.2）、`apps/server/README.md` |

---

## 0. 先纠正一个前提：M2 主体已经落地

盘点结论与「M2 还没开工」的印象不同 —— 四项能力的代码、测试与文档都在仓库里，
而且客户端已经接入启动路径。所以本文**不是从零开始的实施计划，而是收尾计划**：
写清楚「哪四项已经算完成、完成到什么程度」，再列出**真正还没闭合的缺口**。

| 能力 | 状态 | 证据位置 |
|---|---|---|
| 云端路由 + 降级链 | ✅ 已落地 | `apps/server/vibe_server/gateway/routing.py`（`decide()` 四层判定）、`gateway/proxy.py`（cloud→local 降级）；11 条测试 |
| 出站脱敏 | ✅ 已落地（**拦截式**） | `routing.py:scan_text()` 扫**实际外发正文**（不信任客户端申报），命中即强制本地；`tests/test_routing.py::OutboundScanTests` |
| SSO / 认证 | ✅ 已落地（**未对真实 IdP 验过**） | `auth/service.py`（口令哈希、access/refresh、refresh 轮换 + 重放检测）、`auth/oidc.py`（PKCE + state + nonce）；14 条测试 |
| RBAC + 作用域 | ✅ 已落地 | `rbac.py`：5 角色 / 17 权限，`scope_team()` / `scope_user()` 显式核对；`tests/test_policy.py` 覆盖 |
| 配额 | ✅ 已落地 | `gateway/quota.py`（预留-结算）、`GET /api/v1/quota`、`usage_summary()`；31 条测试 |
| 策略下发 | ✅ 已落地（只收紧） | `policy.py` + 客户端 `governance/policy.py`；`tests/test_policy.py` 43 条 |
| 审计上报 + 脱敏契约 | ✅ 已落地 | 服务端 `redaction.py` 三道闸门 + 哈希链；客户端 `governance/audit_upload.py` |
| 客户端接入 | ✅ 已接线 | `agent_core/governance/runtime.py:wire()` 是唯一装配入口，CLI 与桌面端后端都调它；`tests/test_governance_wiring.py` 16 条 |

**测试基线**：agent-core 286 条（14 个文件）、服务端 235 条（11 个文件）。
**收口时**：agent-core **303** 条（跳过 1 条）、服务端 **261** 条（跳过 2 条为既有的
Windows `echo` 平台缺口，与本次改动无关），见 §5 验收口径。
**此后**：W1 的目录浏览器又补了 5 条用例（见 `docs/M1.5-PLAN.md` §7），agent-core
全量现为 **308** 条 —— 下文凡写「303/261 全绿」处同此口径。

> 因此本方案里**没有**「实现 LLM 网关」「实现 RBAC」这类任务 —— 那些已经做完了，
> 重写一遍只会把已经钉住的语义打散。

---

## 1. 缺口清单（本文的全部工作范围）

| # | 缺口 | 现状证据 | 影响 | 状态 |
|---|---|---|---|---|
| G1 | **配额在客户端不可见** | `GET /api/v1/quota` 已存在（`api/routes.py`），但客户端无消费者；界面只有一个治理状态徽标（`host/ui/app.js`），无剩余额度 | 用户被 429 打回本地时完全不知道为什么 | ✅ E1 已闭合 |
| G2 | **治理链路没有端到端冒烟** | `scripts/` 只有 `e2e_session.py` / `e2e_desktop.py` / `e2e_package.py` | 「登录→策略→网关→审计上报」整链只在单测里拼装过，没在真进程上跑过 | ✅ E2 已闭合 |
| G3 | **OIDC 未对真实 IdP 打通，且无专属测试** | `apps/server/README.md` §边界自述；`apps/server/tests/` 下**没有** `test_oidc.py` | SSO 是「团队可用」的入口功能，却没有任何东西钉住它 | ✅ E3 已闭合 |
| G4 | **密钥托管只有 `env` 被真跑过** | `gateway/secrets.py` 有 `env`/`file`/`vault` 三种；测试只覆盖了前两者的一部分 | `SECRETS_PROVIDER=vault` 是文档里的能力、实际未验证 | ✅ E4 **已定档**（无 Vault 分支）：`file` 分支钉住，`vault` 标未验证 |
| G5 | **部署形态未落盘** | `infra/` 目录不存在（`ARCHITECTURE.md` §8 曾把 `infra/docker-compose.yml` 列为 M2 交付） | 内网部署目前靠「拷目录 + 手配环境变量」，没有可复制的形态 | ✅ E4 **已定档**：不落 compose，沿用拷目录；切换条件见 README §边界 |

> 五条缺口均已闭合或定档（E1–E4，逐项见 §2 与 `docs/ARCHITECTURE.md` §6「M2 收尾补记」）。
> 「已定档」= 结论与切换条件已写进文档并落到代码/测试；不等于相关能力已被真跑过。

**不在本文范围**：多实例前置条件（共享 `JWT_SECRET` / 换 PG / 限流共享）已由
`docs/M4-OPS-PLAN.md` §5 记录并给了切换触发条件，不在此重复。

---

## 2. 任务拆分

> 每个任务给出：改动位置、实现要点、验收标准。
> 通用约束：不引入第三方依赖（内网离线可跑）；审计只记长度与摘要（ADR-006）；
> 不配 `VIBE_SERVER_URL` 时行为必须一行不变。

### E1 配额可视（G1，P0）

| 子项 | 内容 |
|---|---|
| E1.1 | `governance/client.py`：新增 `quota()`，打 `GET /api/v1/quota` |
| E1.2 | `governance/runtime.py`：`wire()` 装配时拉一次配额，写进 `WireResult`；失败**不阻断**启动，只降级为「未知」 |
| E1.3 | `host/session.py`：`/api/config` 的 `governance` 段增加 `quota` 字段 |
| E1.4 | `host/ui/app.js`：治理徽标的 tooltip 展示「今日云端 token 剩余 / RPM」；429 触发降级时追加一条 notice |
| E1.5 | `tests/test_governance_wiring.py`：钉住「配额拉取失败不影响启动」「不配 server_url 时 `quota` 字段为空」 |

**验收**：
- mock 服务端返回配额 → 界面 tooltip 显示剩余额度；
- 配额用尽 → 徽标状态变化 + 一行 notice 说明「已降级到本地模型」；
- 未配 `VIBE_SERVER_URL` → 界面与响应**与今天逐字节一致**（303/261 全绿）。

### E2 治理链路端到端冒烟（G2，P0）

新增 `packages/agent-core/scripts/e2e_governance.py`，一条命令跑完整链：

1. 临时目录起 `vibe_server`（`PORT=0` 由系统分配）+ `mock_model_server.py` 当上游；
2. `bootstrap` 建组织 + 签发注册令牌 → CLI `governance register` / `login`；
3. 发布策略两轮：`allowCloud=false`（断言请求留在本地）与 `allowCloud=true` + 重任务（断言走云端）；
4. 网关**非流式与流式各一轮**，断言两端聚合结果一致；
5. 发一条含私钥样式的正文，断言被 `scan_text` 压回本地；
6. 审计上报一批，断言链头连续、且**载荷只有键白名单字段**（无正文）；
7. 退出时按进程树清理。

**验收**：脚本退出码 0；退出后无残留 `vibe_server` / 后端进程（沿用 `e2e_package.py` 的进程快照比对做法）。

### E3 OIDC 真实化（G3，P1）

| 子项 | 内容 |
|---|---|
| E3.1 | `apps/server/tests/test_oidc.py`：用标准库 `http.server` 起一个**假 IdP**（JWKS + token 端点），走 begin → callback → 拿令牌 → `/auth/me` |
| E3.2 | 钉住三条负面语义：state 重放被拒、未配置 OIDC 时 `begin` 返回 404、claim→角色映射缺字段时按最小权限（`developer`） |
| E3.3 | `apps/server/README.md`：补「对接公司 IdP 需要填什么」（issuer / client_id / redirect_uri / claim 映射表） |

**验收**：假 IdP 上整流程通；三条负面用例全绿；README 里 IdP 参数表与代码字段一一对应。

### E4 密钥托管与部署形态定档（G4 + G5，P2） —— ✅ 已定档（无 Vault 分支）

两个缺口都要先问运维再动手，所以做的是「定档」而不是直接实现：

| 分支 | 触发条件 | 动作 |
|---|---|---|
| 内网**有** Vault | 运维确认已部署 | 补 `secrets.py` 的 vault 分支测试（同样用假 Vault HTTP 服务），`infra/` 落 `docker-compose.yml` |
| 内网**没有** Vault | 运维说没有 | 只补 `file` provider 的测试与文档，把 vault 明确标为「未验证，切换条件：接入 Vault 时」 |

**已定档：走「没有 Vault」这一支**（运维确认内网未部署 HashiCorp Vault）。

- **密钥托管**：以 `env`（默认）/ `file` 为准。`file` 分支新增
  `apps/server/tests/test_secrets.py`（19 条：读取 / 轮换 `reload` / **惰性解析**的
  结构校验 / 权限过宽启动即失败 / 明文不进 `repr` 与台账 / `doctor` 集成），
  并在 `scripts/e2e_governance.py` 里补**真进程**验证 —— 服务端以
  `SECRETS_PROVIDER=file` 启动，一枚只写在临时 `secrets.json` 的哨兵密钥必须
  原样出现在云端上游的 `Authorization: Bearer` 上（假模型 `--record-headers` 记头比对，
  已反向验证过：故意不发该头时冒烟确实变红）。
  `vault` 分支**未验证** —— 只钉住配置闸门（缺 `VAULT_ADDR`/`VAULT_TOKEN` 启动前失败），
  不写假装通过的协议层测试；**切换条件：接入 Vault 时**补假 Vault 服务再解除标记。
- **部署形态**：**不落 `infra/docker-compose.yml`**，沿用「拷目录 + 手配环境变量」。
  切换条件见 `docs/M4-OPS-PLAN.md` §5（多实例共享配额/限流时才落 compose/K8s）。
- 结论写进 `apps/server/README.md` §边界；配置表把 `vault` 标为「未验证」。

**验收**：与运维确认后二选一，结论写进 `apps/server/README.md` §边界；
无论选哪条，`doctor` 对缺失配置必须给出 FAIL + 一行可执行建议。

> 已满足：`doctor` 的密钥托管缺配置会给 `[FAIL]`，且错误原文自带
> 「修复：创建它并 `chmod 600`，或改用 `SECRETS_PROVIDER=env`」，由
> `test_secrets.py::DoctorSecretTests` 单测钉住，并在 `e2e_governance.py`
> 里用**真进程 doctor** 再验一遍（有效配置报 `[OK ]` 且只报数量；缺文件
> 非零退出且打印修复建议）。

**推荐**：先按「没有 Vault」这一支收口。理由是当前规模（一台机器、几十人），
`file` + 目录权限已能覆盖，而落了 compose 反而会把「拷一个目录就能跑」
变成「先起两个中间件」—— 这正是 ADR-009 要避免的事。

---

## 3. 明确不做（守住边界）

- ❌ **不做出站「掩码改写」**。现状是**拦截式**：命中即强制本地。
  改成掩码会引入一种更糟的状态 —— 「改写过、但确实发出去了」，
  而且改写后的 prompt 不可复现、排查时对不上。
  拦截式已经满足 `ARCHITECTURE.md` §11.2 第 5 条（「拦截疑似密钥外发」）。
- ❌ 不自建 IdP / 不引入 OAuth 库（标准库够）。
- ❌ 不迁 PG / Redis、不落 K8s（M4 已判，见 `docs/M4-OPS-PLAN.md` §5/§6）。
- ❌ 不做多 Agent / Repo Wiki / 向量检索（M3）。
- ❌ 不做多租户与计费（M4 之后再说）。

---

## 4. 风险与取舍

| 风险 | 缓解 |
|---|---|
| E1 引入「配额未知」这一新状态，容易被误读为「不限」 | 界面上三态要分开：**已知有额度 / 未知 / 已用尽**，未知时文案明确写「未能获取配额」 |
| E2 的假上游会掩盖真实推理端的兼容问题 | 脚本只验**链路**，不验模型效果；推理端兼容由既有 `mock_model_server.py` 单测覆盖 |
| E3 假 IdP 与真实 IdP 行为差异 | 只覆盖协议层（PKCE/state/nonce/claim 映射），差异项写进 README 的「对接清单」 |
| G4/G5 取决于运维答复，可能一直悬着 | 任务设计成「定档」即可交付：结论 + 文档 + `doctor` 行为，不依赖对方给环境 |

---

## 5. 验收口径：M2 收尾算完成的标准

1. agent-core 全量回归全绿（收口时 **303** 条，跳过 1 条，原基线 286）；
2. 服务端全量回归全绿（收口时 **261** 条，原基线 235；跳过 2 条，是既有的
   Windows 平台缺口 —— `echo` 不是可执行文件，与本次改动无关）；
3. `scripts/e2e_governance.py` 一条命令跑通，且退出后无残留进程；
4. 「不配 `VIBE_SERVER_URL` 时行为一行未变」有测试钉住；
5. G4/G5 给出定档结论并写进 `apps/server/README.md`；
6. `docs/ARCHITECTURE.md` §6 的 M2 落地表补上本节四条任务的实际交付。

---

## 6. 后续事项（不属于 M2 收尾）

### W1 Web 界面内的「打开文件夹作为工作区」入口 —— ✅ 已实现

| 项 | 内容 |
|---|---|
| 原现状 | 桌面壳已有菜单「文件 → 切换工作区…」（`apps/desktop/src/main.js`，Ctrl+O：`showOpenDialog` → 写配置 → 重启 → `app.relaunch`）；**浏览器预览里看不到这个入口**，因为 host 侧没有任何工作区 API / 界面 |
| 为什么不能原地热切换 | `Config.workspace` 在启动时就绑定了 PathGuard、检查点目录与审计哈希链（`<workspace>/.vibe/audit.jsonl`）。原地改一个字段会让「审计链已推进」和「jail 已换根」两个事实互相矛盾 |
| 做法（已落地） | host 侧提供工作区接口，选完后**优雅重启一套上下文**（重建 Session / PathGuard / 检查点 / 审计链），桌面壳与浏览器共用同一条路径 |
| 验收（已通过） | 浏览器里能选目录并切换；切换后文件树、会话、审计链都指向新工作区；切换前有明确的「会重启后端」提示 |

**已定决策**（与用户对齐）：

1. **会话列表范围：仅当前工作区** —— 沿用 A2 现状，同一个 `~/.vibe/sessions.db` 按
   `workspace` 字段过滤；审计链本来就是每工作区一条，不做跨工作区合并。
2. **选目录方式：路径输入 + 最近列表** —— 不做内置目录浏览接口；最近列表来自
   `SessionStore.list_workspaces()`（按最近活跃倒序，天然过滤掉已不存在的目录），
   桌面壳与浏览器共用同一份真源。
3. **桌面壳统一走 host 接口** —— 菜单保留原生对话框选目录，选完写配置并调
   `POST /api/workspaces/open`；失败时回退到原来的 `app.relaunch` 重启路径。

**实现要点**：

- `GET /api/workspaces`：返回 `{current, workspaces:[{path, updatedAt, current}]}`。
- `GET /api/fs/dirs?path=…`（**后补**）：内置目录浏览器的只读列目录接口，返回
  `{path, parent, dirs:[{name, path}]}`；空 `path` 返回 `{roots, home}`。它是全仓唯一
  故意不过 `PathGuard` 的读路径（服务的就是「挑 jail 之外的目录」），边界靠 host 闸门
  （仅本机 + token + Host/Origin）+ 输入校验兜住，且只回传目录名、不读文件内容。
- `POST /api/workspaces/open {path}`：空闲时用 `replace(config, workspace=…, session_id=…)`
  造新 Config，再构造新 `Session`（PathGuard / CheckpointManager / AuditLog 随 config 重建）。
  **刻意复用同一条 EventBus**，切换后由一条 `session.ready` 把新全貌推给所有已连接界面
  （多标签页一起刷新，SSE 不掉线）。任务执行中（`state != idle`）返回 409 拒绝切换。
- 旧会话收尾：切换前 `end_session("workspace_switch")`，在**旧**工作区的链上留一条
  `session.end`；新会话在**新**工作区的链上重新 `session.start` —— 两条链各自完整、互不污染。
- 界面：标题栏工作区路径做成按钮 `#tb-workspace`，点击弹出 `#ws-modal`，内含
  「切换会**重启后端上下文**」提示、最近列表、路径输入框、内置目录浏览器
  （`#ws-dirs` / `#ws-cwd` / `#btn-ws-up`，可逐级进出并回到盘符列表）。
- `HostServer.session` 改为取属性（委托 `ctx.session`），避免切换后宿主侧还留着旧会话引用。