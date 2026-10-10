"""走治理网关的模型客户端。

ADR-007 的客户端落地：**客户端永不持有云端 API Key**。
模型请求发给网关，网关注入密钥、做路由决策、记账；客户端只需要
一个短期访问令牌。

三种失败必须区分对待，这是这层最核心的设计：

| 失败 | 处理 |
|---|---|
| 网关不可达 | **降级到本地模型，任务继续** —— 治理是增强，不是单点依赖 |
| 429 配额用尽 | 同上：降级到本地。配额不是错误，是「今天云端额度没了」 |
| 401 令牌过期 | 刷新后重试一次 |
| 4xx（请求本身的问题） | **不降级** —— 降级只会用同一个坏请求再打一次，还把真因盖掉 |

最后一条与网关侧的降级判定是同一个原则（见服务端 gateway/proxy.py）。
两端都遵守它，否则会出现「本地也报错、原因不明」的情况。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ..errors import LLMError
from ..llm.base import ChatResponse, ToolCall, parse_arguments, parse_text_tool_calls
from ..llm.openai_compat import OpenAICompatClient, consume_chat_sse

# 网关特有的、需要特殊处理的状态码
_STATUS_QUOTA = 429
_STATUS_UNAUTHORIZED = 401


@dataclass
class RouteInfo:
    """服务端返回的路由元信息。

    这些信息要展示给用户：「本次由本地模型处理」/「云端不可用已降级」。
    **不显示会让用户以为自己在用云端模型**，从而对结果质量产生错误预期。
    """

    target: str = ""
    task: str = ""
    reason: str = ""
    sensitive: bool = False
    degraded_from: str = ""
    latency_ms: int = 0
    usage_estimated: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "task": self.task,
            "reason": self.reason,
            "sensitive": self.sensitive,
            "degradedFrom": self.degraded_from,
            "latencyMs": self.latency_ms,
            "usageEstimated": self.usage_estimated,
        }

    def notice(self) -> str:
        if self.degraded_from:
            return f"云端不可用，本次已降级为{_target_label(self.target)}处理"
        if self.sensitive:
            return "本次请求命中敏感内容，已强制由本地模型处理（代码不出内网）"
        return ""


def _target_label(target: str) -> str:
    return {"cloud": "云端模型", "local": "本地模型"}.get(target, target or "未知目标")


class GatewayLLMClient:
    """通过治理网关调用模型。

    不继承 `OpenAICompatClient`，因为它对 HTTP 错误的语义要求不同：
    父类把 429 当"可重试"，而网关的 429 是**配额用尽**，
    重试一万次也是一样的结果，正确动作是降级到本地。
    继承再覆写会让"哪些行为变了"变得不直观，不如显式重写请求层。
    """

    def __init__(
        self,
        *,
        gateway_url: str,
        model: str,
        token_provider,
        touched_paths: tuple[str, ...] = (),
        task: str = "",
        prefer_local: bool = False,
        timeout: int = 300,
        supports_native_tools: bool = True,
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.model = model
        # 传函数而不是令牌字符串：令牌会过期，而客户端实例的生命周期更长。
        # 传字符串的话，长会话跑到一半令牌过期，所有后续调用都会 401。
        self.token_provider = token_provider
        self.touched_paths = tuple(touched_paths)
        self.task = task
        self.prefer_local = prefer_local
        self.timeout = timeout
        self.supports_native_tools = supports_native_tools
        self.last_route = RouteInfo()
        self._opener = _build_opener(self.gateway_url)

    # ------------------------------------------------------------------ 接口
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if tools and self.supports_native_tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        data = self._post("/chat/completions", payload)
        return self._to_response(data)

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        on_delta=None,
    ) -> ChatResponse:
        """流式版 chat()：SSE 透传链路（客户端 ← 网关 ← 上游）。

        错误映射与 chat() 严格一致 —— 429 是配额、401 是令牌、
        连不上是可降级的网关故障。若这里出现第三种语义，
        GovernedClient 的降级判定就会在流式路径上做出与一次性路径
        相反的决定，表现为「同一个故障流式降级、非流式判死」。
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if tools and self.supports_native_tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        response = self._open_stream("/chat/completions", payload)
        with response:
            # 与非流式路径一样吸收路由元信息头。少了这一步，流式请求就
            # **永远拿不到** X-Vibe-Route-*（降级/敏感/目标），表现为
            # 「同一个故障流式静默、非流式能提示」—— 两条路径的元信息必须等价。
            self._absorb_headers(response.headers)
            content_type = str(response.headers.get("Content-Type") or "")
            if "text/event-stream" not in content_type:
                # 网关没按流式回（老版本网关/中间层剥掉了 SSE）——按一次性处理
                raw = response.read().decode("utf-8", errors="replace")
                try:
                    return self._to_response(json.loads(raw) if raw.strip() else {})
                except json.JSONDecodeError as exc:
                    raise LLMError(f"网关返回的不是合法 JSON: {exc}") from exc
            try:
                content, tool_calls_raw, finish_reason, usage = consume_chat_sse(
                    response, on_delta=on_delta
                )
            except TimeoutError as exc:
                raise LLMError(
                    f"流式读取网关响应超时（{self.timeout}s），生成中途断流。"
                ) from exc
            except OSError as exc:
                # 流已经开始，降级没意义（本地回退会从头再跑一遍同样的任务）；
                # 如实报错，让上层把已完成的部分呈现给用户。
                raise LLMError(f"网关流式响应中断: {exc}") from exc

        # 重新拼成 OpenAI 协议形状，复用一次性路径的解析
        # （含文本协议 ```tool 围栏兜底 —— 两条路径解析必须一致）。
        return self._to_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": content,
                            "tool_calls": [
                                {
                                    "id": item["id"],
                                    "function": dict(item["function"]),
                                }
                                for item in tool_calls_raw
                            ],
                        },
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": usage,
            }
        )

    # ------------------------------------------------------------------ 请求
    def _headers(self) -> dict[str, str]:
        token = self.token_provider() if callable(self.token_provider) else str(self.token_provider)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "vibe-agent-core/0.1",
            "Authorization": f"Bearer {token}",
        }
        if self.touched_paths:
            # 换行分隔而不是 JSON：路径里字符复杂，JSON 转义在各语言实现里
            # 容易出差异，而换行分隔的解析处处一致。
            headers["X-Vibe-Paths"] = "\n".join(self.touched_paths[:200])
        if self.prefer_local:
            headers["X-Vibe-Prefer-Local"] = "1"
        if self.task:
            headers["X-Vibe-Task"] = self.task
        return headers

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.gateway_url + ("/v1" + path if not self.gateway_url.endswith("/v1") else path)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=self._headers(), method="POST")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                self._absorb_headers(response.headers)
                raw = response.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:800]
            parsed = _safe_json(detail)
            error = parsed.get("error") if isinstance(parsed, dict) else {}
            message = str(error.get("message") or "") if isinstance(error, dict) else ""

            if exc.code == _STATUS_QUOTA:
                # 配额用尽 → 交给上层降级到本地模型。
                # 这里**不**抛 LLMError：它会被当成"模型坏了"，
                # 而实际是"换个后端就能继续"。
                raise QuotaUnavailable(message or "云端配额已用尽", detail=detail) from exc
            if exc.code == _STATUS_UNAUTHORIZED:
                raise TokenRejected(message or "访问令牌被拒绝", detail=detail) from exc
            raise LLMError(
                f"网关返回 HTTP {exc.code}\n  地址: {url}\n  返回: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            # 网关不可达不是"模型故障"，而是"治理面暂时用不了"——
            # 上层应降级到本地模型继续，而不是把任务判死。
            raise GatewayUnavailable(f"无法连接治理网关: {reason}", detail=str(reason)) from exc
        except json.JSONDecodeError as exc:
            raise LLMError(f"网关返回的不是合法 JSON: {exc}") from exc

    def _open_stream(self, path: str, payload: dict[str, Any]):
        """打开流式连接。与 _post 的唯一差别：返回响应对象而不是读尽的字节，
        错误映射完全一致（这是流式降级判定正确性的前提）。"""
        url = self.gateway_url + ("/v1" + path if not self.gateway_url.endswith("/v1") else path)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = self._headers()
        headers["Accept"] = "text/event-stream"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            return self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:800]
            parsed = _safe_json(detail)
            error = parsed.get("error") if isinstance(parsed, dict) else {}
            message = str(error.get("message") or "") if isinstance(error, dict) else ""
            if exc.code == _STATUS_QUOTA:
                raise QuotaUnavailable(message or "云端配额已用尽", detail=detail) from exc
            if exc.code == _STATUS_UNAUTHORIZED:
                raise TokenRejected(message or "访问令牌被拒绝", detail=detail) from exc
            raise LLMError(
                f"网关返回 HTTP {exc.code}\n  地址: {url}\n  返回: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise GatewayUnavailable(f"无法连接治理网关: {reason}", detail=str(reason)) from exc

    def _absorb_headers(self, headers: Any) -> None:
        def get(name: str, default: str = "") -> str:
            try:
                return str(headers.get(name, default) or default)
            except Exception:  # noqa: BLE001
                return default

        self.last_route = RouteInfo(
            target=get("X-Vibe-Route-Target"),
            task=get("X-Vibe-Route-Task"),
            reason=get("X-Vibe-Route-Reason"),
            sensitive=get("X-Vibe-Route-Sensitive") == "1",
            degraded_from=get("X-Vibe-Degraded-From"),
            latency_ms=_to_int(get("X-Vibe-Upstream-Latency-Ms")),
            usage_estimated=get("X-Vibe-Usage-Estimated") == "1",
        )

    # ------------------------------------------------------------------ 解析
    def _to_response(self, data: dict[str, Any]) -> ChatResponse:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(
                "网关返回中没有 choices 字段，响应结构不符合 OpenAI 协议。\n"
                f"  原始响应片段: {json.dumps(data, ensure_ascii=False)[:400]}"
            )
        choice = choices[0] or {}
        message = choice.get("message") or {}
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )

        tool_calls: list[ToolCall] = []
        for index, item in enumerate(message.get("tool_calls") or []):
            if not isinstance(item, dict):
                continue
            function = item.get("function") or {}
            name = function.get("name")
            if not name:
                continue
            raw_args = function.get("arguments", "")
            tool_calls.append(
                ToolCall(
                    id=str(item.get("id") or f"call_{index}"),
                    name=str(name),
                    arguments=parse_arguments(raw_args),
                    raw_arguments=raw_args
                    if isinstance(raw_args, str)
                    else json.dumps(raw_args, ensure_ascii=False),
                )
            )
        if not tool_calls and content:
            tool_calls = parse_text_tool_calls(content)

        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=str(choice.get("finish_reason") or ""),
            usage=data.get("usage") or {},
        )


