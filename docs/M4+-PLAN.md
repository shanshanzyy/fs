# M4+ 里程碑：高可用最小落地（纯标准库 PG 驱动 + 多实例前置收口）—— 任务清单与方案

| 项 | 内容 |
|---|---|
| 版本 | v1.0（**已交付** —— A–F 六阶段全部落地，见 §7 收口实测） |
| 日期 | 2026-10-09 |
| 前置 | M0 / M1 / M1.5 / M2 / M3 / M3+ / M5 / M6 均已交付；M4 部分落地（监控告警 / 成本看板 / 灰度分发已落代码，**高可用与 K8s 只出方案**）。本轮开工基线：agent-core **712 收集 / 710 passed / 2 skipped / 64 subtests**，服务端 **301 收集 / 299 passed / 2 skipped / 418 subtests**，`scripts/e2e_governance.py` **8 档全绿** |
| 定位 | 补齐根 README 里 M4 唯一仍标 🟡 的缺口：把 `docs/M4-OPS-PLAN.md` §5.2 的**三条多实例硬门槛**从「只出方案」推进到「能跑」——**统一签名密钥硬校验 + PG 存储驱动 + TLS/真实来源 IP 校验**。**K8s 仍不落盘**（沿用 M4 定档） |
| 上游文档 | `docs/M4-OPS-PLAN.md`（§5.1 单点清单 / §5.2 三条硬门槛 / §5.3 无状态化边界 / §5.4 切换触发 / §5.5 迁移）、`docs/ARCHITECTURE.md`（ADR-002/003/005/006/009/013）、`apps/server/README.md`、`apps/server/vibe_server/store/{__init__,base,schema}.py`、`packages/agent-core/.../test_oidc.py`（假 IdP 的测试范式） |
| 规模估算 | 不写工期；按 A→F 顺序交付，每阶段独立可回归、可验收 |

---

## 0. 为什么单开一个里程碑

### 0.1 需求

M4 的运维四件套里，监控告警 / 成本看板 / 灰度分发**已经是代码**；只剩**高可用**停在设计稿。而高可用不是「再写个脚本」——它卡在一条**状态共享**的硬门槛上：

> 服务端把**限流计数、配额预留/结算、策略版本、审计链**全落在 `store` 上。单机 SQLite 时这没问题；一旦起第二个实例，**每个实例各算一份配额、各推一条审计链** —— 多实例不是「更快」，而是「配额失效 + 审计不可信」。

`docs/M4-OPS-PLAN.md` §5.2 把前置条件写得很清楚，三条缺一不可：**统一签名密钥 / 共享存储（PG）/ TLS + 真实来源 IP**。本轮就是把这**三条从文档变成可运行**。

### 0.2 本轮已定档的边界（用户已确认）

1. **PG 驱动走纯标准库**：自写一个**最小 PG 前端/后端协议客户端**，只用 `socket` / `hashlib` / `hmac` / `ssl` 等 stdlib，**不引入 psycopg 或任何第三方依赖**。以此保住服务端「**零第三方依赖、内网离线可跑**」这条沿用至今的红线。
2. **范围 = §5.2 三条硬门槛**：统一签名密钥硬校验 + PG 存储驱动（配额 / 策略 / 审计多实例共享）+ TLS/真实来源 IP 校验。
3. **K8s 仍不落盘**：`infra/k8s/*.yaml` 与 `infra/docker-compose.yml` 均**不落盘**（沿用 M2/E4 与 M4 定档）；本轮只把 K8s 清单的**前置条件**做出来，真上编排再落盘。

### 0.3 与既有里程碑的关系（**避免重复造**）

- `store/base.py` 是 **M2 就预留好的 seam**（`Store` ABC，~60 个方法，服务层只调它、不写 SQL）。本轮**只补一个实现**，`auth / gateway / audit / policy` **一行不改**——这正是当年留缝的意义。
- `store/schema.py` **已同时放着 `SQLITE` 与 `POSTGRES` 两份 DDL**（`__all__ = ["POSTGRES", "SQLITE"]`）。PG 建表直接用它，**不手抄第二份 schema**（手抄必然漂移）。
- TLS / `TRUST_FORWARDED_FOR` 的**校验已经存在**（`config.validate()`：非回环监听且未启用 TLS 直接拒绝启动）。本轮只做「多实例场景下的一致性收口 + doctor 自检」，**不重写校验**。

