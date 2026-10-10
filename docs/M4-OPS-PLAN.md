# M4 规模化 — 落地方案与运维设计

| 项 | 内容 |
|---|---|
| 版本 | v1.0（方案，待评审） |
| 日期 | 2026-09-24 |
| 对应 | `docs/ARCHITECTURE.md` §6「M4 规模化」 |
| 覆盖 | 高可用、监控告警、灰度分发、成本看板 |

---

## 0. 本次边界（先说清楚做到哪一步）

§6 给 M4 列了四项交付物。本次的口径是**只做能写成代码并被测试钉住的部分**，
基础设施形态只出设计：

| 交付物 | 本次做到 | 交付位置 |
|---|---|---|
| 监控告警 | ✅ **代码落地** | `metrics.py` + `GET /metrics`（Prometheus 文本格式，21 条测试） |
| 成本看板 | ✅ **代码落地** | `QuotaService.usage_summary()` + `GET /api/v1/usage` |
| 灰度分发 | ✅ **代码落地** | `features.py` + `policy.features` / `bundle.features`（30 条测试） |
| 高可用 | 📄 只出方案 | 本文 §5 |
| K8s 清单 | 📄 只出设计 | 本文 §6（`infra/k8s/` **本次不落盘**） |

**为什么不落 K8s**：当前规模是「一台机器、几十个人、内网离线」，
adr-009 已判定微服务运维成本是单体的 3–10 倍。把 K8s 清单先写出来，
最大的风险不是写错，而是**有人看到清单就去用了** —— 那时会把
「拷一个目录就能跑」变成「先懂一套编排系统」，得不偿失。
所以本文只给设计、触发条件与迁移路径，落地留到触发条件真出现时。

---

## 1. 现状盘点：已经有的三个抓手

M4 之前只有日志。现在多了三个可被外部系统消费的端点，方案全部建立在它们之上：

| 端点 | 认证 | 用途 | 关键语义 |
|---|---|---|---|
| `GET /healthz` | 公开 | 存活/存储健康 | 存储异常时返回 **503**（不是 200 带 `ok:false`），可直接当探针 |
| `GET /metrics` | 公开 | 指标抓取 | Prometheus 文本 `version=0.0.4`，**手写渲染**（`dependencies` 必须保持为空） |
| `GET /api/v1/usage` | `usage:read` | 成本看板数据源 | 按天聚合，带作用域校验与截断标记 |

> 两个公开端点的取舍是一致的：监控系统通常不携带应用令牌，要求认证的结果
> 往往是被改成「拿管理员令牌去爬」——权限反而更大。
> 代价是这两个端点必须自己守底线：`/metrics` 的标签里**没有任何用户维度**。

---

## 2. 监控告警

### 2.1 指标清单（当前实际渲染的全集）

| 指标 | 类型 | 标签 | 说明 |
|---|---|---|---|
| `vibe_build_info` | gauge | `version` | 恒为 1，用于对齐「跑的是哪个版本」 |
| `vibe_uptime_seconds` | gauge | —— | 进程已运行秒数（起点用注入时钟，与 `/healthz` 同源） |
| `vibe_store_up` | gauge | —— | 1=存储可用，0=不可用 |
| `vibe_requests_total` | counter | `code` | 按响应码聚合 |
| `vibe_route_requests_total` | counter | `method`, `route` | `route` 是**路由模板**（如 `/api/v1/devices/{deviceId}/revoke`） |
| `vibe_request_duration_ms_total` | counter | —— | 耗时累计（毫秒） |
| `vibe_request_duration_ms_count` | counter | —— | 被计时的请求数 |
| `vibe_request_duration_ms_avg` | gauge | —— | 平均耗时（省得看板做除法做错） |
| `vibe_quota_rejections_total` | counter | —— | 429 数，单独一条，否则告警规则要从 status 维度里挑、容易漏 |
| `vibe_metrics_label_overflow` | gauge | —— | **仅在溢出时出现**，1=路由标签集合已超上限 |

