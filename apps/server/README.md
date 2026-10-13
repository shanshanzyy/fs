# vibe-governance —— 服务端治理面（M2）

团队要用的那一层：**认证 / 授权 / 模型网关 / 配额 / 策略下发 / 审计**。

它**不是**代码代理。代码永远不出客户端：服务端拿不到你的源码、Prompt、
模型输出，只有元数据与审计事件（见 [ADR-006](../docs/ARCHITECTURE.md)）。
这一条不是口号，是 `redaction.py` 里三道会**拒收整条事件**的闸门。

- **零第三方依赖**：纯标准库，内网离线可跑。`python -m vibe_server` 就能起。
- **默认安全**：没配策略时是「保守默认」（不允许上云、不允许自动审批），
  而不是「先放开、等管理员想起来去收」。

---

## 30 秒上手

```bash
cd apps/server

# 1) 建第一个组织所有者（顺便签一枚注册令牌，用来把开发者拉进来）
python -m vibe_server bootstrap \
  --email owner@corp.example --password 'Str0ng!Passw0rd'

# 2) 起服务（默认 127.0.0.1:8787，数据落到 ./.vibe-governance）
python -m vibe_server serve
```

`serve` 会打印两行机器可读的输出，方便脚本抓：

```
VIBE_SRV_URL=http://127.0.0.1:8787
VIBE_SRV_DB=/path/to/.vibe-governance/governance.db
```

### 自检

内网部署最常见的失败不是代码错，而是「环境少配了一件事」，
而那种问题表现为服务起来之后莫名其妙的 4xx/5xx。把检查前置：

```bash
python -m vibe_server doctor --probe http://127.0.0.1:8787
```

四项：存储 / 密钥托管 / 上游配置 / 策略。缺全局策略会 `FAIL` 并给出
一行可执行建议。**在启动脚本里跑一次 doctor，比事后翻日志便宜得多。**

> **多实例意图下会多出第五项「多实例」**（统一密钥 / 共享存储 / TLS+来源 IP 是否就绪）；
> 单机（不设 `VIBE_SRV_MULTI_INSTANCE`、`DB` 非 `postgres://`）**不打印该项**，输出与改造前一致。

### 子命令一览

| 命令 | 作用 |
|---|---|
| `serve`（默认） | 起服务 |
| `bootstrap --email --password [--name] [--enrollment-uses N]` | 建首个组织所有者 + 签发注册令牌（`--enrollment-uses 0` 跳过签发） |
| `invite --email --password [--role] [--uses] [--count] [--ttl-hours] [--team-id] [--note]` | 管理员签发注册令牌（邀请码），把同事拉进组织 |
| `doctor [--probe URL]` | 环境自检 |

`bootstrap` 只在库为空时可用 —— 它是个一次性入口，不走 HTTP 认证，
所以不能留成「任何人都能给自己开个 owner」。组织建好后**拉人靠 `invite`**：
它用管理员本人账号口令鉴权（CLI 无设备，故直接装配 actor、不走 `authenticate`），
再以 `enrollment:write` 权限签发注册令牌。同事侧拿令牌走
`vibe-agent governance register --enrollment-token <token>`（桌面端标题栏
「治理面」→ 填地址 → 选「邀请码注册」同效）。

> 集中治理面的**部署手册（Windows 内网单机 + Caddy/Nginx 反代 TLS + 服务化 + 备份恢复 + 排障）**
> 见 [`docs/DEPLOY.md`](../../docs/DEPLOY.md)。

---

## 配置