# --------------------------------------------------------------------------- 错误
class GatewayUnavailable(LLMError):
    """网关连不上。**这是可降级的**，不是致命错误。"""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class QuotaUnavailable(LLMError):
    """配额/限流。同样可降级到本地。"""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class TokenRejected(LLMError):
    """访问令牌被拒。不可降级为「继续用网关」，但可以降级为本地模型。"""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


# --------------------------------------------------------------------------- 降级
@dataclass
class GovernedClient:
    """带回退的模型客户端：优先网关，失败则本地。

    这是给 Agent 用的最终客户端。它对上层暴露的仍然只是 `chat()`，
    于是 Agent 循环**完全不知道**治理面的存在 —— 这正是 M1 用
    `AgentHooks` + `Approver` 换前端、M2 用同一手法换模型后端的思路延续。
    """

    primary: Any
    fallback: Any = None
    notices: list[str] = field(default_factory=list)
    _failed: bool = False
    _using_fallback: bool = False

    @property
    def using_fallback(self) -> bool:
        return self._using_fallback

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> ChatResponse:
        if self._failed or self.primary is None:
            if self.fallback is None:
                raise LLMError("主模型不可用，且没有配置本地回退模型")
            return self._delegate_fallback(messages, tools, temperature)

        try:
            response = self.primary.chat(messages, tools=tools, temperature=temperature)
            route = getattr(self.primary, "last_route", None)
            if isinstance(route, RouteInfo):
                notice = route.notice()
                if notice and notice not in self.notices:
                    self.notices.append(notice)
            self._using_fallback = False
            return response
        except (GatewayUnavailable, QuotaUnavailable, TokenRejected) as exc:
            if self.fallback is None:
                # 没有本地回退可用时，如实抛出 —— 不要吞掉错误然后返回空响应，
                # 那会让 Agent 以为"模型什么都不想说"，进而做出错误决策。
                raise LLMError(
                    f"{exc}\n"
                    "  治理面不可用且未配置本地模型回退。\n"
                    "  若希望离线也能工作，请配置 VIBE_GATEWAY_FALLBACK=1 并确保"
                    "本地模型（Ollama/vLLM）可达。"
                ) from exc

            self._failed = True
            self._using_fallback = True
            note = (
                "治理面不可用（"
                + type(exc).__name__
                + "），已自动切换到本地模型继续。审计仍会记录本次降级。"
            )
            if note not in self.notices:
                self.notices.append(note)
            return self._delegate_fallback(messages, tools, temperature)

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        on_delta=None,
    ) -> ChatResponse:
        """流式版 chat()，降级语义与 chat() 完全一致。

        降级后回退客户端可能没有 chat_stream（如测试假模型）——
        那就对它调 chat()：丢一次流式体验可以接受，
        为保流式而放弃降级等于把「网关抖一下」升级成「任务失败」。
        """
        if self._failed or self.primary is None:
            if self.fallback is None:
                raise LLMError("主模型不可用，且没有配置本地回退模型")
            return self._delegate_fallback_stream(messages, tools, temperature, on_delta)

        try:
            response = self.primary.chat_stream(
                messages, tools=tools, temperature=temperature, on_delta=on_delta
            )
            route = getattr(self.primary, "last_route", None)
            if isinstance(route, RouteInfo):
                notice = route.notice()
                if notice and notice not in self.notices:
                    self.notices.append(notice)
            self._using_fallback = False
            return response
        except (GatewayUnavailable, QuotaUnavailable, TokenRejected) as exc:
            if self.fallback is None:
                raise LLMError(
                    f"{exc}\n"
                    "  治理面不可用且未配置本地模型回退。\n"
                    "  若希望离线也能工作，请配置 VIBE_GATEWAY_FALLBACK=1 并确保"
                    "本地模型（Ollama/vLLM）可达。"
                ) from exc
            self._failed = True
            self._using_fallback = True
            note = (
                "治理面不可用（"
                + type(exc).__name__
                + "），已自动切换到本地模型继续。审计仍会记录本次降级。"
            )
            if note not in self.notices:
                self.notices.append(note)
            return self._delegate_fallback_stream(messages, tools, temperature, on_delta)

    def _delegate_fallback_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float | None,
        on_delta,
    ) -> ChatResponse:
        stream_fn = getattr(self.fallback, "chat_stream", None)
        try:
            if callable(stream_fn):
                return stream_fn(
                    messages, tools=tools, temperature=temperature, on_delta=on_delta
                )
            return self.fallback.chat(messages, tools=tools, temperature=temperature)  # type: ignore[no-any-return]
        except LLMError as exc:
            raise LLMError(
                f"本地回退模型调用失败: {exc}\n"
                "  这通常意味着本地模型服务没起来（Ollama/vLLM），"
                "或 base_url 指错了地址。"
            ) from exc

    def _delegate_fallback(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float | None,
    ) -> ChatResponse:
        try:
            return self.fallback.chat(messages, tools=tools, temperature=temperature)  # type: ignore[no-any-return]
        except LLMError as exc:
            raise LLMError(
                f"本地回退模型调用失败: {exc}\n"
                "  这通常意味着本地模型服务没起来（Ollama/vLLM），"
                "或 base_url 指错了地址。"
            ) from exc

    def describe(self) -> dict[str, Any]:
        return {
            "usingFallback": self._using_fallback,
            "primary": type(self.primary).__name__ if self.primary else None,
            "fallback": type(self.fallback).__name__ if self.fallback else None,
            "notices": list(self.notices),
        }