### 2.2 三个必须知道的坑

1. **这是进程内计数，不是从库里聚合的。**
   监控通常 15 秒抓一次，每次 `/metrics` 都去查 SQLite 等于让监控本身
   成为数据库的负载来源。代价是**每个实例只报自己的量** ——
   而「跨实例求和」本来就是 Prometheus 的职责。写法见 §2.3。
2. **`route` 标签有 200 条上限，溢出归 `(other)`。**
   路由是有限集合，路径参数是无限的（`/devices/{id}/revoke` 里的 id 有无数个）。
   直接拿原始路径当标签会造出无界基数、把监控自己打爆。
   真看到 `(other)` 说明路由表本身出了问题，不是「多了几个接口」这么简单。
3. **`vibe_request_duration_ms_avg` 不能跨实例求平均。**
   它是「本实例的平均」。跨实例的正确写法是
   `sum(rate(..._total)) / sum(rate(..._count))`，直接 `avg()` 会算错
   （各实例请求量不等时偏差很大）。

### 2.3 抓取配置

```yaml
scrape_configs:
  - job_name: vibe-governance
    metrics_path: /metrics
    scheme: http            # 生产应经 TLS 终止反代，改 https
    static_configs:
      - targets: ["10.0.0.11:8787", "10.0.0.12:8787"]
    relabel_configs:
      - target_label: instance     # 保住「哪台报的」，否则求和后无法定位实例
        replacement: ${1}
```

跨实例聚合（写在告警规则或看板里，**不要**期望被监控方自己求和）：

```promql
# 全站 QPS（按响应码）
sum by (code) (rate(vibe_requests_total[5m]))

# 全站 5xx 比例
sum(rate(vibe_requests_total{code=~"5.."}[5m]))
  / sum(rate(vibe_requests_total[5m]))

# 全站平均耗时
sum(rate(vibe_request_duration_ms_total[5m]))
  / sum(rate(vibe_request_duration_ms_count[5m]))
```

### 2.4 告警规则样例

```yaml
groups:
  - name: vibe-governance
    rules:
      - alert: VibeInstanceDown
        expr: up{job="vibe-governance"} == 0
        for: 2m
        labels: {severity: critical}
        annotations: {summary: "治理面实例连续 2 分钟不可达"}

      - alert: VibeStoreDown
        # /healthz 与 /metrics 共用同一个 store.health()，两边会同时变红
        expr: vibe_store_up == 0
        for: 1m
        labels: {severity: critical}
        annotations: {summary: "存储不可用，策略下发与审计上报都会失败"}

      - alert: VibeErrorRateHigh
        expr: |
          sum(rate(vibe_requests_total{code=~"5.."}[5m]))
            / sum(rate(vibe_requests_total[5m])) > 0.05
        for: 5m
        labels: {severity: warning}

      - alert: VibeQuotaRejectionsSpiking
        # 429 激增要么是有人被误伤，要么是配额定得太紧 —— 两种都该有人看一眼
        expr: sum(rate(vibe_quota_rejections_total[5m])) > 1
        for: 5m
        labels: {severity: warning}

      - alert: VibeLatencyHigh
        expr: |
          sum(rate(vibe_request_duration_ms_total[5m]))
            / sum(rate(vibe_request_duration_ms_count[5m])) > 2000
        for: 10m
        labels: {severity: warning}

      - alert: VibeMetricsLabelOverflow
        # 指标只在溢出时出现，所以这条规则在正常时自然不触发，无需 absent()
        expr: max(vibe_metrics_label_overflow) > 0
        for: 5m
        labels: {severity: warning}
        annotations: {summary: "路由标签集合已溢出，新路由被归入 (other)"}

      - alert: VibeRestartLoop
        # uptime 反复归零 = 进程在重启循环（探针还没到阈值时先看到这个）
        expr: changes(vibe_uptime_seconds[15m]) > 3
        labels: {severity: warning}
```