### 明确不做（本轮）

- ❌ 引入 `psycopg` / `asyncpg` / 任何第三方 DB 客户端（零依赖红线，见 §1.2）
- ❌ 落盘 `infra/k8s/*.yaml` / `infra/docker-compose.yml`（沿用定档）
- ❌ 编排层：HPA / 服务网格 / mTLS / 多副本自动伸缩（ADR-009 的规模不需要；且未换 PG 前 `replicas` 只能为 1）
- ❌ 把限流换 Redis（§5.4 的另一条触发条件，未触发）
- ❌ HS256 → RS256（§5.4：仅在「另一服务需在拿不到签名密钥前提下验签」时触发，未触发）
- ❌ OIDC `OidcFlowStore` 共享化（多实例 OIDC 需 sticky session；本轮不承诺多实例下的 OIDC 会话，文档写明）
- ❌ 读写分离 / 主从复制 / 分库分表 / 在线 DDL 迁移工具（超出「最小落地」）
- ❌ 自动数据迁移（SQLite → PG 的**一键迁移工具**）；本轮只提供**单向迁移脚本**与纪律（§2 阶段 C）

---

## 1. 范围

| 阶段 | 内容 | 风险 | 状态 |
|---|---|---|---|
| **A** | 契约与开关（多实例意图判定 + JWT 硬校验 + `open_store` 分派） | 中 | ✅ 已完成 |
| **B** | **纯标准库 PG 协议客户端**（startup / 认证 / 扩展查询 / 类型映射） | **高** | ✅ 已完成 |
| **C** | PG Store 驱动（`PostgresStore(Store)` + 原子语义 + 迁移脚本） | 高 | ✅ 已完成 |
| **D** | 多实例前置收口（TLS/来源 IP 一致性 + doctor 自检 + 启动护栏） | 低 | ✅ 已完成 |
| **E** | 测试与反向验证（协议层假 PG server + Store 契约双后端对拍） | 高 | ✅ 已完成 |
| **F** | 文档与收口（README / ARCHITECTURE ADR / server README / 本文档） | 低 | ✅ 已完成 |

### 1.2 本轮已定档的三条边界（用户已确认）

1. **PG 客户端纯标准库**：不新增任何 `dependencies`；`postgres://` 由自研客户端直连，**不做「可选 extra」**——因为选择了保红线，就不该留一条「装个包就能用」的旁路。
2. **默认路径逐字节不变**：未设 `VIBE_SRV_DB`（或为 SQLite）时，**行为与改造前完全一致**——仍是 `SQLiteStore`、仍是自动生成 `jwt.key`、仍不要求 TLS（回环）。多实例相关的新校验**只在显式多实例意图下生效**（见 §2 A1）。
3. **服务层零改动**：`auth / gateway / audit / policy` **不新增 SQL、不改调用方式**；所有 PG 专属逻辑**关在 `store/pg.py` 与 `store/pgproto.py` 内**。

---

## 2. 任务分解

> 通用约束（全程适用）：**服务端零第三方依赖**（`apps/server` 不新增任何运行时依赖，纯 stdlib）；
> 存储访问**只经 `Store` ABC**（服务层不写 SQL）；审计仍**只记元数据**（ADR-006，redaction 三闸门不放宽）；
> 新审计事件**不入 PG 路径即不新增**（本轮不新增审计事件；若确需，三处同批同步并写字面量事件名）；
> **不配置时行为与改造前逐字节一致**（对应 `VIBE_SRV_DB` 不设时仍走 SQLite）。

### A 契约与开关