全部走环境变量，前缀 `VIBE_SRV_`。也可以 `--data-dir / --db / --host / --port` 覆盖前四项。

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATA_DIR` | `./.vibe-governance` | 数据目录（含 `jwt.key`、默认库文件） |
| `DB` | `<DATA_DIR>/governance.db` | 库路径，或 `sqlite:///...` 形式；**填 `postgres://user:pass@host:5432/db?sslmode=require` 即启用共享 PG 存储**（多实例必需，见「存储」小节） |
| `MULTI_INSTANCE` | `0` | 置 `1` 声明本部署多实例。**开启即触发三条硬门槛校验**：`JWT_SECRET` 必须显式 ≥32 字符、`DB` 必须是 `postgres://…`、`REQUIRE_TLS=1` 且 `TRUST_FORWARDED_FOR=1`，缺一即拒绝启动 |
| `HOST` / `PORT` | `127.0.0.1` / `8787` | 监听地址；`PORT=0` 由系统分配 |
| `REQUIRE_TLS` | `0` | 置 `1` 表示「TLS 已由前置反代处理」。**监听非回环地址时必须置 1**，否则直接拒绝启动 |
| `TRUST_FORWARDED_FOR` | `0` | 置于反代之后时置 1，才会采信 `X-Forwarded-For` |
| `JWT_SECRET` | 自动生成并存 `DATA_DIR/jwt.key` | 多实例部署**必须**共享同一值，否则互相不认签发的令牌 |
| `JWT_ISSUER` / `JWT_AUDIENCE` | `vibe-governance` / `vibe-agent` | 令牌的 iss / aud |
| `ACCESS_TTL` | 1800 | 访问令牌有效期（秒），即 30 分钟 |
| `REFRESH_TTL` | 30 天 | 刷新令牌有效期（秒） |
| `ENROLLMENT_TTL` | 7 天 | 注册令牌默认有效期（秒） |
| `OPEN_REGISTRATION` | `0` | 置 `1` 允许**不带邀请码**注册（落到默认团队、developer）。默认关闭 —— 内网自助起步时打开，暴露到更大范围前记得关掉并改用邀请码/SSO |
| `POLICY_REQUIRED` | `1` | 是否要求下发策略（关掉也只是回**保守默认**，不是全放开） |
| `POLICY_MAX_STALENESS` | 12h | 策略过期阈值；客户端超时未更新则进受限模式 |
| `DEFAULT_DAILY_CLOUD_TOKENS` | 200000 | 默认每日云端 token 配额（0 = 不限） |
| `DEFAULT_DAILY_REQUESTS` | 0 | 默认每日请求数配额（0 = 不限） |
| `DEFAULT_RPM` | 60 | 默认每分钟请求数上限 |
| `CLOUD_UPSTREAM_URL` / `CLOUD_UPSTREAM_KEY_REF` | 空 | 云端上游地址 + 密钥**引用名**（不是密钥本身） |
| `LOCAL_UPSTREAM_URL` / `LOCAL_UPSTREAM_KEY_REF` | 空 | 本地上游；不配则客户端直连本地模型，不经网关 |
| `UPSTREAM_TIMEOUT` | 300 | 上游超时（秒） |
| `SECRETS_PROVIDER` | `env` | 密钥托管后端：`env` / `file` / `vault`（`vault` **未验证**，见 §边界） |
| `SECRETS_FILE` | `<DATA_DIR>/secrets.json` | `file` 后端读取的文件（JSON 对象，键=引用名，值=字符串；权限须 `600`） |
| `VAULT_ADDR` / `VAULT_TOKEN` / `VAULT_MOUNT` | 空 / 空 / `secret` | `SECRETS_PROVIDER=vault` 时的 Vault 连接（**未验证**，见 §边界） |
| `SECRET_MAX_AGE_DAYS` | 90 | 密钥轮换提醒阈值（`doctor` 会报） |
| `AUDIT_MAX_BATCH_EVENTS` | 500 | 单批审计事件条数上限 |
| `AUDIT_MAX_PAYLOAD_BYTES` | 4096 | 单条事件载荷上限（超了说明有人在往里塞正文） |
| `OIDC_ISSUER` / `OIDC_CLIENT_ID` | 空 | 对接 IdP 的必填两项；**两项都非空**才启用 OIDC，否则走口令 + 注册令牌 |
| `OIDC_CLIENT_SECRET` | 空 | 机密客户端才填。留空 = 公开客户端，此时 PKCE 是唯一的兑换凭证 |
| `OIDC_REDIRECT_URI` | 空 | 必须与 IdP 上登记的**逐字节一致** |
| `OIDC_SCOPES` | `openid email profile` | 申请的 scope；`openid` 不能少 |

> 密钥永远只以**引用名**出现在策略与网关配置里（`CLOUD_UPSTREAM_KEY_REF`），
> 明文只在服务端由 SecretStore 解析。任何调用方都拿不到明文。

---

## 接口清单

前缀 `POST /api/v1`。**除标注「公开」外，全部需要 `Authorization: Bearer <accessToken>`。**

### 健康与版本

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/healthz` | 公开 | 存活探针 |
| GET | `/api/v1/version` | 公开 | 版本 |

### 认证

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/v1/auth/bootstrap` | 公开* | 建首个所有者（*库为空时才成功） |
| POST | `/api/v1/auth/register` | 公开 | 用注册令牌开通账号并登录 |
| POST | `/api/v1/auth/login` | 公开 | 口令登录，返回 access + refresh |
| POST | `/api/v1/auth/refresh` | 公开 | 刷新（**轮换**：旧 refresh 立即作废） |
| POST | `/api/v1/auth/logout` | 可选认证 | 吊销 jti / refresh（可 `allDevices`） |
| GET | `/api/v1/auth/me` | 登录即可 | 当前主体（角色 / 团队 / 权限） |
| GET | `/api/v1/auth/oidc/begin` | 公开 | 开始 OIDC 流程；未配 OIDC 时 **404** |
| POST | `/api/v1/auth/oidc/callback` | 公开 | OIDC 回调换会话（入参 `state` / `code`） |

### 静态页（登录页，Q 阶段）

服务端**唯一**的 HTML 出口。之前账号 API 早已就绪，但界面用户没有登录入口
（登录只能靠 CLI），这里补一个极简登录页把闭环接上。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/login` | 公开 | 登录页（邮箱/密码 → `POST /api/v1/auth/login`；OIDC 场景给「用 SSO 登录」） |
| GET | `/assets/{name}` | 公开 | 登录页同目录静态资源（如 `/assets/login.js`） |

要点：

- **零第三方依赖、手写渲染**：`api/static.py` 读 `api/static/` 下的文件原样返回，
  不引模板引擎（同 `httpd.py` 的取舍——内网离线装不上就别引）。
- **安全头**：页面自带收紧的 CSP（`default-src 'self'` / `connect-src 'self'` /
  `form-action 'none'` / `frame-ancestors 'none'`）；`Referrer-Policy: no-referrer`
  由 `httpd._send()` 对**每个**响应统一附加（放统一出口比放每个 handler 可靠）。
- **路径穿越防护**：`/assets/{name}` 只允许单个路径段，编码过的 `%2e%2e%2f` 会被
  Router 解码后拒掉（`tests/test_static.py` 钉住）。
- **薄壳纪律**：桌面端只在治理面「已配置但未登录/降级」时给「去登录」入口，
  指向 `<serverUrl>/login`；登录逻辑全在服务端，Electron 壳里不实现任何认证。
- 不改变「服务端不存代码」的红线（ADR-006）：这里服务的是应用自带的登录页文件，
  目录固定、只读，与用户代码 / prompt / 模型输出无关。

### 对接公司 IdP（OIDC）

#### 要给运维 / IdP 管理员什么

| IdP 需要的登记项 | 值 |
|---|---|
| 客户端类型 | 服务端 Web 应用（**机密客户端**填 `OIDC_CLIENT_SECRET`；纯公开客户端也可以，靠 PKCE） |
| 回调地址（redirect_uri） | 与 `VIBE_SRV_OIDC_REDIRECT_URI` **逐字节一致**，例如 `https://vibe.corp.example/api/v1/auth/oidc/callback` |
| 授权类型 / 响应类型 | 授权码（`code`），**不要**隐式流 |
| PKCE | **必须开启 S256**。IdP 不支持 PKCE 时不要接 —— 授权码在重定向途中可被截获，PKCE 是它单独无法兑换的唯一保证 |
| 签名算法 | `RS256`。只白名单这一个，`ES256` 与 `alg=none` 一律拒 |
| scope | `openid email profile` |

