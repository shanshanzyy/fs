# M8 · 治理面 Web 控制台 实施方案

## 1. 背景（为什么做）

治理面（`apps/server`）的账号 / RBAC / 策略 / 配额 / 审计 / 设备 / 密钥台账 API 早已就绪，但浏览器侧**只有一个极简登录页** `GET /login`（M6-Q 阶段补的最小闭环）。也就是说：
- 管理员要建团队、发邀请码、看审计、调配额，目前只能靠 CLI 或手写 HTTP 请求；
- 老板 / 审计员这类「要全局视图但不敲命令」的角色**没有入口**。

这正对应「与 Trae 等产品的差距分析」里 ③「治理面 Web 控制台」这一项（①② 已由 M7 消化）。目标：补一个**浏览器管理控制台**，让治理面从「有 API」变成「可被非工程角色使用」，且**不改动任何现有 API 语义**。

里程碑编号：沿用序列，本项为 **M8**（M0–M7 已闭合）。

## 2. 现状与可复用点（已验证）

| 复用对象 | 位置 | 说明 |
|---|---|---|
| 静态出口范式 | `apps/server/vibe_server/api/static.py` | `register_static()` 已实现 `GET /login` + `GET /assets/{name}`：单段路径、三道穿越防护（拒 `..`/分隔符 → `resolve()` 父目录校验 → `is_file()`）、MIME 表、统一 `CSP` 常量 |
| 登录页样例 | `api/static/login.html` + `login.js` | JS 里接口路径**写成完整字面量**（如 `/api/v1/auth/login`），供 `test_static.py` 与 `routes.API` 比对防漂移 |
| Router | `apps/server/vibe_server/httpd.py` | `Router.get/post/add(template, handler, auth=..., permissions=(...))`；auth 三态 `AUTH_PUBLIC/AUTH_OPTIONAL/AUTH_REQUIRED`；`Request` 提供 `.actor/.json()/.q()/.q_int()` |
| RBAC | `apps/server/vibe_server/rbac.py` | 角色 owner/team_admin/developer/auditor/viewer；权限 `resource:action`；`Actor.scope_team()/scope_user()`、`visible_team_ids(perm)` |
| 既有 store 方法 | `store/base.py` | `list_teams()`、`list_team_members(team_id)` **已存在**，直接复用（无需新增） |
| 测试脚手架 | `apps/server/tests/support.py` | `make_config` / `serve_app` / `http_get` / `bootstrap_and_login` / `actor_of` / `owner_actor` |

**确认的缺口**：service 层无 `list_teams/list_team_members/list_enrollments`，路由层无 `GET /teams`、`GET /teams/{id}/members`、`GET /enrollments`；`enrollment_tokens` 表无列表查询方法。

## 3. 方案总览

**零构建静态控制台**（vanilla HTML/CSS/JS，无框架、无 CDN、无构建步骤，与既有登录页同范式）+ **3 个薄读接口**。全部资源同源、CSP 锁死、复用 `static.py` 的防护。三条纪律：

1. **前端隐藏 + 服务端强制**：界面按 `/auth/me` 的 `permissions` 隐藏无权面板，但真正的闸门始终是路由上的 `permissions=(...)`——前端隐藏只是体验。
2. **密钥永不回值**：控制台只展示既有元数据（`/secrets` 的 name/fingerprint/rotatedAt、`/enrollments` 不含 `token_hash`）。
3. **不改变 ADR-006 红线**：服务的是应用自带静态文件，固定目录、只读、与用户代码/prompt/模型输出无关。

## 4. 路由与文件布局

Router 只匹配**定长单段** `{name}`，故控制台资源放独立子目录、走单段路由，避免与 `login.js` 同目录混淆。

新增静态文件（均落 `apps/server/vibe_server/api/static/console/`）：
- `console.html` — 外壳；**无内联脚本 / 无 `onclick` 等内联事件**，用 `<script type="module" src="/console/assets/app.js">`
- `console.css` — 全部样式（CSP 的 `style-src 'self'` 已允许同源外链）
- `app.js` — 引导：读 `/auth/me` → 按权限建导航 → 分派到面板
- `api.js` — fetch 封装（注入 `Authorization: Bearer` + 401 单飞刷新）
- `panels.js` — 九个面板的渲染逻辑（单文件，减少文件数）