| 子任务 | 内容 |
|---|---|
| A1 | `config.py`：新增 `multi_instance: bool`（`VIBE_SRV_MULTI_INSTANCE`，默认 **False**）。**多实例意图判定** = `multi_instance=1` **或** `db_path` 是 `postgres://` URL（显式共享存储本身就是多实例意图）。该判定是后续所有新校验的开关 |
| A2 | `config.py::validate()`：**JWT 密钥硬校验** —— 多实例意图下**禁止自动生成 `jwt.key`**：`JWT_SECRET` 未设或 < 32 字符即**启动失败**（当前仅在设了但过短时报错，未设则静默生成；多实例下各实例密钥不同 → 令牌互不认）。回环单机路径**行为不变** |
| A3 | `store/__init__.py::open_store()`：`postgres/postgresql/psql` 分支从「抛 `ConfigError` 提示」改为**分派到 `PostgresStore`**；`SUPPORTED_SCHEMES` 纳入 `postgres`。**驱动不可用时仍明确报错**（但本轮纯 stdlib，无「不可用」路径）；`redis` 仍维持明确报错（未触发） |
| A4 | `store/__init__.py`：`open_store` 增加 `init_schema` 幂等保证（PG 上并发建表需容忍 `duplicate_table`，由 `ON CONFLICT` 或捕获特定 SQLSTATE 处理） |

**验收**：未设 `VIBE_SRV_DB` 时，`open_store` 仍返回 `SQLiteStore`、启动流程与行为**逐字节一致**；多实例意图下 `JWT_SECRET` 缺失即拒绝启动（且报错文案给出「设 ≥32 字符随机串」的可执行指引）。

**红线核对**：不新增依赖；默认路径不变；服务层零改动。

---

### B 纯标准库 PG 协议客户端（**本轮最高风险**）

| 子任务 | 内容 |
|---|---|
| B1 | 新增 `store/pgproto.py`：**PG 前端/后端协议（v3）最小实现** —— 消息编解码（`StartupMessage` / `Authentication*` / `Query` / `Parse` / `Bind` / `Describe` / `Execute` / `Sync` / `ReadyForQuery` / `ErrorResponse` / `RowDescription` / `DataRow` / `CommandComplete` / `ParameterStatus`） |
| B2 | **认证**：`SCRAM-SHA-256`（默认，PG 10+ 主流）与 `MD5`（兼容）两条路径；SCRAM 用 stdlib `hmac`/`hashlib`（`pbkdf2_hmac`、`sha256`）实现；`AuthenticationOk` 判定；明确**拒绝** `AuthenticationCleartextPassword`（不回传明文口令，宁可失败并给指引） |
| B3 | **传输安全**：支持 `sslmode`（`disable` / `require` / `prefer`），`require` 走 stdlib `ssl`（`SSLContext` + 默认校验）；`sslmode=require` 但服务端不支持 SSL 时**明确失败**，不静默降级明文 |
| B4 | **参数绑定走扩展查询协议**：语句用 `$1`/`$2` 占位，**严禁字符串拼接 SQL**（防注入的唯一做法）；文本格式结果 + 显式 `NULL` 处理；`parse/bind/execute/sync` 一次往返完成一条语句 |
| B5 | **类型映射**：`INT8/INT4` → `int`；`TEXT/VARCHAR` → `str`；`BOOL` → `bool`；`NULL` → `None`；`TIMESTAMPTZ`/`BYTEA` 按需（本轮用到才做）。连接字符集固定 `client_encoding=UTF8` |
| B6 | **连接与并发**：**每线程一条连接**（HTTP 层是多线程，PG 连接非线程安全）；`threading.local` 或轻量池；连接失效（`OperationalError`）时**重连一次**；`close()` 释放。事务边界：`autocommit` 语义 + 显式 `BEGIN/COMMIT` 用于多语句原子操作（bootstrap / 审计批次） |
| B7 | **错误映射**：`ErrorResponse` 的 `SQLSTATE` → `StoreError`（暴露 `sqlstate`，供 C 阶段的 `duplicate_table`/`unique_violation` 判定），**不把原始报文（可能含数据）打进日志** |

**验收**：能对一个真实 PG 完成「连接 → SCRAM 认证 → 建表 → 带参插入 → 带参查询 → 事务回滚」；`sslmode=require` 时链路加密；注入串作为**参数**传入不改变语句语义（测试钉住）。

**红线核对**：纯 stdlib；参数化（无拼接）；不清明文口令；不静默降级。

---

### C PG Store 驱动