#### 服务端这边填什么

```bash
VIBE_SRV_OIDC_ISSUER=https://sso.corp.example/realms/corp
VIBE_SRV_OIDC_CLIENT_ID=vibe-governance
VIBE_SRV_OIDC_CLIENT_SECRET=<交给密钥托管，别写进 shell history>
VIBE_SRV_OIDC_REDIRECT_URI=https://vibe.corp.example/api/v1/auth/oidc/callback
```

`ISSUER` 填的是**发现文档的基地址**，服务端会去取
`<ISSUER>/.well-known/openid-configuration`，从里面拿 `authorization_endpoint` /
`token_endpoint` / `jwks_uri`。发现文档里的 `issuer` 与配置不一致时**直接拒**
（防止被指到另一个 IdP）。JWKS 缓存 1 小时，命中缓存未及时轮换时会强制刷新一次。

#### claim 映射表

| ID Token 声明 | 服务端怎么用 | 缺失时 |
|---|---|---|
| `sub` | 账号唯一标识，存成 `external_id`，**优先按它匹配账号** | 拒绝登录（没有 sub 无法建立身份） |
| `email` | 其次按它匹配账号；也是账号的展示邮箱 | 用 `<sub>@sso.invalid` 占位，并记一条审计 |
| `email_verified` | 为 `false` 时**拒绝登录** | 缺失时放行（部分纯内网 IdP 不返回该字段） |
| `name` / `preferred_username` | 显示名 | 取邮箱 `@` 前那段 |
| `nonce` | 与 begin 时下发的值比对 | 有 nonce 要求时不一致即拒 |
| `iss` / `aud` / `azp` / `exp` / `iat` | 由服务端逐项校验 | 缺 `exp` 即拒 |
| `role` / `roles` / `groups` / `vibe_role` | **完全不读** | —— |

> 最后一行是刻意的，也是本表里最要紧的一条：**SSO 只证明「你是谁」，不证明
> 「你该有多大权限」。** 角色只从治理面的成员表推导（见 §安全模型 2），
> 首次 SSO 登录建的是 `developer` + 默认团队，不是 `admin`。
> 把角色映射交给 IdP 的组声明，等于把提权做成「改一下 IdP 上的组名」就能完成的动作。

另外两个刻意的选择：**只校验 ID Token**，不拿 `access_token` 去调 userinfo
（减少对 IdP 的信任面）；[`tests/test_oidc.py`](tests/test_oidc.py) 用标准库
`http.server` 起一个**会真的拒绝**的假 IdP（自己核对 PKCE 挑战值、真做 RS256 签名），
把上述协议层语义逐条钉住。

### 用户 / 团队 / 设备 / 注册令牌

| 方法 | 路径 | 权限 |
|---|---|---|
| GET | `/api/v1/users` | `user:read` |
| POST | `/api/v1/teams` | `team:write` |
| POST | `/api/v1/teams/{teamId}/members` | `team:write` |
| POST | `/api/v1/enrollments` | `enrollment:write` |
| GET | `/api/v1/devices` | `device:read` |
| POST | `/api/v1/devices/{deviceId}/revoke` | `device:revoke` |

### 策略 / 配额

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/v1/policy` | `policy:read` | 拉取**合并后**的生效策略 + etag |
| POST | `/api/v1/policy` | `policy:write` | 发布一版（全局 / 团队） |
| GET | `/api/v1/policy/versions` | `policy:read` | 版本历史（只回摘要） |
| GET | `/api/v1/quota` | `quota:read` | 用量与余量视图 |
| GET | `/api/v1/quota/policies` | `quota:read` | 配额策略列表（自动按作用域可见性过滤） |
| POST | `/api/v1/quota` | `quota:write` | 设配额（global / team / user） |
| GET | `/api/v1/quota/budget` | `quota:read` | 读**预算策略**（成本预算 → 推导出的每日额度明细） |
| POST | `/api/v1/quota/budget` | `quota:write` | 设预算策略（global / team / user），服务端自动推导额度 |

> **配额额度来源链（ADR-025）**：`用户显式 > 团队显式 > 全局显式 > 预算推导值 >
> config 默认`。只要任一层有显式策略行就用它（**0 = 不限**，不会被推导顶掉）；
> 三层都没有时才用**预算推导值** —— 成本预算 `budget`（`unit_price>0` 时先 `/ unit_price`
> 换算成 token 预算）再 `/ window`（默认当月天数）、团队作用域再按成员数分摊，**全程向下取整**。
> `GET /api/v1/quota`（`status()`）会回 `source`（`explicit:<scope>` / `derived:<scope>` /
> `default`）与 `derived` / `budget` 明细，便于界面显示「额度从哪来」。
> `GET/POST /api/v1/quota/budget` 在服务层做**作用域校验**（读其他团队预算 → 403）。

### 模型网关

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/v1/gateway/models` | `gateway:use` | 可用模型（受策略白名单约束） |
| POST | `/api/v1/gateway/v1/chat/completions` | `gateway:use` | OpenAI 兼容代理：路由 → 配额 → 注入密钥 → 上游 |

