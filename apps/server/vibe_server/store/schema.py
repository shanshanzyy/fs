"""表结构。

设计约定（换 Postgres 时照这条走）：
- 主键一律用应用层生成的字符串 ID（UUID hex），不依赖自增 —— 自增在分库/合并时是灾难
- 时间一律存 **epoch 秒整数**（列名以 `_at` 结尾），不存字符串。字符串时间无法索引优化、
  时区一乱就没法比大小；展示层再格式化
- 「自然日」单独存成 `YYYY-MM-DD` 文本（配额分组键），用的是本地时区
- 金额/配额一律整数，不用浮点
- 布尔用 0/1 整数（SQLite 无原生布尔；PG 里对应 BOOLEAN，见 POSTGRES 版本）

两套 DDL 同时维护在这里，是为了让「换库」这件事变成改一行配置，
而不是启动一次考古。
"""

from __future__ import annotations

SQLITE = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

-- ------------------------------------------------------------------ 组织
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL DEFAULT '',
    external_id   TEXT,                        -- 对接 SSO 时的 sub
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_external ON users(external_id);

CREATE TABLE IF NOT EXISTS teams (
    id         TEXT PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    team_id    TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (user_id, team_id)
);
CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships(user_id);
CREATE INDEX IF NOT EXISTS idx_memberships_team ON memberships(team_id);

CREATE TABLE IF NOT EXISTS projects (
    id         TEXT PRIMARY KEY,
    team_id    TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    repo       TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    UNIQUE (team_id, name)
);

CREATE TABLE IF NOT EXISTS project_grants (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (project_id, user_id)
);