**告警要接向哪里**：内网无外网告警通道时，最省事的是 Prometheus 的
`alertmanager` → 内网 webhook（钉钉/飞书机器人或自建）。这条属于部署，
不属于本仓库代码。

---

## 3. 成本看板

### 3.1 数据源

`GET /api/v1/usage?userId=&teamId=&days=`（权限 `usage:read`），返回：

```jsonc
{
  "scope":    { "userId": "", "teamId": "team-1" },   // 二选一，teamId 优先
  "sinceDay": "2026-08-26",
  "untilDay": "2026-09-24",
  "days":     30,
  "truncated": false,        // true = 行数触顶，合计偏低
  "totals":   { "cloudTokens": 0, "localTokens": 0, "requests": 0 },
  "series":   [ { "day": "2026-08-26", "cloudTokens": 0, "localTokens": 0, "requests": 0 } ]
}
```

三条设计要点：

- **`days` 夹到 `[1, 180]`，且不吃 `days=0` 当默认值。**
  显式写 `?days=0` 是想看今天，静默放大成 30 天等于把「一天的量」
  换成一个月的量且毫无提示。要更大窗口就明说要几天。
- **`truncated` 必须展示。** 取到行数上限（`days × 500`）说明被截断，
  合计会偏低 —— 如实标出来，而不是让看板画出一个「看起来正常但少了几天」的曲线。
- **本地与云端分开统计。** `localTokens` 不计费，但它正是
  「默认本地优先」（ADR-010）省下多少钱的证据，单独一列比合并掉有价值。

### 3.2 报表形态

| 看板 | 数据 | 用途 |
|---|---|---|
| 团队趋势 | `days=30` 的 `series` 折线 | 发现「这周突然涨了」 |
| 个人排行 | 逐人 `userId` 查询后排序 | 定位大户，用于配额调优 |
| 成本构成 | `cloudTokens` vs `localTokens` 饼图 | 证明本地优先策略的收益 |
| 配额打回 | `vibe_quota_rejections_total`（§2） | 成本与「被拒」要放在一起看，否则只看到账单一侧 |

### 3.3 RBAC 边界（不可放宽）

- 查自己 / 自己团队：`usage:read` 即可。
- **跨用户**：走 `actor.scope_user(target, PERM_USAGE_READ)`。
- **跨团队**：必须是该团队成员，或 `org_wide` 主体；否则
  `Forbidden("无权查看该团队的用量")`。
- 理由：能查全公司用量 = 能看全公司开销。这是**经营数据**，不是运维数据。

---

## 4. 灰度分发

### 4.1 机制

灰度定义随策略下发（`policy.features`），客户端拿到的是**按自己算的判定结果**
（`bundle.features`，flag 名 → bool）。两者刻意分开：

- `document["features"]` 是**定义**（含百分比、名单），界面要解释
  「你为什么在这个灰度里」才需要它。
- `bundle.features` 是**判定**，客户端决定走哪条分支只看后者。

分桶：`sha256(flag 名 + \x1f + user_id) % 100`。三个性质都是刻意的：

1. **稳定** —— 同一个人同一个 flag 永远落在同一个桶，不会刷新一次就换边。
2. **per-flag 独立** —— 不会「一次改动把人同时踢出所有灰度」。
3. **无身份不开** —— 拿不到 user_id 时一律判 false，宁可不给。

合并算子（与能力开关同一套纪律，**只收紧**）：

| 维度 | 算子 |
|---|---|
| `enabled` | AND —— 任一层关就是关 |
| `rolloutPercent` | MIN —— 任一层收紧就按最紧的 |
| `denylist` | 并集 —— 任一层禁的全都禁 |
| `allowlist` | 交集 —— 任一层没列的就不可用（空 = 不限制） |

**并且：上层未定义的 flag，下层按默认（关）参与合并。**
这条是防「团队管理员自己写一个 flag 就把灰度节奏绕过去」。