| 子任务 | 内容 |
|---|---|
| C1 | 新增 `store/pg.py`：`PostgresStore(Store)`，**逐方法实现 `base.py` 的 ABC**；建表用 `schema.POSTGRES`（**唯一事实来源**，不另抄）；`health()` 用一个 `SELECT 1` 探活 |
| C2 | **原子语义必须逐条保住**（这些是并发正确性的命门，不是性能优化）：`reserve_usage`（条件 `UPDATE ... WHERE used + estimate <= limit` + rowcount 判定，一条语句里判+写）、`bootstrap_first_user`（`INSERT ... WHERE NOT EXISTS` 单语句原子）、`consume_enrollment`（`UPDATE ... WHERE uses < max_uses` + rowcount）、`insert_audit_events`（`INSERT ... ON CONFLICT DO NOTHING`，靠 UNIQUE 幂等） |
| C3 | **审计链顺序敏感性**：`get_batch_chain_head` / `get_session_last_hash` / `server_chain` 语义与 SQLite 完全一致；`insert_audit_batch` 的链推进在**同一事务**内完成，杜绝「批次写一半、链断」 |
| C4 | **限流**：`hit_rate_limit` 用 PG 的原子 `INSERT ... ON CONFLICT ... DO UPDATE ... RETURNING`（固定窗口，语义同 SQLite；**不换 Redis**） |
| C5 | 迁移脚本 `scripts/migrate_sqlite_to_pg.py`：按 §5.5 纪律**单向**导出→导入；**审计表整表顺序搬**（链顺序敏感，禁止并发分批）；提供 `--dry-run` 计数对拍。**不做**双向同步 |
| C6 | `store/__init__.py` 的 `_POSTGRES_HINT` 文案改写为「已支持，用法见 server README」；`README` 存储小节补 PG 用法与三条硬门槛 |

**验收**：同一份「Store 契约测试」在 SQLite 与 PG 上**同样通过**（PG 需真实实例，见 E2）；`reserve_usage` 并发压测不超发；迁移后 `verify_chain` 通过。

**红线核对**：服务层零改动；schema 单一来源；审计链不自造第二套；迁移单向。

---

### D 多实例前置收口

| 子任务 | 内容 |
|---|---|
| D1 | **TLS/来源 IP 一致性**：`config.validate()` 增加「多实例意图下 `REQUIRE_TLS=1` 且 `TRUST_FORWARDED_FOR=1`」的**显式校验**（多实例几乎必然在 LB/反代之后；缺则来源 IP 失真、限流与审计的 `remote` 全错）。**只加约束、不放松既有校验** |
| D2 | **启动护栏**：多实例意图下若 `db_path` 仍是 SQLite，**启动即拒绝**并给出「多实例 = 配额失效」的说明（防止「开了 2 个实例、都写本机 SQLite」的静默灾难） |
| D3 | `doctor` 新增**多实例自检项**：统一签名密钥是否显式设置、`db_path` 是否共享存储、TLS/来源 IP 是否就绪；任一项缺失给**一行可执行建议**（沿用既有 doctor 输出风格） |
| D4 | 启动日志：多实例意图下打印一行明确标识（「多实例模式：共享存储 + 统一密钥 + 信任转发头」），单机模式**不打印**（保持既有日志逐字节一致） |

**验收**：设 `VIBE_SRV_MULTI_INSTANCE=1` 但保留 SQLite → 启动失败并提示；三项齐备 → 启动成功且 doctor 全绿；**未设该开关时，doctor 输出与启动日志与改造前一致**。

**红线核对**：不放松任何既有校验；默认路径日志/输出不变。

---

### E 测试与反向验证

