"""治理面 HTTP 接口。

约定三条：

1. **每个接口的权限声明写在路由上**（`permissions=(...)`），由 HTTP 层统一校验。
   handler 里只做**作用域**校验（`scope_team` / `scope_user`）——
   那是「这个人能不能碰这份数据」，无法在路由层表达。
2. **响应只回元数据。** 没有任何接口会返回代码、prompt、模型输出。
   唯一接触正文的是网关代理，它是透传，不落地。
3. **错误回结构化 JSON**，由 `errors.ApiError` 统一映射，handler 里只管抛。
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from .. import rbac, redaction
from ..errors import ApiError, BadRequest, Forbidden, NotFound
from ..gateway import proxy
from ..gateway.search import MAX_QUERY_CHARS, MAX_RESULTS_CAP, searxng_search
from ..gateway.secrets import fingerprint as secret_fingerprint
from ..hashing import fingerprint
from ..httpd import (
    AUTH_OPTIONAL,
    AUTH_PUBLIC,
    AUTH_REQUIRED,
    Request,
    Response,
    Router,
)
from ..metrics import render_prometheus
from ..releases import packages_dir, safe_package_name
from .static import register_static

API = "/api/v1"


# --------------------------------------------------------------------------- 工具
def _obj(request: Request) -> dict[str, Any]:
    return request.json()


def _int_field(body: dict[str, Any], name: str, default: int = 0) -> int:
    value = body.get(name, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise BadRequest(f"{name} 需要整数")
    try:
        return int(value)
    except ValueError as exc:
        raise BadRequest(f"{name} 需要整数，收到 {value!r}") from exc


def _str_field(body: dict[str, Any], name: str, default: str = "") -> str:
    value = body.get(name, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise BadRequest(f"{name} 需要字符串")
    return value


def _bool_field(body: dict[str, Any], name: str, default: bool = False) -> bool:
    value = body.get(name, default)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    raise BadRequest(f"{name} 需要布尔值")


# --------------------------------------------------------------------------- 健康
def register_health(router: Router) -> None:
    @router.get("/healthz", auth=AUTH_PUBLIC)
    def healthz(request: Request) -> Response:
        ok, detail = request.app.store.health()
        payload = {
            "ok": ok,
            "store": detail,
            "version": request.app.version,
            "uptimeSeconds": request.app.clock.now() - request.app.started_at,
            # 健康检查刻意**不回**配置细节（上游地址、密钥 provider 等）：
            # 它通常不需要认证，能少说就少说。
        }
        return Response.json(payload, status=200 if ok else 503)

    @router.get(API + "/version", auth=AUTH_PUBLIC)
    def version(request: Request) -> Response:
        return Response.json({"version": request.app.version})

    @router.get("/metrics", auth=AUTH_PUBLIC)
    def metrics(request: Request) -> Response:
        """Prometheus 文本格式指标（M4）。

        与 `/healthz` 一样**不要求认证**：监控系统通常不携带应用令牌，
        要求认证的结果往往是被改成「用管理员令牌去爬」，权限反而更大。
        代价是这个端点必须自己守住底线 —— 标签里只有接口模板与状态码，
        没有任何用户维度（见 metrics.py 的三条取舍）。
        """
        store_ok, _detail = request.app.store.health()
        body = render_prometheus(
            request.app.metrics.snapshot(),
            version=request.app.version,
            uptime_seconds=request.app.clock.now() - request.app.started_at,
            store_ok=store_ok,
        )
        return Response.text(body)


# --------------------------------------------------------------------------- 认证
def register_auth(router: Router) -> None:
    @router.post(API + "/auth/bootstrap", auth=AUTH_PUBLIC)
    def bootstrap(request: Request) -> Response:
        body = _obj(request)
        result = request.app.auth_service.bootstrap_org(
            email=_str_field(body, "email"),
            password=_str_field(body, "password"),
            display_name=_str_field(body, "displayName"),
        )
        return Response.json(result, status=HTTPStatus.CREATED)

    @router.get(API + "/auth/config", auth=AUTH_PUBLIC)
    def auth_config(request: Request) -> Response:
        """公开的注册策略探针。

        客户端登录门据此决定「邀请码」是必填还是可留空 —— 硬编码一份策略在
        客户端必然与服务端漂移，然后表现为「一批注册全被拒」。
        这里**只回一个布尔**，不泄漏任何配置细节。
        """
        return Response.json(
            {"registrationOpen": bool(getattr(request.app.config, "open_registration", False))}
        )

    @router.post(API + "/auth/register", auth=AUTH_PUBLIC)
    def register(request: Request) -> Response:
        body = _obj(request)
        result = request.app.auth_service.register_with_enrollment(
            enrollment_token=_str_field(body, "enrollmentToken"),
            email=_str_field(body, "email"),
            password=_str_field(body, "password"),
            display_name=_str_field(body, "displayName"),
            device_name=_str_field(body, "deviceName"),
            platform=_str_field(body, "platform"),
            fingerprint=_str_field(body, "fingerprint"),
            client_ip=request.remote,
        )
        return Response.json(result, status=HTTPStatus.CREATED)

    @router.post(API + "/auth/login", auth=AUTH_PUBLIC)
    def login(request: Request) -> Response:
        body = _obj(request)
        result = request.app.auth_service.login(
            email=_str_field(body, "email"),
            password=_str_field(body, "password"),
            device_name=_str_field(body, "deviceName"),
            platform=_str_field(body, "platform"),
            fingerprint=_str_field(body, "fingerprint"),
            client_ip=request.remote,
        )
        return Response.json(result)

    @router.post(API + "/auth/refresh", auth=AUTH_PUBLIC)
    def refresh(request: Request) -> Response:
        body = _obj(request)
        result = request.app.auth_service.refresh(
            refresh_token=_str_field(body, "refreshToken"), client_ip=request.remote
        )
        return Response.json(result)

    @router.post(API + "/auth/logout", auth=AUTH_OPTIONAL)
    def logout(request: Request) -> Response:
        body = _obj(request)
        result = request.app.auth_service.logout(
            access_token=request.bearer(),
            refresh_token=_str_field(body, "refreshToken"),
            all_devices=_bool_field(body, "allDevices"),
        )
        return Response.json(result)

    @router.get(API + "/auth/me")
    def me(request: Request) -> Response:
        return Response.json({"actor": request.actor.describe()})

    @router.get(API + "/auth/oidc/begin", auth=AUTH_PUBLIC)
    def oidc_begin(request: Request) -> Response:
        provider = request.app.oidc
        if provider is None:
            raise NotFound("未配置 OIDC，请用注册令牌或口令登录")
        started = provider.begin()
        # state / verifier 由服务端暂存（短 TTL），不交给客户端保管 ——
        # 交给客户端就等于让 CSRF 防护依赖客户端诚实。
        request.app.oidc_flows.put(started["state"], started)
        return Response.json({"authorizationUrl": started["authorizationUrl"]})

    @router.post(API + "/auth/oidc/callback", auth=AUTH_PUBLIC)
    def oidc_callback(request: Request) -> Response:
        provider = request.app.oidc
        if provider is None:
            raise NotFound("未配置 OIDC")
        body = _obj(request)
        state = _str_field(body, "state")
        flow = request.app.oidc_flows.pop(state)
        if flow is None:
            # state 不认识 → 可能是 CSRF，也可能是流程过期。都拒。
            raise Forbidden("OIDC state 无效或已过期，请重新发起登录")
        claims = provider.exchange(
            code=_str_field(body, "code"),
            code_verifier=flow["codeVerifier"],
            nonce=flow["nonce"],
        )
        result = request.app.auth_service.login_via_oidc(
            claims=claims,
            device_name=_str_field(body, "deviceName"),
            platform=_str_field(body, "platform"),
            fingerprint=_str_field(body, "fingerprint"),
            client_ip=request.remote,
        )
        return Response.json(result)


# --------------------------------------------------------------------------- 用户与团队
def register_admin(router: Router) -> None:
    @router.get(API + "/users", permissions=(rbac.PERM_USER_READ,))
    def list_users(request: Request) -> Response:
        return Response.json(
            {
                "users": request.app.auth_service.list_org_users(
                    actor=request.actor,
                    limit=min(max(1, request.q_int("limit", 100)), 500),
                    offset=max(0, request.q_int("offset", 0)),
                )
            }
        )

    @router.post(API + "/teams", permissions=(rbac.PERM_TEAM_WRITE,))
    def create_team(request: Request) -> Response:
        body = _obj(request)
        result = request.app.auth_service.create_team(
            actor=request.actor,
            slug=_str_field(body, "slug"),
            name=_str_field(body, "name"),
        )
        return Response.json(result, status=HTTPStatus.CREATED)

    @router.post(API + "/teams/{teamId}/members", permissions=(rbac.PERM_TEAM_WRITE,))
    def add_member(request: Request, teamId: str) -> Response:
        body = _obj(request)
        result = request.app.auth_service.add_team_member(
            actor=request.actor,
            team_id=teamId,
            user_id=_str_field(body, "userId"),
            role=_str_field(body, "role", rbac.ROLE_DEVELOPER),
        )
        return Response.json(result, status=HTTPStatus.CREATED)

    def remove_member(request: Request, teamId: str, userId: str) -> Response:
        return Response.json(
            request.app.auth_service.remove_team_member(
                actor=request.actor, team_id=teamId, user_id=userId
            )
        )

    router.add(
        "DELETE",
        API + "/teams/{teamId}/members/{userId}",
        remove_member,
        permissions=(rbac.PERM_TEAM_WRITE,),
    )

    @router.post(API + "/enrollments", permissions=(rbac.PERM_ENROLLMENT_WRITE,))
    def create_enrollment(request: Request) -> Response:
        body = _obj(request)
        result = request.app.auth_service.create_enrollment(
            actor=request.actor,
            team_id=_str_field(body, "teamId"),
            role=_str_field(body, "role", rbac.ROLE_DEVELOPER),
            max_uses=_int_field(body, "maxUses", 1),
            ttl=_int_field(body, "ttl", 0),
            note=_str_field(body, "note"),
        )
        return Response.json(result, status=HTTPStatus.CREATED)

    @router.get(API + "/devices", permissions=(rbac.PERM_DEVICE_READ,))
    def list_devices(request: Request) -> Response:
        return Response.json(
            {
                "devices": request.app.auth_service.list_devices(
                    actor=request.actor, user_id=request.q("userId")
                )
            }
        )

    @router.post(API + "/devices/{deviceId}/revoke", permissions=(rbac.PERM_DEVICE_REVOKE,))
    def revoke_device(request: Request, deviceId: str) -> Response:
        return Response.json(
            request.app.auth_service.revoke_device(actor=request.actor, device_id=deviceId)
        )

    # ---------------------------------------------------------- 控制台只读口（M8）
    # 仅补读口：控制台（/console）要列团队、列成员、列邀请码台账，
    # 这三样此前只有 CLI / 手写请求可用。写口已存在（POST /teams 等）。
    @router.get(API + "/teams", permissions=(rbac.PERM_TEAM_READ,))
    def list_teams(request: Request) -> Response:
        return Response.json({"teams": request.app.auth_service.list_teams(actor=request.actor)})

    @router.get(API + "/teams/{teamId}/members", permissions=(rbac.PERM_TEAM_READ,))
    def list_team_members(request: Request, teamId: str) -> Response:
        return Response.json(
            {
                "members": request.app.auth_service.list_team_members(
                    actor=request.actor, team_id=teamId
                )
            }
        )

    @router.get(API + "/enrollments", permissions=(rbac.PERM_ENROLLMENT_WRITE,))
    def list_enrollments(request: Request) -> Response:
        return Response.json(
            {
                "enrollments": request.app.auth_service.list_enrollments(
                    actor=request.actor, team_id=request.q("teamId")
                )
            }
        )


# --------------------------------------------------------------------------- 策略
def register_policy(router: Router) -> None:
    @router.get(API + "/policy", permissions=(rbac.PERM_POLICY_READ,))
    def get_policy(request: Request) -> Response:
        bundle = request.app.policy_service.resolve(
            actor=request.actor, team_id=request.q("teamId")
        )
        etag = bundle.etag
        # 304：客户端拿这个省流量，也省掉一次无意义的配置重算
        if request.headers.get("if-none-match") == etag:
            return Response.json({}, status=HTTPStatus.NOT_MODIFIED, ETag=etag)
        return Response.json(bundle.describe(), ETag=etag)

    @router.post(API + "/policy", permissions=(rbac.PERM_POLICY_WRITE,))
    def publish_policy(request: Request) -> Response:
        body = _obj(request)
        document = body.get("policy")
        if not isinstance(document, dict):
            raise BadRequest("缺少 policy 对象")
        result = request.app.policy_service.publish(
            actor=request.actor,
            scope_type=_str_field(body, "scopeType", "global"),
            scope_id=_str_field(body, "scopeId"),
            document=document,
            note=_str_field(body, "note"),
        )
        return Response.json(result, status=HTTPStatus.CREATED)

    @router.get(API + "/policy/versions", permissions=(rbac.PERM_POLICY_READ,))
    def policy_versions(request: Request) -> Response:
        return Response.json(
            {
                "versions": request.app.policy_service.list_versions(
                    actor=request.actor,
                    scope_type=request.q("scopeType", "global"),
                    scope_id=request.q("scopeId"),
                )
            }
        )


# --------------------------------------------------------------------------- 配额
def register_quota(router: Router) -> None:
    @router.get(API + "/quota", permissions=(rbac.PERM_QUOTA_READ,))
    def get_quota(request: Request) -> Response:
        return Response.json(
            request.app.quota_service.status(
                actor=request.actor,
                user_id=request.q("userId"),
                team_id=request.q("teamId"),
            )
        )

    @router.get(API + "/quota/policies", permissions=(rbac.PERM_QUOTA_READ,))
    def list_quota_policies(request: Request) -> Response:
        return Response.json({"policies": request.app.quota_service.list_policies(actor=request.actor)})

    @router.get(API + "/quota/budget", permissions=(rbac.PERM_QUOTA_READ,))
    def get_quota_budget(request: Request) -> Response:
        """读预算策略（推导用），作用域校验同 `GET /quota`。"""
        return Response.json(
            request.app.quota_service.get_budget(
                actor=request.actor,
                scope_type=request.q("scopeType") or "user",
                scope_id=request.q("scopeId"),
            )
        )

    @router.post(API + "/quota/budget", permissions=(rbac.PERM_QUOTA_WRITE,))
    def set_quota_budget(request: Request) -> Response:
        """写预算策略（推导用）。它不是显式额度 —— 只在没有显式策略时补默认。"""
        body = _obj(request)
        result = request.app.quota_service.set_budget(
            actor=request.actor,
            scope_type=_str_field(body, "scopeType", "user"),
            scope_id=_str_field(body, "scopeId"),
            budget=_int_field(body, "budget", 0),
            unit_price=_int_field(body, "unitPrice", 0),
            window=_int_field(body, "window", 0),
        )
        return Response.json(result)

    @router.get(API + "/usage", permissions=(rbac.PERM_USAGE_READ,))
    def usage_summary(request: Request) -> Response:
        """按天汇总用量（M4 成本看板的数据源）。

        权限声明在路由上（`usage:read`），handler 里只做作用域校验：
        「能不能用这个接口」与「能不能看这条数据」是两件事 ——
        看自己只需登录，看别人要有 usage:read，看团队要有 usage:read_team。
        """
        return Response.json(
            request.app.quota_service.usage_summary(
                actor=request.actor,
                user_id=request.q("userId"),
                team_id=request.q("teamId"),
                days=request.q_int("days", 30),
            )
        )

    @router.post(API + "/quota", permissions=(rbac.PERM_QUOTA_WRITE,))
    def set_quota(request: Request) -> Response:
        body = _obj(request)
        result = request.app.quota_service.set_policy(
            actor=request.actor,
            scope_type=_str_field(body, "scopeType", "user"),
            scope_id=_str_field(body, "scopeId"),
            daily_cloud_tokens=_int_field(body, "dailyCloudTokens", 0),
            daily_requests=_int_field(body, "dailyRequests", 0),
            requests_per_minute=_int_field(body, "requestsPerMinute", 0),
        )
        return Response.json(result)


# --------------------------------------------------------------------------- 网关
def register_gateway(router: Router) -> None:
    @router.get(API + "/gateway/models", permissions=(rbac.PERM_GATEWAY_USE,))
    def list_models(request: Request) -> Response:
        return Response.json(request.app.gateway.list_models(actor=request.actor))

    @router.post(API + "/gateway/v1/chat/completions", permissions=(rbac.PERM_GATEWAY_USE,))
    def chat_completions(request: Request) -> Response:
        body = proxy.require_json_object(request.body)

        # 客户端声明的触碰路径：用请求头传，避免污染模型请求体
        touched = _parse_paths(request.headers.get("x-vibe-paths") or "")
        prefer_local = (request.headers.get("x-vibe-prefer-local") or "") in ("1", "true")
        task = request.headers.get("x-vibe-task") or ""

        # 取该主体的生效策略，路由决策要按它来（只能收紧）
        bundle = request.app.policy_service.resolve(actor=request.actor)
        policy = dict(bundle.document)
        if task:
            policy["task"] = task

        # 流式（M3）：SSE 透传。路由、配额、降级判定与非流式共用同一套，
        # 分叉只允许发生在「连接怎么打开、用量怎么结算」上。
        if body.get("stream"):
            def recorder(*, event_type: str, payload: dict) -> None:
                request.app.audit_recorder(
                    actor=request.actor,
                    device_id=request.actor.device_id,
                    event_type=event_type,
                    payload=payload,
                )

            session = request.app.gateway.open_stream(
                actor=request.actor,
                body=body,
                touched_paths=touched,
                prefer_local=prefer_local,
                policy=policy,
                audit_recorder=recorder,
            )
            return Response(
                status=200,
                stream=session.iter_chunks(),
                content_type="text/event-stream; charset=utf-8",
                headers=session.downstream_headers(),
                # 客户端提前断开时由 HTTP 层兜底释放预留（生成器可能从未启动）
                stream_abort=session.abort,
            )

        outcome = request.app.gateway.chat(
            actor=request.actor,
            body=body,
            touched_paths=touched,
            prefer_local=prefer_local,
            policy=policy,
        )

        # 审计只记元数据；正文到这里为止，不再往任何地方写
        request.app.audit_recorder(
            actor=request.actor,
            device_id=request.actor.device_id,
            event_type="gateway.response" if outcome.call.ok else "gateway.denied",
            payload=(
                {
                    "route": outcome.call.target,
                    "status": outcome.call.status,
                    "latency_ms": outcome.call.latency_ms,
                    "total_tokens": outcome.call.usage.total_tokens,
                    "usage_estimated": outcome.call.usage.estimated,
                    "degraded_from": outcome.degraded_from,
                }
                if outcome.call.ok
                else {"kind": "upstream_error", "route": outcome.call.target}
            ),
        )

        headers = proxy.GatewayService.downstream_headers(outcome)

        if not outcome.call.ok:
            # 上游失败：如实回状态与清洗过的原因，**不泄漏上游原文**
            # （原文可能含密钥或被注入的内容）。
            status = outcome.call.status if outcome.call.status >= 400 else 502
            return Response.json(
                {
                    "error": {
                        "code": "upstream_error",
                        "message": outcome.call.error or "上游调用失败",
                        "route": outcome.call.target,
                        "degradedFrom": outcome.degraded_from or None,
                        # 清洗过的上游说明，便于排查；不含密钥与正文
                        "upstream": outcome.call.upstream_message or None,
                    }
                },
                status=status,
                **headers,
            )

        return Response(
            status=200,
            raw=outcome.call.body,
            content_type="application/json; charset=utf-8",
            headers=headers,
        )

    @router.post(API + "/search", permissions=(rbac.PERM_GATEWAY_USE,))
    def search_relay(request: Request) -> Response:
        """搜索中转（M7）：客户端零密钥，搜索上游只活在服务端。

        认证与 LLM 网关同源（JWT + gateway:use）。查询词在服务端也只记
        指纹 —— 服务端不做查询内容留存，与客户端 tool.web_search 同口径。
        未配置上游 / 上游不可达都明确回 502，不静默降级。
        """
        body = _obj(request)
        query = _str_field(body, "query").strip()
        if not query:
            raise BadRequest("缺少 query")
        if len(query) > MAX_QUERY_CHARS:
            raise BadRequest(f"查询词过长（上限 {MAX_QUERY_CHARS} 字符）")
        max_results = min(max(1, _int_field(body, "maxResults", 8)), MAX_RESULTS_CAP)

        search_url = request.app.config.search_url
        if not search_url:
            raise ApiError(
                502, "search_upstream_error", "服务端未配置搜索上游（VIBE_SRV_SEARCH_URL）"
            )

        results = searxng_search(search_url, query, max_results)

        request.app.audit_recorder(
            actor=request.actor,
            device_id=request.actor.device_id,
            event_type="web.search_relay",
            payload={
                "query_digest": fingerprint(query),
                "query_chars": len(query),
                "results": len(results),
            },
        )
        return Response.json({"results": results})


def _parse_paths(raw: str) -> tuple[str, ...]:
    """解析客户端声明的触碰路径。

    用换行分隔而不是 JSON：路径里可能出现各种字符，JSON 转义容易被
    不同语言的实现弄出差异，而换行分隔的解析在任何语言里都一样。
    上限 200 条，防止有人塞几万条把敏感扫描拖慢。
    """
    if not raw:
        return ()
    items = [part.strip() for part in raw.replace("\r", "\n").split("\n")]
    return tuple(item for item in items if item)[:200]


# --------------------------------------------------------------------------- 审计
def register_audit(router: Router) -> None:
    @router.post(API + "/audit/events")
    def ingest_events(request: Request) -> Response:
        body = _obj(request)
        result = request.app.audit_service.ingest(actor=request.actor, body=body)
        return Response.json(result.describe(), status=HTTPStatus.ACCEPTED)

    @router.get(API + "/audit/events", permissions=(rbac.PERM_AUDIT_READ,))
    def query_events(request: Request) -> Response:
        params = {
            "teamId": request.q("teamId"),
            "userId": request.q("userId"),
            "deviceId": request.q("deviceId"),
            "sessionId": request.q("sessionId"),
            "eventType": request.q("eventType"),
            "since": request.q_int("since", 0),
            "until": request.q_int("until", 0),
            "limit": request.q_int("limit", 100),
            "offset": request.q_int("offset", 0),
        }
        return Response.json(request.app.audit_service.query(actor=request.actor, params=params))

    @router.get(API + "/audit/summary", permissions=(rbac.PERM_AUDIT_READ,))
    def audit_summary(request: Request) -> Response:
        return Response.json(
            request.app.audit_service.summary(
                actor=request.actor,
                team_id=request.q("teamId"),
                since=request.q_int("since", 0),
            )
        )

    @router.post(API + "/audit/verify", permissions=(rbac.PERM_AUDIT_READ,))
    def audit_verify(request: Request) -> Response:
        body = _obj(request)
        return Response.json(
            request.app.audit_service.verify(
                actor=request.actor, device_id=_str_field(body, "deviceId")
            )
        )

    @router.get(API + "/audit/schema", permissions=(rbac.PERM_AUDIT_READ,))
    def audit_schema(request: Request) -> Response:
        """把服务端接受的事件契约暴露给客户端，省去两端口径不一致的来回猜。

        发的是**完整契约**（每个事件类型各自的字段表），不只是事件名清单。
        客户端据此在本地先过滤：只有「事件名在册 + 字段在白名单内」的条目
        才会进入上传链。这件事必须在客户端做而不是靠服务端兜底 ——
        服务端拒绝是「整条拒收」，而那条事件此时已经算进客户端的上传链了，
        一旦被拒就永久丢失（链已推进，重传不会补上）。
        """
        return Response.json(
            {
                "eventTypes": redaction.known_event_types(),
                "fields": redaction.event_schema(),
                "deniedKeys": sorted(redaction.DENIED_KEYS),
                "commonFields": ["session_id", "seq", "ts"],
                "maxStringLength": redaction.MAX_STRING,
                "maxEventsPerBatch": request.app.config.audit_max_batch_events,
            }
        )


# --------------------------------------------------------------------------- 密钥台账
def register_secrets(router: Router) -> None:
    @router.get(API + "/secrets", permissions=(rbac.PERM_SECRET_READ,))
    def list_secrets(request: Request) -> Response:
        """只回台账（名字 + 指纹 + 轮换时间），**永远不回值**。

        这一层刻意不提供「读取密钥值」的接口 —— 需要值的地方只有网关，
        它在进程内直接问 SecretStore。多一个读值接口就多一个泄漏面。
        """
        rows = request.app.store.list_secret_meta()
        overdue = request.app.secret_rotation_report(rows)
        return Response.json(
            {
                "provider": request.app.secrets.describe()["provider"],
                "secrets": [
                    {
                        "name": str(r.get("name") or ""),
                        "provider": str(r.get("provider") or ""),
                        "fingerprint": str(r.get("fingerprint") or ""),
                        "rotatedAt": int(r.get("rotated_at") or 0),
                        "note": str(r.get("note") or ""),
                    }
                    for r in rows
                ],
                "rotationOverdue": overdue,
            }
        )

    @router.post(API + "/secrets", permissions=(rbac.PERM_SECRET_WRITE,))
    def register_secret(request: Request) -> Response:
        """登记密钥元数据（不写入值）。

        值和台账分离是刻意的：值在 Vault / env / 受限文件里由运维写，
        台账在这里由服务登记。合并成一个「写密钥」接口的话，
        密钥就会经过 HTTP 请求体，进日志、进抓包、进错误栈。
        """
        body = _obj(request)
        name = _str_field(body, "name").strip()
        if not name:
            raise BadRequest("缺少 name")
        record = request.app.secrets.get(name)
        if record is None:
            raise NotFound(f"托管存储里没有名为 {name!r} 的密钥，请先写入值再登记台账")
        row = request.app.store.upsert_secret_meta(
            name,
            request.app.secrets.describe()["provider"],
            "",
            secret_fingerprint(record.value),
            note=_str_field(body, "note"),
            now=request.app.clock.now(),
        )
        return Response.json(
            {
                "name": str(row.get("name") or name),
                "fingerprint": str(row.get("fingerprint") or ""),
                "rotatedAt": int(row.get("rotated_at") or 0),
            },
            status=HTTPStatus.CREATED,
        )


# --------------------------------------------------------------------------- 客户端发布（M9）
def register_releases(router: Router) -> None:
    """客户端发布与更新检查。

    权限：读全角色（`release:read`），写仅 owner（`release:write`）。
    `release` 是**全局资源**（一份清单下发到所有客户端），因此不做团队隔离，
    handler 里也没有 `scope_team` —— 这条要显式写出来，避免后来者以为漏了。
    """

    @router.get(API + "/client/releases", permissions=(rbac.PERM_RELEASE_READ,))
    def list_releases(request: Request) -> Response:
        return Response.json(
            {
                # 公钥随列表一起给，方便控制台与运维核对「客户端内置的是哪一把」。
                # 公钥只能验签、不能签名，公开无妨。
                "pubkey": request.app.releases.public_key(),
                "releases": request.app.releases.list(
                    channel=request.q("channel"),
                    platform=request.q("platform"),
                    arch=request.q("arch"),
                    include_revoked=request.q("includeRevoked") in ("1", "true"),
                    limit=request.q_int("limit", 100),
                ),
            }
        )

    @router.get(API + "/client/updates/check", auth=AUTH_OPTIONAL)
    def check_update(request: Request) -> Response:
        """客户端检查更新。**公开可访问**：未接入治理面的客户端也要能查。

        设备稳定标识优先取已认证主体的 `device_id`（与 M4 灰度同一批 key），
        未认证时退回 `?device=` —— 客户端在接入前会用 `sha256(hostname+machineId)`
        算一个稳定值传上来。两者都没有则不参与灰度分桶（宁可不动）。
        """
        actor = request.actor
        device_key = (str(getattr(actor, "device_id", "") or "") or request.q("device")).strip()
        result = request.app.releases.check(
            channel=request.q("channel", "stable"),
            platform=request.q("platform", "windows"),
            arch=request.q("arch", "x64"),
            current=request.q("current", "0"),
            device_key=device_key,
        )
        # kid 取自命中 release 自己记录的签名者公钥指纹（没有 release 就是空）——
        # 刻意不读服务端私钥：这一条是**公开**端点，不该为它触发一次密钥加载/生成。
        result["pubkeyKid"] = str((result.get("release") or {}).get("pubkeyKid") or "")
        return Response.json(result)

    @router.post(API + "/client/releases", permissions=(rbac.PERM_RELEASE_WRITE,))
    def create_release(request: Request) -> Response:
        """发布一个版本。**签名在服务端完成**（私钥不出进程、不进请求体）。"""
        body = _obj(request)
        created = request.app.releases.publish(
            version=_str_field(body, "version"),
            channel=_str_field(body, "channel", "stable"),
            sha256=_str_field(body, "sha256"),
            size=_int_field(body, "size"),
            platform=_str_field(body, "platform", "windows"),
            arch=_str_field(body, "arch", "x64"),
            url=_str_field(body, "url"),
            notes=_str_field(body, "notes"),
            mandatory=_bool_field(body, "mandatory"),
            min_version=_str_field(body, "minVersion"),
            rollout_percent=_int_field(body, "rolloutPercent", 100),
            created_by=str(getattr(request.actor, "user_id", "") or ""),
        )
        request.app.audit_recorder(
            actor=request.actor,
            device_id=str(getattr(request.actor, "device_id", "") or ""),
            event_type="release.publish",
            payload={
                "releaseId": created["id"],
                "version": created["version"],
                "channel": created["channel"],
                "platform": created["platform"],
                "arch": created["arch"],
                # 只记指纹，不记整串 sha256（与 ADR-006 的「不落正文」同口径）
                "sha256Digest": created["sha256"][:16],
                "size": created["size"],
                "mandatory": created["mandatory"],
                "rolloutPercent": created["rolloutPercent"],
                "seq": created["seq"],
            },
        )
        return Response.json(created, status=HTTPStatus.CREATED)

    @router.post(API + "/client/releases/revoke", permissions=(rbac.PERM_RELEASE_WRITE,))
    def revoke_release(request: Request) -> Response:
        """撤销一个发布 —— 客户端下次 check 起不再看到它。

        刻意用 `POST /releases/revoke` + body 带 id，而不是 `{id}/revoke`：
        Router 只匹配**定长单段** `{name}`，两段模板匹配不上。
        """
        body = _obj(request)
        release_id = _str_field(body, "id").strip()
        if not release_id:
            raise BadRequest("缺少 id")
        request.app.releases.get(release_id)  # 不存在先 404，别把 404 说成「已撤销」
        if not request.app.releases.revoke(release_id):
            raise NotFound("该发布记录已撤销")
        request.app.audit_recorder(
            actor=request.actor,
            device_id=str(getattr(request.actor, "device_id", "") or ""),
            event_type="release.revoke",
            payload={"releaseId": release_id, "revoked": True},
        )
        return Response.json({"id": release_id, "revoked": True})

    @router.get(API + "/client/packages/{name}", auth=AUTH_OPTIONAL)
    def serve_package(request: Request, name: str) -> Response:
        """治理面自托管的安装器二进制（默认开，内网单机部署最省事）。

        转发的是**安装器二进制**（不是源码/Prompt/模型输出），不触碰 ADR-006。
        穿越防护三道照 `static._serve_asset` 的口径：名字白名单 + resolve 后仍在
        包目录内 + 必须是文件。
        """
        safe = safe_package_name(name)
        base = packages_dir(request.app.config.data_dir)
        target = (base / safe).resolve()
        root = base.resolve()
        if target != root and root not in target.parents:
            raise Forbidden("非法包路径")
        if not target.is_file():
            raise NotFound("安装包不存在")
        return Response(
            status=200,
            raw=target.read_bytes(),
            content_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{safe}"'},
        )


# --------------------------------------------------------------------------- 装配
def build_router() -> Router:
    router = Router()
    register_health(router)
    # 静态页（Q 阶段）：服务端唯一的 HTML 出口，公开、不需 token。
    # 顶层路径 /login 与 /assets/*，与 /api/v1/* 无冲突。
    register_static(router)
    register_auth(router)
    register_admin(router)
    register_policy(router)
    register_quota(router)
    register_gateway(router)
    register_audit(router)
    register_secrets(router)
    register_releases(router)
    return router


__all__ = [
    "API",
    "build_router",
    "register_audit",
    "register_health",
    "register_releases",
    "register_secrets",
    "register_static",
]
