"""存储接口（换库的缝）。

**这一层的存在意义只有一个**：让「从 SQLite 换到 Postgres」变成换驱动，
而不是把 SQL 方言抄得到处都是。

因此有两条纪律：
1. 服务层（auth / gateway / audit / policy）**只调用这里的方法，不写 SQL**；
2. 每个方法都要能在两个后端上表达得一样 —— 一旦某个需求非要用某种数据库的
   独门特性，那就说明这个需求应该重新设计，而不是把方言引进来。

`SQLiteStore` 是 M2 的唯一实现。`open_store()` 按 URL 前缀分派，
`postgres://` 会给出明确的「怎么补」而不是静默降级 —— 静默降级到 SQLite
是这类项目最危险的一种"温柔"。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

Row = dict[str, Any]


class Store(ABC):
    """持久化接口。所有方法返回朴素 dict / list[dict]，不返回 ORM 对象。"""

    # ============================================================== 生命周期
    @abstractmethod
    def init_schema(self) -> None:
        """建表（幂等）。"""

    @abstractmethod
    def close(self) -> None:
        """释放连接。"""

    @abstractmethod
    def health(self) -> tuple[bool, str]:
        """返回 (是否健康, 说明)。用于 /healthz。"""

    # ============================================================== 用户
    @abstractmethod
    def create_user(
        self,
        user_id: str,
        email: str,
        display_name: str,
        password_hash: str,
        *,
        external_id: str | None = None,
        now: int = 0,
    ) -> Row: ...

    @abstractmethod
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
        """在一个事务里建首个用户 + 确保默认团队 + 写入其 membership。

        返回 False 表示已有用户（此时不得产生任何写入）。判定与写入必须在
        同一条语句里完成，否则并发 bootstrap 会各建一个 owner。
        """

    @abstractmethod
    def get_user(self, user_id: str) -> Row | None: ...

    @abstractmethod
    def find_user_by_email(self, email: str) -> Row | None: ...

    @abstractmethod
    def find_user_by_external_id(self, external_id: str) -> Row | None: ...

    @abstractmethod
    def update_user(self, user_id: str, *, now: int = 0, **fields: Any) -> None: ...

    @abstractmethod
    def list_users(self, *, limit: int = 100, offset: int = 0) -> list[Row]: ...

    # ============================================================== 团队
    @abstractmethod
    def create_team(self, team_id: str, slug: str, name: str, *, now: int = 0) -> Row: ...

    @abstractmethod
    def get_team(self, team_id: str) -> Row | None: ...

    @abstractmethod
    def find_team_by_slug(self, slug: str) -> Row | None: ...

    @abstractmethod
    def list_teams(self) -> list[Row]: ...

    @abstractmethod
    def add_membership(
        self, membership_id: str, user_id: str, team_id: str, role: str, *, now: int = 0
    ) -> Row: ...

    @abstractmethod
    def get_membership(self, user_id: str, team_id: str) -> Row | None: ...

    @abstractmethod
    def set_membership_role(self, user_id: str, team_id: str, role: str) -> None: ...

    @abstractmethod
    def remove_membership(self, user_id: str, team_id: str) -> bool: ...

    @abstractmethod
    def list_memberships(self, user_id: str) -> list[Row]:
        """返回该用户的全部成员关系，含 team_slug / team_name，便于一次性建 Actor。"""

    @abstractmethod
    def list_team_members(self, team_id: str) -> list[Row]: ...

    # ============================================================== 项目
    @abstractmethod
    def create_project(
        self, project_id: str, team_id: str, name: str, repo: str, *, now: int = 0
    ) -> Row: ...

    @abstractmethod
    def list_projects(self, team_id: str) -> list[Row]: ...

    @abstractmethod
    def get_project(self, project_id: str) -> Row | None: ...

    @abstractmethod
    def grant_project(
        self, grant_id: str, project_id: str, user_id: str, role: str, *, now: int = 0
    ) -> None: ...

    @abstractmethod
    def list_project_grants(self, project_id: str) -> list[Row]: ...

    @abstractmethod
    def find_project_grant(self, project_id: str, user_id: str) -> Row | None: ...

    # ============================================================== 设备与令牌
    @abstractmethod
    def create_device(
        self,
        device_id: str,
        user_id: str,
        name: str,
        platform: str,
        fingerprint: str,
        *,
        now: int = 0,
    ) -> Row: ...

    @abstractmethod
    def get_device(self, device_id: str) -> Row | None: ...

    @abstractmethod
    def list_devices(self, user_id: str) -> list[Row]: ...

    @abstractmethod
    def revoke_device(self, device_id: str, *, now: int = 0) -> bool: ...

    @abstractmethod
    def touch_device(self, device_id: str, *, now: int = 0) -> None: ...

    @abstractmethod
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
    ) -> Row: ...

    @abstractmethod
    def find_refresh_by_hash(self, token_hash: str) -> Row | None: ...

    @abstractmethod
    def mark_refresh_used(self, token_id: str, now: int, replaced_by: str) -> None: ...

    @abstractmethod
    def revoke_refresh_family(self, family_id: str, now: int) -> int:
        """吊销整族。返回受影响条数。刷新令牌重用检测的处置动作。"""

    @abstractmethod
    def revoke_refresh_token(self, token_id: str, now: int) -> None: ...

    @abstractmethod
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
    ) -> Row: ...

    @abstractmethod
    def find_enrollment_by_hash(self, token_hash: str) -> Row | None: ...

    @abstractmethod
    def consume_enrollment(self, enrollment_id: str, now: int) -> bool:
        """原子地占用一次使用额度。返回 False 表示已用尽。"""

    @abstractmethod
    def list_enrollments(self, *, team_id: str = "") -> list[Row]:
        """列出邀请码台账（M8 控制台）。**永不含 token_hash** ——
        列表只需要「给哪个团队、何时过期、用了几次」，哈希没有展示价值。"""

    # ============================================================== 客户端发布（M9）
    @abstractmethod
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
        """登记一条客户端发布版本。台账是**追加式**的：同一 version 允许再次登记
        （回退旧版本时正是这么做），查询侧永远取 seq 最大的一条。"""

    @abstractmethod
    def list_releases(
        self,
        *,
        channel: str = "",
        platform: str = "",
        arch: str = "",
        include_revoked: bool = False,
        limit: int = 100,
    ) -> list[Row]:
        """列出发布台账（M9 控制台）。默认按 seq 倒序、排除已撤销。"""

    @abstractmethod
    def get_release(self, release_id: str) -> Row | None: ...

    @abstractmethod
    def latest_release(
        self, *, channel: str, platform: str, arch: str, now: int
    ) -> Row | None:
        """取某 (channel, platform, arch) 下 seq 最大且未撤销的一条。"""

    @abstractmethod
    def next_release_seq(self) -> int:
        """返回下一个全局单调递增的发布序号（当前最大值 + 1）。"""

    @abstractmethod
    def revoke_release(self, release_id: str, now: int) -> bool:
        """撤销一条发布。返回 False 表示不存在或已撤销。"""

    @abstractmethod
    def revoke_access_jti(self, jti: str, expires_at: int, now: int) -> None: ...

    @abstractmethod
    def is_access_jti_revoked(self, jti: str) -> bool: ...

    @abstractmethod
    def purge_expired(self, now: int) -> dict[str, int]:
        """清理过期数据，返回各表清理条数。"""

    @abstractmethod
    def record_auth_event(
        self, kind: str, *, user_id: str = "", detail: str = "", remote: str = "", now: int = 0
    ) -> None: ...

    @abstractmethod
    def list_auth_events(self, *, limit: int = 100, user_id: str = "") -> list[Row]: ...

    # ============================================================== 配额
    @abstractmethod
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
    ) -> Row: ...

    @abstractmethod
    def get_quota_policy(self, scope_type: str, scope_id: str) -> Row | None: ...

    @abstractmethod
    def list_quota_policies(self) -> list[Row]: ...

    @abstractmethod
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
        """写入/更新预算策略（用于推导每日额度，不是显式额度）。"""

    @abstractmethod
    def get_budget_policy(self, scope_type: str, scope_id: str) -> Row | None: ...

    @abstractmethod
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
        """原子地「占用」额度。

        必须把「判断」和「写入」放进同一条语句（带条件的 UPDATE + rowcount），
        否则并发下两个请求会同时通过检查然后双双超额。
        返回 False 表示额度不足。
        """

    @abstractmethod
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
        """结算：释放预留、按实际用量入账。"""

    @abstractmethod
    def release_usage(self, day: str, user_id: str, estimate: int, *, now: int = 0) -> None:
        """上游失败时释放预留（不入账）。"""

    @abstractmethod
    def get_usage(self, day: str, user_id: str) -> Row: ...

    @abstractmethod
    def list_usage(self, day: str, *, team_id: str = "") -> list[Row]: ...

    @abstractmethod
    def usage_series(
        self, *, user_id: str = "", team_id: str = "", since_day: str, limit: int = 60
    ) -> list[Row]: ...

    @abstractmethod
    def hit_rate_limit(self, scope_key: str, window_start: int, limit: int) -> tuple[bool, int]:
        """固定窗口限流。返回 (是否放行, 当前计数)。limit<=0 表示不限。"""

    # ============================================================== 审计
    @abstractmethod
    def insert_audit_events(self, rows: list[Row]) -> int:
        """批量写入。靠 UNIQUE 约束实现幂等，返回真正新增的条数。"""

    @abstractmethod
    def insert_audit_batch(self, row: Row) -> None: ...

    @abstractmethod
    def get_batch_chain_head(self, device_id: str) -> str:
        """取该设备上一批次的链头；没有批次时返回创世值。"""

    @abstractmethod
    def get_device_last_seq(self, device_id: str, session_id: str) -> int: ...

    @abstractmethod
    def get_session_last_hash(self, device_id: str, session_id: str) -> str:
        """取该会话已入库的最后一条事件的链哈希；没有则返回创世值。

        追加批次时必须从这里续链，否则第二批次之后 prev_hash 就会断，
        校验时会把「正常的第二批」误报成篡改。
        """

    @abstractmethod
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
    ) -> list[Row]: ...

    @abstractmethod
    def count_audit_events(
        self, *, team_id: str = "", user_id: str = "", since: int = 0
    ) -> int: ...

    @abstractmethod
    def audit_summary(self, *, team_id: str = "", since: int = 0) -> list[Row]:
        """按事件类型聚合，用于用量与合规视图。"""

    @abstractmethod
    def server_chain(self, device_id: str, *, limit: int = 1000) -> list[Row]:
        """取该设备的批次链，供完整性校验。"""

    # ============================================================== 策略
    @abstractmethod
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
    ) -> Row: ...

    @abstractmethod
    def next_policy_version(self, scope_type: str, scope_id: str) -> int: ...

    @abstractmethod
    def get_latest_policy(self, scope_type: str, scope_id: str) -> Row | None: ...

    @abstractmethod
    def list_policy_versions(
        self, scope_type: str, scope_id: str, *, limit: int = 20
    ) -> list[Row]: ...

    # ============================================================== 密钥台账
    @abstractmethod
    def upsert_secret_meta(
        self,
        name: str,
        provider: str,
        ref: str,
        fingerprint: str,
        *,
        note: str = "",
        now: int = 0,
    ) -> Row: ...

    @abstractmethod
    def get_secret_meta(self, name: str) -> Row | None: ...

    @abstractmethod
    def list_secret_meta(self) -> list[Row]: ...


__all__ = ["Row", "Store"]
