"""角色与权限（RBAC）。

这里的重点不是「有角色」——那太容易了——而是**防水平越权**：
团队管理员能管自己团队，但读不到别的团队的任何数据。

因此 `Actor` 不只带「能做什么」，还带**「对谁做」**。每个需要作用域的接口
都必须显式声明它要访问的是哪个 team / user，由 `scope_team()` / `scope_user()`
去核对。这不是可选的美德，而是这两条硬约束：

- `require_permission(actor, "audit:read")` 只回答「这个人有没有审计读取权」；
- `actor.scope_team(team_id)` 才回答「这个团队他有没有资格看」。

两者必须同时通过。**只检查前者是这类系统最经典的一个洞**：
角色对，主体错，于是 A 团队管理员把 B 团队的审计翻了个遍。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus

from .errors import ApiError, Forbidden

# --------------------------------------------------------------------------- 角色
ROLE_OWNER = "owner"            # 组织所有者：跨团队
ROLE_TEAM_ADMIN = "team_admin"  # 团队管理员：仅本团队
ROLE_DEVELOPER = "developer"    # 开发：自己用 + 看自己的用量
ROLE_AUDITOR = "auditor"        # 审计员：只读，跨团队但不可写
ROLE_VIEWER = "viewer"          # 只读观察者：仅自己

ROLES = (ROLE_OWNER, ROLE_TEAM_ADMIN, ROLE_DEVELOPER, ROLE_AUDITOR, ROLE_VIEWER)

# --------------------------------------------------------------------------- 权限
# 命名约定 `<资源>:<动作>`。动作里 `read` 只读、`write` 改数据、`admin` 管配置。
PERM_AUDIT_READ = "audit:read"
PERM_AUDIT_EXPORT = "audit:export"
PERM_USAGE_READ = "usage:read"
PERM_USAGE_READ_TEAM = "usage:read_team"
PERM_QUOTA_READ = "quota:read"
PERM_QUOTA_WRITE = "quota:write"
PERM_POLICY_READ = "policy:read"
PERM_POLICY_WRITE = "policy:write"
PERM_USER_READ = "user:read"
PERM_USER_WRITE = "user:write"
PERM_TEAM_READ = "team:read"
PERM_TEAM_WRITE = "team:write"
PERM_ENROLLMENT_WRITE = "enrollment:write"
PERM_DEVICE_READ = "device:read"
PERM_DEVICE_REVOKE = "device:revoke"
PERM_SECRET_READ = "secret:read"
PERM_SECRET_WRITE = "secret:write"
PERM_GATEWAY_USE = "gateway:use"

# 每个角色拿到的最小集合。**从最小权限写起**，需要时再加，
# 而不是「先给全了，出问题再收」——后者收不回来，因为收权限会被当成故障。
_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_OWNER: frozenset(
        {
            PERM_AUDIT_READ,
            PERM_AUDIT_EXPORT,
            PERM_USAGE_READ,
            PERM_USAGE_READ_TEAM,
            PERM_QUOTA_READ,
            PERM_QUOTA_WRITE,
            PERM_POLICY_READ,
            PERM_POLICY_WRITE,
            PERM_USER_READ,
            PERM_USER_WRITE,
            PERM_TEAM_READ,
            PERM_TEAM_WRITE,
            PERM_ENROLLMENT_WRITE,
            PERM_DEVICE_READ,
            PERM_DEVICE_REVOKE,
            PERM_SECRET_READ,
            PERM_SECRET_WRITE,
            PERM_GATEWAY_USE,
        }
    ),
    ROLE_TEAM_ADMIN: frozenset(
        {
            PERM_AUDIT_READ,
            PERM_AUDIT_EXPORT,
            PERM_USAGE_READ,
            PERM_USAGE_READ_TEAM,
            PERM_QUOTA_READ,
            PERM_QUOTA_WRITE,
            PERM_POLICY_READ,
            PERM_POLICY_WRITE,
            PERM_USER_READ,
            PERM_TEAM_READ,
            PERM_ENROLLMENT_WRITE,
            PERM_DEVICE_READ,
            PERM_DEVICE_REVOKE,
            PERM_GATEWAY_USE,
        }
    ),
    ROLE_DEVELOPER: frozenset(
        {
            PERM_AUDIT_READ,
            PERM_USAGE_READ,
            PERM_QUOTA_READ,
            PERM_POLICY_READ,
            PERM_TEAM_READ,
            PERM_DEVICE_READ,
            PERM_GATEWAY_USE,
        }
    ),
    # 审计员跨团队只读：能看全部审计，但改不了任何东西。
    # 这是「合规要看全局，但不能让看的人顺手把记录改了」的标准做法。
    ROLE_AUDITOR: frozenset(
        {
            PERM_AUDIT_READ,
            PERM_AUDIT_EXPORT,
            PERM_USAGE_READ,
            PERM_USAGE_READ_TEAM,
            PERM_QUOTA_READ,
            PERM_POLICY_READ,
            PERM_USER_READ,
            PERM_TEAM_READ,
            PERM_DEVICE_READ,
        }
    ),
    ROLE_VIEWER: frozenset(
        {
            PERM_USAGE_READ,
            PERM_QUOTA_READ,
            PERM_POLICY_READ,
        }
    ),
}

# 能跨团队的角色。只有这两个，且都不可写业务数据（auditor 全只读）。
_ORG_WIDE_ROLES = frozenset({ROLE_OWNER, ROLE_AUDITOR})


# --------------------------------------------------------------------------- Actor
@dataclass(frozen=True)
class Membership:
    team_id: str
    team_slug: str
    team_name: str
    role: str


@dataclass(frozen=True)
class Actor:
    """一次经过认证的请求主体。

    `permissions` 是各角色权限的**并集**（一个人可能既在 A 团队当开发，
    又在 B 团队当管理员）。但如果只看并集，就会出现
    「用 A 团队的管理员身份去管 B 团队」——所以作用域校验必须
    落到**具体那一条 membership 的角色**上，见 `scope_team`。
    """

    user_id: str
    email: str
    display_name: str
    roles: tuple[str, ...]
    memberships: tuple[Membership, ...] = ()
    device_id: str = ""
    token_id: str = ""
    is_org_wide: bool = False
    permissions: frozenset[str] = field(default_factory=frozenset)

    # ------------------------------------------------------------------ 判定
    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def role_in(self, team_id: str) -> str:
        for m in self.memberships:
            if m.team_id == team_id:
                return m.role
        return ""

    @property
    def team_ids(self) -> tuple[str, ...]:
        return tuple(m.team_id for m in self.memberships)

    def describe(self) -> dict[str, object]:
        return {
            "userId": self.user_id,
            "email": self.email,
            "displayName": self.display_name,
            "roles": list(self.roles),
            "teams": [
                {"id": m.team_id, "slug": m.team_slug, "name": m.team_name, "role": m.role}
                for m in self.memberships
            ],
            "orgWide": self.is_org_wide,
            "permissions": sorted(self.permissions),
        }

    # ------------------------------------------------------------------ 作用域
    def scope_team(self, team_id: str, permission: str = "") -> None:
        """核对「这个主体有没有资格碰这个团队的数据」。

        规则：组织级角色可任意团队；其余必须是自己所属的团队。
        传入 permission 时会额外核对**他在该团队里的角色**是否含这项权限，
        从而堵住「拿 A 团队管理员身份操作 B 团队」的洞。
        """
        if not team_id:
            raise Forbidden("缺少 team 作用域，无法判定权限")
        if self.is_org_wide:
            return
        role = self.role_in(team_id)
        if not role:
            # 刻意不区分「团队不存在」与「你不属于它」：
            # 区分开等于把「哪些团队存在」泄漏给任何登录用户。
            raise Forbidden("无权访问该团队的数据")
        if permission and permission not in _ROLE_PERMISSIONS.get(role, frozenset()):
            raise Forbidden(f"你在该团队的角色（{role}）不具备 {permission} 权限")

    def scope_user(self, user_id: str, permission: str = "") -> None:
        """核对「这个主体能不能看/改这个用户的数据」。

        规则：本人永远可以；组织级可任意用户；团队管理员仅限**同团队**成员。
        团队管理员不能跨团队看人 —— 这正是水平越权最常见的入口。
        """
        if not user_id:
            raise Forbidden("缺少 user 作用域，无法判定权限")
        if user_id == self.user_id:
            return
        if self.is_org_wide:
            return
        if not self.can(PERM_USER_READ):
            raise Forbidden("无权访问其他用户的数据")
        if permission and not self.can(permission):
            raise Forbidden(f"缺少权限 {permission}")
        # 必须是同一个团队的人
        mine = set(self.team_ids)
        if not mine:
            raise Forbidden("无权访问其他用户的数据")
        target = _target_teams(user_id)
        if not (mine & target):
            raise Forbidden("无权访问其他团队的成员数据")

    # ------------------------------------------------------------------ 导出
    def visible_team_ids(self, permission: str = "") -> list[str] | None:
        """返回可访问的团队列表；None 表示「全部」组织级访问。

        列表接口据此过滤，避免每个 handler 各写一遍 if-else 漏掉一处。
        """
        if self.is_org_wide:
            return None
        if permission:
            return [
                m.team_id
                for m in self.memberships
                if permission in _ROLE_PERMISSIONS.get(m.role, frozenset())
            ]
        return list(self.team_ids)


# --------------------------------------------------------------------------- 装配
# Actor 查「目标用户属于哪些团队」需要一个查表函数。用模块级绑定而不是字段注入，
# 是因为 Actor 是 frozen dataclass，且会被序列化进日志——塞个 lambda 进去不合适。
def _target_teams(user_id: str) -> set[str]:
    """查某用户所属团队。未绑定时返回空集（一律拒绝，而非一律放行）。"""
    if _LOOKUP is None:
        return set()
    return set(_LOOKUP(user_id))


_LOOKUP = None


def bind_lookup(fn: object) -> None:
    """由 app 装配时调用，注入「查某用户所属团队」的实现。"""
    global _LOOKUP
    _LOOKUP = fn


def permissions_for(roles: tuple[str, ...] | list[str]) -> frozenset[str]:
    result: set[str] = set()
    for role in roles:
        result |= _ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(result)


def build_actor(
    *,
    user_id: str,
    email: str,
    display_name: str,
    memberships: list[dict[str, object]],
    roles: list[str] | None = None,
    device_id: str = "",
    token_id: str = "",
) -> Actor:
    """从存储行装配 Actor。

    `roles` 不传时由 memberships 推导：其中最高的一档决定组织级身份。
    组织级身份**只能来自 owner/auditor 这个角色**，不能靠「团队数多」升格 ——
    否则一个人多进几个团队就变成了组织管理员。
    """
    parsed = [
        Membership(
            team_id=str(m["team_id"]),
            team_slug=str(m.get("team_slug") or ""),
            team_name=str(m.get("team_name") or ""),
            role=str(m["role"]),
        )
        for m in memberships
    ]
    effective = list(roles) if roles is not None else [m.role for m in parsed]
    org_wide = any(r in _ORG_WIDE_ROLES for r in effective)
    return Actor(
        user_id=user_id,
        email=email,
        display_name=display_name,
        roles=tuple(dict.fromkeys(effective)),
        memberships=tuple(parsed),
        device_id=device_id,
        token_id=token_id,
        is_org_wide=org_wide,
        permissions=permissions_for(effective),
    )


def require_permission(actor: Actor | None, permission: str) -> None:
    if actor is None:
        from .errors import Unauthenticated

        raise Unauthenticated()
    if not actor.can(permission):
        raise ApiError(
            HTTPStatus.FORBIDDEN,
            "forbidden",
            f"缺少权限 {permission}",
            details={"required": permission, "roles": list(actor.roles)},
        )


def role_exists(role: str) -> bool:
    return role in _ROLE_PERMISSIONS


__all__ = [
    "PERM_AUDIT_EXPORT",
    "PERM_AUDIT_READ",
    "PERM_DEVICE_READ",
    "PERM_DEVICE_REVOKE",
    "PERM_ENROLLMENT_WRITE",
    "PERM_GATEWAY_USE",
    "PERM_POLICY_READ",
    "PERM_POLICY_WRITE",
    "PERM_QUOTA_READ",
    "PERM_QUOTA_WRITE",
    "PERM_SECRET_READ",
    "PERM_SECRET_WRITE",
    "PERM_TEAM_READ",
    "PERM_TEAM_WRITE",
    "PERM_USAGE_READ",
    "PERM_USAGE_READ_TEAM",
    "PERM_USER_READ",
    "PERM_USER_WRITE",
    "ROLES",
    "ROLE_AUDITOR",
    "ROLE_DEVELOPER",
    "ROLE_OWNER",
    "ROLE_TEAM_ADMIN",
    "ROLE_VIEWER",
    "Actor",
    "Membership",
    "bind_lookup",
    "build_actor",
    "permissions_for",
    "require_permission",
    "role_exists",
]