> 客户端**永不持有**云端 API Key。密钥在网关注入，客户端只拿短期 token。

### 审计

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/v1/audit/events` | 登录即可 | 上报一批事件（带客户端声明的链头） |
| GET | `/api/v1/audit/events` | `audit:read` | 查询 |
| GET | `/api/v1/audit/summary` | `audit:read` | 汇总 |
| POST | `/api/v1/audit/verify` | `audit:read` | 校验服务端链 |
| GET | `/api/v1/audit/schema` | `audit:read` | **事件契约**：类型 → 字段 → 类型 |

### 密钥

| 方法 | 路径 | 权限 |
|---|---|---|
| GET | `/api/v1/secrets` | `secret:read` |
| POST | `/api/v1/secrets` | `secret:write` |

---

## 安全模型（六个不能省的地方）

### 1. 认证：刷新令牌轮换 + 重放检测

access token 短期（默认 30 分钟），refresh token **一次一换**。
旧 refresh 再次出现 = 有人拿到了它 → **整族作废 + 该设备作废**，
并写一条 `refresh.reuse_detected` 认证事件。
把「令牌泄漏」从「无法察觉」变成「留下轨迹的入侵信号」。

### 2. 授权：作用域是必填项，不是可选项

`require_permission(actor, "audit:read")` 只回答「这个人有没有审计读取权」；
`actor.scope_team(team_id)` 才回答「这个团队他有没有资格看」。**两者都要过。**

只检查前者是这类系统最经典的洞：角色对、主体错，
于是 A 团队管理员把 B 团队的审计翻了个遍。
所以每个涉及 team/user 的接口都必须显式声明作用域。

### 3. 策略合并：只收紧，且顺序无关

开关 AND、数值上限 MIN、拒绝清单并集、白名单交集。
团队策略**不可能**把全局的「禁止上云」翻回来。

`routing.sensitiveForceLocal` 恒为 true，文档里写 `false` 会**整份被拒收**
（不是静默忽略 —— 忽略的话，写文档的人会以为生效了）。

四种算子都满足交换律，所以合并结果与文档顺序无关，
`etag` 才稳定（否则客户端会以为自己拿到了新策略，反复重拉）。

### 4. 路由红线：不信任客户端的申报

命中敏感路径 → 强制本地，**即使策略允许上云**。但「有没有命中」不能由客户端说了算，
所以网关**额外扫描实际外发的正文**：私钥块、密钥样式串、敏感路径、
带内嵌凭据的连接串（`postgres://user:pw@host`）、凭据赋值（`password = ...`）。
扫描刻意偏保守：宁可偶尔误判把请求压回本地（慢一点），也不放过。

### 5. 审计：两条链，两个链头不共用字段名

- **事件链**：每条事件串起来，客户端拿它的头当**下一批**的起点；
- **批次链**：证明批次没有被截断或重排。

早期实现里两者共用了 `chainHead` 这个名字，导致上报从第二批起永久卡死，
症状却是「数据可能被篡改」的假告警。现在分别叫 `eventChainHead` /
`batchChainHead`，且**不提供任何** `chainHead` 别名 —— 让混用在字段层面就写不出来。

上报是**幂等**的（`UNIQUE(device_id, session_id, seq)`），重传不会重复入库，
也不会误报篡改。已入库的行**不会**被重新串链，所以客户端丢了本地水位线文件、
从头重传也不会把链写坏。

### 6. 脱敏：拒收整条，而不是裁剪字段

`redaction.py` 三道闸门：键白名单（按事件类型枚举）、值形状
（不许换行、长度上限、不许长得像代码）、危险键黑名单
（`content`/`code`/`prompt`/`diff`，以及平台自造的 `command`/`pattern`/`error`）。

任一道不过就**拒收整条事件**。裁剪是更糟的选择：它让客户端以为记下了、实际没记，
排查时对不上账。`_SCHEMA` 是契约的**唯一事实来源**，
客户端经 `/api/v1/audit/schema` 拉取，避免两端各硬编码一份名单然后漂移。

**P0-D 编辑面新增的四类事件**（`edit.complete` / `edit.rewrite.preview` /
`edit.rewrite.apply` / `edit.save`）同样遵守 ADR-006：字段只有
`path` / `prefix_digest` / `suffix_digest` / `selection_digest` / `chars` / `hunks`
/ `applied` / `total` / `added` / `removed` / `backend` / `strategy` —— **全是长度、
计数与指纹，没有任何代码正文**。客户端侧的 `vocabulary._RULES` 与服务端侧的
`_SCHEMA` 必须同步增删，两端对齐由 `test_chain_compat.py` 的双向核对钉住
（客户端产出的每个事件都必须被判类，且不得出现服务端未声明的字段）。

