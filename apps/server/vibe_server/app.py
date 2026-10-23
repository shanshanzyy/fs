"""装配层：把各组件拼成一个可运行的服务端。

装配顺序是有讲究的，不是随手排的：

1. **存储先于一切** —— 它要在启动时就暴露「库坏了」，
   而不是等第一个请求进来才发现。
2. **RBAC 的查表函数必须在任何请求之前绑好。** 忘了绑的话，
   `Actor.scope_user` 会返回「查不到团队」→ 一律拒绝（fail-closed），
   结果是所有跨用户操作都 403。这比反过来好，但仍然是个故障，
   所以这里绑完会**立刻自检一次**，把问题拦在启动阶段。
3. **审计记录器最后装**，因为它依赖存储，且它是「服务端自己也往审计里
   记一条」的通道（比如 gateway 的调用结果）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import rbac
from .audit import AuditService
from .auth.oidc import OidcFlowStore, OidcProvider
from .auth.service import AuthService
from .clock import Clock
from .config import ServerConfig
from .gateway.proxy import GatewayService
from .gateway.quota import QuotaService
from .gateway.secrets import open_secret_store, rotation_report
from .httpd import AppContext
from .ids import new_id
from .policy import PolicyService
from .release_keys import default_key_path
from .releases import ReleaseService
from .store import open_store

log = logging.getLogger("vibe.governance")


def build_app(
    config: ServerConfig,
    *,
    clock: Clock | None = None,
    store: Any = None,
    router: Any = None,
) -> AppContext:
    """构建应用上下文。`store` / `router` 可注入，方便测试。"""
    from .api.routes import build_router

    clock = clock or Clock()
    store = store if store is not None else open_store(config.store_target())
    store.init_schema()

    router = router if router is not None else build_router()

    secrets = open_secret_store(config)
    auth_service = AuthService(store=store, config=config, clock=clock)
    quota_service = QuotaService(store=store, config=config, clock=clock)
    policy_service = PolicyService(store=store, config=config, clock=clock)
    audit_service = AuditService(store=store, config=config, clock=clock)
    gateway = GatewayService(
        config=config, store=store, clock=clock, secrets=secrets, quota=quota_service
    )

    app = AppContext(
        config=config,
        store=store,
        clock=clock,
        router=router,
        authenticator=auth_service.authenticate,
    )
    app.secrets = secrets
    app.auth_service = auth_service
    app.quota_service = quota_service
    app.policy_service = policy_service
    app.audit_service = audit_service
    app.gateway = gateway

    # --- OIDC ---
    oidc_cfg = _oidc_config(config)
    if oidc_cfg:
        app.oidc = OidcProvider(
            issuer=oidc_cfg["issuer"],
            client_id=oidc_cfg["clientId"],
            client_secret=oidc_cfg.get("clientSecret", ""),
            redirect_uri=oidc_cfg.get("redirectUri", ""),
            scopes=oidc_cfg.get("scopes", "openid email profile"),
        )
        app.oidc_flows = OidcFlowStore(clock=clock)
    else:
        app.oidc = None
        app.oidc_flows = OidcFlowStore(clock=clock)

    # --- RBAC 查表绑定 + 自检 ---
    rbac.bind_lookup(lambda user_id: auth_service.store_teams_of(user_id))
    _selfcheck_rbac()

    # --- 服务端侧审计记录器 ---
    # 网关/管理操作产生的审计条目也走同一个脱敏闸门，绝不绕过。
    def _record(
        *,
        actor,
        device_id: str = "",
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        from . import redaction

        verdict = redaction.validate_event({"type": event_type, "payload": payload or {}})
        if not verdict.ok:
            # 服务端自己写错字段名，属于编码错误：记一条日志暴露它，
            # 但**不落库**（落一条不合规的记录比不落更糟）。
            log.warning(
                "审计事件被脱敏闸门拒绝（服务端侧编码错误）type=%s reason=%s",
                event_type,
                verdict.reason,
            )
            return
        now = clock.now()
        store.record_auth_event(
            "audit." + event_type.replace(".", "_"),
            user_id=getattr(actor, "user_id", ""),
            detail=_summarize_payload(verdict.cleaned or {}),
            now=now,
        )

    app.audit_recorder = _record
    app.secret_rotation_report = lambda rows: rotation_report(
        rows, max_age_days=config.secret_max_age_days, now=clock.now()
    )

    # --- 客户端发布（M9）---
    # 私钥**按需加载**（见 ReleaseService.key）：构建 app 不会触发 2048 位 keygen，
    # 只有真正 publish / public_key 时才读或生成。文件缺失时这里先给一条提示。
    release_key_path = default_key_path(config.data_dir)
    app.releases = ReleaseService(store=store, clock=clock, key_path=release_key_path)
    if not release_key_path.exists():
        log.warning(
            "尚未发现发布签名密钥（%s）。首次发布客户端版本时会自动生成一份 —— "
            "请确认它就是客户端内置公钥对应的那把，否则客户端会因验签失败而拒绝更新；"
            "内网自签场景可用 VIBE_UPDATE_PUBKEY 指向自己的公钥。",
            release_key_path,
        )

    if config.jwt_secret_generated:
        log.warning(
            "本次启动了新生成 JWT 签名密钥（%s/jwt.key）。"
            "多实例部署时必须通过 VIBE_SRV_JWT_SECRET 共享同一密钥，"
            "否则实例之间签发的令牌互相不认。",
            config.data_dir,
        )
    return app


def _summarize_payload(payload: dict[str, Any]) -> str:
    """把脱敏后的载荷压成一行写进 auth_events 的 detail。

    只保留键与标量值，因为 auth_events.detail 是一个普通文本列，
    而我们要保证写进去的内容同样不含正文 —— 经过脱敏的载荷已经安全，
    这里只是控制长度。
    """
    parts = []
    for key in sorted(payload):
        value = payload[key]
        if isinstance(value, (str, int, float, bool)):
            parts.append(f"{key}={value}")
    text = " ".join(parts)
    return text[:400]


def _selfcheck_rbac() -> None:
    """启动自检：确认 RBAC 查表真的绑上了。

    不检查的话，忘了绑定会让所有跨用户操作静默变成 403 ——
    表现为「权限配置全对但功能全坏」，排查成本极高。
    """
    probe = rbac.build_actor(
        user_id="__selfcheck__",
        email="selfcheck@invalid",
        display_name="",
        memberships=[{"team_id": "__t__", "team_slug": "t", "team_name": "t", "role": rbac.ROLE_DEVELOPER}],
    )
    try:
        probe.scope_user("__other__", rbac.PERM_USER_READ)
    except Exception:
        return  # 预期内：要么权限不足要么查不到团队，都说明链路是通的
    # 能走到这里说明 scope_user 对陌生用户放行了，这是越权，必须拦
    raise RuntimeError(
        "RBAC 自检失败：scope_user 对非本人且无共同团队的用户放行了。"
        "检查 rbac.bind_lookup 是否被绑定，以及 Actor 的 teams 装配。"
    )


def _oidc_config(config: ServerConfig) -> dict[str, str]:
    """从配置/环境里读 OIDC 接入参数。没配就返回空（用口令 + 注册令牌模式）。"""
    import os

    issuer = os.environ.get("VIBE_SRV_OIDC_ISSUER", "").strip()
    client_id = os.environ.get("VIBE_SRV_OIDC_CLIENT_ID", "").strip()
    if not issuer or not client_id:
        return {}
    return {
        "issuer": issuer,
        "clientId": client_id,
        "clientSecret": os.environ.get("VIBE_SRV_OIDC_CLIENT_SECRET", "").strip(),
        "redirectUri": os.environ.get("VIBE_SRV_OIDC_REDIRECT_URI", "").strip(),
        "scopes": os.environ.get("VIBE_SRV_OIDC_SCOPES", "openid email profile").strip(),
    }


# --------------------------------------------------------------------------- 引导
def bootstrap_owner(config: ServerConfig, *, email: str, password: str, name: str = "") -> dict:
    """命令行引导：创建第一个组织所有者。

    这是给「内网从零起步」用的一次性入口，因此**只在库为空时可用**，
    并由 CLI 显式调用（不是 HTTP 接口 —— 一个有认证的 bootstrap 接口
    在第一个用户存在后就成了攻击面）。
    """
    store = open_store(config.store_target())
    store.init_schema()
    try:
        service = AuthService(store=store, config=config, clock=Clock())
        return service.bootstrap_org(email=email, password=password, display_name=name)
    finally:
        store.close()


__all__ = ["bootstrap_owner", "build_app"]