### 4.2 发布流程（推荐五步）

| 步 | 动作 | 观察 |
|---|---|---|
| 1 | 全局策略里加 flag，`enabled=true, percent=0` | 定义阶段，无人受影响 |
| 2 | 内部 dogfood：`allowlist` 加几个 user_id | 走查界面与日志 |
| 3 | 摘掉 allowlist，`percent` 10 → 30 → 60 | 每档停留观察 `vibe_requests_total{code=~"5.."}` 与业务指标 |
| 4 | `percent=100`，保持观察期 | —— |
| 5 | 观察期结束，删除 flag 定义与代码分支 | 避免灰度定义长期堆积（上限 `MAX_FLAGS=200`） |

### 4.3 撤销路径（出事了怎么办）

按优先级从快到慢：

1. **`denylist` 加人** —— 秒级、只影响个体，且是「收紧」，任何层都做得到。
2. **`percent` 降到 0** —— 全局关闭该 flag，但保留定义，便于回滚回来。
3. **`enabled=false`** —— 总闸，保留名单与百分比，语义上最明确。

> 为什么紧急止血在任何层都做得到：因为合并只收紧。
> 团队层写「关」永远有效，写「开」永远无效 —— 这时刻意为之。

### 4.4 纪律

- flag 名：字母开头，`[A-Za-z0-9_.-]`，≤64 字符，全局唯一。
- 团队层**不能引入**全局未定义的 flag（`merge_flags` 会让它按默认关合并）。
- 名单项数量与长度有上限（200 项 / 128 字符），防止把策略文档撑爆。
- 定义会进 etag：名称与名单**都做过去重+排序**，保证合并顺序无关、etag 可复现。

---

## 5. 高可用（设计，不落地）

### 5.1 当前单点清单

| # | 单点 | 后果 | 程度 |
|---|---|---|---|
| 1 | SQLite 单文件 | 单机故障即全停；并发写有上限 | 高 |
| 2 | `jwt.key` 自动生成 | **多实例间令牌互不认**（每实例一份随机密钥） | 高（多实例硬门槛） |
| 3 | 限流计数在 SQLite（固定窗口） | 多实例各算一份 → 实际放行 ≈ 实例数 × RPM | 中 |
| 4 | `OidcFlowStore` 在进程内 | OIDC 回调若落到另一实例 → state 找不到、登录失败 | 中（仅 OIDC 场景） |
| 5 | `Metrics` 在进程内 | 跨实例需 Prometheus 侧求和 | 低（设计如此，非故障） |

### 5.2 多实例前置条件（三条硬门槛，缺一不可）

```bash
# 门槛 1：统一签名密钥（≥32 字符）。不设就会用各实例自己生成的 jwt.key
VIBE_SRV_JWT_SECRET=<32+ 字符随机串>

# 门槛 2：存储换成 PG（各实例共享配额、限流、策略、审计）
VIBE_SRV_DB=postgres://user:pass@pg.internal:5432/vibe?sslmode=require

# 门槛 3：TLS 终止 + 真实来源 IP（否则 config.validate 直接拒绝非回环监听）
VIBE_SRV_REQUIRE_TLS=1
VIBE_SRV_TRUST_FORWARDED_FOR=1
```

> 门槛 2 是当前最重的一块：限流与配额都落在 store 上，
> 不换 PG 就多实例 = 配额形同虚设。这也是 §6 里「不要急着上 K8s」的原因 ——
> 编排解决的是调度问题，解决不了状态共享问题。