**P0-E 语言服务器新增的三类事件**（`tool.lsp_diagnostics` / `lsp.rename.preview` /
`lsp.rename.apply`）同样只记元数据：诊断只报 `diagnostics` / `error_count` /
`warning_count` 三个**计数**与 `server` 名，诊断正文只进模型上下文、绝不外发；
重命名只记 `path` / `line` / `char` / `files` / `edits` / `added` / `removed` /
`strategy`，新名字只落长度与指纹（`new_name_digest` / `new_name_chars`）。
重命名是**用户本人的动作**，因此不走审批浮层，但落盘前仍收检查点、运行中返回 409。

**P2-12 计划视图新增的一类事件**（`plan.toggle`）同样只记元数据：字段只有
`id` / `status` / `steps_count` / `revision` —— 步骤**正文**只留在本机内存，
服务端只回答「标注了哪一步、标成什么、计划共几步」。人工勾选计划属
**用户本人的动作**（ADR-011），不走审批浮层、运行中也不 409（不写盘）。

**P1-8 多文件原子编辑新增的一类事件**（`edit.transaction`）同样只记元数据：
字段只有 `files_count` / `changed_count` / `added` / `removed` 四个**计数**、
`strategy`（短枚举，如 `atomic`）与 `rolled_back`（布尔）—— 各文件的 diff **正文**
只进审批预览，绝不外发（ADR-006）。`changed_count` 的语义是「回滚后**仍留在盘上**的
改动数」，回滚成功时记 0，配合 `rolled_back` 共同表达「这次多文件改动是否一个字都没留下」。

**P1-9 增量索引 / 文件监听新增的一类事件**（`index.rebuild`）同样只记元数据：
字段只有 `added` / `changed` / `removed` / `files_count` / `ms` 五个**计数**与耗时 ——
变更文件的**路径与正文**一律不进审计（ADR-006）。每次后台 watcher 应用一批变更即记一条，
可用于观察「索引是否在持续跟随工作区变化」。

**P1-6 外部 MCP server 新增的两类事件**（`mcp.server_start` / `mcp.call`）同样只记元数据：
`mcp.server_start` 只有 `server` / `transport` / `ok` / `tools` / `ms`（工具**计数**与耗时）；
`mcp.call` 只有 `server` / `tool` / `ok` / `argc` / `args_digest` / `ms` —— 参数**正文**以
`args_text` 作为本地摘要源键、上传目标为 `args_digest`（**摘要**），服务端只见到
指纹与计数、拿不到任何参数原文（ADR-006）。外部工具**恒 REVIEW**、每次调用都走人工审批
（ADR-015），审计只用于回答「谁在什么时候调了哪个 server 的哪个工具、成功没有」。

**P1-7 交互式终端新增的两类事件**（`term.open` / `term.close`）同样只记元数据：
`term.open` 只有 `termId` / `argc` / `argv_digest` / `cols` / `rows` / `mode` / `ms`，
`term.close` 再加一个 `exit_code` —— shell 命令的**正文**以 `argv_digest`（摘要）上报，
服务端只见到指纹、尺寸与计数、拿不到任何命令原文（ADR-006）。终端里的命令是
**用户本人的动作**（ADR-011），不走审批浮层；但它与 Agent 通道**彻底隔离**，
Agent 仍只能走受审批的 `run_command`（ADR-016）。`term.open`/`term.close` 各记一条，
逐次击键（`/api/term/input`）**不记**，否则会把审计淹成噪音。

**P1-10 多模态图片输入新增的一类事件**（`image.upload`）同样只记元数据：
字段只有 `bytes`（字节数）/ `ext`（扩展名）/ `digest`（内容摘要）—— 图片**正文**
（base64）绝不上报，服务端只见到大小、类型与指纹（ADR-006）。图片输入默认 **`off`**
（`VIBE_VISION`），因为图片会随请求发往用户配置的模型端点、非本地时即离开本机；
`off` 时上传端点直接拒绝，`auto` 时上游不支持则自动回退为纯文本（见 ADR-018）。

**P1-11 浏览器预览 / 网页自检新增的一类事件**（`visual.check`）同样只记元数据：
字段只有 `url_digest`（页面地址摘要）/ `width` / `height` / `diff_ratio`（差异千分比）/
`baseline_missing`（是否缺基线）/ `ms` —— 截图**内容**（base64 PNG）绝不上报，服务端只见到
尺寸、差异比与摘要（ADR-006）。这里的 `diff_ratio` 是**粗粒度分块均色差**（见 ADR-019），
是「整块变了没有」的提示，不是像素级判定；`baseline_missing` 为真时前端引导「设为基线」，
服务端只如实记录，不把它当错误。

**P2-15 规则与记忆体系新增的三类事件**（`rules.load` / `memory.add` / `memory.inject`）
同样只记元数据：`rules.load` 只有 `layers` / `chars` / `truncated_count`（层数与字符计数）；
`memory.add` 只有 `scope`（短标识，如 `user`/`agent`）/ `chars` / `digest`（内容摘要）；
`memory.inject` 只有 `count` / `chars`。**规则与记忆的正文绝不上报**（ADR-006）——服务端只见到
层数、长度与指纹。记忆是**持久化注入面**：`remember` 工具恒为 REVIEW 级、写入必经人工审批
（ADR-008），模型只能提议、用户确认后才落库；注入时正文一律按不可信内容包裹（见 ADR-021）。

---

## 存储：为什么先 SQLite，怎么换驱动

目标是「一台机器、几十个人」时先落 SQLite（零外部依赖，部署 = 拷一个目录）。
但存储被抽成了 seam：