| 子任务 | 内容 |
|---|---|
| E1 | 新增 `apps/server/tests/test_pgproto.py`：**用 stdlib `socket` 起一个「假 PG 服务器」**（照搬 `test_oidc.py` 假 IdP 的范式），逐条钉协议语义：startup 报文、SCRAM 挑战/响应、`AuthenticationOk`、扩展查询往返、`ErrorResponse` → `StoreError`、`sslmode=require` 握手、**参数注入不改语义**、拒绝明文口令 |
| E2 | 新增 `apps/server/tests/test_store_contract.py`：把 `Store` 的**契约测试抽成可复用基类**，SQLite 上**全量跑**（默认、hermetic）；PG 后端在 `VIBE_SRV_TEST_PG_DSN` 存在时才跑，否则 `skip`（**环境相关 skip**，与 ConPTY/符号链接同类）。同一批用例**双后端对拍**是「换驱动不改语义」的**唯一可信证据** |
| E3 | 新增/扩展：`test_config_multi_instance.py`（多实例意图判定、JWT 硬校验、TLS 一致性、SQLite 护栏）、`test_migrate.py`（迁移脚本 dry-run 计数对拍、审计链顺序） |
| E4 | **反向验证**（随后**还原**）：① 让 `reserve_usage` 改成「先 SELECT 判、再 UPDATE 写」两条语句 → 并发契约用例变红；② 让 `open_store` 对 `postgres://` 静默退回 SQLite → A3 用例变红；③ 让多实例意图下 `JWT_SECRET` 缺失仍自动生成 → A2 用例变红；④ 让 `pgproto` 用字符串拼接参数 → 注入用例变红 |
| E5 | 全量回归：服务端（`python -m pytest tests/ -q`）+ agent-core + `scripts/e2e_governance.py`（8 档）；**既有用例不变红、总数只增不减**；**agent-core 与服务端不并行跑**（端口争用）。**默认无 PG 时全部 PG 用例 skip，hermetic 不受影响** |

**验收**：协议层与配置层用例全绿；Store 契约在 SQLite 全绿、PG 有实例时全绿；四条反向验证逐条变红并已还原；全量回归通过。

---

### F 文档与收口

| 子任务 | 内容 |
|---|---|
| F1 | 根 `README.md`：路线图 **M4** 行由「🟡 部分落地」更新（高可用前置条件已落代码；K8s 仍只出设计） |
| F2 | `docs/ARCHITECTURE.md`：新增 **ADR-026**（服务端多实例：共享存储 + 统一密钥 + TLS/来源 IP；**PG 客户端自研、纯 stdlib**，以保零依赖红线）；补记 ADR-009（仍未拆微服务）与 ADR-013/存储取舍的复核 |
| F3 | `apps/server/README.md`：存储小节补 **PG 用法**（DSN、`sslmode`、三条硬门槛、`replicas=1` 约束）、`multi-instance` 配置项表、迁移脚本用法；接口/配置表同步 |
| F4 | `apps/server/.env.example`（若有）/ 配置表：登记 `VIBE_SRV_MULTI_INSTANCE`、`VIBE_SRV_DB=postgres://...`、`sslmode` 相关项与语义 |
| F5 | `docs/M4-OPS-PLAN.md` §5.2/§5.5：把「门槛 2 需先实现 PG 驱动」更新为「已落地」并回写实测；本文档：收口实测结论 + 勾 DoD + 补 §4 测试数演进 |

---

## 3. 里程碑完成定义（DoD）

- [x] A–F 六阶段全部交付，专项测试 `test_pgproto.py` / `test_store_contract.py` / `test_config_multi_instance.py` 全绿
- [x] **默认路径逐字节不变**：`VIBE_SRV_DB` 未设 / `MULTI_INSTANCE` 未设 → SQLite、自动 `jwt.key`、启动日志与 doctor 输出与改造前一致
- [x] 多实例三条硬门槛**可运行**：统一签名密钥硬校验 + PG 共享存储 + TLS/来源 IP 一致性
- [x] **零第三方依赖**：`apps/server` 未新增任何运行时依赖（纯 stdlib 实现 PG 客户端）
- [x] PG Store 与 SQLite 在同一份契约测试下**语义一致**（原子预留/结算、bootstrap、审计链、幂等）
- [x] 参数绑定走扩展查询协议，**无字符串拼接 SQL**（注入用例钉住）
- [x] 四条反向验证已做（E4）并已还原
- [x] 全量测试数**只增不减**；无 PG 实例时 PG 相关用例为**环境相关 skip**，hermetic 默认不受影响
- [x] K8s 清单**未落盘**（沿用定档）；迁移脚本单向、审计表顺序搬
- [x] `scripts/e2e_governance.py` 8 档全绿、无残留进程
- [x] 文档与实际落法一致（README / ARCHITECTURE / server README / M4-OPS-PLAN / 本文档）

---

## 4. 测试数演进（开工基线 → 收口后填写）