`static.py` 的 `register_static()` 内增两条路由（复用 `_read` 与同一 `CSP`）：
| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/console` | 公开 | 控制台外壳页 |
| GET | `/console/assets/{name}` | 公开 | 控制台静态资源（沿用三道穿越防护；`name` 仅单段） |

> CSP 已含 `script-src 'self'`，ES 模块与外部 JS/CSS 均可加载；**内联 `<script>` / 内联事件会被 CSP 拦掉**，所有监听一律 `addEventListener`。

## 5. 浏览器会话与鉴权

- **复用**`sessionStorage` 键 `vibe.accessToken` / `vibe.refreshToken`（与 `login.js` 同键），保证从 `/login` 跳到 `/console` 天然共享会话。
- 载入无 token → `location.replace('/login')`（可带 `?next=/console`）。
- `api.js` 统一注入 Bearer；遇 **401** 走公开的 `POST /api/v1/auth/refresh` 刷新一次并重试，失败则清 token 跳登录（令牌过期 UX）。
- 导航栏由 `GET /api/v1/auth/me`（`Actor.describe()` 已返回 `permissions`）驱动，缺权限面板隐藏/置灰。

## 6. 信息架构（面板 → 端点 → 闸门）

| 面板 | 调用的端点 | 前端闸门 |
|---|---|---|
| 概览 | `GET /healthz`、`GET /api/v1/version`、`GET /metrics`（公开）、`GET /auth/me` | 恒显示 |
| 成员与邀请码 | `GET /users`、`GET /enrollments`(新)、`POST /enrollments`、`GET /teams/{id}/members`(新) | `user:read` / `enrollment:write` |
| 团队 | `GET /teams`(新)、`POST /teams`、`POST/DELETE /teams/{id}/members` | `team:read` / `team:write` |
| 设备 | `GET /devices`、`POST /devices/{id}/revoke` | `device:read` / `device:revoke` |
| 策略 | `GET /policy`、`GET /policy/versions`、`POST /policy` | `policy:read` / `policy:write` |
| 配额 | `GET /quota`、`GET /quota/policies`、`GET /quota/budget`、`POST /quota`、`POST /quota/budget` | `quota:read` / `quota:write` |
| 审计 | `GET /audit/events`、`GET /audit/summary`、`POST /audit/verify`、`GET /audit/schema` | `audit:read` |
| 用量 | `GET /usage`（`?userId/teamId/days`） | `usage:read` |
| 密钥台账 | `GET /secrets`（只读） | `secret:read` |

**降级矩阵**：owner 全显；team_admin 无 `secret:*`/`user:write` → 隐藏密钥、成员只读同团队；developer → 概览 + 自己用量/配额 + 策略只读 + 自己设备；auditor → 全只读面板；viewer → 仅用量/配额/策略只读。

## 7. 新增服务端端点与 store/service 方法

**store 层**（`store/base.py` + 实现 `store/sqlite.py` / `store/pg.py`）：
- 新增 `list_enrollments(self, *, team_id: str = "") -> list[Row]` —— `SELECT id,team_id,role,created_by,created_at,expires_at,max_uses,uses,revoked_at FROM enrollment_tokens`（**永不含 `token_hash`**；`team_id` 非空则按其过滤）。
- `list_teams()` / `list_team_members(team_id)` **已存在**，直接复用。

**service 层**（`auth/service.py`）新增三个薄方法，各含 `require_permission` + 作用域过滤：
- `list_teams(*, actor)` → `require_permission(PERM_TEAM_READ)`；非 org-wide 按 `actor.team_ids` 过滤。
- `list_team_members(*, actor, team_id)` → `actor.scope_team(team_id, PERM_TEAM_READ)`。
- `list_enrollments(*, actor, team_id="")` → `require_permission(PERM_ENROLLMENT_WRITE)`；非 org-wide 按 `visible_team_ids(PERM_ENROLLMENT_WRITE)` 过滤。

**路由层**（`api/routes.py::register_admin`）新增三条：
- `GET /api/v1/teams` `permissions=(PERM_TEAM_READ,)` → `{"teams":[{id,slug,name,createdAt,memberCount}]}`
- `GET /api/v1/teams/{teamId}/members` `permissions=(PERM_TEAM_READ,)` → `{"members":[{userId,email,displayName,role,status,createdAt}]}`
- `GET /api/v1/enrollments` `permissions=(PERM_ENROLLMENT_WRITE,)` → `{"enrollments":[{id,teamId,role,createdBy,createdAt,expiresAt,maxUses,uses,revoked}]}`

> `GET /auth/me` 已回 `teams`，无需再加 user-detail 接口。

## 8. 任务分解

- **Part A 服务端补口**：`a1` store `list_enrollments`（base+sqlite+pg）→ `a2` service 三方法 → `a3` 三新路由 → `a4` 后端用例。
- **Part B 静态控制台**：`b1` `static.py` 增 `/console` + `/console/assets/{name}` → `b2` `console.html` → `b3` `console.css` → `b4` `api.js`（会话/刷新）→ `b5` `app.js`（导航 + RBAC 显隐）→ `b6` `panels.js`（九面板）→ `b7` 前端契约用例。
- **Part C 文档**：`c1` `apps/server/README.md` → `c2` 根 `README.md` 增 M8 行 → `c3` `docs/ARCHITECTURE.md` 增 ADR-029 + 落盘 `docs/M8-PLAN.md`。

## 9. 测试

新增 `apps/server/tests/test_console.py`（仿 `test_static.py`：钉路由模板 / CSP / 穿越 / `routes.API` 防漂移；docstring 声明用例数，用 `subTest`）。约 16 例：
1. `GET /console` 200 + `text/html` + CSP 含 `default-src 'self'`；
2. `GET /console/assets/app.js` 200 + `javascript` + CSP；
3. `/console/assets/..%2fpyproject.toml` → 403（穿越）；
4. `/console/assets/nope.js` → 404；
5. `/console` / `/console/assets/app.js` 未带 token 仍 200（公开）；
6. 与 `/login`、`/assets/login.js` 无路由冲突，各自 resolve；
7. `app.js`/`api.js` 内字面量含 `'/api/v1/auth/me'`、`'/api/v1/auth/refresh'`（防漂移）；
8. `console.html` 不含 `onclick=` 与内联 `<script>`（防 CSP 违规）；
9. `GET /teams` 无 token → 401；带 `team:read` → 200；
10. `GET /teams/{id}/members` 跨团队 → 403（`scope_team`）；
11. `GET /enrollments` 以 developer 身份 → 403；
12. `GET /enrollments` 以 owner → 200 且响应**不含 `tokenHash`**；
13. `GET /teams` 非 org-wide 只回本团队；
14. 控制台脚本以 `fetch` + `Authorization` 头访问（文本断言）；
15. `api.js` 含 401 → refresh 单飞重试逻辑（文本断言）；
16. `sessionStorage` 使用 `vibe.accessToken` 键（与 login.js 一致）。

另：`GET /teams` / `GET /enrollments` 的 store/service 用例并入现有 `test_store_contract.py` / 新端点用例，据实计入总数。

**计数同步**：`apps/server/tests` 总用例数与 `apps/server/README.md` 用例表（新增 `test_console.py` 行）同步更新。

## 10. 文档同步

- `apps/server/README.md`：扩展「静态页」小节（新增 `/console`、`/console/assets/{name}` 路由行）；「接口清单 · 用户与团队」补三条新接口；用例表 + 总数更新。
- 根 `README.md`：进度表新增 **M8** 行，链到 `docs/M8-PLAN.md`。
- `docs/ARCHITECTURE.md`：新增 **ADR-029「治理面 Web 控制台」**，固化五项决策（零构建 / 同源 / CSP 锁死 / 前端隐藏+服务端强制 / 密钥永不回值），交叉引用 ADR-006；「M0–M8 演进路线」提一句。

## 11. 风险与边界

- **CSP 违规**：禁内联事件 / 内联脚本，监听一律 `addEventListener`（用例 8 钉死）。
- **路径穿越**：新资源路由复用逐段校验（用例 2–4 钉死）。
- **XSS**：即便审计是纯元数据（ADR-006），一律 `textContent` 渲染，**禁用 `innerHTML` 拼接服务端数据**；`/metrics` 文本只截断展示、不注入 DOM。
- **大列表**：`/audit/events`、`/usage`、`/users` 走 `limit/offset` 分页（默认 100），前端「加载更多」。
- **令牌过期**：401 → 单飞 refresh 重试一次，失败跳登录（用例 15）。
- **明确不做**：不做 CSR/SSR 框架、不做实时推送（轮询即可）、不做 i18n、不新增写业务数据的 API（仅补三条读接口）。

## 12. 验证方式（端到端）

1. 单测：`cd apps/server && python -m pytest tests/test_console.py -q`，再跑全量 `python -m pytest -q`（三跑道之 server 跑道），核对总数与跳过数。
2. 本地起服务：`python -m vibe_server --data-dir <tmp> bootstrap` 建 owner → `python -m vibe_server serve`；
3. 浏览器打开 `http://127.0.0.1:8787/console`：未登录自动跳 `/login`；登录后逐面板验证——概览、成员（发一枚邀请码）、团队（建团队+加成员）、设备、策略（读+发布）、配额（读写）、审计（查询/摘要/校验）、用量、密钥台账；确认无 CSP 控制台报错、越权面板按角色隐藏。
4. 用 developer 账号复登，确认团队/成员/审计/密钥面板隐藏，直连 `GET /enrollments` 得 403。
5. 确认 `/console/assets/..%2f…` 返回 403。
