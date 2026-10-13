# 集中治理面部署手册（Windows 内网单机 · 多人共用）

| 项 | 内容 |
|---|---|
| 版本 | v1.0（2026-10-13） |
| 目标形态 | **一台内网 Windows 机器**跑一个治理面进程，同事各自的本机客户端（桌面端 / CLI）连到它 |
| 部署方式 | 沿用项目定档「**拷目录 + 环境变量**」；**不落 `infra/docker-compose.yml` / K8s 清单**（切换条件见 [server README §边界](../apps/server/README.md)） |
| 对应代码 | `apps/server`（治理面，`python -m vibe_server`）、`packages/agent-core`（客户端接入） |
| 前置文档 | [服务端 README](../apps/server/README.md)（配置项 / 接口 / 安全模型）、[M4-OPS-PLAN.md](M4-OPS-PLAN.md)（监控告警 / 高可用） |

> 本手册只讲「怎么把已有的东西立起来、把同事接进来」。治理面**不存代码 / Prompt /
> 模型输出**，只存元数据与审计事件（ADR-006）；这一条不因部署形态而变。

---

## 0. 一页速览

```text
① 拷目录到服务器           apps/server 与你选的 Python
② bootstrap 建管理员       一次性，只在库为空时可用
③ 起服务（先回环自测）       python -m vibe_server serve
④ 对外：反代 TLS + 两个开关  REQUIRE_TLS=1 + TRUST_FORWARDED_FOR=1
⑤ invite 发邀请码           每个同事一枚
⑥ 同事侧接入                桌面端「治理面」弹窗 / CLI governance register
```

**一条最容易踩的坑先说**：治理面**默认只监听 `127.0.0.1`**，且**监听非回环地址时
强制要求 `REQUIRE_TLS=1`**（否则直接拒绝启动）。所以「让同事连上」的正确姿势不是
把服务裸绑到 `0.0.0.0` 明文对外，而是**在它前面放一个做 TLS 终止的反向代理**，
并打开 `REQUIRE_TLS=1` + `TRUST_FORWARDED_FOR=1`。§4 给了 Caddy / Nginx 两份样例。

---

## 1. 前置条件

| 项 | 要求 |
|---|---|
| 操作系统 | Windows 10/11 或 Windows Server（内网） |
| Python | 3.10+（治理面**零第三方依赖**，纯标准库，无需 pip 安装任何东西） |
| 网络 | 服务器有内网固定地址；同事机器能访问到反代暴露的地址 |
| 存储 | 默认 SQLite（单文件，落在数据目录）——「一台机器、几十个人」够用 |
| 磁盘 | 审计会持续增长，给数据目录留足空间（见 §8 备份） |

> **不需要**：Docker、Redis、Vault、外部数据库。这些是规模触发后才上的
> （多实例共享、滑动窗口限流、密钥集中托管），当前规模用不上，上了反而破坏
> 「拷一个目录就能跑」（ADR-009）。

---

## 2. 落地代码与数据目录

治理面**不从 PyPI 装**，而是直接把仓库里的 `apps/server` 拷到服务器：

```powershell
# 在服务器上，选一个稳定目录（示例）
$Root = "D:\vibe-governance"
New-Item -ItemType Directory -Force -Path $Root | Out-Null
# 把仓库的 apps\server\vibe_server 整个拷进来（只读代码，不动）
Copy-Item -Recurse -Force "<仓库>\apps\server\vibe_server" "$Root\vibe_server"
Copy-Item -Force "<仓库>\apps\server\scripts" "$Root\scripts" -Recurse
```

**数据目录**（库文件、`jwt.key`、默认 `secrets.json` 都落在这里）：

```
D:\vibe-governance\data\        ← 建议显式指定，别用默认的 .\vibe-governance
├─ governance.db                ← SQLite 库（用户 / 团队 / 策略 / 配额 / 审计）
├─ jwt.key                      ← 单机自动生成的签名密钥（0600）
└─ secrets.json                 ← 仅当 SECRETS_PROVIDER=file 时存在
```

> 数据目录与代码目录**分开**：升级时只换代码，不动数据；备份时只备数据。

---

## 3. 建管理员（一次性）

`bootstrap` 只在库为空时可用 —— 它是「从零起步」的一次性入口，不走 HTTP 认证。
组织建好后，加人一律走**邀请码**（§5）或 SSO。

```powershell
$env:VIBE_SRV_DATA_DIR = "D:\vibe-governance\data"
$env:VIBE_SRV_DB       = "D:\vibe-governance\data\governance.db"

python -m vibe_server bootstrap `
    --email owner@corp.example --password "Str0ng!Passw0rd" `
    --enrollment-uses 0
```

`--enrollment-uses 0` 表示**先不签发**邀请码（本手册用 `invite` 统一发，见 §5）。

---