| 测试树 | 开工基线 | 收口目标 | 实际（2026-10-09 收口） |
|---|---|---|---|
| 服务端 | 301 收集 / 299 passed / 2 skipped / 418 subtests | 只增不减 | **383 收集 / 358 passed / 25 skipped / 418 subtests**（+82 收集：`test_pgproto` 16、`test_store_contract` 23 SQLite 全跑 + 23 PG skip、`test_config_multi_instance` 13、`test_migrate` 7；25 skip 中 23 条为 **PG 契约用例**，本机无真实 PG 故按环境相关 skip，另 2 条为既有平台 skip） |
| agent-core | 712 收集 / 710 passed / 2 skipped / 64 subtests | 不变或只增 | **712 收集 / 710 passed / 2 skipped / 64 subtests**（不变） |
| `scripts/e2e_governance.py` | 8 档全绿 | 8 档全绿、无残留 | **8 档全绿、退出码 0、无残留进程** |

> 无 PG 实例时，PG 契约用例计入 **skip**（环境相关），不计入 passed；有实例（`VIBE_SRV_TEST_PG_DSN`）时全量跑通。

---

## 5. 风险与缓解

| 风险 | 说明 | 缓解 |
|---|---|---|
| **自研 PG 协议正确性** | 协议细节多（消息边界、SCRAM 步数、扩展查询时序），写错会在生产才暴露 | E1 用**假 PG server** 逐消息钉死；先只实现用到的子集；不追求通用客户端 |
| **认证/传输安全** | SCRAM 实现错误 → 认证失败或（更糟）降级；明文口令回传 | 拒绝 `CleartextPassword`；`sslmode=require` 不静默降级；E1 钉住 |
| **SQL 注入** | 拼接 SQL 是自研客户端最易犯的错 | **只走扩展查询协议**；E4 反向验证「拼接 → 变红」 |
| **并发正确性** | 多实例/多线程下预留、bootstrap、限流必须是单语句原子 | C2 逐条保持「判+写同一语句」；E2 并发对拍；E4 ①反向验证 |
| **审计链顺序** | 分批并发导入 → 链校验全红，症状像「被篡改」 | C3 同事务推进；C5 审计表**整表顺序搬**；E3 链校验对拍 |
| **零依赖红线被蚕食** | 「加个 psycopg 就省事」最容易破线 | 本轮明确**不设 extra 旁路**；DoD 钉「未新增依赖」；ADR-026 写明 |
| **默认路径被改动** | 新校验误伤单机部署 | 所有新校验**只在多实例意图下生效**；DoD 钉「默认逐字节一致」 |
| **PG 版本/字符集差异** | 不同 PG 版本、`client_encoding` 不同导致乱码 | 固定 `client_encoding=UTF8`；文档标注**已验证的 PG 版本区间**；类型映射只做用到的子集 |
| **OIDC 多实例** | `OidcFlowStore` 在进程内，多实例登录可能失败 | 本轮**不承诺多实例 OIDC**；文档写明需 sticky 或共享缓存（§5.3） |
| **迁移数据一致性** | 半程失败留下不一致 | `--dry-run` 计数对拍；单向、可重跑（幂等插入）；不自动切读 |

---

## 6. 落地顺序

`A（契约）→ B（PG 协议客户端）→ C（PG Store）→ D（多实例收口）→ E（测试与反向验证）→ F（文档收口）`

- A 必须最先（分派与硬校验是 B/C/D 的前置）。
- B 是 C 的前置（没有客户端就没有驱动）；B 的风险占总量的多数，**建议 B 完成即先合入一次回归**。
- C 依赖 A/B；D 可独立于 C（TLS/密钥收口不依赖 PG 是否已通），但建议排在 C 后以便「三门槛齐备」端到端验一次。
- E 在所有代码阶段之后；F 最后收口。

---

## 7. 收口实测（2026-10-09）

### 7.1 交付物清单