# --------------------------------------------------------------------------- 工厂
def open_governed_client(
    *,
    config: Any,
    gateway_url: str = "",
    token_provider=None,
    fallback_client: Any = None,
    touched_paths: tuple[str, ...] = (),
    task: str = "",
    prefer_local: bool = False,
    allow_gateway: bool = True,
) -> Any:
    """决定用哪个客户端。

    三种情况：
    - 没接入治理面 → 直接返回本地客户端（M0/M1 的原有行为，一行不变）
    - 接了治理面 → 网关优先 + 本地回退（若能提供回退客户端）
    - `allow_gateway=False`（策略禁云，或本机就是网关的宿主）→ 只用本地
    """
    if not allow_gateway or not gateway_url or token_provider is None:
        if fallback_client is not None:
            return fallback_client
        return OpenAICompatClient(
            config.base_url,
            config.model,
            api_key=config.api_key,
            timeout=config.request_timeout,
        )

    primary = GatewayLLMClient(
        gateway_url=gateway_url,
        model=config.model,
        token_provider=token_provider,
        touched_paths=touched_paths,
        task=task,
        prefer_local=prefer_local,
        timeout=config.request_timeout,
    )
    if fallback_client is None:
        # 没有本地回退也给一个：网关抖一下就让整个任务失败，是不能接受的
        fallback_client = OpenAICompatClient(
            config.base_url,
            config.model,
            api_key=config.api_key,
            timeout=config.request_timeout,
        )
    return GovernedClient(primary=primary, fallback=fallback_client)


def _build_opener(base_url: str) -> urllib.request.OpenerDirector:
    host = (urllib.parse.urlparse(base_url).hostname or "").lower()
    if host in {"127.0.0.1", "localhost", "::1"} or "." not in host:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _safe_json(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _to_int(value: str) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


__all__ = [
    "GatewayLLMClient",
    "GatewayUnavailable",
    "GovernedClient",
    "QuotaUnavailable",
    "RouteInfo",
    "TokenRejected",
    "open_governed_client",
]
