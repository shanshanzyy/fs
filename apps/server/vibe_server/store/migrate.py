"""SQLite → PostgreSQL 单向迁移。

**只做单向，不做双向同步**（对应 M4+-PLAN §C5 / M4-OPS-PLAN §5.5）。双向同步
需要一个「谁赢」的冲突解决策略，那是一个独立产品，不是一次迁移该顺手带上的东西。
真要做回迁，就再把这份反过来跑一次，而不是让代码同时猜两个方向。

三条纪律：

1. **审计表整表顺序搬**。`audit_events` 与 `audit_batches` 都是哈希链，顺序错一步，
   校验就会把正常数据判成「被篡改」。所以读源库时**按主键顺序**（`ORDER BY id`），
   并且禁止并发分批 —— 这里就是一个 `for` 循环，串行到底。
2. **可重跑（幂等）**。插入一律 `ON CONFLICT DO NOTHING`，重复跑不会产生重复行；
   半程失败后直接重跑，不用先手工清库。
3. **计数对拍**。迁移前后逐表比对源/目标的行数，`--dry-run` 只报源库计数、不写目标。

为什么落成可导入的模块而不是一个大脚本：计数对拍、顺序搬这两件事要能被测试钉住，
而「能测」的前置就是「能 import」。CLI 只是薄壳（`apps/server/scripts/`）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

from .pg import ensure_schema
from .pgproto import PGPool

# 单条 INSERT 最多拼多少行。PG 参数上限 65535，最宽的 audit_events 有 13 列，
# 200 行 = 2600 个参数，留足余量也避免报文过大。
_INSERT_CHUNK = 200


@dataclass(frozen=True)
class TableSpec:
    """一张表的搬迁规格。

    - `columns`：要写入目标的列（顺序即绑定顺序）。
    - `key`：唯一键列，既是幂等判定依据，也是测试里去重的依据。
    - `order_by`：读源库时的排序表达式（**审计表顺序敏感**，靠它保证）。
    - `serial`：是否含 `BIGSERIAL` 自增主键；是则显式带上 id 并搬迁后推进序列，
      避免之后的新写入撞上已搬过来的 id。
    """

    name: str
    columns: tuple[str, ...]
    key: tuple[str, ...]
    order_by: str
    serial: bool = False


# 顺序即依赖顺序：先用户、团队，再引用它们的行。audit_batches 在 audit_events 后，
# 但两者之间没有外键，顺序只是表达「先事件、后批次链」的意图。
TABLES: tuple[TableSpec, ...] = (
    TableSpec(
        "users",
        ("id", "email", "display_name", "password_hash", "external_id",
         "status", "created_at", "updated_at"),
        ("id",),
        "created_at, id",
    ),
    TableSpec("teams", ("id", "slug", "name", "created_at"), ("id",), "id"),
    TableSpec(
        "memberships",
        ("id", "user_id", "team_id", "role", "created_at"),
        ("id",),
        "id",
    ),
    TableSpec(
        "projects",
        ("id", "team_id", "name", "repo", "created_at"),
        ("id",),
        "id",
    ),
    TableSpec(
        "project_grants",
        ("id", "project_id", "user_id", "role", "created_at"),
        ("id",),
        "id",
    ),
    TableSpec(
        "devices",
        ("id", "user_id", "name", "platform", "fingerprint",
         "created_at", "last_seen_at", "revoked_at"),
        ("id",),
        "id",
    ),
    TableSpec(
        "refresh_tokens",
        ("id", "device_id", "user_id", "family_id", "token_hash",
         "issued_at", "expires_at", "used_at", "revoked_at", "replaced_by", "client_ip"),
        ("id",),
        "id",
    ),
    TableSpec(
        "enrollment_tokens",
        ("id", "token_hash", "team_id", "role", "created_by",
         "created_at", "expires_at", "max_uses", "uses", "revoked_at"),
        ("id",),
        "id",
    ),
    TableSpec("revoked_tokens", ("jti", "expires_at", "revoked_at"), ("jti",), "jti"),
    TableSpec(
        "quota_policies",
        ("id", "scope_type", "scope_id", "daily_cloud_tokens", "daily_requests",
         "requests_per_minute", "updated_at", "updated_by"),
        ("id",),
        "id",
    ),
    TableSpec(
        "quota_budgets",
        ("id", "scope_type", "scope_id", "budget", "unit_price", "window",
         "updated_at", "updated_by"),
        ("id",),
        "id",
    ),
    TableSpec(
        "usage_counters",
        ("day", "user_id", "team_id", "cloud_tokens", "local_tokens",
         "requests", "reserved_tokens", "updated_at"),
        ("day", "user_id"),
        "day, user_id",
    ),
    TableSpec(
        "rate_counters",
        ("scope_key", "window_start", "count"),
        ("scope_key", "window_start"),
        "scope_key, window_start",
    ),
    TableSpec(
        "audit_events",
        ("id", "device_id", "user_id", "team_id", "session_id", "seq", "event_type",
         "ts", "payload", "prev_hash", "hash", "batch_id", "received_at"),
        ("device_id", "session_id", "seq"),   # 幂等键用业务唯一键，不靠自增 id
        "id",                                  # 顺序搬：按原插入序（= 时间序）
        serial=True,
    ),
    TableSpec(
        "audit_batches",
        ("id", "device_id", "user_id", "team_id", "first_seq", "last_seq", "event_count",
         "client_chain_head", "prev_chain_head", "chain_head", "sequence_gap", "received_at"),
        ("id",),
        "received_at, rowid",                  # rowid 是 SQLite 的隐式插入序
    ),
    TableSpec(
        "auth_events",
        ("id", "ts", "user_id", "kind", "detail", "remote"),
        ("id",),
        "id",
        serial=True,
    ),
    TableSpec(
        "policies",
        ("id", "scope_type", "scope_id", "version", "document",
         "created_at", "created_by", "note"),
        ("id",),
        "id",
    ),
    TableSpec(
        "secret_meta",
        ("name", "provider", "ref", "fingerprint", "rotated_at", "created_at", "note"),
        ("name",),
        "name",
    ),
)

@dataclass
class MigrationReport:
    """迁移结果。`source` / `target` 是逐表行数，`inserted` 是本次真正写入的行数。"""

    source: dict[str, int]
    target: dict[str, int]
    inserted: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False

    def mismatches(self) -> dict[str, tuple[int, int]]:
        """行数对不上的表：{表名: (源行数, 目标行数)}。"""
        return {
            name: (self.source[name], self.target.get(name, 0))
            for name in self.source
            if self.source[name] != self.target.get(name, 0)
        }

    @property
    def ok(self) -> bool:
        return not self.mismatches()


class Target(Protocol):
    """迁移目标。抽成协议是为了让测试能塞一个内存假目标 —— 本机没有真实 PG，
    但「顺序搬」「幂等」「计数对拍」这些**逻辑**必须能在没有 PG 的情况下被钉住。"""

    def ensure_schema(self) -> None: ...

    def count(self, table: str) -> int: ...

    def insert(self, spec: TableSpec, rows: Sequence[tuple[Any, ...]]) -> int: ...

    def close(self) -> None: ...


# ============================================================================ 读源库
def _connect_source(sqlite_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(sqlite_path))
    conn.row_factory = sqlite3.Row
    return conn


def source_counts(sqlite_path: str | Path) -> dict[str, int]:
    """逐表统计源库行数。表不存在按 0 计（老库可能还没建齐所有表）。"""
    counts: dict[str, int] = {}
    conn = _connect_source(sqlite_path)
    try:
        for spec in TABLES:
            try:
                row = conn.execute(f"SELECT COUNT(*) AS c FROM {spec.name}").fetchone()
                counts[spec.name] = int(row["c"]) if row else 0
            except sqlite3.OperationalError:
                counts[spec.name] = 0
    finally:
        conn.close()
    return counts


def read_rows(sqlite_path: str | Path, spec: TableSpec) -> list[tuple[Any, ...]]:
    """按 `spec.columns` 顺序读出该表全部行，**按 `spec.order_by` 排序**。

    审计表靠这里的排序保证「整表顺序搬」；其他表排序只是让结果确定、可复现。
    """
    conn = _connect_source(sqlite_path)
    try:
        try:
            cursor = conn.execute(
                f"SELECT {', '.join(spec.columns)} FROM {spec.name} ORDER BY {spec.order_by}"
            )
        except sqlite3.OperationalError:
            return []
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        conn.close()


# ============================================================================ 写目标
class PostgresTarget:
    """真实目标：纯标准库 PG 客户端。所有写入都走 `$n` 参数绑定，绝不拼 SQL 值。"""

    def __init__(self, dsn: str) -> None:
        self._pool = PGPool(dsn)

    def ensure_schema(self) -> None:
        ensure_schema(self._pool)

    def count(self, table: str) -> int:
        rows = self._pool.run(f"SELECT COUNT(*) AS c FROM {table}").rows
        return int(rows[0]["c"]) if rows else 0

    def insert(self, spec: TableSpec, rows: Sequence[tuple[Any, ...]]) -> int:
        total = 0
        columns_sql = ", ".join(spec.columns)
        for start in range(0, len(rows), _INSERT_CHUNK):
            chunk = rows[start:start + _INSERT_CHUNK]
            tuples: list[str] = []
            params: list[Any] = []
            for row in chunk:
                placeholders = []
                for value in row:
                    params.append(value)
                    placeholders.append(f"${len(params)}")
                tuples.append("(" + ", ".join(placeholders) + ")")
            sql = (
                f"INSERT INTO {spec.name} ({columns_sql})"
                f" VALUES {', '.join(tuples)} ON CONFLICT DO NOTHING"
            )
            result = self._pool.run(sql, tuple(params))
            total += result.rowcount
        if spec.serial and rows:
            self._advance_sequence(spec.name)
        return total

    def _advance_sequence(self, table: str) -> None:
        """把自增序列推到已搬入的最大 id 之后，避免之后的新写入撞上旧 id。"""
        self._pool.run_simple(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'),"
            f" COALESCE((SELECT MAX(id) FROM {table}), 1))"
        )

    def close(self) -> None:
        self._pool.close()


# ============================================================================ 编排
def migrate(
    sqlite_path: str | Path,
    target: Target,
    *,
    dry_run: bool = False,
    tables: Iterable[TableSpec] = TABLES,
) -> MigrationReport:
    """把源 SQLite 搬到目标。串行、逐表、顺序敏感。

    返回 `MigrationReport`：`source` 为源库计数，`target` 为搬迁后目标库计数，
    `inserted` 为本次真正写入的行数（幂等重跑时会小于源行数）。`dry_run=True`
    时只统计源库、不动目标。
    """
    target.ensure_schema()
    source = source_counts(sqlite_path)
    inserted: dict[str, int] = {}
    if not dry_run:
        for spec in tables:
            rows = read_rows(sqlite_path, spec)
            inserted[spec.name] = target.insert(spec, rows)
    target_counts = {spec.name: target.count(spec.name) for spec in tables}
    return MigrationReport(
        source=source, target=target_counts, inserted=inserted, dry_run=dry_run
    )


__all__ = [
    "MigrationReport",
    "PostgresTarget",
    "TableSpec",
    "TABLES",
    "migrate",
    "read_rows",
    "source_counts",
]