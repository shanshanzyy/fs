"""PostgreSQL 实现（多实例的共享存储）。

与 `SQLiteStore` **语义逐条对齐**，只是换了方言。三条不能让步的东西：

1. **原子判定必须留在一条语句里**。`reserve_usage` / `consume_enrollment` /
   `bootstrap_first_user` 都靠「带条件的 UPDATE + rowcount」或
   「INSERT ... WHERE NOT EXISTS」一次完成判与写 —— 换成「先 SELECT 判、再 UPDATE 写」
   就会在多实例下超发 / 各建一个 owner。这不是性能问题，是正确性问题。
2. **审计链按顺序推进**（`insert_audit_batch` 与链头更新同事务）。链是哈希链，
   顺序错一步，校验就会把正常数据判成「被篡改」。
3. **服务层零改动**。所有 PG 专属语法都关在本文件里，`auth / gateway / audit / policy`
   一行不改 —— 这正是 `store/base.py` 这条缝存在的意义。

方言差异只有三处，且都无害：`MAX(0,x)` → `GREATEST(0,x)`；`INSERT OR IGNORE` →
`ON CONFLICT DO NOTHING`；SQLite 的隐式 `rowid` 在 PG 里不存在，批次排序改用
`last_seq` 作次级键（同一设备上批次按序号推进，语义等价且两端一致）。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator

from ..errors import StoreError
from ..hashing import GENESIS
# 角色白名单与可编辑字段从 SQLite 实现**引**，不在这里再抄一份：两份真相必然漂移。
from .sqlite import _MEMBERSHIP_ROLES, _USER_EDITABLE
from .base import Row, Store
from .pgproto import (
    PGConnection,
    PGConnectionError,
    PGError,
    PGPool,
    _IDEMPOTENT_DDL_STATES,
    split_statements,
)
from .schema import POSTGRES

# 单条 INSERT 里最多拼多少行。PG 参数上限 65535，审计列有 12 个，
# 100 行 = 1200 个参数，留足余量，也避免一次报文过大。
_INSERT_CHUNK = 100

_AUDIT_COLUMNS = (
    "device_id", "user_id", "team_id", "session_id", "seq", "event_type",
    "ts", "payload", "prev_hash", "hash", "batch_id", "received_at",
)
_BATCH_COLUMNS = (
    "id", "device_id", "user_id", "team_id", "first_seq", "last_seq",
    "event_count", "client_chain_head", "prev_chain_head", "chain_head",
    "sequence_gap", "received_at",
)


def ensure_schema(pool: PGPool) -> None:
    """逐条执行 `schema.POSTGRES` 的 DDL（幂等）。

    逐条而不是整段执行，是为了对**并发建表竞态**做兜底：两个实例同时启动时，
    `CREATE TABLE IF NOT EXISTS` 在极窄的时间窗内仍可能撞车并抛
    `duplicate_table`(42P07) / `duplicate_object`(42710)，这两类直接容忍。

    抽成模块级函数是给迁移脚本复用：它需要一个「确保目标库有表」的动作，
    但不该为了这一件事去 new 一个 `PostgresStore`。
    """
    for statement in split_statements(POSTGRES):
        try:
            pool.run_simple(statement)
        except PGError as exc:
            if exc.sqlstate in _IDEMPOTENT_DDL_STATES:
                continue
            raise


def _decode_audit_row(row: Row) -> Row:
    """payload 列存的是 JSON 文本，读出来要还原成对象（与 SQLite 一致）。"""
    raw = row.get("payload")
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {}
        row["payload"] = parsed if isinstance(parsed, dict) else {}
    elif raw is None:
        row["payload"] = {}
    return row


class PostgresStore(Store):
    """共享存储。多实例部署下所有实例连同一个库，配额与审计才有唯一真相。"""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._pool = PGPool(dsn)

    # ============================================================== 连接
    @contextmanager
    def tx(self) -> Iterator[PGConnection]:
        """写事务：显式 `BEGIN` / `COMMIT`，异常 `ROLLBACK`。

        `BEGIN` 之前若连接已断，说明事务里什么都还没做，可以安全地重连再开始
        一次；一旦 `BEGIN` 成功，后续语句失败就**不再自动重试** —— 那会把可能
        已生效的一半再执行一遍。
        """
        conn = self._pool.acquire()
        try:
            conn.exec_simple("BEGIN")
        except PGConnectionError:
            self._pool.drop()
            conn = self._pool.acquire()
            conn.exec_simple("BEGIN")
        try:
            yield conn
        except BaseException:
            try:
                conn.exec_simple("ROLLBACK")
            except Exception:  # noqa: BLE001 - 回滚失败说明连接已废，丢弃即可
                self._pool.drop()
            raise
        conn.exec_simple("COMMIT")

    # ============================================================== 工具
    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> Row | None:
        rows = self._pool.run(sql, params).rows
        return rows[0] if rows else None

    def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[Row]:
        return self._pool.run(sql, params).rows

    def _run(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        return self._pool.run(sql, params).rowcount

    # ============================================================== 生命周期
    def init_schema(self) -> None:
        ensure_schema(self._pool)

    def close(self) -> None:
        self._pool.close()

    def health(self) -> tuple[bool, str]:
        try:
            conn = self._pool.acquire()
            self._pool.run("SELECT 1")
            version = conn.server_version or "?"
            return True, f"postgres ok, {self._pool.dsn.label()}, server {version}"
        except Exception as exc:  # noqa: BLE001
            return False, f"postgres 不可用: {exc}"

    # ============================================================== 用户
    def create_user(
        self,
        user_id: str,
        email: str,
        display_name: str,
        password_hash: str,
        *,
        external_id: str | None = None,
        now: int = 0,
    ) -> Row:
        with self.tx() as conn:
            conn.exec_params(
                "INSERT INTO users (id, email, display_name, password_hash, external_id,"
                " status, created_at, updated_at)"
                " VALUES ($1, $2, $3, $4, $5, 'active', $6, $6)",
                (user_id, email.strip().lower(), display_name, password_hash, external_id, now),
            )
        row = self.get_user(user_id)
        assert row is not None
        return row

    def get_user(self, user_id: str) -> Row | None:
        return self._one("SELECT * FROM users WHERE id = $1", (user_id,))

    def find_user_by_email(self, email: str) -> Row | None:
        return self._one("SELECT * FROM users WHERE email = $1", (email.strip().lower(),))

    def find_user_by_external_id(self, external_id: str) -> Row | None:
        return self._one("SELECT * FROM users WHERE external_id = $1", (external_id,))

    def update_user(self, user_id: str, *, now: int = 0, **fields: Any) -> None:
        unknown = set(fields) - _USER_EDITABLE
        if unknown:
            raise StoreError(f"不允许更新的字段: {', '.join(sorted(unknown))}")
        if not fields:
            return
        # 列名走白名单；值一律走 $n 占位符，绝不拼接
        assignments = ", ".join(f"{key} = ${i}" for i, key in enumerate(fields, start=1))
        params = list(fields.values()) + [now, user_id]
        self._run(
            f"UPDATE users SET {assignments}, updated_at = ${len(fields) + 1}"
            f" WHERE id = ${len(fields) + 2}",
            tuple(params),
        )

    def list_users(self, *, limit: int = 100, offset: int = 0) -> list[Row]:
        return self._all(
            "SELECT id, email, display_name, external_id, status, created_at"
            " FROM users ORDER BY created_at LIMIT $1 OFFSET $2",
            (limit, offset),
        )

    def bootstrap_first_user(
        self,
        user_id: str,
        email: str,
        display_name: str,
        password_hash: str,
        *,
        membership_id: str,
        role: str,
        team_id: str,
        team_slug: str,
        team_name: str,
        now: int = 0,
    ) -> bool:
        if role not in _MEMBERSHIP_ROLES:
            raise StoreError(f"未知角色 {role!r}，可选: {sorted(_MEMBERSHIP_ROLES)}")
        slug = team_slug.strip().lower()
        with self.tx() as conn:
            # 「建号 + 判空」一条语句完成，避免并发 bootstrap 各建一个 owner
            inserted = conn.exec_params(
                "INSERT INTO users (id, email, display_name, password_hash, external_id,"
                " status, created_at, updated_at)"
                " SELECT $1, $2, $3, $4, NULL, 'active', $5, $5"
                " WHERE NOT EXISTS (SELECT 1 FROM users)",
                (user_id, email.strip().lower(), display_name, password_hash, now),
            ).rowcount
            if inserted == 0:
                return False
            # 默认团队按 slug 幂等：已存在就复用
            conn.exec_params(
                "INSERT INTO teams (id, slug, name, created_at) VALUES ($1, $2, $3, $4)"
                " ON CONFLICT (slug) DO NOTHING",
                (team_id, slug, team_name, now),
            )
            row = conn.exec_params("SELECT id FROM teams WHERE slug = $1", (slug,)).rows
            effective_team = str(row[0]["id"]) if row else team_id
            conn.exec_params(
                "INSERT INTO memberships (id, user_id, team_id, role, created_at)"
                " VALUES ($1, $2, $3, $4, $5)"
                " ON CONFLICT (user_id, team_id) DO UPDATE SET role = $4",
                (membership_id, user_id, effective_team, role, now),
            )
            return True

    # ============================================================== 团队
    def create_team(self, team_id: str, slug: str, name: str, *, now: int = 0) -> Row:
        with self.tx() as conn:
            conn.exec_params(
                "INSERT INTO teams (id, slug, name, created_at) VALUES ($1, $2, $3, $4)",
                (team_id, slug.strip().lower(), name, now),
            )
        row = self.get_team(team_id)
        assert row is not None
        return row

    def get_team(self, team_id: str) -> Row | None:
        return self._one("SELECT * FROM teams WHERE id = $1", (team_id,))

    def find_team_by_slug(self, slug: str) -> Row | None:
        return self._one("SELECT * FROM teams WHERE slug = $1", (slug.strip().lower(),))

    def list_teams(self) -> list[Row]:
        return self._all("SELECT * FROM teams ORDER BY slug")

    def add_membership(
        self, membership_id: str, user_id: str, team_id: str, role: str, *, now: int = 0
    ) -> Row:
        if role not in _MEMBERSHIP_ROLES:
            raise StoreError(f"未知角色 {role!r}，可选: {sorted(_MEMBERSHIP_ROLES)}")
        with self.tx() as conn:
            conn.exec_params(
                "INSERT INTO memberships (id, user_id, team_id, role, created_at)"
                " VALUES ($1, $2, $3, $4, $5)"
                " ON CONFLICT (user_id, team_id) DO UPDATE SET role = $4",
                (membership_id, user_id, team_id, role, now),
            )
        row = self.get_membership(user_id, team_id)
        assert row is not None
        return row

    def get_membership(self, user_id: str, team_id: str) -> Row | None:
        return self._one(
            "SELECT * FROM memberships WHERE user_id = $1 AND team_id = $2",
            (user_id, team_id),
        )

    def set_membership_role(self, user_id: str, team_id: str, role: str) -> None:
        if role not in _MEMBERSHIP_ROLES:
            raise StoreError(f"未知角色 {role!r}")
        self._run(
            "UPDATE memberships SET role = $1 WHERE user_id = $2 AND team_id = $3",
            (role, user_id, team_id),
        )

    def remove_membership(self, user_id: str, team_id: str) -> bool:
        return (
            self._run(
                "DELETE FROM memberships WHERE user_id = $1 AND team_id = $2",
                (user_id, team_id),
            )
            > 0
        )

    def list_memberships(self, user_id: str) -> list[Row]:
        return self._all(
            "SELECT m.team_id, m.role, m.created_at,"
            " t.slug AS team_slug, t.name AS team_name"
            " FROM memberships m JOIN teams t ON t.id = m.team_id"
            " WHERE m.user_id = $1 ORDER BY t.slug",
            (user_id,),
        )

    def list_team_members(self, team_id: str) -> list[Row]:
        return self._all(
            "SELECT m.user_id, m.role, m.created_at, u.email, u.display_name, u.status"
            " FROM memberships m JOIN users u ON u.id = m.user_id"
            " WHERE m.team_id = $1 ORDER BY u.email",
            (team_id,),
        )

    # ============================================================== 项目
    def create_project(
        self, project_id: str, team_id: str, name: str, repo: str, *, now: int = 0
    ) -> Row:
        with self.tx() as conn:
            conn.exec_params(
                "INSERT INTO projects (id, team_id, name, repo, created_at)"
                " VALUES ($1, $2, $3, $4, $5)",
                (project_id, team_id, name, repo, now),
            )
        row = self.get_project(project_id)
        assert row is not None
        return row

    def list_projects(self, team_id: str) -> list[Row]:
        return self._all(
            "SELECT * FROM projects WHERE team_id = $1 ORDER BY name", (team_id,)
        )

    def get_project(self, project_id: str) -> Row | None:
        return self._one("SELECT * FROM projects WHERE id = $1", (project_id,))

    def grant_project(
        self, grant_id: str, project_id: str, user_id: str, role: str, *, now: int = 0
    ) -> None:
        with self.tx() as conn:
            conn.exec_params(
                "INSERT INTO project_grants (id, project_id, user_id, role, created_at)"
                " VALUES ($1, $2, $3, $4, $5)"
                " ON CONFLICT (project_id, user_id) DO UPDATE SET role = $4",
                (grant_id, project_id, user_id, role, now),
            )

    def list_project_grants(self, project_id: str) -> list[Row]:
        return self._all(
            "SELECT g.user_id, g.role, u.email, u.display_name"
            " FROM project_grants g JOIN users u ON u.id = g.user_id"
            " WHERE g.project_id = $1 ORDER BY u.email",
            (project_id,),
        )

    def find_project_grant(self, project_id: str, user_id: str) -> Row | None:
        return self._one(
            "SELECT * FROM project_grants WHERE project_id = $1 AND user_id = $2",
            (project_id, user_id),
        )

    # ============================================================== 设备与令牌
    def create_device(
        self,
        device_id: str,
        user_id: str,
        name: str,
        platform: str,
        fingerprint: str,
        *,
        now: int = 0,
    ) -> Row:
        with self.tx() as conn:
            conn.exec_params(
                "INSERT INTO devices (id, user_id, name, platform, fingerprint,"
                " created_at, last_seen_at) VALUES ($1, $2, $3, $4, $5, $6, $6)",
                (device_id, user_id, name, platform, fingerprint, now),
            )
        row = self.get_device(device_id)
        assert row is not None
        return row

    def get_device(self, device_id: str) -> Row | None:
        return self._one("SELECT * FROM devices WHERE id = $1", (device_id,))

    def list_devices(self, user_id: str) -> list[Row]:
        return self._all(
            "SELECT id, user_id, name, platform, created_at, last_seen_at, revoked_at"
            " FROM devices WHERE user_id = $1 ORDER BY created_at DESC",
            (user_id,),
        )

    def revoke_device(self, device_id: str, *, now: int = 0) -> bool:
        with self.tx() as conn:
            changed = conn.exec_params(
                "UPDATE devices SET revoked_at = $1 WHERE id = $2 AND revoked_at IS NULL",
                (now, device_id),
            ).rowcount
            # 吊销设备必须连带吊销它名下所有刷新令牌，否则攻击者还能继续续期
            conn.exec_params(
                "UPDATE refresh_tokens SET revoked_at = $1"
                " WHERE device_id = $2 AND revoked_at IS NULL",
                (now, device_id),
            )
        return changed > 0

    def touch_device(self, device_id: str, *, now: int = 0) -> None:
        self._run(
            "UPDATE devices SET last_seen_at = $1 WHERE id = $2", (now, device_id)
        )

    def create_refresh_token(
        self,
        token_id: str,
        device_id: str,
        user_id: str,
        family_id: str,
        token_hash: str,
        issued_at: int,
        expires_at: int,
        *,
        client_ip: str = "",
    ) -> Row:
        with self.tx() as conn:
            conn.exec_params(
                "INSERT INTO refresh_tokens (id, device_id, user_id, family_id, token_hash,"
                " issued_at, expires_at, client_ip)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                (token_id, device_id, user_id, family_id, token_hash, issued_at, expires_at, client_ip),
            )
        return self._one("SELECT * FROM refresh_tokens WHERE id = $1", (token_id,)) or {}

    def find_refresh_by_hash(self, token_hash: str) -> Row | None:
        return self._one("SELECT * FROM refresh_tokens WHERE token_hash = $1", (token_hash,))

    def mark_refresh_used(self, token_id: str, now: int, replaced_by: str) -> None:
        self._run(
            "UPDATE refresh_tokens SET used_at = $1, replaced_by = $2 WHERE id = $3",
            (now, replaced_by, token_id),
        )

    def revoke_refresh_family(self, family_id: str, now: int) -> int:
        return self._run(
            "UPDATE refresh_tokens SET revoked_at = $1"
            " WHERE family_id = $2 AND revoked_at IS NULL",
            (now, family_id),
        )

    def revoke_refresh_token(self, token_id: str, now: int) -> None:
        self._run(
            "UPDATE refresh_tokens SET revoked_at = $1 WHERE id = $2 AND revoked_at IS NULL",
            (now, token_id),
        )

    def create_enrollment(
        self,
        enrollment_id: str,
        token_hash: str,
        *,
        team_id: str | None,
        role: str,
        created_by: str,
        now: int,
        expires_at: int,
        max_uses: int = 1,
    ) -> Row:
        with self.tx() as conn:
            conn.exec_params(
                "INSERT INTO enrollment_tokens (id, token_hash, team_id, role, created_by,"
                " created_at, expires_at, max_uses, uses)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 0)",
                (enrollment_id, token_hash, team_id, role, created_by, now, expires_at, max_uses),
            )
        return self._one(
            "SELECT * FROM enrollment_tokens WHERE id = $1", (enrollment_id,)
        ) or {}

    def find_enrollment_by_hash(self, token_hash: str) -> Row | None:
        return self._one(
            "SELECT * FROM enrollment_tokens WHERE token_hash = $1", (token_hash,)
        )

    def consume_enrollment(self, enrollment_id: str, now: int) -> bool:
        # 条件写在同一条 UPDATE 里，靠 rowcount 判定，避免「先查后写」竞态
        return (
            self._run(
                "UPDATE enrollment_tokens SET uses = uses + 1"
                " WHERE id = $1 AND revoked_at IS NULL AND expires_at > $2"
                "   AND uses < max_uses",
                (enrollment_id, now),
            )
            > 0
        )

    def list_enrollments(self, *, team_id: str = "") -> list[Row]:
        # 台账刻意**不 SELECT token_hash**（同 SQLite 驱动口径）：哈希没有展示价值。
        cols = (
            "id, team_id, role, created_by, created_at, expires_at, max_uses, uses, revoked_at"
        )
        if team_id:
            return self._all(
                "SELECT " + cols + " FROM enrollment_tokens WHERE team_id = $1"
                " ORDER BY created_at DESC",
                (team_id,),
            )
        return self._all("SELECT " + cols + " FROM enrollment_tokens ORDER BY created_at DESC")

    # ============================================================== 客户端发布（M9）
    _RELEASE_COLS = (
        "id, channel, version, platform, arch, url, sha256, size, notes, mandatory,"
        " min_version, rollout_percent, seq, signature, signature_alg, pubkey_kid,"
        " published_at, revoked_at, created_by"
    )

    def insert_release(
        self,
        release_id: str,
        *,
        channel: str,
        version: str,
        platform: str,
        arch: str,
        url: str,
        sha256: str,
        size: int,
        notes: str,
        mandatory: bool,
        min_version: str,
        rollout_percent: int,
        seq: int,
        signature: str,
        signature_alg: str,
        pubkey_kid: str,
        created_by: str,
        now: int,
    ) -> Row:
        with self.tx() as conn:
            conn.exec_params(
                "INSERT INTO client_releases (id, channel, version, platform, arch, url,"
                " sha256, size, notes, mandatory, min_version, rollout_percent, seq,"
                " signature, signature_alg, pubkey_kid, published_at, created_by)"
                " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)",
                (
                    release_id,
                    channel,
                    version,
                    platform,
                    arch,
                    url,
                    sha256,
                    size,
                    notes[:2000],
                    mandatory,
                    min_version,
                    rollout_percent,
                    seq,
                    signature,
                    signature_alg,
                    pubkey_kid,
                    now,
                    created_by,
                ),
            )
        return (
            self._one(
                "SELECT " + self._RELEASE_COLS + " FROM client_releases WHERE id = $1",
                (release_id,),
            )
            or {}
        )

    def list_releases(
        self,
        *,
        channel: str = "",
        platform: str = "",
        arch: str = "",
        include_revoked: bool = False,
        limit: int = 100,
    ) -> list[Row]:
        params: list[object] = []
        where: list[str] = []
        if channel:
            params.append(channel)
            where.append(f"channel = ${len(params)}")
        if platform:
            params.append(platform)
            where.append(f"platform = ${len(params)}")
        if arch:
            params.append(arch)
            where.append(f"arch = ${len(params)}")
        if not include_revoked:
            where.append("revoked_at IS NULL")
        sql = "SELECT " + self._RELEASE_COLS + " FROM client_releases"
        if where:
            sql += " WHERE " + " AND ".join(where)
        params.append(limit)
        sql += f" ORDER BY seq DESC LIMIT ${len(params)}"
        return self._all(sql, tuple(params))

    def get_release(self, release_id: str) -> Row | None:
        return self._one(
            "SELECT " + self._RELEASE_COLS + " FROM client_releases WHERE id = $1",
            (release_id,),
        )

    def latest_release(
        self, *, channel: str, platform: str, arch: str, now: int
    ) -> Row | None:
        return self._one(
            "SELECT " + self._RELEASE_COLS + " FROM client_releases"
            " WHERE channel = $1 AND platform = $2 AND arch = $3"
            "   AND revoked_at IS NULL AND published_at <= $4"
            " ORDER BY seq DESC LIMIT 1",
            (channel, platform, arch, now),
        )

    def next_release_seq(self) -> int:
        row = self._one("SELECT COALESCE(MAX(seq), 0) AS m FROM client_releases")
        return int((row or {}).get("m") or 0) + 1

    def revoke_release(self, release_id: str, now: int) -> bool:
        return (
            self._run(
                "UPDATE client_releases SET revoked_at = $1"
                " WHERE id = $2 AND revoked_at IS NULL",
                (now, release_id),
            )
            > 0
        )

    def revoke_access_jti(self, jti: str, expires_at: int, now: int) -> None:
        with self.tx() as conn:
            conn.exec_params(
                "INSERT INTO revoked_tokens (jti, expires_at, revoked_at)"
                " VALUES ($1, $2, $3) ON CONFLICT (jti) DO NOTHING",
                (jti, expires_at, now),
            )

    def is_access_jti_revoked(self, jti: str) -> bool:
        return self._one("SELECT 1 AS x FROM revoked_tokens WHERE jti = $1", (jti,)) is not None

    def purge_expired(self, now: int) -> dict[str, int]:
        result: dict[str, int] = {}
        with self.tx() as conn:
            result["revoked_tokens"] = conn.exec_params(
                "DELETE FROM revoked_tokens WHERE expires_at < $1", (now,)
            ).rowcount
            # 过期且未使用的刷新令牌可清；用过的（replaced_by 非空）留着当重用证据
            result["refresh_tokens"] = conn.exec_params(
                "DELETE FROM refresh_tokens WHERE expires_at < $1 AND used_at IS NULL", (now,)
            ).rowcount
            result["enrollment_tokens"] = conn.exec_params(
                "DELETE FROM enrollment_tokens WHERE expires_at < $1 AND uses = 0", (now,)
            ).rowcount
        return result

    def record_auth_event(
        self, kind: str, *, user_id: str = "", detail: str = "", remote: str = "", now: int = 0
    ) -> None:
        self._run(
            "INSERT INTO auth_events (ts, user_id, kind, detail, remote)"
            " VALUES ($1, $2, $3, $4, $5)",
            (now, user_id, kind, detail[:200], remote[:64]),
        )

    def list_auth_events(self, *, limit: int = 100, user_id: str = "") -> list[Row]:
        if user_id:
            return self._all(
                "SELECT * FROM auth_events WHERE user_id = $1 ORDER BY ts DESC LIMIT $2",
                (user_id, limit),
            )
        return self._all(
            "SELECT * FROM auth_events ORDER BY ts DESC LIMIT $1", (limit,)
        )

    # ============================================================== 配额
    def upsert_quota_policy(
        self,
        policy_id: str,
        scope_type: str,
        scope_id: str,
        *,
        daily_cloud_tokens: int,
        daily_requests: int,
        requests_per_minute: int,
        updated_by: str,
        now: int,
    ) -> Row:
        with self.tx() as conn:
            conn.exec_params(
                "INSERT INTO quota_policies (id, scope_type, scope_id, daily_cloud_tokens,"
                " daily_requests, requests_per_minute, updated_at, updated_by)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)"
                " ON CONFLICT (scope_type, scope_id) DO UPDATE SET"
                "   daily_cloud_tokens = $4, daily_requests = $5,"
                "   requests_per_minute = $6, updated_at = $7, updated_by = $8",
                (
                    policy_id, scope_type, scope_id, daily_cloud_tokens,
                    daily_requests, requests_per_minute, now, updated_by,
                ),
            )
        return self.get_quota_policy(scope_type, scope_id) or {}

    def get_quota_policy(self, scope_type: str, scope_id: str) -> Row | None:
        return self._one(
            "SELECT * FROM quota_policies WHERE scope_type = $1 AND scope_id = $2",
            (scope_type, scope_id),
        )

    def list_quota_policies(self) -> list[Row]:
        return self._all("SELECT * FROM quota_policies ORDER BY scope_type, scope_id")

    def upsert_budget_policy(
        self,
        budget_id: str,
        scope_type: str,
        scope_id: str,
        *,
        budget: int,
        unit_price: int,
        window: int,
        updated_by: str,
        now: int,
    ) -> Row:
        with self.tx() as conn:
            conn.exec_params(
                "INSERT INTO quota_budgets (id, scope_type, scope_id, budget, unit_price,"
                " window, updated_at, updated_by)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)"
                " ON CONFLICT (scope_type, scope_id) DO UPDATE SET"
                "   budget = $4, unit_price = $5, window = $6,"
                "   updated_at = $7, updated_by = $8",
                (
                    budget_id, scope_type, scope_id, int(budget),
                    int(unit_price), int(window), now, updated_by,
                ),
            )
        return self.get_budget_policy(scope_type, scope_id) or {}

    def get_budget_policy(self, scope_type: str, scope_id: str) -> Row | None:
        return self._one(
            "SELECT * FROM quota_budgets WHERE scope_type = $1 AND scope_id = $2",
            (scope_type, scope_id),
        )

    def reserve_usage(
        self,
        day: str,
        user_id: str,
        team_id: str,
        *,
        estimate: int,
        token_limit: int,
        request_limit: int,
        now: int,
    ) -> bool:
        estimate = max(0, int(estimate))
        with self.tx() as conn:
            conn.exec_params(
                "INSERT INTO usage_counters (day, user_id, team_id, updated_at)"
                " VALUES ($1, $2, $3, $4) ON CONFLICT (day, user_id) DO NOTHING",
                (day, user_id, team_id, now),
            )
            # 判定与写入同一条语句 —— 这是不超发的关键（token_limit/request_limit 为 0 表示不限）
            return (
                conn.exec_params(
                    "UPDATE usage_counters"
                    "   SET reserved_tokens = reserved_tokens + $1, updated_at = $2"
                    " WHERE day = $3 AND user_id = $4"
                    "   AND ($5 <= 0 OR cloud_tokens + reserved_tokens + $1 <= $5)"
                    "   AND ($6 <= 0 OR requests < $6)",
                    (estimate, now, day, user_id, token_limit, request_limit),
                ).rowcount
                > 0
            )

    def settle_usage(
        self,
        day: str,
        user_id: str,
        team_id: str,
        *,
        estimate: int,
        actual_tokens: int,
        is_cloud: bool,
        counted_request: bool,
        now: int,
    ) -> None:
        cloud = max(0, int(actual_tokens)) if is_cloud else 0
        local = 0 if is_cloud else max(0, int(actual_tokens))
        with self.tx() as conn:
            conn.exec_params(
                "INSERT INTO usage_counters (day, user_id, team_id, updated_at)"
                " VALUES ($1, $2, $3, $4)"
                " ON CONFLICT (day, user_id) DO UPDATE SET team_id = $3",
                (day, user_id, team_id, now),
            )
            conn.exec_params(
                "UPDATE usage_counters SET"
                "   reserved_tokens = GREATEST(0, reserved_tokens - $1),"
                "   cloud_tokens = cloud_tokens + $2,"
                "   local_tokens = local_tokens + $3,"
                "   requests = requests + $4,"
                "   updated_at = $5"
                " WHERE day = $6 AND user_id = $7",
                (
                    max(0, int(estimate)), cloud, local,
                    1 if counted_request else 0, now, day, user_id,
                ),
            )

    def release_usage(self, day: str, user_id: str, estimate: int, *, now: int = 0) -> None:
        self._run(
            "UPDATE usage_counters SET reserved_tokens = GREATEST(0, reserved_tokens - $1),"
            " updated_at = $2 WHERE day = $3 AND user_id = $4",
            (max(0, int(estimate)), now, day, user_id),
        )

    def get_usage(self, day: str, user_id: str) -> Row:
        return self._one(
            "SELECT * FROM usage_counters WHERE day = $1 AND user_id = $2", (day, user_id)
        ) or {
            "day": day,
            "user_id": user_id,
            "team_id": "",
            "cloud_tokens": 0,
            "local_tokens": 0,
            "requests": 0,
            "reserved_tokens": 0,
        }

    def list_usage(self, day: str, *, team_id: str = "") -> list[Row]:
        if team_id:
            return self._all(
                "SELECT * FROM usage_counters WHERE day = $1 AND team_id = $2"
                " ORDER BY cloud_tokens DESC",
                (day, team_id),
            )
        return self._all(
            "SELECT * FROM usage_counters WHERE day = $1 ORDER BY cloud_tokens DESC", (day,)
        )

    def usage_series(
        self, *, user_id: str = "", team_id: str = "", since_day: str, limit: int = 60
    ) -> list[Row]:
        clauses = ["day >= $1"]
        params: list[Any] = [since_day]
        if user_id:
            params.append(user_id)
            clauses.append(f"user_id = ${len(params)}")
        if team_id:
            params.append(team_id)
            clauses.append(f"team_id = ${len(params)}")
        params.append(limit)
        where = " AND ".join(clauses)
        return self._all(
            f"SELECT day, user_id, team_id, cloud_tokens, local_tokens, requests"
            f" FROM usage_counters WHERE {where} ORDER BY day DESC LIMIT ${len(params)}",
            tuple(params),
        )

    def hit_rate_limit(self, scope_key: str, window_start: int, limit: int) -> tuple[bool, int]:
        if limit <= 0:
            return True, 0
        # 计数与自增合并在一条 INSERT ... ON CONFLICT ... RETURNING 里，天然原子
        row = self._one(
            "INSERT INTO rate_counters (scope_key, window_start, count)"
            " VALUES ($1, $2, 1)"
            " ON CONFLICT (scope_key, window_start)"
            " DO UPDATE SET count = rate_counters.count + 1"
            " RETURNING count",
            (scope_key, window_start),
        )
        count = int(row["count"]) if row else 1
        return count <= limit, count

    # ============================================================== 审计
    def insert_audit_events(self, rows: list[Row]) -> int:
        if not rows:
            return 0
        # payload 是 JSON 文本列，序列化在存储层做（见 sqlite.py 的说明：链哈希依赖它）
        prepared = [
            {
                **{c: row.get(c) for c in _AUDIT_COLUMNS},
                "payload": json.dumps(
                    row.get("payload") or {},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ),
            }
            for row in rows
        ]
        columns_sql = ", ".join(_AUDIT_COLUMNS)
        total = 0
        with self.tx() as conn:
            for start in range(0, len(prepared), _INSERT_CHUNK):
                chunk = prepared[start:start + _INSERT_CHUNK]
                tuples: list[str] = []
                params: list[Any] = []
                for row in chunk:
                    placeholders = []
                    for column in _AUDIT_COLUMNS:
                        params.append(row[column])
                        placeholders.append(f"${len(params)}")
                    tuples.append("(" + ", ".join(placeholders) + ")")
                # UNIQUE(device_id, session_id, seq) 让重传天然幂等
                total += conn.exec_params(
                    f"INSERT INTO audit_events ({columns_sql}) VALUES {', '.join(tuples)}"
                    f" ON CONFLICT DO NOTHING",
                    tuple(params),
                ).rowcount
        return total

    def insert_audit_batch(self, row: Row) -> None:
        columns_sql = ", ".join(_BATCH_COLUMNS)
        placeholders = ", ".join(f"${i}" for i in range(1, len(_BATCH_COLUMNS) + 1))
        with self.tx() as conn:
            conn.exec_params(
                f"INSERT INTO audit_batches ({columns_sql}) VALUES ({placeholders})"
                f" ON CONFLICT DO NOTHING",
                tuple(row.get(c) for c in _BATCH_COLUMNS),
            )

    def get_batch_chain_head(self, device_id: str) -> str:
        # SQLite 用 rowid 作次级键；PG 无 rowid，用 last_seq —— 同一设备批次按序号推进，
        # 语义等价（received_at 相同时仍能取到最后一批）。
        row = self._one(
            "SELECT chain_head FROM audit_batches WHERE device_id = $1"
            " ORDER BY received_at DESC, last_seq DESC LIMIT 1",
            (device_id,),
        )
        return str(row["chain_head"]) if row else GENESIS

    def get_device_last_seq(self, device_id: str, session_id: str) -> int:
        row = self._one(
            "SELECT MAX(seq) AS s FROM audit_events WHERE device_id = $1 AND session_id = $2",
            (device_id, session_id),
        )
        return int(row["s"]) if row and row["s"] is not None else 0

    def get_session_last_hash(self, device_id: str, session_id: str) -> str:
        row = self._one(
            "SELECT hash FROM audit_events WHERE device_id = $1 AND session_id = $2"
            " ORDER BY seq DESC LIMIT 1",
            (device_id, session_id),
        )
        return str(row["hash"]) if row else GENESIS

    def query_audit_events(
        self,
        *,
        team_id: str = "",
        user_id: str = "",
        device_id: str = "",
        session_id: str = "",
        event_type: str = "",
        since: int = 0,
        until: int = 0,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Row]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("team_id", team_id), ("user_id", user_id),
            ("device_id", device_id), ("session_id", session_id),
            ("event_type", event_type),
        ):
            if value:
                params.append(value)
                clauses.append(f"{column} = ${len(params)}")
        if since:
            params.append(since)
            clauses.append(f"ts >= ${len(params)}")
        if until:
            params.append(until)
            clauses.append(f"ts <= ${len(params)}")
        params.extend([limit, offset])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._all(
            f"SELECT * FROM audit_events{where} ORDER BY ts DESC, id DESC"
            f" LIMIT ${len(params) - 1} OFFSET ${len(params)}",
            tuple(params),
        )
        return [_decode_audit_row(row) for row in rows]

    def count_audit_events(
        self, *, team_id: str = "", user_id: str = "", since: int = 0
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if team_id:
            params.append(team_id)
            clauses.append(f"team_id = ${len(params)}")
        if user_id:
            params.append(user_id)
            clauses.append(f"user_id = ${len(params)}")
        if since:
            params.append(since)
            clauses.append(f"ts >= ${len(params)}")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self._one(f"SELECT COUNT(*) AS c FROM audit_events{where}", tuple(params))
        return int(row["c"]) if row else 0

    def audit_summary(self, *, team_id: str = "", since: int = 0) -> list[Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if team_id:
            params.append(team_id)
            clauses.append(f"team_id = ${len(params)}")
        if since:
            params.append(since)
            clauses.append(f"ts >= ${len(params)}")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._all(
            f"SELECT event_type, COUNT(*) AS n, COUNT(*) AS events, MAX(ts) AS last_ts"
            f" FROM audit_events{where} GROUP BY event_type ORDER BY n DESC",
            tuple(params),
        )

    def server_chain(self, device_id: str, *, limit: int = 1000) -> list[Row]:
        return self._all(
            "SELECT * FROM audit_batches WHERE device_id = $1"
            " ORDER BY received_at ASC, last_seq ASC LIMIT $2",
            (device_id, limit),
        )

    # ============================================================== 策略
    def insert_policy(
        self,
        policy_id: str,
        scope_type: str,
        scope_id: str,
        version: int,
        document: str,
        *,
        created_by: str,
        note: str,
        now: int,
    ) -> Row:
        with self.tx() as conn:
            conn.exec_params(
                "INSERT INTO policies (id, scope_type, scope_id, version, document,"
                " created_at, created_by, note)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                (policy_id, scope_type, scope_id, version, document, now, created_by, note),
            )
        row = self._one("SELECT * FROM policies WHERE id = $1", (policy_id,))
        assert row is not None
        return row

    def next_policy_version(self, scope_type: str, scope_id: str) -> int:
        row = self._one(
            "SELECT MAX(version) AS v FROM policies WHERE scope_type = $1 AND scope_id = $2",
            (scope_type, scope_id),
        )
        current = int(row["v"]) if row and row["v"] is not None else 0
        return current + 1

    def get_latest_policy(self, scope_type: str, scope_id: str) -> Row | None:
        return self._one(
            "SELECT * FROM policies WHERE scope_type = $1 AND scope_id = $2"
            " ORDER BY version DESC LIMIT 1",
            (scope_type, scope_id),
        )

    def list_policy_versions(
        self, scope_type: str, scope_id: str, *, limit: int = 20
    ) -> list[Row]:
        return self._all(
            "SELECT id, scope_type, scope_id, version, created_at, created_by, note"
            " FROM policies WHERE scope_type = $1 AND scope_id = $2"
            " ORDER BY version DESC LIMIT $3",
            (scope_type, scope_id, limit),
        )

    # ============================================================== 密钥台账
    def upsert_secret_meta(
        self,
        name: str,
        provider: str,
        ref: str,
        fingerprint: str,
        *,
        note: str = "",
        now: int = 0,
    ) -> Row:
        with self.tx() as conn:
            conn.exec_params(
                "INSERT INTO secret_meta (name, provider, ref, fingerprint, rotated_at,"
                " created_at, note) VALUES ($1, $2, $3, $4, $5, $5, $6)"
                " ON CONFLICT (name) DO UPDATE SET"
                "   provider = $2, ref = $3, fingerprint = $4, rotated_at = $5, note = $6",
                (name, provider, ref, fingerprint, now, note),
            )
        return self._one("SELECT * FROM secret_meta WHERE name = $1", (name,)) or {}

    def get_secret_meta(self, name: str) -> Row | None:
        return self._one("SELECT * FROM secret_meta WHERE name = $1", (name,))

    def list_secret_meta(self) -> list[Row]:
        return self._all("SELECT * FROM secret_meta ORDER BY name")


__all__ = ["PostgresStore", "ensure_schema"]