```
vibe_server/store/
├─ base.py     # Store 抽象：业务代码只认这个接口
├─ sqlite.py   # 默认驱动（单机）
├─ pg.py       # Postgres 驱动（多实例共享）—— 逐方法对齐 base.py 契约
├─ pgproto.py  # 自研 PG v3 协议客户端（**纯标准库**，不引 psycopg）
├─ migrate.py  # SQLite → PG 一次性迁移逻辑
└─ schema.py   # 两种方言的 DDL（SQLite / PostgreSQL，单一事实来源）
```

`open_store(path)` 按 `DB` 的形式选驱动：`postgres://…` → `PostgresStore`，
其余 → `SQLiteStore`。业务代码（service 层）**不 import 具体驱动**，
所以换 PostgreSQL 是「新增一个驱动文件」，不是「改所有调用方」；
**不认识的 scheme（如 `redis://`）明确报错，绝不静默退回 SQLite**。

### 什么时候该换（切换触发条件，别凭感觉换）

- 需要**多实例共享**配额/限流状态，或并发写显著上升 → 换 PostgreSQL（用法见下）；
- 需要滑动窗口 / 令牌桶限流（现在的固定窗口在单机够用） → 上 Redis（**未触发**）。

### 用 PG（多实例）—— 三条硬门槛，缺一即拒绝启动

多实例不是「起两个进程」就行：限流计数、配额预留/结算、策略版本、审计链**都在 store 上**，
不共享就会**各算一份配额、各推一条审计链**（症状：配额莫名失效、审计对不上）。
所以只要多实例，下面三条**缺一不可**，任一条不满足 `config.validate()` **直接拒绝启动**
（宁可起不来，也不让静默灾难发生）：

```bash
# 1) 统一签名密钥：多实例禁止各实例自生成 jwt.key（否则令牌互不认 → 随机掉登录）
VIBE_SRV_JWT_SECRET=<32+ 字符随机串>   # 生成：python -c "import secrets;print(secrets.token_urlsafe(48))"

# 2) 共享存储：所有实例连同一个 PG，配额/策略/审计才有唯一真相
VIBE_SRV_DB=postgres://user:pass@pg.internal:5432/vibe?sslmode=require

# 3) TLS + 真实来源 IP：多实例几乎必然在 LB/反代之后
VIBE_SRV_REQUIRE_TLS=1
VIBE_SRV_TRUST_FORWARDED_FOR=1

# 显式声明多实例意图（等价于「DB 已是 postgres://」）
VIBE_SRV_MULTI_INSTANCE=1
```

- **DSN 走 URL 形式**：`postgres://[user[:pass]@]host[:port]/db[?sslmode=…&connect_timeout=…&application_name=…]`；
  口令做百分号解码且**永不回显**（日志只用 `host:port/db`）。
- **`sslmode`**：`disable` / `allow` / `prefer`（默认）/ `require` / `verify-ca` / `verify-full`。
  `require` 及以上**必须** TLS：服务端不支持时**直接失败**，不静默降级明文。
- **认证**：`SCRAM-SHA-256`（PG 10+ 默认）与 `MD5`；遇 `AuthenticationCleartextPassword`
  **直接放弃**（宁可连不上，也不把口令明文丢到网络上）。
- **`replicas` 约束**：**换 PG 之前 `replicas` 只能是 1**；三条门槛齐备后才是真可水平扩。
- **参数化**：所有带参语句走**扩展查询协议**（`$1` 占位），**无一处字符串拼接 SQL**。
- **零依赖**：PG 客户端自研、纯标准库（`store/pgproto.py`），**不引入 `psycopg` 或任何第三方包**
  —— 保住「内网离线可跑」这条红线（见 ADR-026）。

### SQLite → PG 一次性迁移

```bash
cd apps/server
# 先计数对拍（不写目标库）
python scripts/migrate_sqlite_to_pg.py ./.vibe-governance/governance.db \
    --dsn "$VIBE_SRV_DB" --dry-run
# 确认无误后真搬（幂等，可重跑：重复执行不会写重复行）
python scripts/migrate_sqlite_to_pg.py ./.vibe-governance/governance.db --dsn "$VIBE_SRV_DB"
```

**单向、可重跑**：只做 SQLite → PG，不双向同步、不自动切读。迁移是**独立脚本**，
服务端运行时不会自动触发；搬完后自行把 `VIBE_SRV_DB` 指向 PG 重启即可。

---

## 运行测试

```bash
cd apps/server
python -m unittest discover -s tests -t .
```