## 4. 起服务 + 对外（反代 TLS）

### 4.1 先回环自测（不上反代）

```powershell
python -m vibe_server serve
# 输出两行机器可读信息，供脚本抓：
#   VIBE_SRV_URL=http://127.0.0.1:8787
#   VIBE_SRV_DB=D:\vibe-governance\data\governance.db
```

另开一个窗口自检：

```powershell
python -m vibe_server doctor --probe http://127.0.0.1:8787
```

`doctor` 会逐项检查存储 / 密钥托管 / 上游配置 / 策略，缺项给一行可执行建议。
**内网部署 90% 的问题在这一步就能暴露**，比事后翻日志便宜得多。

### 4.2 对外：反向代理做 TLS 终止

治理面自身**不终结 TLS**（`httpd.py` 是纯标准库 HTTP 服务）。对外时按下面两步走。

**第一步：给治理面配环境变量。** 反代在同一台机器时，治理面仍只监听回环：

```powershell
$env:VIBE_SRV_HOST              = "127.0.0.1"   # 反代在同机
$env:VIBE_SRV_PORT              = "8787"
$env:VIBE_SRV_REQUIRE_TLS       = "1"           # 声明「入口已加密」
$env:VIBE_SRV_TRUST_FORWARDED_FOR = "1"         # 采信 X-Forwarded-For / Proto
```

> 反代在**另一台**机器时，把 `VIBE_SRV_HOST` 改成 `0.0.0.0`（此时 `REQUIRE_TLS=1`
> 是**强制**的，缺它直接拒绝启动）。

两个开关的作用：
- `REQUIRE_TLS=1`：除 `/healthz`（探针豁免）外，**明文直连一律 403**；
- `TRUST_FORWARDED_FOR=1`：只有打开它，服务端才采信 `X-Forwarded-Proto: https`
  判定「本次连接是加密的」。**不打开就永远判为明文**，请求全被 403。
  （这也是安全设计：否则任何直连客户端自带一个 `X-Forwarded-Proto: https`
  就能绕过明文拒绝。）

**第二步：反代转发并带上头。** 下面是两份样例，二选一。

#### 样例 A：Caddy（自动申请/续期证书，最省事）

```caddyfile
vibe.corp.example {
    reverse_proxy 127.0.0.1:8787
}
```

Caddy 会自动：获取证书、终结 TLS、并**自动带上** `X-Forwarded-For` 与
`X-Forwarded-Proto: https` —— 与上面两个开关正好配套。

> **纯内网警告**：上面的 `vibe.corp.example` 依赖 Caddy 自动 ACME 签发，要求域名
> **公网可解析**且 **80/443 可达** —— 内网环境通常两条都不满足，会自动签发失败。
> 内网改用 Caddy 自带的**本地 CA**（`tls internal`）：
>
> ```caddyfile
> localhost:8443 {          # 换成你的内网域名 / 端口（如 vibe.corp.local:443）
>     tls internal          # 本地 CA 签发，不依赖公网
>     reverse_proxy 127.0.0.1:8787
> }
> ```
>
> 首次 `caddy run` 会**自动把本地根证书装进 Windows 信任库**（日志出现
> `certificate installed properly in windows trusts`），因此本机客户端（含 Python
> `ssl.create_default_context`）直接就能信任，无需再配 `SSL_CERT_FILE`。
> 换机器或换 Caddy 数据目录后需重新信任：`caddy trust`。
>
> 实测备注：curl（Schannel 后端）对本地 CA 会报 `CRYPT_E_NO_REVOCATION_CHECK`
> （无法检查吊销），加 `--ssl-no-revoke` 即可 —— 这是**吊销检查**问题，不是证书不受信。

#### 样例 B：Nginx（已有 Nginx/证书体系时）

```nginx
server {
    listen 443 ssl;
    server_name vibe.corp.example;

    ssl_certificate     C:/certs/vibe.crt;
    ssl_certificate_key C:/certs/vibe.key;

    location / {
        proxy_pass http://127.0.0.1:8787;

        # 这两行是 REQUIRE_TLS/TRUST_FORWARDED_FOR 生效的前提，缺一不可
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For   $remote_addr;

        proxy_set_header Host              $host;
        proxy_read_timeout 300s;   # 网关是透传的，推理请求可能很慢
    }
}

server {   # 可选：把明文 80 跳转到 443
    listen 80;
    server_name vibe.corp.example;
    return 301 https://$host$request_uri;
}
```

> Nginx **不会**自动设 `X-Forwarded-Proto`，必须像上面那样显式设，否则治理面
> 会把所有请求当明文拒绝（表现为「HTTP 403，且日志说需要 TLS」）。

### 4.3 服务化（开机自启，无需人工登录）

用 Windows 自带的**任务计划程序**，不需要装任何第三方工具。