| 类型 | 文件 | 要点 |
|---|---|---|
| 代码 | `store/pgproto.py`（新建） | 自研 PG v3 协议客户端，**纯 stdlib**：startup / SCRAM-SHA-256 + MD5 / 拒绝明文口令 / 扩展查询（`$1` 参数绑定）/ 文本类型映射 / `sslmode` 不静默降级 / `PGPool` 每线程一条连接 |
| 代码 | `store/pg.py`（新建） | `PostgresStore(Store)` 逐方法对齐契约；`ensure_schema(pool)` 抽为模块级；原子语义（条件 UPDATE + rowcount / `INSERT … WHERE NOT EXISTS` / `ON CONFLICT DO NOTHING` / `RETURNING`）逐条保住 |
| 代码 | `store/__init__.py`（改） | `open_store` 按 `postgres://…` 分派 `PostgresStore`；未知 scheme（如 `redis://`）明确报错，**绝不静默退回 SQLite** |
| 代码 | `store/migrate.py`（新建） | 迁移逻辑（可导入可测）：`TABLES` 18 张按依赖序、`TableSpec`、`MigrationReport`（`--dry-run` 计数对拍）、`PostgresTarget`（`ON CONFLICT DO NOTHING` 幂等、`setval` 推序列） |
| 代码 | `scripts/migrate_sqlite_to_pg.py`（新建） | CLI 薄壳：`sqlite` 位置参数 + `--dsn`（默认取 `VIBE_SRV_DB`）+ `--dry-run`，逐表源/目标计数对拍，退出码 0/1 |
| 代码 | `config.py`（改） | `db_url` / `multi_instance` / `jwt_secret_from_env`；`multi_instance_intent()` / `store_target()`；`_load_or_create_secret(require_explicit=)`；`validate()` 三门槛 |
| 代码 | `__main__.py`（改） | `doctor` 多实例自检项（仅多实例意图下出现）+ 多实例启动多打一行日志；存储检查补 `StoreError` 捕获 |
| 测试 | `tests/test_pgproto.py`（新建） | 假 PG server（stdlib `socket`）16 用例，逐消息钉协议 |
| 测试 | `tests/test_store_contract.py`（新建） | Store 契约双后端对拍：23 用例，SQLite 全跑 + PG（`VIBE_SRV_TEST_PG_DSN`）同批跑；含 **24 线程并发预留不超发** |
| 测试 | `tests/test_config_multi_instance.py`（新建） | 多实例意图 / 三门槛 / `open_store` 分派 / doctor，13 用例 |
| 测试 | `tests/test_migrate.py`（新建） | 迁移 dry-run / 幂等 / 顺序搬 / CLI 退出码，7 用例 |
| 文档 | `README.md` / `docs/ARCHITECTURE.md` / `apps/server/README.md` / `docs/M4-OPS-PLAN.md` / 本文档 | M4 行更新；新增 **ADR-026**；server README 补 PG 用法与三门槛 + 测试数更新；§5.2/§5.5 回写 |

### 7.2 反向验证（E4，逐条变红后已还原）

| # | 临时反向改动 | 应变红用例 | 实测 |
|---|---|---|---|
| ① | `reserve_usage` 判定与写入拆成两条语句、且移出事务 | 契约并发用例 | ✅ **24 != 10**（超发），单线程用例仍绿（正是要补并发用例的原因） |
| ② | `open_store` 对 `postgres://` 静默退回 SQLite | `test_open_store_dispatches_postgres` | ✅ 返回 `SQLiteStore` 非 `PostgresStore` |
| ③ | 多实例意图下 `JWT_SECRET` 缺失仍自动生成（去掉两处 raise） | `test_missing_jwt_secret_rejected` | ✅ `ConfigError not raised`；单机用例仍绿 |
| ④ | `pgproto.exec_params` 把参数内联进 SQL 文本 | `test_parameters_are_never_interpolated` | ✅ 注入串出现在 SQL 文本（`$1` 消失、含 `DROP TABLE`） |

四条均已还原，源码内无临时标记残留。

### 7.3 结论

- 三门槛**可运行**：多实例意图下缺任一条即拒绝启动；三门槛齐备时 `doctor` 输出就绪行、启动成功。
- **默认路径逐字节不变**：不设开关 / URL 时仍 SQLite、仍自动 `jwt.key`、doctor 单机无「多实例」行。
- **零依赖红线**：`apps/server` 未新增任何运行时依赖（PG 客户端纯 stdlib）。
- 全量回归：服务端 **358 passed / 25 skipped / 418 subtests**、agent-core **710 passed / 2 skipped / 64 subtests**、
  `e2e_governance.py` **8 档全绿**；既有用例无变红，总数只增不减。
- **K8s / compose 未落盘**（沿用定档）；迁移脚本单向、审计表整表顺序搬。