383 个用例，分十九组（其中 25 条 skip —— 2 条按平台跳过（Windows 上没有可用的 `echo`），
23 条为 **PG 契约用例**：仅在设了 `VIBE_SRV_TEST_PG_DSN` 指向真实可清空的 PG 时才跑，
否则按「环境相关 skip」处理，默认 hermetic 不受影响）：

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `test_auth_tokens.py` | 15 | 令牌轮换 / 重放撤销整族 / 口令强度 / 登录不区分「用户不存在」与「密码错」 |
| `test_chain_compat.py` | 25 | 两条链头、连续三批不漂移、**从零重传不写坏链**、词汇表漂移双向核对（**P0-E 三个新事件、P2-12 的 `plan.toggle`、P1-8 的 `edit.transaction`、P1-9 的 `index.rebuild`、P1-6 的 `mcp.server_start` / `mcp.call`、P1-7 的 `term.open` / `term.close`、P1-10 的 `image.upload`、P1-11 的 `visual.check` 与 P2-15 的 `rules.load` / `memory.add` / `memory.inject` 也在此对账**） |
| `test_local_audit_wiring.py` | 7 | 用**真实代码**产出的事件全部通过服务端契约（非空断言，防自证） |
| `test_routing.py` | 11 | 敏感红线、外发正文扫描、策略只收紧 |
| `test_quota.py` | 31 | 预留-结算-释放、**20 线程并发抢配额**（TOCTOU）、跨天重置、限流、作用域 |
| `test_quota_derive.py` | 29 | 预算→每日额度**推导数学**（单价换算 / 窗口 / 成员分摊 / 向下取整 / 0 与负值）、**解析链显式恒优先**（含 `0=不限`）、推导额度被 `reserve` 真拦住、`status()` 来源标注、`GET/POST /api/v1/quota/budget` 的权限与作用域（越权 403） |
| `test_redaction.py` | 32 | 键黑/白名单、值形状拒码、摘要、整条拒收 |
| `test_policy.py` | 43 | 只收紧、**交换律（6 个排列同一 etag）**、`sensitiveForceLocal` 拒收、客户端映射、过期降级 |
| `test_cli_bootstrap.py` | 11 | CLI 首跑链路：`bootstrap` 且**用签出的令牌真的注册一个开发者**；`invite` 的权限（口令错误 / 账号不存在均返回 1）、批量签发互异、`--role`/`--uses` 语义、缺凭据返回 2 |
| `test_oidc.py` | 7 | 假 IdP 上走完 begin → callback → `/auth/me`；PKCE 与 client_secret 真被核验；state 重放 / nonce 不符 / 邮箱未验证 / IdP 自称 owner 全被拒 |
| `test_gateway_stream.py` | 16 | 流式透传 SSE（HTTP 层不再被 `stream=true` 拦下）、流式下的配额结算、`X-Vibe-Route-*` 与非流式一致、降级与失败路径 |
| `test_metrics_usage.py` | 21 | `/metrics` 的 Prometheus 文本渲染与 HTTP 暴露、`usage_summary()` 口径、端点声明 |
| `test_feature_flags.py` | 30 | 开关求值、规范化、发布合并、与策略服务接线 |
| `test_secrets.py` | 19 | 密钥托管：`file`（惰性解析、权限过宽即失败、明文不进 repr/台账、`reload` 轮换）、`env`、`vault` 只验配置闸门、`doctor` 集成 |
| `test_static.py` | 10 | 登录页（Q）：`/login` 200 + `text/html` + `Referrer-Policy: no-referrer`、CSP 头、未认证可访问、`/assets/*` 路径穿越（含 `%2e%2e%2f`）被拒、与 `/api/v1/auth/*` 无路由冲突、`login.js` 接口路径与 `routes.API` 防漂移 |
| `test_pgproto.py` | 16 | **假 PG server**（stdlib `socket`，照搬 `test_oidc.py` 假 IdP 范式）逐消息钉协议：startup + SCRAM-SHA-256 全流程、MD5 路径、**拒绝明文口令**、`ErrorResponse`→`StoreError`、**参数注入只作 `$1` 绑定值出现**、`sslmode=require` 且服务端无 TLS 时明确失败不降级、`split_statements` 与 DSN 解析 |
| `test_store_contract.py` | 23（+23 PG skip） | **Store 契约双后端对拍**：同一批断言在 SQLite 全量跑、PG（`VIBE_SRV_TEST_PG_DSN`）**同批跑**。覆盖用户 / 团队 / 成员 / bootstrap 单语句原子、项目授权、设备刷新令牌、注册令牌占用、**配额原子预留（含 24 线程并发不超发）** / 结算 / 释放、固定窗口限流、配额与预算策略、审计幂等 + 链头/会话尾/批次顺序、吊销清理、密钥台账、认证事件 |
| `test_config_multi_instance.py` | 13 | 多实例意图判定（共享 URL / 开关）、**默认单机逐字节不变**（仍自动落 `jwt.key`）、三门槛校验（缺 `JWT_SECRET` / 过短、拒 SQLite、必须 `REQUIRE_TLS`、必须 `TRUST_FORWARDED_FOR`）、`open_store` 分派（**PG 不静默退回 SQLite**、未知 scheme 报错）、`doctor` 单机无「多实例」行 / 多实例就绪时输出就绪行 |
| `test_migrate.py` | 7 | SQLite→PG 迁移：`--dry-run` 只统计源、round-trip 计数一致、**重跑幂等**、审计事件按 `seq` 顺序搬、缺表计数为 0、CLI 缺源 / 缺 DSN 返回 1 |

> 这组测试的价值不只在「通过」：写它们的时候抓出并修掉了四个真缺陷 ——
> 外发扫描漏判明文口令与带凭据的连接串、十六进制摘要只认小写（会**永久丢审计**）、
> 代码特征漏判带属性的 HTML 标签、以及**发布 `{"allowCloud": true}` 生效值却是 `false`**
> 的静默失效。前三个见对应模块的注释，第四个见 `policy.py` 的 `_normalize`。

---

## 排查

**先跑 `doctor`。** 它覆盖了内网部署 90% 的「环境问题」。

| 症状 | 多半是 |
|---|---|
| 起不来，`ConfigError` | 监听了非回环地址但 `REQUIRE_TLS=1`；或端口非法 |
| 客户端全部 401 | 多实例各自生成了 `jwt.key` —— 必须共享 `JWT_SECRET` |
| 5xx 里只有 `requestId` | 这是刻意的：堆栈进服务端日志，凭 `requestId` 关联，不把内部结构暴露给调用方 |
| 审计上报从第二批起失败 | 检查响应里读的是不是 `eventChainHead`（不是 `chainHead`） |
| 审计某类事件一条都没有 | 看 `/audit/schema` 里该类型的字段，客户端可能在被拒（拒绝原因会回给它） |
| 云端路由用不上 | `doctor` 看上游配置；再看策略里 `allowCloud` 与模型白名单 |
| 配额「明明没怎么用就超了」 | 预留是按**估算上限**占的，上限偏保守是刻意的（宁可估大，估小会超发） |