> **M4+ 收口实测（2026-10-09）**：三条门槛**均已落代码并可运行**（详见 `docs/M4+-PLAN.md`）。
> ① 统一密钥：多实例意图下 `JWT_SECRET` 未设/过短即**拒绝启动**（禁止自生成 `jwt.key`）；
> ② 共享存储：自研**纯 stdlib PG 驱动**（`store/pgproto.py` + `store/pg.py`，零第三方依赖），
> `open_store` 按 `postgres://` 分派，SQLite↔PG 同一份契约测试双后端对拍通过；
> ③ TLS/来源 IP：多实例意图下 `REQUIRE_TLS=1` 且 `TRUST_FORWARDED_FOR=1` 缺一即拒绝启动。
> 迁移脚本 `scripts/migrate_sqlite_to_pg.py` 单向、可重跑、`--dry-run` 计数对拍（审计表整表顺序搬）。
> **K8s / compose 仍未落盘**（沿用定档）：门槛已通，编排留待真上时再落。

### 5.3 无状态化边界

| 层 | 能否水平扩 | 说明 |
|---|---|---|
| HTTP 层（`httpd.py`） | ✅ 天然无状态 | 多线程、无会话粘连 |
| 网关代理（`gateway/proxy.py`） | ✅ 透传不落地 | 但会成为关键路径，扩它收益最直接 |
| 策略 / 配额 / 审计 | ⚠️ 状态在 store | 换 PG 后可共享 |
| OIDC 流程暂存 | ❌ 进程内 | 多实例需 sticky session，或迁到共享缓存 |
| 指标 | ⚠️ 进程内 | 无需处理，交给 Prometheus 聚合 |

### 5.4 切换路线与触发条件

| 触发条件 | 动作 |
|---|---|
| 同时在线 > 500，或需要独立扩缩容某模块 | 才评估拆服务（ADR-009） |
| 需要多实例共享配额/限流，或并发写显著上升 | SQLite → PG（新增驱动，不改调用方） |
| 需要滑动窗口 / 令牌桶限流 | 限流计数 → Redis |
| 「另一个服务需在拿不到签名密钥的前提下验签」 | HS256 → RS256 |

### 5.5 数据迁移（SQLite → PG 时）

1. 新增 `store/pg.py` 驱动，`open_store()` 按 DSN 分派 —— 业务代码零改动（这一条是 M2 就预留好的 seam）。
2. 表结构用现有 schema 生成（`store/base.py` 是唯一事实来源），避免手抄漂移。
3. 导出 → 导入按表单向，**审计链要整表搬**：
   哈希链是顺序敏感的，分批并发导入会让链校验全线失败，
   而症状会表现为「记录被篡改」—— 排查方向被彻底带偏。
4. 双写观察期：新写入同时落两库，比对计数后再切读。

> **M4+ 落地情况（2026-10-09）**：第 1–3 条已落代码 —— `store/migrate.py`（逻辑，可导入可测）
> + `scripts/migrate_sqlite_to_pg.py`（CLI 薄壳）。**单向、可重跑**（`INSERT … ON CONFLICT DO NOTHING`
> 幂等）、提供 `--dry-run` **计数对拍**（逐表源/目标行数比对，退出码 0/1）；
> 18 张表按**依赖顺序**搬，`audit_events` / `audit_batches` 均按 `id`（`received_at,rowid`）
> **整表顺序**搬以保哈希链顺序；带自增 id 的表迁移后 `setval` 推进序列。
> 第 4 条（双写观察期）**本轮不做** —— 迁移是**离线独立脚本**，服务端运行时不会自动触发，
> 也不做自动切读；搬完由运维手动把 `VIBE_SRV_DB` 指向 PG 重启。

---

## 6. K8s 部署设计（只设计，本次不落盘）

### 6.1 拓扑

```
          [内网 LB / TLS 终止反代]
                     │
        ┌────────────┴────────────┐
   [governance-0]           [governance-1]        ← Deployment, replicas=2
        │                        │
        └────────────┬───────────┘
                     │
              [共享存储：PG]                       ← 门槛 2 未满足前 replicas 只能是 1
```

### 6.2 探针（直接复用已落地端点，无需新增代码）