-- ------------------------------------------------------------------ 设备与令牌
CREATE TABLE IF NOT EXISTS devices (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT NOT NULL DEFAULT '',
    platform     TEXT NOT NULL DEFAULT '',
    fingerprint  TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL DEFAULT 0,
    revoked_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id          TEXT PRIMARY KEY,
    device_id   TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    family_id   TEXT NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    issued_at   INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    used_at     INTEGER,
    revoked_at  INTEGER,
    replaced_by TEXT,
    client_ip   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_refresh_family ON refresh_tokens(family_id);
CREATE INDEX IF NOT EXISTS idx_refresh_device ON refresh_tokens(device_id);

CREATE TABLE IF NOT EXISTS enrollment_tokens (
    id         TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    team_id    TEXT REFERENCES teams(id),
    role       TEXT NOT NULL DEFAULT 'developer',
    created_by TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    max_uses   INTEGER NOT NULL DEFAULT 1,
    uses       INTEGER NOT NULL DEFAULT 0,
    revoked_at INTEGER
);

-- 访问令牌是无状态的，登出要能立即生效就得留一份吊销名单。
-- 只存 jti（不存整串令牌），并按过期时间清理，避免无限增长。
CREATE TABLE IF NOT EXISTS revoked_tokens (
    jti        TEXT PRIMARY KEY,
    expires_at INTEGER NOT NULL,
    revoked_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revoked_expires ON revoked_tokens(expires_at);

-- ------------------------------------------------------------------ 配额
CREATE TABLE IF NOT EXISTS quota_policies (
    id                 TEXT PRIMARY KEY,
    scope_type         TEXT NOT NULL,            -- global | team | user
    scope_id           TEXT NOT NULL DEFAULT '',
    daily_cloud_tokens INTEGER NOT NULL DEFAULT 0,
    daily_requests     INTEGER NOT NULL DEFAULT 0,
    requests_per_minute INTEGER NOT NULL DEFAULT 0,
    updated_at         INTEGER NOT NULL,
    updated_by         TEXT NOT NULL DEFAULT '',
    UNIQUE (scope_type, scope_id)
);

-- 预算策略：用于**推导**每日额度（不是显式额度，见 gateway/quota.py 的解析链）。
-- budget 的含义由 unit_price 决定：unit_price>0 时 budget 是成本预算，
-- token 预算 = budget / unit_price；否则 budget 直接就是 token 预算。
-- window 是摊销窗口天数，0 表示取「当月天数」。预算为 0 视为未设、不推导。
CREATE TABLE IF NOT EXISTS quota_budgets (
    id         TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL,                    -- global | team | user
    scope_id   TEXT NOT NULL DEFAULT '',
    budget     INTEGER NOT NULL DEFAULT 0,
    unit_price INTEGER NOT NULL DEFAULT 0,
    window     INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    updated_by TEXT NOT NULL DEFAULT '',
    UNIQUE (scope_type, scope_id)
);

-- 计数与配额判定必须在同一条 SQL 里完成，否则并发下会超发。
-- 见 sqlite.py 的 reserve_quota：用带条件的 UPDATE + rowcount 做原子判定。
CREATE TABLE IF NOT EXISTS usage_counters (
    day            TEXT NOT NULL,
    user_id        TEXT NOT NULL,
    team_id        TEXT NOT NULL DEFAULT '',
    cloud_tokens   INTEGER NOT NULL DEFAULT 0,
    local_tokens   INTEGER NOT NULL DEFAULT 0,
    requests       INTEGER NOT NULL DEFAULT 0,
    reserved_tokens INTEGER NOT NULL DEFAULT 0,
    updated_at     INTEGER NOT NULL,
    PRIMARY KEY (day, user_id)
);
CREATE INDEX IF NOT EXISTS idx_usage_team ON usage_counters(day, team_id);

CREATE TABLE IF NOT EXISTS rate_counters (
    scope_key    TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    count        INTEGER NOT NULL,
    PRIMARY KEY (scope_key, window_start)
);

-- ------------------------------------------------------------------ 审计
-- UNIQUE(device_id, session_id, seq) 让「网络抖动导致客户端重传」天然幂等，
-- 不用额外做去重逻辑。
CREATE TABLE IF NOT EXISTS audit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL,
    user_id     TEXT NOT NULL DEFAULT '',
    team_id     TEXT NOT NULL DEFAULT '',
    session_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    event_type  TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    UNIQUE (device_id, session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_audit_team_time ON audit_events(team_id, ts);
CREATE INDEX IF NOT EXISTS idx_audit_user_time ON audit_events(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_events(device_id, session_id);

-- 批次链（链的链）：每条批次记录链接上一批次的链头。
-- 客户端本地链只能证明「我本地的记录没被改」，批次链能额外证明
-- 「上传过程没有被截断或重放」—— 两者缺一不可。
CREATE TABLE IF NOT EXISTS audit_batches (
    id               TEXT PRIMARY KEY,
    device_id        TEXT NOT NULL,
    user_id          TEXT NOT NULL DEFAULT '',
    team_id          TEXT NOT NULL DEFAULT '',
    first_seq        INTEGER NOT NULL,
    last_seq         INTEGER NOT NULL,
    event_count      INTEGER NOT NULL,
    client_chain_head TEXT NOT NULL,
    prev_chain_head  TEXT NOT NULL,
    chain_head       TEXT NOT NULL,
    sequence_gap     INTEGER NOT NULL DEFAULT 0,
    received_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batches_device ON audit_batches(device_id, received_at);

CREATE TABLE IF NOT EXISTS auth_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    kind    TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT '',
    remote  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_auth_events_time ON auth_events(ts);

-- ------------------------------------------------------------------ 策略
CREATE TABLE IF NOT EXISTS policies (
    id         TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL,                    -- global | team
    scope_id   TEXT NOT NULL DEFAULT '',
    version    INTEGER NOT NULL,
    document   TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT '',
    UNIQUE (scope_type, scope_id, version)
);

-- ------------------------------------------------------------------ 密钥元数据
-- 只存「引用 + 指纹 + 轮换时间」，**永远不存密钥明文**。
-- 密钥本身在 Vault / 环境变量 / 受限文件里，这一层只做台账。
CREATE TABLE IF NOT EXISTS secret_meta (
    name        TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    ref         TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL,
    rotated_at  INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);
"""


POSTGRES = """
-- 与 SQLITE 版本语义一致，只是类型与自增写法不同。
-- 未在 M2 启用（见 README 的「存储为什么先用 SQLite」），
-- 先落在这里，让换库时不用重新考古一遍表结构。

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL DEFAULT '',
    external_id   TEXT,
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    BIGINT NOT NULL,
    updated_at    BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_external ON users(external_id);

CREATE TABLE IF NOT EXISTS teams (
    id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    created_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    role TEXT NOT NULL, created_at BIGINT NOT NULL,
    UNIQUE (user_id, team_id)
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    name TEXT NOT NULL, repo TEXT NOT NULL DEFAULT '',
    created_at BIGINT NOT NULL, UNIQUE (team_id, name)
);

CREATE TABLE IF NOT EXISTS project_grants (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL, created_at BIGINT NOT NULL, UNIQUE (project_id, user_id)
);

CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '', platform TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '', created_at BIGINT NOT NULL,
    last_seen_at BIGINT NOT NULL DEFAULT 0, revoked_at BIGINT
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    family_id TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
    issued_at BIGINT NOT NULL, expires_at BIGINT NOT NULL,
    used_at BIGINT, revoked_at BIGINT, replaced_by TEXT,
    client_ip TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS enrollment_tokens (
    id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
    team_id TEXT REFERENCES teams(id), role TEXT NOT NULL DEFAULT 'developer',
    created_by TEXT NOT NULL DEFAULT '', created_at BIGINT NOT NULL,
    expires_at BIGINT NOT NULL, max_uses INTEGER NOT NULL DEFAULT 1,
    uses INTEGER NOT NULL DEFAULT 0, revoked_at BIGINT
);

CREATE TABLE IF NOT EXISTS revoked_tokens (
    jti TEXT PRIMARY KEY, expires_at BIGINT NOT NULL, revoked_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS quota_policies (
    id TEXT PRIMARY KEY, scope_type TEXT NOT NULL, scope_id TEXT NOT NULL DEFAULT '',
    daily_cloud_tokens BIGINT NOT NULL DEFAULT 0,
    daily_requests BIGINT NOT NULL DEFAULT 0,
    requests_per_minute INTEGER NOT NULL DEFAULT 0,
    updated_at BIGINT NOT NULL, updated_by TEXT NOT NULL DEFAULT '',
    UNIQUE (scope_type, scope_id)
);

CREATE TABLE IF NOT EXISTS quota_budgets (
    id TEXT PRIMARY KEY, scope_type TEXT NOT NULL, scope_id TEXT NOT NULL DEFAULT '',
    budget BIGINT NOT NULL DEFAULT 0, unit_price BIGINT NOT NULL DEFAULT 0,
    window INTEGER NOT NULL DEFAULT 0,
    updated_at BIGINT NOT NULL, updated_by TEXT NOT NULL DEFAULT '',
    UNIQUE (scope_type, scope_id)
);

CREATE TABLE IF NOT EXISTS usage_counters (
    day TEXT NOT NULL, user_id TEXT NOT NULL, team_id TEXT NOT NULL DEFAULT '',
    cloud_tokens BIGINT NOT NULL DEFAULT 0, local_tokens BIGINT NOT NULL DEFAULT 0,
    requests BIGINT NOT NULL DEFAULT 0, reserved_tokens BIGINT NOT NULL DEFAULT 0,
    updated_at BIGINT NOT NULL, PRIMARY KEY (day, user_id)
);

CREATE TABLE IF NOT EXISTS rate_counters (
    scope_key TEXT NOT NULL, window_start BIGINT NOT NULL, count BIGINT NOT NULL,
    PRIMARY KEY (scope_key, window_start)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id BIGSERIAL PRIMARY KEY,
    device_id TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '',
    team_id TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL, seq BIGINT NOT NULL,
    event_type TEXT NOT NULL, ts BIGINT NOT NULL, payload TEXT NOT NULL,
    prev_hash TEXT NOT NULL, hash TEXT NOT NULL, batch_id TEXT NOT NULL,
    received_at BIGINT NOT NULL, UNIQUE (device_id, session_id, seq)
);

CREATE TABLE IF NOT EXISTS audit_batches (
    id TEXT PRIMARY KEY, device_id TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '',
    team_id TEXT NOT NULL DEFAULT '', first_seq BIGINT NOT NULL, last_seq BIGINT NOT NULL,
    event_count INTEGER NOT NULL, client_chain_head TEXT NOT NULL,
    prev_chain_head TEXT NOT NULL, chain_head TEXT NOT NULL,
    sequence_gap INTEGER NOT NULL DEFAULT 0, received_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_events (
    id BIGSERIAL PRIMARY KEY, ts BIGINT NOT NULL, user_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', remote TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS policies (
    id TEXT PRIMARY KEY, scope_type TEXT NOT NULL, scope_id TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL, document TEXT NOT NULL, created_at BIGINT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
    UNIQUE (scope_type, scope_id, version)
);

CREATE TABLE IF NOT EXISTS secret_meta (
    name TEXT PRIMARY KEY, provider TEXT NOT NULL, ref TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL, rotated_at BIGINT NOT NULL, created_at BIGINT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
"""

__all__ = ["POSTGRES", "SQLITE"]