先写一个启动脚本 `D:\vibe-governance\start.cmd`：

```bat
@echo off
set VIBE_SRV_DATA_DIR=D:\vibe-governance\data
set VIBE_SRV_DB=D:\vibe-governance\data\governance.db
set VIBE_SRV_HOST=127.0.0.1
set VIBE_SRV_PORT=8787
set VIBE_SRV_REQUIRE_TLS=1
set VIBE_SRV_TRUST_FORWARDED_FOR=1
cd /d D:\vibe-governance
"C:\Python310\python.exe" -m vibe_server serve
```

注册成开机任务（管理员 PowerShell 执行一次）：

```powershell
schtasks /Create /TN "VibeGovernance" /TR "D:\vibe-governance\start.cmd" `
    /SC ONSTART /RU "SYSTEM" /RL HIGHEST /F
schtasks /Run /TN "VibeGovernance"     # 立即跑一次
schtasks /Query /TN "VibeGovernance"   # 看状态
```

> `/RU SYSTEM` 让服务在**没人登录**时也能跑。请确保 `D:\vibe-governance\data`
> 对 `SYSTEM` 可读写（默认可以）。

---

## 5. 把同事接进来（邀请码）

组织建好后，新账号只能靠**邀请码**或 SSO。每天要用的正是这条 `invite`：

```powershell
# 管理员本人账号鉴权；一次给 3 个同事各签一枚
python -m vibe_server invite `
    --email owner@corp.example --password "Str0ng!Passw0rd" `
    --count 3
```

输出（令牌**只显示这一次**，请立即分发）：

```
已签发 3 枚注册令牌（只显示这一次，请立刻分发给同事）：

  [1] vbe_xxxxxxxxxxxxxxxx
      角色 developer，可用 1 次，过期于 1792588800
  [2] ...
  [3] ...

同事在本机执行（把 <服务地址> / <同事邮箱> 换成实际值）：
  vibe-agent governance register --server <服务地址> --email <同事邮箱> --enrollment-token <上面的令牌>
或在桌面端标题栏「治理面」→ 填地址 → 选「邀请码注册」。
```

可调参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--role` | `developer` | 新账号角色。`owner`/`team_admin` 只有组织所有者能签 |
| `--uses` | `1` | 每枚令牌可用次数（1–100）。**1 枚对应 1 个同事**，别图省事发多用途的 |
| `--count` | `1` | 一次签几枚（1–50） |
| `--ttl-hours` | 0 | 有效期（小时）；0 = 用服务端默认（`ENROLLMENT_TTL`，默认 7 天） |
| `--team-id` | 空 | 限定新账号所属团队（留空 = 默认团队） |
| `--note` | 空 | 备注，只进服务端审计 |

> **为什么必须先校验管理员口令**：`invite` 直接读写本地库。不校验的话，
> 任何能碰到数据目录的人都能量一枚令牌（还可能带角色）—— 等于把提权做成了
> 「本地文件读权限」。`bootstrap` 不校验是因为它只能在库为空时跑一次。

---

## 6. 同事侧接入

**桌面端（推荐）**：标题栏「治理面」按钮 →
- 填**治理面地址**（如 `https://vibe.corp.example`）；
- 已有账号：邮箱 + 口令 → 「登录」；
- 新同事：切到「邀请码注册」，填邮箱 + 口令 + 邀请码 → 「注册」。

登录成功后**热接入**：当前会话的模型客户端与策略闸门立即换成治理面那一套，
**不重建对话 / 检查点 / 审计链**，也无需重启。

**CLI（等价）**：

```powershell
# 新同事注册
vibe-agent governance register --server https://vibe.corp.example `
    --email alice@corp.example --enrollment-token <邀请码>