```yaml
livenessProbe:
  httpGet: {path: /healthz, port: 8787}
  # 存储挂掉返回 503 —— 而 liveness 用 503 会触发重启，
  # 重启治不了存储故障。所以 liveness 只判「进程还活着」，
  # 用 TCP 探测更合适；/healthz 的 503 留给 readiness。
  initialDelaySeconds: 10
  periodSeconds: 20
readinessProbe:
  httpGet: {path: /healthz, port: 8787}   # 503 即摘流量，语义正好
  periodSeconds: 10
```

> 这一处值得单独说：`/healthz` 在存储异常时返回 503 是**正确**的，
> 但把它同时接到 liveness 上会造成「存储抖一下 → 全部实例被重启 →
> 存储更抖」的雪崩。**liveness 用 TCP，readiness 用 /healthz。**

### 6.3 清单骨架（真要做时再落盘）

| 对象 | 关键点 |
|---|---|
| `Deployment` | `replicas` 受门槛 2 约束（未换 PG 只能为 1）；`strategy: RollingUpdate`，`maxUnavailable: 0` |
| `Secret` | `VIBE_SRV_JWT_SECRET`、上游密钥引用；**不进镜像、不进 ConfigMap** |
| `ServiceMonitor` | 抓 `/metrics`，保留 `instance` 标签（§2.3） |
| `Service` | 暴露 8787；TLS 由 Ingress 终止 |
| `NetworkPolicy` | 只允许 LB → governance:8787 与 Prometheus → :8787 |
| `PVC` | 仅 SQLite 形态需要；换 PG 后删除 |

### 6.4 本次不做

- 不写 `infra/k8s/*.yaml`（`infra/docker-compose.yml` 经 M2/E4 **定档为不落盘** ——
  单机部署沿用「拷目录 + 环境变量」，理由与切换条件见 `apps/server/README.md` §边界）。
- 不做多副本（前置条件未满足，强上等于配额失效）。
- 不做 HPA（当前瓶颈是存储不是 CPU）。
- 不做服务网格 / mTLS（ADR-009 的规模不需要）。

---

## 7. 风险与已知约束

| # | 约束 | 影响 | 处置 |
|---|---|---|---|
| 1 | `jwt.key` 自动生成 | 多实例令牌互不认 | 启动日志已有明确提醒，部署时必须显式设 `VIBE_SRV_JWT_SECRET` |
| 2 | 限流为固定窗口 | 窗口边界可能有 2× 突发 | 单机可接受；高并发换 Redis（§5.4） |
| 3 | 配额预留偏高 | 单次调用可能短暂占更多额度 | 刻意为之：宁可估大防超发（见 `quota.py` 模块说明） |
| 4 | `/metrics` 无需认证 | 任何能访问该端口者可见全局请求量与延迟 | 不暴露用户维度；网络层限制抓取来源（§6.3 NetworkPolicy） |
| 5 | 标签基数 200 上限 | 超出的路由并入 `(other)`，丢失该路由的细分 | 有 `vibe_metrics_label_overflow` 告警（§2.4） |
| 6 | 灰度定义上限 200 个 | 长期堆积会挤占 | 发布流程第 5 步要求清理（§4.2） |
| 7 | `usage_summary` 有行数上限 | 极端窗口下合计偏低 | 返回 `truncated=true` 如实标记（§3.1） |
| 8 | OIDC flows 在进程内 | 多实例登录可能失败 | 未上多实例；要上需 sticky 或共享缓存（§5.3） |

---

## 8. 与既有文档的关系

- `docs/ARCHITECTURE.md` §3.5 是服务端治理面的**目标形态**；本文是 M4 阶段
  「哪些已变成代码、哪些还只是设计」的对账，落地情况同时回写在 §6。
- M2 的五处刻意偏离（HTTP 层用标准库、HS256、SQLite、只收紧合并、redaction 三闸门）
  在本文里**不是待偿还的债**：每条都记了切换触发条件（§5.4），
  触发条件没出现之前不提前做。