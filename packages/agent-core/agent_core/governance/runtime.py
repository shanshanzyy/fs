"""治理面的统一装配层（M3）。

M2 完成时治理面是「完整但没接线」的：`GovernanceClient`、`PolicyCache`、
`GatewayLLMClient`、`AuditUploader` 都能独立工作，却没有一个入口把它们
拼进 Agent 的启动路径。这个模块就是那个入口 —— CLI 与桌面端（host/session）
都只调用 `wire()` 一个函数，装配细节只写一遍。

三条纪律贯穿始终：

1. **未配置 = 原行为。** `Config.server_url` 为空时 `wire()` 返回
   `status="off"`，Agent 拿到的客户端与 M0/M1 完全一致。治理面是可选增强，
   不是把纯本地用户劫持进一个他们没要求过的依赖里。

2. **降级要「说」出来，不能「卡」住。** 服务端不可达、凭证失效、
   未登录 —— 每种情况都降级为本地模型继续干活，但必须把原因放进
   `notices`，由上层展示。静默降级会让用户以为自己在用云端模型。

3. **策略只收紧，从不开口子。** 本模块只调用 `policy.apply()`
   （只收紧语义），从不拿服务端返回的文档反向「放宽」本机配置。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import LLMError
from ..llm.openai_compat import OpenAICompatClient
from .audit_upload import AuditUploader
from .client import GovernanceClient, GovernanceError
from .credentials import CredentialStore, Credentials
from .policy import PolicyCache, RuntimeConstraints, apply
from .gateway_llm import open_governed_client

# 策略缓存放用户级目录：策略约束的是「这台机器上的 Agent」，
# 与具体工作区无关（放工作区里反而会随仓库被同事拷走、版本漂移）。
POLICY_CACHE_FILE = "policy-cache.json"


@dataclass
class GovernanceRuntime:
    """`wire()` 的产出：装配完成后 Agent 需要的一切。

    Agent 只消费 `config` / `client` / `constraints` / `notices` 四个字段，
    其余字段供 CLI 的 `governance status` 与审计上传使用。
    """

    #: 应用策略后的配置（可能比原配置更收紧；frozen dataclass 的新实例）
    config: Any
    #: 策略运行时约束（拒绝清单、强制审批清单、能否上云）
    constraints: RuntimeConstraints
    #: 交给 Agent 用的模型客户端；None = 用 Agent 的默认（本地直连）
    client: Any = None
    #: 治理面 API 客户端；None = 未接入或已降级
    governance: GovernanceClient | None = None
    #: 审计上传器；None = 不上传
    uploader: AuditUploader | None = None
    #: 给用户看的通知（已去重；展示后可清空）
    notices: list[str] = field(default_factory=list)
    #: off / unauthenticated / degraded / governed
    status: str = "off"
    #: 降级原因（仅 degraded / unauthenticated 时有值）
    degraded_reason: str = ""
    #: 服务端配额视图（只有 governed 且拉取成功才有）。**None 表示「这份信息没有」**，
    #: 与「额度不限」不同 —— 后者在视图里是 `remaining.cloudTokens = None`。
    #: 这两种状态在界面上必须分开显示，否则「没拉到」会被读成「随便用」。
    quota: dict[str, Any] | None = None
    #: 配额拉取失败的原因。**失败不降级、不进 notices** —— 看不到配额不影响干活。
    quota_error: str = ""

    def describe(self) -> dict[str, Any]:
        """`governance status` 与桌面端状态徽标共用的视图。不含任何令牌。"""
        view: dict[str, Any] = {
            "status": self.status,
            "degradedReason": self.degraded_reason,
            "notices": list(self.notices),
            "quota": self.quota,
            "quotaError": self.quota_error,
            "constraints": self.constraints.describe(),
            "client": self.client.describe() if hasattr(self.client, "describe") else None,
        }
        # 治理面地址：界面据此把「去登录」指向 `<serverUrl>/login`（Q 阶段）。
        # 这不是秘密（就是运维填进配置的地址），但没配治理面时**不出现** ——
        # 纯本地用户不该看到任何治理相关的字段。
        server_url = getattr(self.config, "server_url", "") if self.config is not None else ""
        if server_url:
            view["serverUrl"] = server_url
        if self.governance is not None and self.governance.credentials is not None:
            view["credentials"] = self.governance.credentials.describe()
        if self.uploader is not None:
            view["uploadState"] = self.uploader.local_chain_status()
        return view


def wire(config: Any, *, store: CredentialStore | None = None, now: int | None = None) -> GovernanceRuntime:
    """按配置装配治理面。**永远不抛异常** —— 治理面装配失败不能挡住干活。"""
    current = int(now if now is not None else time.time())

    # --- 1) 未配置：M0/M1 的原行为，一行不变 ---
    if not config.server_url:
        return GovernanceRuntime(config=config, constraints=RuntimeConstraints(), status="off")

    credential_store = store or CredentialStore()

    # --- 2) 配置了但没登录：如实告知，本地默认策略继续 ---
    credentials = credential_store.load()
    if credentials is None or not credentials.is_usable(server=config.server_url):
        return GovernanceRuntime(
            config=config,
            constraints=RuntimeConstraints(),
            status="unauthenticated",
            degraded_reason=(
                "已配置治理面（" + config.server_url + "）但本机没有可用登录。"
                "执行 vibe-agent governance login 完成登录后，策略与配额才会生效。"
            ),
            notices=[
                "治理面未登录，本次以本地默认策略运行（仅本地模型）。"
            ],
        )

    client = GovernanceClient(config.server_url, store=credential_store, credentials=credentials)

    try:
        client.ensure_token()
    except (GovernanceError, LLMError) as exc:
        # 刷新失败（被吊销/过期/不可达）。凭证已不可用，与「未登录」同样处理，
        # 但原因不同 —— 要区分「从没登录」和「登录被吊销」。
        return GovernanceRuntime(
            config=config,
            constraints=RuntimeConstraints(),
            status="degraded",
            governance=None,
            degraded_reason=f"会话刷新失败：{exc}",
            notices=["治理面会话已失效，本次以本地默认策略运行（仅本地模型）。"],
        )

    # --- 3) 拉策略（带缓存与 ETag），决定运行时约束 ---
    cache = PolicyCache(path=_cache_path(credential_store)).load()
    try:
        response = client.fetch_policy(etag=cache.etag)
    except (GovernanceError, LLMError) as exc:
        # 服务端暂时不可达：**按缓存策略继续**（缓存有就按缓存，没有就本地默认），
        # 这正是 PolicyCache 存在的意义 —— 策略过期判定在 apply() 里做。
        if cache.document:
            note = f"治理面暂时不可达（{exc}），按本地缓存的策略继续。"
            return _assemble(config, client, credentials, cache, notice=note, now=current)
        return GovernanceRuntime(
            config=config,
            constraints=RuntimeConstraints(),
            client=None,
            governance=client,
            status="degraded",
            degraded_reason=f"策略拉取失败且无本地缓存：{exc}",
            notices=["治理面不可达且无策略缓存，本次以本地默认策略运行。"],
        )

    if response.status == 200:
        body = response.body if isinstance(response.body, dict) else {}
        document = body.get("policy") if isinstance(body.get("policy"), dict) else body
        etag = response.header("etag")
        version = int(body.get("version") or 0) if isinstance(body, dict) else 0
        cache.store(document=document, etag=etag, version=version, now=current)
    elif response.status == 304:
        cache.fetched_at = current  # 304 说明缓存仍有效，刷新时间戳
        cache.save()
    else:
        # 意料之外的状态码：按无缓存处理，走保守默认
        cache = PolicyCache(path=_cache_path(credential_store))

    return _assemble(config, client, credentials, cache, notice="", now=current)


# --------------------------------------------------------------------------- 内部
def _cache_path(store: CredentialStore) -> Path:
    return store.path.parent / POLICY_CACHE_FILE


def _assemble(
    config: Any,
    client: GovernanceClient,
    credentials: Credentials,
    cache: PolicyCache,
    *,
    notice: str,
    now: int,
) -> GovernanceRuntime:
    """拿着「可用的客户端 + 缓存策略」完成最终装配。"""
    stale = cache.is_stale(max_staleness=config.policy_max_staleness, now=now)
    new_config, constraints = apply(
        cache.document,
        config=config,
        stale=stale,
        max_staleness=config.policy_max_staleness,
    )
    if constraints.source_version == 0:
        constraints.source_version = cache.version
    notices = list(constraints.notes)
    if notice:
        notices.append(notice)

    # --- 模型客户端：云端允许且会话有效 → 网关优先 + 本地回退 ---
    gateway_url = config.gateway_url or client.gateway_base_url()
    allow_gateway = constraints.allow_cloud and not constraints.stale
    fallback = OpenAICompatClient(
        base_url=config.base_url,
        model=config.model,
        api_key=config.api_key,
        timeout=config.request_timeout,
    )
    model_client = open_governed_client(
        config=new_config,
        gateway_url=gateway_url if allow_gateway else "",
        token_provider=client.ensure_token,
        fallback_client=fallback,
        allow_gateway=allow_gateway,
    )
    if not allow_gateway:
        notes_extra = (
            "策略禁用云端，模型请求全部走本地。"
            if cache.document
            else "策略未启用云端，模型请求全部走本地。"
        )
        if notes_extra not in notices:
            notices.append(notes_extra)

    # --- 审计上传器：契约拉不到就不上传（fail-closed，见 AuditUploader.upload）---
    uploader = _build_uploader(client, credentials, new_config, notices)

    # --- 配额视图：纯展示。拉不到不影响干活，所以要单独接住错误 ---
    quota, quota_error = _fetch_quota(client)

    return GovernanceRuntime(
        config=new_config,
        constraints=constraints,
        client=model_client,
        governance=client,
        uploader=uploader,
        notices=notices,
        status="governed",
        quota=quota,
        quota_error=quota_error,
    )


def _build_uploader(
    client: GovernanceClient,
    credentials: Credentials,
    config: Any,
    notices: list[str],
) -> AuditUploader | None:
    try:
        body = client.fetch_audit_schema()
    except (GovernanceError, LLMError) as exc:
        notices.append(f"事件契约拉取失败（{exc}），本次会话不上传审计事件。")
        return None
    fields = body.get("fields")
    event_types = body.get("eventTypes")
    if not isinstance(fields, dict) or not fields:
        notices.append("服务端事件契约为空，本次会话不上传审计事件。")
        return None
    uploader = AuditUploader(
        client=client,
        audit_path=config.audit_path,
        state_path=Path(config.workspace) / ".vibe" / "audit-upload-state.json",
        device_id=credentials.device_id,
        schema=fields,
        known_event_types=set(event_types) if isinstance(event_types, (list, tuple, set)) else set(),
    )
    if uploader.state.last_error:
        notices.append(f"上次审计上传未完成（{uploader.state.last_error}），将在本次结束时重试。")
    return uploader


def _fetch_quota(client: GovernanceClient) -> tuple[dict[str, Any] | None, str]:
    """拉一份配额视图，返回 (视图, 失败原因)。

    **刻意不抛异常、不进 notices**：配额只是「让人知道还剩多少」的展示信息，
    拿不到它不该让本次会话多一条告警 —— 那会把噪音混进「策略禁用云端」
    这类真正需要用户知道的提示里。原因单独放在 `quota_error` 里，由界面按需显示。
    """
    try:
        body = client.fetch_quota()
    except (GovernanceError, LLMError) as exc:
        return None, str(exc)
    return (body if isinstance(body, dict) else {}), ""


def upload_audit_now(runtime: GovernanceRuntime) -> dict[str, Any]:
    """上传一次审计事件。给 CLI 退出路径与 `/governance upload` 共用。

    返回结果描述而非抛异常：上传失败不该让用户丢掉已经完成的工作。
    """
    if runtime.uploader is None:
        return {"uploaded": 0, "reason": runtime.degraded_reason or "审计上传未启用"}
    try:
        result = runtime.uploader.upload()
    except (GovernanceError, LLMError) as exc:
        return {"uploaded": 0, "reason": str(exc)}
    return result.describe() if hasattr(result, "describe") else {"uploaded": 0}


__all__ = [
    "GovernanceRuntime",
    "POLICY_CACHE_FILE",
    "upload_audit_now",
    "wire",
]