# 之后每次
vibe-agent governance login   --server https://vibe.corp.example --email alice@corp.example
vibe-agent governance status            # 看当前接入状态（= 下次启动 Agent 会发生什么）
vibe-agent governance devices           # 看已注册设备
vibe-agent governance logout            # 退出并清除本地凭证
```

凭证落在**用户级** `%USERPROFILE%\.vibe\credentials.json`（0o600）。
因此「CLI 登录一次，桌面端自动接入」是设计行为 —— 同一台机器共用一份凭证。

> 同事若不设治理面地址，客户端就是**纯本地模式**，一切照旧（服务端不可达时
> 也只是降级本地继续，治理是增强而非单点依赖）。

---

## 7. 密钥托管（可选）

网关向上游转发时要注入云端 API Key，而**密钥永远只以「引用名」出现在配置里**，
明文由服务端 `SecretStore` 解析。内网无 Vault，定档用 `file`：

```powershell
$env:VIBE_SRV_SECRETS_PROVIDER  = "file"
$env:VIBE_SRV_SECRETS_FILE      = "D:\vibe-governance\data\secrets.json"
$env:VIBE_SRV_CLOUD_UPSTREAM_KEY_REF = "cloud-main"   # 引用名，不是密钥本身
```

`secrets.json`（**权限须 600**，POSIX 上权限过宽会让服务端启动即失败）：

```json
{ "cloud-main": "sk-真实密钥写在这里" }
```

`doctor` 会检查：文件不存在 → `FAIL` 并给出创建 + `chmod 600` 的建议。

---

## 8. 数据备份与恢复

**要备份的就一个目录**：`D:\vibe-governance\data`（库 + `jwt.key` + `secrets.json`）。

> ⚠️ `jwt.key` **必须一起备份**：它是令牌的签名密钥，丢了所有已发令牌立即失效，
> 全员被迫重新登录；而 `secrets.json` 丢了则云端网关不可用。

SQLite 在写入时直接复制文件可能得到不一致的快照，**用「停服 → 拷贝 → 起服」**：

```powershell
schtasks /End /TN "VibeGovernance"                      # 停
Copy-Item -Recurse -Force "D:\vibe-governance\data" `
    "E:\backup\vibe-governance-$(Get-Date -Format yyyyMMdd)"   # 拷
schtasks /Run /TN "VibeGovernance"                      # 起
```

**恢复**：停服 → 用备份覆盖 `data` 目录 → 起服。库结构由 `init_schema` 幂等保证，
不需要手工建表。

**升级**：只替换代码目录（`vibe_server`/`scripts`），数据目录不动；
重启服务即可（建表是幂等的）。升级前建议先做一次 §8 的备份。

> **切 PostgreSQL 时**：迁移脚本是**单向、可重跑**的离线工具，服务端运行时不会
> 自动触发。见 [server README §存储](../apps/server/README.md) 与
> `scripts/migrate_sqlite_to_pg.py --dry-run`。

---

## 9. 日常运维

| 动作 | 命令 / 端点 |
|---|---|
| 存活探测 | `GET /healthz`（存储异常返回 **503**，可直接当探针） |
| 指标抓取 | `GET /metrics`（Prometheus 文本；**无用户维度**） |
| 环境自检 | `python -m vibe_server doctor --probe <url>` |
| 版本 | `GET /api/v1/version` |
| 成本看板数据 | `GET /api/v1/usage`（权限 `usage:read`） |

抓取与告警规则样例、跨实例聚合写法见 [M4-OPS-PLAN.md](M4-OPS-PLAN.md) §2。

---

## 10. 排障速查

| 症状 | 多半是 |
|---|---|
| 起不来，`配置错误` | 监听了非回环地址但没开 `REQUIRE_TLS=1`；或端口非法 |
| 同事全部 401 | **多实例**各自生成了 `jwt.key` —— 多实例必须共享 `JWT_SECRET`（单机不涉及） |
| 反代后所有请求 403 | 反代没带 `X-Forwarded-Proto: https`，或服务端没开 `TRUST_FORWARDED_FOR=1` |
| `/login` 能打开但接口 403 | 同上（`/login` 是静态页，接口走 `_is_secure` 判定） |
| 同事注册报「令牌无效/已用尽」 | 令牌已过期（超过 `ENROLLMENT_TTL`）或被别人先用掉了（`--uses` 发太宽） |
| 「明明没怎么用就超配了」 | 配额按**估算上限**预留，偏保守是刻意的（宁可估大防超发） |
| 审计上报从第二批起失败 | 检查客户端读的是不是 `eventChainHead`（不是 `chainHead`） |
| 某个新接口 4xx 且只有 `requestId` | 这是刻意的：堆栈进服务端日志，凭 `requestId` 关联 |

日志：`serve -v` 开调试；`X-Request-Id` 贯穿请求与日志，用它把「客户端看到的那次
失败」和「服务端日志里的那条记录」对上，而不用把请求内容记下来。

---

## 11. 明确没做的（边界）

- **不落 `infra/docker-compose.yml` / K8s 清单**：当前「一台机器、几十个人」用
  「拷目录 + 环境变量」已够。切换条件 = 需要多实例共享配额/限流状态，或需要
  滑动窗口限流（见 [M4-OPS-PLAN.md](M4-OPS-PLAN.md) §5.4）。
- **不做多实例**（本手册是单机形态）：多实例需要共享 PG + 统一 `JWT_SECRET` +
  TLS/来源 IP 三条硬门槛齐备（代码已就绪，见 [M4+-PLAN.md](M4+-PLAN.md)）。
- **OIDC 未对过真实 IdP**：协议层已被假 IdP 逐条测住，接公司 IdP 的登记清单见
  [server README §OIDC](../apps/server/README.md)。想用 SSO 就走它，否则走邀请码。
- **`SECRETS_PROVIDER=vault` 未验证**：内网无 Vault，以 `file` 为准。
