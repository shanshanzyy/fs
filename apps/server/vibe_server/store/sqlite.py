"""SQLite 实现。

三个必须做对的地方：

1. **每个线程一条连接**（`threading.local`）。`sqlite3` 的连接不是线程安全的，
   而在 HTTP 服务里每个请求都是一条新线程 —— 共用一条连接会随机报
   「SQLite objects created in a thread can only be used in that same thread」。
2. **写事务用 `BEGIN IMMEDIATE`**。默认的延迟事务是「先读后升写」，
   两个并发请求会同时读到旧值再各自去写，配额就超发了。
   `IMMEDIATE` 在事务开始时就拿写锁，把并发问题在源头堵住。
3. **所有读写显式开事务**。Python 3.12+ 起 `sqlite3` 的隐式提交行为有变化，
   依赖隐式行为迟早被版本升级咬一口。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..errors import StoreError
from ..hashing import GENESIS
from ..rbac import ROLES as _RBAC_ROLES
from .base import Row, Store
from .schema import SQLITE

_USER_EDITABLE = {
    "display_name", "password_hash", "external_id", "status", "email",
}
# 角色白名单**从 RBAC 引**，不在这里再抄一份。
# 抄一份的后果是实打实的：这里曾只列了 developer/team_admin，
# 而 `owner` 是该有的角色 —— 结果是「创建组织所有者」直接 500，
# 因为存储层把合法角色当成了非法输入。两份真相必然漂移。
_MEMBERSHIP_ROLES = set(_RBAC_ROLES)


def _new_id() -> str:
    return uuid.uuid4().hex


class SQLiteStore(Store):
    """单文件存储。适合「一台机器、几十个人」这个规模。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()

    # ------------------------------------------------------------------ 连接
    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self.path),
                timeout=10.0,
                isolation_level=None,      # 自己管事务，不用 sqlite3 的隐式行为
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """写事务。

        `_write_lock` 是进程内的公平排队；`BEGIN IMMEDIATE` 是跨进程的锁。
        两者都需要：前者避免同进程线程把 `busy_timeout` 耗光，后者保证多进程安全。
        """
        conn = self._conn
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    # ------------------------------------------------------------------ 工具
    def _one(self, sql: str, params: dict[str, Any] | tuple = ()) -> Row | None:
        cur = self._conn.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params: dict[str, Any] | tuple = ()) -> list[Row]:
        return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def _run(self, sql: str, params: dict[str, Any] | tuple = ()) -> int:
        cur = self._conn.execute(sql, params)
        return cur.rowcount

    # ============================================================== 生命周期
    def init_schema(self) -> None:
        # executescript 会隐式提交，因此不能包在 tx() 里
        self._conn.executescript(SQLITE)

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def health(self) -> tuple[bool, str]:
        try:
            self._conn.execute("SELECT 1").fetchone()
            size = self.path.stat().st_size if self.path.exists() else 0
            return True, f"sqlite ok, {size / 1024:.1f} KiB"
        except Exception as exc:  # noqa: BLE001
            return False, f"sqlite 不可用: {exc}"

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
            conn.execute(
                "INSERT INTO users (id, email, display_name, password_hash, external_id,"
                " status, created_at, updated_at)"
                " VALUES (:id, :email, :name, :pwd, :ext, 'active', :now, :now)",
                {
                    "id": user_id,
                    "email": email.strip().lower(),
                    "name": display_name,
                    "pwd": password_hash,
                    "ext": external_id,
                    "now": now,
                },
            )
        row = self.get_user(user_id)
        assert row is not None
        return row

    def get_user(self, user_id: str) -> Row | None:
        return self._one("SELECT * FROM users WHERE id = :id", {"id": user_id})

    def find_user_by_email(self, email: str) -> Row | None:
        return self._one(
            "SELECT * FROM users WHERE email = :email", {"email": email.strip().lower()}
        )

    def find_user_by_external_id(self, external_id: str) -> Row | None:
        return self._one(
            "SELECT * FROM users WHERE external_id = :ext", {"ext": external_id}
        )

    def update_user(self, user_id: str, *, now: int = 0, **fields: Any) -> None:
        # 字段名走白名单，绝不把外部传入的 key 直接拼进 SQL
        unknown = set(fields) - _USER_EDITABLE
        if unknown:
            raise StoreError(f"不允许更新的字段: {', '.join(sorted(unknown))}")
        if not fields:
            return
        assignments = ", ".join(f"{key} = :{key}" for key in fields)
        params = dict(fields, uid=user_id, now=now)
        self._run(
            f"UPDATE users SET {assignments}, updated_at = :now WHERE id = :uid", params
        )

    def list_users(self, *, limit: int = 100, offset: int = 0) -> list[Row]:
        return self._all(
            "SELECT id, email, display_name, external_id, status, created_at"
            " FROM users ORDER BY created_at LIMIT :limit OFFSET :offset",
            {"limit": limit, "offset": offset},
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
        """原子地创建第一个组织所有者：建号 + 确保默认团队 + 写入 membership。

        三件事必须在**同一个事务**里：
        * 分开做 → 并发 bootstrap 是「先查后写」，会各建一个 owner；
        * 分开做 → 进程在中间崩溃会留下「有账号但没有 membership」的 owner，
          而角色全部由 membership 推导，表现为登录进去每个接口都 403。
        返回 False 表示已有用户，此时不产生任何写入。
        """
        if role not in _MEMBERSHIP_ROLES:
            raise StoreError(f"未知角色 {role!r}，可选: {sorted(_MEMBERSHIP_ROLES)}")
        slug = team_slug.strip().lower()
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO users (id, email, display_name, password_hash, external_id,"
                " status, created_at, updated_at)"
                " SELECT :id,:email,:name,:pwd,NULL,'active',:now,:now"
                " WHERE NOT EXISTS (SELECT 1 FROM users)",
                {
                    "id": user_id,
                    "email": email.strip().lower(),
                    "name": display_name,
                    "pwd": password_hash,
                    "now": now,
                },
            )
            if cur.rowcount == 0:
                return False
            # 默认团队按 slug 幂等：已存在就复用，别造出第二个默认团队。
            conn.execute(
                "INSERT INTO teams (id, slug, name, created_at)"
                " VALUES (:id,:slug,:name,:now)"
                " ON CONFLICT(slug) DO NOTHING",
                {"id": team_id, "slug": slug, "name": team_name, "now": now},
            )
            row = conn.execute(
                "SELECT id FROM teams WHERE slug = :slug", {"slug": slug}
            ).fetchone()
            effective_team = str(row["id"]) if row is not None else team_id
            conn.execute(
                "INSERT INTO memberships (id, user_id, team_id, role, created_at)"
                " VALUES (:id,:uid,:tid,:role,:now)"
                " ON CONFLICT(user_id, team_id) DO UPDATE SET role = :role",
                {
                    "id": membership_id,
                    "uid": user_id,
                    "tid": effective_team,
                    "role": role,
                    "now": now,
                },
            )
            return True

    # ============================================================== 团队
    def create_team(self, team_id: str, slug: str, name: str, *, now: int = 0) -> Row:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO teams (id, slug, name, created_at) VALUES (:id,:slug,:name,:now)",
                {"id": team_id, "slug": slug.strip().lower(), "name": name, "now": now},
            )
        row = self.get_team(team_id)
        assert row is not None
        return row

    def get_team(self, team_id: str) -> Row | None:
        return self._one("SELECT * FROM teams WHERE id = :id", {"id": team_id})

    def find_team_by_slug(self, slug: str) -> Row | None:
        return self._one(
            "SELECT * FROM teams WHERE slug = :slug", {"slug": slug.strip().lower()}
        )

    def list_teams(self) -> list[Row]:
        return self._all("SELECT * FROM teams ORDER BY slug")

    def add_membership(
        self, membership_id: str, user_id: str, team_id: str, role: str, *, now: int = 0
    ) -> Row:
        if role not in _MEMBERSHIP_ROLES:
            raise StoreError(f"未知角色 {role!r}，可选: {sorted(_MEMBERSHIP_ROLES)}")
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO memberships (id, user_id, team_id, role, created_at)"
                " VALUES (:id,:uid,:tid,:role,:now)"
                " ON CONFLICT(user_id, team_id) DO UPDATE SET role = :role",
                {
                    "id": membership_id,
                    "uid": user_id,
                    "tid": team_id,
                    "role": role,
                    "now": now,
                },
            )
        row = self.get_membership(user_id, team_id)
        assert row is not None
        return row

    def get_membership(self, user_id: str, team_id: str) -> Row | None:
        return self._one(
            "SELECT * FROM memberships WHERE user_id = :uid AND team_id = :tid",
            {"uid": user_id, "tid": team_id},
        )

    def set_membership_role(self, user_id: str, team_id: str, role: str) -> None:
        if role not in _MEMBERSHIP_ROLES:
            raise StoreError(f"未知角色 {role!r}")
        self._run(
            "UPDATE memberships SET role = :role WHERE user_id = :uid AND team_id = :tid",
            {"role": role, "uid": user_id, "tid": team_id},
        )

    def remove_membership(self, user_id: str, team_id: str) -> bool:
        return (
            self._run(
                "DELETE FROM memberships WHERE user_id = :uid AND team_id = :tid",
                {"uid": user_id, "tid": team_id},
            )
            > 0
        )

    def list_memberships(self, user_id: str) -> list[Row]:
        return self._all(
            "SELECT m.team_id, m.role, m.created_at,"
            " t.slug AS team_slug, t.name AS team_name"
            " FROM memberships m JOIN teams t ON t.id = m.team_id"
            " WHERE m.user_id = :uid ORDER BY t.slug",
            {"uid": user_id},
        )

    def list_team_members(self, team_id: str) -> list[Row]:
        return self._all(
            "SELECT m.user_id, m.role, m.created_at, u.email, u.display_name, u.status"
            " FROM memberships m JOIN users u ON u.id = m.user_id"
            " WHERE m.team_id = :tid ORDER BY u.email",
            {"tid": team_id},
        )

    # ============================================================== 项目
    def create_project(
        self, project_id: str, team_id: str, name: str, repo: str, *, now: int = 0
    ) -> Row:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO projects (id, team_id, name, repo, created_at)"
                " VALUES (:id,:tid,:name,:repo,:now)",
                {"id": project_id, "tid": team_id, "name": name, "repo": repo, "now": now},
            )
        row = self.get_project(project_id)
        assert row is not None
        return row

    def list_projects(self, team_id: str) -> list[Row]:
        return self._all(
            "SELECT * FROM projects WHERE team_id = :tid ORDER BY name", {"tid": team_id}
        )

    def get_project(self, project_id: str) -> Row | None:
        return self._one("SELECT * FROM projects WHERE id = :id", {"id": project_id})

    def grant_project(
        self, grant_id: str, project_id: str, user_id: str, role: str, *, now: int = 0
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO project_grants (id, project_id, user_id, role, created_at)"
                " VALUES (:id,:pid,:uid,:role,:now)"
                " ON CONFLICT(project_id, user_id) DO UPDATE SET role = :role",
                {
                    "id": grant_id,
                    "pid": project_id,
                    "uid": user_id,
                    "role": role,
                    "now": now,
                },
            )

    def list_project_grants(self, project_id: str) -> list[Row]:
        return self._all(
            "SELECT g.user_id, g.role, u.email, u.display_name"
            " FROM project_grants g JOIN users u ON u.id = g.user_id"
            " WHERE g.project_id = :pid ORDER BY u.email",
            {"pid": project_id},
        )

    def find_project_grant(self, project_id: str, user_id: str) -> Row | None:
        return self._one(
            "SELECT * FROM project_grants WHERE project_id = :pid AND user_id = :uid",
            {"pid": project_id, "uid": user_id},
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
            conn.execute(
                "INSERT INTO devices (id, user_id, name, platform, fingerprint,"
                " created_at, last_seen_at) VALUES (:id,:uid,:name,:plat,:fp,:now,:now)",
                {
                    "id": device_id,
                    "uid": user_id,
                    "name": name,
                    "plat": platform,
                    "fp": fingerprint,
                    "now": now,
                },
            )
        row = self.get_device(device_id)
        assert row is not None
        return row

    def get_device(self, device_id: str) -> Row | None:
        return self._one("SELECT * FROM devices WHERE id = :id", {"id": device_id})

    def list_devices(self, user_id: str) -> list[Row]:
        return self._all(
            "SELECT id, user_id, name, platform, created_at, last_seen_at, revoked_at"
            " FROM devices WHERE user_id = :uid ORDER BY created_at DESC",
            {"uid": user_id},
        )

    def revoke_device(self, device_id: str, *, now: int = 0) -> bool:
        with self.tx() as conn:
            changed = conn.execute(
                "UPDATE devices SET revoked_at = :now"
                " WHERE id = :id AND revoked_at IS NULL",
                {"now": now, "id": device_id},
            ).rowcount
            # 设备吊销必须连带吊销它名下的所有刷新令牌，否则攻击者还能继续续期
            conn.execute(
                "UPDATE refresh_tokens SET revoked_at = :now"
                " WHERE device_id = :id AND revoked_at IS NULL",
                {"now": now, "id": device_id},
            )
        return changed > 0

    def touch_device(self, device_id: str, *, now: int = 0) -> None:
        self._run(
            "UPDATE devices SET last_seen_at = :now WHERE id = :id",
            {"now": now, "id": device_id},
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
            conn.execute(
                "INSERT INTO refresh_tokens (id, device_id, user_id, family_id, token_hash,"
                " issued_at, expires_at, client_ip)"
                " VALUES (:id,:did,:uid,:fam,:hash,:iat,:exp,:ip)",
                {
                    "id": token_id,
                    "did": device_id,
                    "uid": user_id,
                    "fam": family_id,
                    "hash": token_hash,
                    "iat": issued_at,
                    "exp": expires_at,
                    "ip": client_ip,
                },
            )
        return self._one("SELECT * FROM refresh_tokens WHERE id = :id", {"id": token_id}) or {}

    def find_refresh_by_hash(self, token_hash: str) -> Row | None:
        return self._one(
            "SELECT * FROM refresh_tokens WHERE token_hash = :hash", {"hash": token_hash}
        )

    def mark_refresh_used(self, token_id: str, now: int, replaced_by: str) -> None:
        self._run(
            "UPDATE refresh_tokens SET used_at = :now, replaced_by = :rep WHERE id = :id",
            {"now": now, "rep": replaced_by, "id": token_id},
        )

    def revoke_refresh_family(self, family_id: str, now: int) -> int:
        return self._run(
            "UPDATE refresh_tokens SET revoked_at = :now"
            " WHERE family_id = :fam AND revoked_at IS NULL",
            {"now": now, "fam": family_id},
        )

    def revoke_refresh_token(self, token_id: str, now: int) -> None:
        self._run(
            "UPDATE refresh_tokens SET revoked_at = :now"
            " WHERE id = :id AND revoked_at IS NULL",
            {"now": now, "id": token_id},
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
            conn.execute(
                "INSERT INTO enrollment_tokens (id, token_hash, team_id, role, created_by,"
                " created_at, expires_at, max_uses, uses)"
                " VALUES (:id,:hash,:tid,:role,:by,:now,:exp,:max,0)",
                {
                    "id": enrollment_id,
                    "hash": token_hash,
                    "tid": team_id,
                    "role": role,
                    "by": created_by,
                    "now": now,
                    "exp": expires_at,
                    "max": max_uses,
                },
            )
        return self._one(
            "SELECT * FROM enrollment_tokens WHERE id = :id", {"id": enrollment_id}
        ) or {}

    def find_enrollment_by_hash(self, token_hash: str) -> Row | None:
        return self._one(
            "SELECT * FROM enrollment_tokens WHERE token_hash = :hash", {"hash": token_hash}
        )

    def consume_enrollment(self, enrollment_id: str, now: int) -> bool:
        # 条件里同时带上 uses/max_uses 与过期与吊销，
        # 靠一条 UPDATE 的 rowcount 判定，避免「先查后写」的竞态
        changed = self._run(
            "UPDATE enrollment_tokens SET uses = uses + 1"
            " WHERE id = :id AND revoked_at IS NULL AND expires_at > :now"
            "   AND uses < max_uses",
            {"id": enrollment_id, "now": now},
        )
        return changed > 0

    def list_enrollments(self, *, team_id: str = "") -> list[Row]:
        # 台账刻意**不 SELECT token_hash**：列表只需要「给哪个团队、何时过期、
        # 用了几次」，哈希没有任何展示价值，回它就是多一个泄漏面。
        cols = (
            "id, team_id, role, created_by, created_at, expires_at, max_uses, uses, revoked_at"
        )
        if team_id:
            return self._all(
                "SELECT " + cols + " FROM enrollment_tokens WHERE team_id = :tid"
                " ORDER BY created_at DESC",
                {"tid": team_id},
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
            conn.execute(
                "INSERT INTO client_releases (id, channel, version, platform, arch, url,"
                " sha256, size, notes, mandatory, min_version, rollout_percent, seq,"
                " signature, signature_alg, pubkey_kid, published_at, created_by)"
                " VALUES (:id,:ch,:ver,:plat,:arch,:url,:sha,:size,:notes,:mand,:minv,"
                " :roll,:seq,:sig,:alg,:kid,:now,:by)",
                {
                    "id": release_id,
                    "ch": channel,
                    "ver": version,
                    "plat": platform,
                    "arch": arch,
                    "url": url,
                    "sha": sha256,
                    "size": size,
                    "notes": notes[:2000],
                    "mand": 1 if mandatory else 0,
                    "minv": min_version,
                    "roll": rollout_percent,
                    "seq": seq,
                    "sig": signature,
                    "alg": signature_alg,
                    "kid": pubkey_kid,
                    "now": now,
                    "by": created_by,
                },
            )
        return (
            self._one(
                "SELECT " + self._RELEASE_COLS + " FROM client_releases WHERE id = :id",
                {"id": release_id},
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
        where: list[str] = []
        params: dict[str, object] = {"limit": limit}
        if channel:
            where.append("channel = :ch")
            params["ch"] = channel
        if platform:
            where.append("platform = :plat")
            params["plat"] = platform
        if arch:
            where.append("arch = :arch")
            params["arch"] = arch
        if not include_revoked:
            where.append("revoked_at IS NULL")
        sql = "SELECT " + self._RELEASE_COLS + " FROM client_releases"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq DESC LIMIT :limit"
        return self._all(sql, params)

    def get_release(self, release_id: str) -> Row | None:
        return self._one(
            "SELECT " + self._RELEASE_COLS + " FROM client_releases WHERE id = :id",
            {"id": release_id},
        )

    def latest_release(
        self, *, channel: str, platform: str, arch: str, now: int
    ) -> Row | None:
        return self._one(
            "SELECT " + self._RELEASE_COLS + " FROM client_releases"
            " WHERE channel = :ch AND platform = :plat AND arch = :arch"
            "   AND revoked_at IS NULL AND published_at <= :now"
            " ORDER BY seq DESC LIMIT 1",
            {"ch": channel, "plat": platform, "arch": arch, "now": now},
        )

    def next_release_seq(self) -> int:
        row = self._one("SELECT COALESCE(MAX(seq), 0) AS m FROM client_releases")
        return int((row or {}).get("m") or 0) + 1

    def revoke_release(self, release_id: str, now: int) -> bool:
        changed = self._run(
            "UPDATE client_releases SET revoked_at = :now"
            " WHERE id = :id AND revoked_at IS NULL",
            {"now": now, "id": release_id},
        )
        return changed > 0

    def revoke_access_jti(self, jti: str, expires_at: int, now: int) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO revoked_tokens (jti, expires_at, revoked_at)"
                " VALUES (:jti,:exp,:now) ON CONFLICT(jti) DO NOTHING",
                {"jti": jti, "exp": expires_at, "now": now},
            )

    def is_access_jti_revoked(self, jti: str) -> bool:
        return (
            self._one("SELECT 1 AS x FROM revoked_tokens WHERE jti = :jti", {"jti": jti})
            is not None
        )

    def purge_expired(self, now: int) -> dict[str, int]:
        result: dict[str, int] = {}
        with self.tx() as conn:
            result["revoked_tokens"] = conn.execute(
                "DELETE FROM revoked_tokens WHERE expires_at < :now", {"now": now}
            ).rowcount
            # 过期且未被使用的刷新令牌可以清；被使用过的（replaced_by 非空）
            # 保留一段时间，它们是重用检测的证据
            result["refresh_tokens"] = conn.execute(
                "DELETE FROM refresh_tokens WHERE expires_at < :now AND used_at IS NULL",
                {"now": now},
            ).rowcount
            result["enrollment_tokens"] = conn.execute(
                "DELETE FROM enrollment_tokens WHERE expires_at < :now AND uses = 0",
                {"now": now},
            ).rowcount
        return result

    def record_auth_event(
        self, kind: str, *, user_id: str = "", detail: str = "", remote: str = "", now: int = 0
    ) -> None:
        self._run(
            "INSERT INTO auth_events (ts, user_id, kind, detail, remote)"
            " VALUES (:now,:uid,:kind,:detail,:remote)",
            {"now": now, "uid": user_id, "kind": kind, "detail": detail[:200], "remote": remote[:64]},
        )

    def list_auth_events(self, *, limit: int = 100, user_id: str = "") -> list[Row]:
        if user_id:
            return self._all(
                "SELECT * FROM auth_events WHERE user_id = :uid ORDER BY ts DESC LIMIT :limit",
                {"uid": user_id, "limit": limit},
            )
        return self._all(
            "SELECT * FROM auth_events ORDER BY ts DESC LIMIT :limit", {"limit": limit}
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
            conn.execute(
                "INSERT INTO quota_policies (id, scope_type, scope_id, daily_cloud_tokens,"
                " daily_requests, requests_per_minute, updated_at, updated_by)"
                " VALUES (:id,:st,:sid,:tok,:req,:rpm,:now,:by)"
                " ON CONFLICT(scope_type, scope_id) DO UPDATE SET"
                "   daily_cloud_tokens = :tok, daily_requests = :req,"
                "   requests_per_minute = :rpm, updated_at = :now, updated_by = :by",
                {
                    "id": policy_id,
                    "st": scope_type,
                    "sid": scope_id,
                    "tok": daily_cloud_tokens,
                    "req": daily_requests,
                    "rpm": requests_per_minute,
                    "now": now,
                    "by": updated_by,
                },
            )
        return self.get_quota_policy(scope_type, scope_id) or {}

    def get_quota_policy(self, scope_type: str, scope_id: str) -> Row | None:
        return self._one(
            "SELECT * FROM quota_policies WHERE scope_type = :st AND scope_id = :sid",
            {"st": scope_type, "sid": scope_id},
        )

    def list_quota_policies(self) -> list[Row]:
        return self._all(
            "SELECT * FROM quota_policies ORDER BY scope_type, scope_id"
        )

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
            conn.execute(
                "INSERT INTO quota_budgets (id, scope_type, scope_id, budget, unit_price,"
                " window, updated_at, updated_by)"
                " VALUES (:id,:st,:sid,:budget,:price,:window,:now,:by)"
                " ON CONFLICT(scope_type, scope_id) DO UPDATE SET"
                "   budget = :budget, unit_price = :price, window = :window,"
                "   updated_at = :now, updated_by = :by",
                {
                    "id": budget_id,
                    "st": scope_type,
                    "sid": scope_id,
                    "budget": int(budget),
                    "price": int(unit_price),
                    "window": int(window),
                    "now": now,
                    "by": updated_by,
                },
            )
        return self.get_budget_policy(scope_type, scope_id) or {}

    def get_budget_policy(self, scope_type: str, scope_id: str) -> Row | None:
        return self._one(
            "SELECT * FROM quota_budgets WHERE scope_type = :st AND scope_id = :sid",
            {"st": scope_type, "sid": scope_id},
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
            conn.execute(
                "INSERT INTO usage_counters (day, user_id, team_id, updated_at)"
                " VALUES (:day,:uid,:tid,:now)"
                " ON CONFLICT(day, user_id) DO NOTHING",
                {"day": day, "uid": user_id, "tid": team_id, "now": now},
            )
            # 判定与写入在同一条语句里 —— 这是不超发的关键。
            # token_limit/request_limit 为 0 表示该维度不限。
            cur = conn.execute(
                "UPDATE usage_counters"
                "   SET reserved_tokens = reserved_tokens + :est, updated_at = :now"
                " WHERE day = :day AND user_id = :uid"
                "   AND (:tlimit <= 0 OR cloud_tokens + reserved_tokens + :est <= :tlimit)"
                "   AND (:rlimit <= 0 OR requests < :rlimit)",
                {
                    "est": estimate,
                    "now": now,
                    "day": day,
                    "uid": user_id,
                    "tlimit": token_limit,
                    "rlimit": request_limit,
                },
            )
            return cur.rowcount > 0

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
            conn.execute(
                "INSERT INTO usage_counters (day, user_id, team_id, updated_at)"
                " VALUES (:day,:uid,:tid,:now)"
                " ON CONFLICT(day, user_id) DO UPDATE SET team_id = :tid",
                {"day": day, "uid": user_id, "tid": team_id, "now": now},
            )
            conn.execute(
                "UPDATE usage_counters SET"
                "   reserved_tokens = MAX(0, reserved_tokens - :est),"
                "   cloud_tokens = cloud_tokens + :cloud,"
                "   local_tokens = local_tokens + :local,"
                "   requests = requests + :req,"
                "   updated_at = :now"
                " WHERE day = :day AND user_id = :uid",
                {
                    "est": max(0, int(estimate)),
                    "cloud": cloud,
                    "local": local,
                    "req": 1 if counted_request else 0,
                    "now": now,
                    "day": day,
                    "uid": user_id,
                },
            )

    def release_usage(self, day: str, user_id: str, estimate: int, *, now: int = 0) -> None:
        self._run(
            "UPDATE usage_counters SET reserved_tokens = MAX(0, reserved_tokens - :est),"
            " updated_at = :now WHERE day = :day AND user_id = :uid",
            {"est": max(0, int(estimate)), "now": now, "day": day, "uid": user_id},
        )

    def get_usage(self, day: str, user_id: str) -> Row:
        return self._one(
            "SELECT * FROM usage_counters WHERE day = :day AND user_id = :uid",
            {"day": day, "uid": user_id},
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
                "SELECT * FROM usage_counters WHERE day = :day AND team_id = :tid"
                " ORDER BY cloud_tokens DESC",
                {"day": day, "tid": team_id},
            )
        return self._all(
            "SELECT * FROM usage_counters WHERE day = :day ORDER BY cloud_tokens DESC",
            {"day": day},
        )

    def usage_series(
        self, *, user_id: str = "", team_id: str = "", since_day: str, limit: int = 60
    ) -> list[Row]:
        clauses = ["day >= :since"]
        params: dict[str, Any] = {"since": since_day, "limit": limit}
        if user_id:
            clauses.append("user_id = :uid")
            params["uid"] = user_id
        if team_id:
            clauses.append("team_id = :tid")
            params["tid"] = team_id
        where = " AND ".join(clauses)
        return self._all(
            f"SELECT day, user_id, team_id, cloud_tokens, local_tokens, requests"
            f" FROM usage_counters WHERE {where} ORDER BY day DESC LIMIT :limit",
            params,
        )

    def hit_rate_limit(self, scope_key: str, window_start: int, limit: int) -> tuple[bool, int]:
        if limit <= 0:
            return True, 0
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO rate_counters (scope_key, window_start, count)"
                " VALUES (:key,:win,0) ON CONFLICT(scope_key, window_start) DO NOTHING",
                {"key": scope_key, "win": window_start},
            )
            conn.execute(
                "UPDATE rate_counters SET count = count + 1"
                " WHERE scope_key = :key AND window_start = :win",
                {"key": scope_key, "win": window_start},
            )
            row = conn.execute(
                "SELECT count FROM rate_counters WHERE scope_key = :key AND window_start = :win",
                {"key": scope_key, "win": window_start},
            ).fetchone()
        count = int(row["count"]) if row else 1
        return count <= limit, count

    # ============================================================== 审计
    def insert_audit_events(self, rows: list[Row]) -> int:
        if not rows:
            return 0
        columns = (
            "device_id", "user_id", "team_id", "session_id", "seq", "event_type",
            "ts", "payload", "prev_hash", "hash", "batch_id", "received_at",
        )
        sql = (
            f"INSERT OR IGNORE INTO audit_events ({', '.join(columns)})"
            f" VALUES ({', '.join(':' + c for c in columns)})"
        )
        # payload 是 JSON 文本列。序列化放在存储层做，因为它才是唯一知道
        # 「这一列是 TEXT」的地方；交给调用方序列化，迟早有人传个 dict 进来
        # 然后在运行时炸（sqlite3 不接受 dict 作为绑定参数）。
        # ensure_ascii=False 保证中文按原样存；sort_keys 让同一份载荷
        # 序列化结果稳定 —— 链哈希依赖它（见 hashing.canonical 的说明）。
        prepared = [
            {
                **{c: row.get(c) for c in columns},
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
        with self.tx() as conn:
            cur = conn.executemany(sql, prepared)
            return cur.rowcount

    @staticmethod
    def _decode_audit_row(row: Row) -> Row:
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

    def insert_audit_batch(self, row: Row) -> None:
        columns = (
            "id", "device_id", "user_id", "team_id", "first_seq", "last_seq",
            "event_count", "client_chain_head", "prev_chain_head", "chain_head",
            "sequence_gap", "received_at",
        )
        with self.tx() as conn:
            conn.execute(
                f"INSERT OR IGNORE INTO audit_batches ({', '.join(columns)})"
                f" VALUES ({', '.join(':' + c for c in columns)})",
                {c: row.get(c) for c in columns},
            )

    def get_batch_chain_head(self, device_id: str) -> str:
        row = self._one(
            "SELECT chain_head FROM audit_batches WHERE device_id = :did"
            " ORDER BY received_at DESC, rowid DESC LIMIT 1",
            {"did": device_id},
        )
        return str(row["chain_head"]) if row else GENESIS

    def get_device_last_seq(self, device_id: str, session_id: str) -> int:
        row = self._one(
            "SELECT MAX(seq) AS s FROM audit_events"
            " WHERE device_id = :did AND session_id = :sid",
            {"did": device_id, "sid": session_id},
        )
        return int(row["s"]) if row and row["s"] is not None else 0

    def get_session_last_hash(self, device_id: str, session_id: str) -> str:
        row = self._one(
            "SELECT hash FROM audit_events"
            " WHERE device_id = :did AND session_id = :sid"
            " ORDER BY seq DESC LIMIT 1",
            {"did": device_id, "sid": session_id},
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
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        for column, value in (
            ("team_id", team_id), ("user_id", user_id),
            ("device_id", device_id), ("session_id", session_id),
            ("event_type", event_type),
        ):
            if value:
                clauses.append(f"{column} = :{column}")
                params[column] = value
        if since:
            clauses.append("ts >= :since")
            params["since"] = since
        if until:
            clauses.append("ts <= :until")
            params["until"] = until
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._all(
            f"SELECT * FROM audit_events{where} ORDER BY ts DESC, id DESC"
            f" LIMIT :limit OFFSET :offset",
            params,
        )
        return [self._decode_audit_row(row) for row in rows]

    def count_audit_events(
        self, *, team_id: str = "", user_id: str = "", since: int = 0
    ) -> int:
        clauses, params = [], {}
        if team_id:
            clauses.append("team_id = :tid")
            params["tid"] = team_id
        if user_id:
            clauses.append("user_id = :uid")
            params["uid"] = user_id
        if since:
            clauses.append("ts >= :since")
            params["since"] = since
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self._one(f"SELECT COUNT(*) AS c FROM audit_events{where}", params)
        return int(row["c"]) if row else 0

    def audit_summary(self, *, team_id: str = "", since: int = 0) -> list[Row]:
        clauses, params = [], {}
        if team_id:
            clauses.append("team_id = :tid")
            params["tid"] = team_id
        if since:
            clauses.append("ts >= :since")
            params["since"] = since
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._all(
            # 别名用 events/n，两个都要：SQL 里写 events 可读性好，
            # 但服务层按 n 读。统一成 n 会丢掉可读性，索性两个都给。
            f"SELECT event_type, COUNT(*) AS n, COUNT(*) AS events, MAX(ts) AS last_ts"
            f" FROM audit_events{where} GROUP BY event_type ORDER BY n DESC",
            params,
        )

    def server_chain(self, device_id: str, *, limit: int = 1000) -> list[Row]:
        return self._all(
            "SELECT * FROM audit_batches WHERE device_id = :did"
            " ORDER BY received_at ASC, rowid ASC LIMIT :limit",
            {"did": device_id, "limit": limit},
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
            conn.execute(
                "INSERT INTO policies (id, scope_type, scope_id, version, document,"
                " created_at, created_by, note)"
                " VALUES (:id,:st,:sid,:ver,:doc,:now,:by,:note)",
                {
                    "id": policy_id,
                    "st": scope_type,
                    "sid": scope_id,
                    "ver": version,
                    "doc": document,
                    "now": now,
                    "by": created_by,
                    "note": note,
                },
            )
        row = self._one("SELECT * FROM policies WHERE id = :id", {"id": policy_id})
        assert row is not None
        return row

    def next_policy_version(self, scope_type: str, scope_id: str) -> int:
        row = self._one(
            "SELECT MAX(version) AS v FROM policies"
            " WHERE scope_type = :st AND scope_id = :sid",
            {"st": scope_type, "sid": scope_id},
        )
        current = int(row["v"]) if row and row["v"] is not None else 0
        return current + 1

    def get_latest_policy(self, scope_type: str, scope_id: str) -> Row | None:
        return self._one(
            "SELECT * FROM policies WHERE scope_type = :st AND scope_id = :sid"
            " ORDER BY version DESC LIMIT 1",
            {"st": scope_type, "sid": scope_id},
        )

    def list_policy_versions(
        self, scope_type: str, scope_id: str, *, limit: int = 20
    ) -> list[Row]:
        return self._all(
            "SELECT id, scope_type, scope_id, version, created_at, created_by, note"
            " FROM policies WHERE scope_type = :st AND scope_id = :sid"
            " ORDER BY version DESC LIMIT :limit",
            {"st": scope_type, "sid": scope_id, "limit": limit},
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
            conn.execute(
                "INSERT INTO secret_meta (name, provider, ref, fingerprint, rotated_at,"
                " created_at, note) VALUES (:name,:prov,:ref,:fp,:now,:now,:note)"
                " ON CONFLICT(name) DO UPDATE SET"
                "   provider = :prov, ref = :ref, fingerprint = :fp,"
                "   rotated_at = :now, note = :note",
                {
                    "name": name,
                    "prov": provider,
                    "ref": ref,
                    "fp": fingerprint,
                    "now": now,
                    "note": note,
                },
            )
        return self._one("SELECT * FROM secret_meta WHERE name = :name", {"name": name}) or {}

    def get_secret_meta(self, name: str) -> Row | None:
        return self._one("SELECT * FROM secret_meta WHERE name = :name", {"name": name})

    def list_secret_meta(self) -> list[Row]:
        return self._all("SELECT * FROM secret_meta ORDER BY name")


__all__ = ["SQLiteStore", "_new_id"]