日志：`-v` 开调试。`X-Request-Id` 贯穿请求与日志，用它把「客户端看到的那次失败」
和「服务端日志里的那条记录」对上，而不用把请求内容记下来。

---

## 边界（明确没做的事）

- **已接进客户端（M3 完成）。** `agent_core/governance/runtime.py` 的 `wire()`
  是唯一装配入口，CLI 与桌面端后端都调它；`Config` 有
  `server_url` / `gateway_url` / `policy_max_staleness`（环境变量
  `VIBE_SERVER_URL` / `VIBE_GATEWAY_URL` / `VIBE_POLICY_MAX_STALENESS`），
  CLI 有 `governance login|register|status|logout|devices|upload` 子命令。
  **不配 `VIBE_SERVER_URL` 就完全是纯本地行为**，M0/M1 一行未变。
- **网关透传流式。** `proxy.py` 的 `open_stream()` 支持 SSE 直通，
  客户端 `chat_stream()` 与本地流式链路并存，不会「走网关就丢流式」。
- **JWT 用 HS256，不是 RS256。** 单服务自签自验，HS256 够了；
  代价是无法让第三方只持公钥验签。切换条件见架构文档 §6。
- **默认存储是 SQLite；多实例可切 PG（自研纯 stdlib 驱动，已落地），但限流仍不上 Redis。**
  见上「存储」小节与 ADR-026 —— PG 驱动已可跑、三条硬门槛齐备即 `replicas>1`；
  限流仍走共享库里的固定窗口计数（**未触发**换 Redis）。
- **HTTP 层是手写的，不是 FastAPI。** 为的是「离线装不上也能起」。
- **没有多租户 / 计费 / 灰度。** 那些是 M4。
- **密钥托管定档：内网没有 Vault，以 `file` 为准；`vault` 未验证。**
  运维确认内网未部署 HashiCorp Vault，所以 M2 收口按「无 Vault」这一支：
  - 实际使用 `env`（默认）或 `file`（`SECRETS_PROVIDER=file` + `SECRETS_FILE`）。
    `file` 分支有两层保障：`tests/test_secrets.py` 单测钉住（读取 / 轮换 reload /
    惰性解析的结构校验 / 权限过宽启动即失败 / 明文不进 repr 与台账）；
    `scripts/e2e_governance.py` 里再用**真进程**跑一遍 —— 服务端以
    `SECRETS_PROVIDER=file` 启动，一枚只写在临时 `secrets.json` 里的哨兵密钥
    必须原样出现在云端上游的 `Authorization: Bearer` 头上（假模型记头比对），
    并顺带断言明文密钥不落服务端日志、未配引用的上游不带 Authorization；
  - `SECRETS_PROVIDER=vault` 的代码在，但**从未对着真实 Vault 跑过**，
    只有「缺 `VAULT_ADDR`/`VAULT_TOKEN` 要在启动前失败」这条配置闸门被测试覆盖。
    **切换条件：内网接入 Vault 时** —— 届时按 `tests/test_oidc.py` 的做法补一个
    假 Vault HTTP 服务，把 KV v2 路径 / 字段取值 / LIST 降级逐条钉死后解除本条标记；
  - `doctor` 对密钥托管的缺失配置会 `FAIL` 并把**修复动作**一并打出来
    （例：文件不存在 → 提示创建它并 `chmod 600`，或改用 `SECRETS_PROVIDER=env`）。
- **部署形态定档：不落 `infra/docker-compose.yml`。** 当前规模（一台机器、几十人）
  「拷目录 + 手配环境变量」已够；起 Postgres + Redis + Vault 会把「拷一个目录就能跑」
  变成「先起两个中间件」，正是 ADR-009 要避免的。**切换条件**：需要多实例共享
  配额/限流状态，或需要滑动窗口限流时，见 `docs/M4-OPS-PLAN.md` §5
  （届时再落 compose / K8s 清单，`infra/k8s/` 同样是「只出设计、未落盘」）。
  > Q 阶段（P2-16）已复核并**维持本条定档**（决策「Q-默认」）：本轮只补前端登录页
  > （`/login`），compose 仍未落盘，属**有意为之**；触发条件同上，一旦需要多实例
  > 共享状态即落 `infra/docker-compose.yml`（单机形态：治理服务端 + 可选中间件）。
  > **M4+ 复核（2026-10-09）**：多实例的**代码前置条件已落地**（见上「存储」与 ADR-026），
  > 但 compose / K8s 清单**仍未落盘**（沿用定档）—— 本轮只把「三条硬门槛」做成可运行，
  > 真上编排再落盘。
- **OIDC 协议层已被测住，但没对过真实 IdP。** `tests/test_oidc.py` 用标准库起假 IdP，
  把 PKCE、state 一次性、nonce、RS256 验签、claim 语义逐条钉死；
  仍未验证的是**真实 IdP 的个性**（发现文档字段差异、时钟偏差、是否支持 S256、
  是否返回 `email_verified`）。对接清单见上「对接公司 IdP（OIDC）」。

> 架构层面的完整取舍（含每条偏离的切换触发条件）见
> [docs/ARCHITECTURE.md §6](../docs/ARCHITECTURE.md)。
