"""网关代理：注入密钥、转发上游、降级链、结算用量。

这一层是 ADR-007 的落点，也是整个 M2 里**唯一能看到 prompt 正文的组件**。
围绕这个事实，有两条硬纪律：

1. **正文到此为止。** 正文用于「发往上游」和「敏感模式扫描」两件事，
   用完即弃。不进日志、不进审计、不进数据库、不进错误信息。
   错误信息一律过 `secrets.sanitize_error()`。
2. **密钥只在这一层出现。** 上游密钥由 `SecretStore` 取出，
   塞进 Authorization 头，然后立刻离开作用域。
   任何返回值、异常、日志里都不含它。

降级链 `云端 → 本地 → 明确报错` 的触发条件写得很刻意：
**只在「上游确实坏了」时降级**（连接失败、超时、429、5xx）。
4xx（尤其中间那些 400/404/422）是**请求本身有问题**，
降级到本地只会把同一个坏请求再打一次，还把真正的原因盖掉 ——
用户会看到「本地模型也报错」，然后去查本地模型，方向全错。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterator

from ..errors import ApiError, BadRequest
from . import routing
from .secrets import SecretRecord, sanitize_error

MAX_UPSTREAM_BYTES = 32 << 20      # 32 MiB
# 这些状态码说明「上游服务本身有问题」，值得降级重试
_DEGRADABLE_STATUS = frozenset({429, 500, 502, 503, 504, 507, 509})


# --------------------------------------------------------------------------- 结构
@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "totalTokens": self.total_tokens,
            "estimated": self.estimated,
        }


@dataclass
class UpstreamCall:
    """一次上游调用的结果。"""

    ok: bool
    status: int
    body: bytes
    usage: Usage
    target: str
    upstream_url: str
    latency_ms: int
    streaming: bool = False
    error: str = ""
    # 上游返回的错误正文（已清洗），用于排查
    upstream_message: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    def json_body(self) -> dict[str, Any]:
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}


@dataclass
@dataclass
class _OpenedStream:
    """_open_stream_connection 的返回：连接结果的三态（成功/上游错/连不上）。"""

    ok: bool
    status: int = 0
    target: str = ""
    latency_ms: int = 0
    response: Any = None
    error: str = ""
    upstream_message: str = ""


@dataclass
class GatewayOutcome:
    """一次经过网关的完整请求的结果，交给 HTTP 层写回。

    **必须是 dataclass**：chat() 以关键字参数构造它。少一个装饰器，
    这个类就退化成「不带参数的构造函数」，chat() 会在**每一次调用**上
    抛 TypeError —— 而网关的非流式主路径也就从来没被真正执行过一次。
    """

    call: UpstreamCall
    decision: routing.RouteDecision
    reservation: Any = None
    degraded_from: str = ""
    degrade_reason: str = ""
    # 只含元数据，绝不含正文
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamSession:
    """一次流式网关调用的会话状态（M3）。

    生命周期比非流式长：连接在 `open_stream()` 里打开，正文在
    `iter_chunks()` 被消费完后才结算。配额预留跨这两段存活 ——
    所以「谁负责 release」必须唯一且必然执行：放在 iter_chunks 的
    finally 里，HTTP 层无论写完还是断开都会走到。
    """

    gateway: "GatewayService"
    decision: routing.RouteDecision
    reservation: Any
    prompt_tokens: int
    response: Any                 # 上游 urllib 响应（打开后由本类关闭）
    target: str
    degraded_from: str = ""
    degrade_reason: str = ""
    open_latency_ms: int = 0
    audit_recorder: Any = None    # routes 层注入的审计回调
    _completion_chars: int = field(default=0, repr=False)
    _usage: Usage | None = field(default=None, repr=False)
    _settled: bool = field(default=False, repr=False)

    # ------------------------------------------------------------------ 消费
    def iter_chunks(self) -> Iterator[bytes]:
        """把上游字节流透传给客户端，边传边吸收用量元数据。

        **透传而不是逐帧重组**：网关不理解 SSE 语义也不需要理解 ——
        重组会引入缓冲延迟并可能破坏帧边界。这里只在字节流的副本上
        做轻量解析（数 token、找 usage 帧），原始字节原样下行。
        """
        buffer = b""
        ok = True
        try:
            while True:
                chunk = self.response.read(4096)
                if not chunk:
                    break
                buffer += chunk
                # 按 SSE 帧边界切出完整帧解析；残尾留到下一轮
                frames = buffer.split(b"\n\n")
                buffer = frames.pop()
                for frame in frames:
                    self._absorb_frame(frame)
                yield chunk
        except OSError:
            # 中途断流：客户端看到的是「流被截断」，这是对的；
            # 但配额要按未完成释放，不能按估算结算。
            ok = False
            raise
        finally:
            try:
                self.response.close()
            except OSError:
                pass
            self._settle(ok)

    def _absorb_frame(self, frame: bytes) -> None:
        for line in frame.split(b"\n"):
            text = line.decode("utf-8", "replace").strip()
            if not text.startswith("data:"):
                continue
            data_str = text[5:].strip()
            if not data_str or data_str == "[DONE]":
                continue
            parsed = _safe_json(data_str)
            if not parsed:
                continue
            if isinstance(parsed.get("usage"), dict):
                found = extract_usage(parsed)
                if found is not None:
                    self._usage = found
            choices = parsed.get("choices") or []
            if choices:
                delta = (choices[0] or {}).get("delta") or {}
                piece = delta.get("content")
                if isinstance(piece, str):
                    self._completion_chars += len(piece)

    def abort(self) -> None:
        """流没被消费（或消费到一半客户端就走了）时的收尾：释放预留。

        幂等：已结算过就什么都不做。HTTP 层在 finally 里调它，
        所以它**必须**能在「生成器从未启动」的情况下把账做平 ——
        那是配额最容易悄悄泄漏的一条路径（客户端提前断开）。
        """
        if self._settled:
            return
        self._settled = True
        try:
            self.gateway.quota.release(self.reservation)
        except Exception:  # noqa: BLE001 —— 释放失败不能拖垮正在收尾的响应
            pass
        try:
            self.response.close()
        except OSError:
            pass

    # ------------------------------------------------------------------ 结算
    def _settle(self, ok: bool) -> None:
        if self._settled:
            return  # iter_chunks 只该被消费一次；双保险防重复结算
        self._settled = True
        is_cloud = self.target == routing.CLOUD
        if not ok:
            self.gateway.quota.release(self.reservation)
        elif self._usage is not None:
            self.gateway.quota.settle(
                self.reservation,
                actual_tokens=self._usage.total_tokens,
                is_cloud=is_cloud,
            )
        else:
            # 上游没给 usage（没开 stream_options.include_usage）：按估算结算，
            # 并明确标注 estimated —— 数字不准可以接受，假装精确不行。
            actual = self.prompt_tokens + self._completion_chars // 4
            self.gateway.quota.settle(
                self.reservation, actual_tokens=actual, is_cloud=is_cloud
            )
        if self.audit_recorder is not None:
            try:
                self.audit_recorder(
                    event_type="gateway.response" if ok else "gateway.denied",
                    payload={
                        "route": self.target,
                        "streaming": True,
                        "status": 200 if ok else 0,
                        "latency_ms": self.open_latency_ms,
                        "total_tokens": (
                            self._usage.total_tokens
                            if self._usage is not None
                            else self.prompt_tokens + self._completion_chars // 4
                        ),
                        "usage_estimated": self._usage is None,
                        "degraded_from": self.degraded_from,
                    },
                )
            except Exception:  # noqa: BLE001 —— 审计失败不拖垮正在下行的响应
                pass

    def downstream_headers(self) -> dict[str, str]:
        """流式的路由元信息头。连接打开时就已发出，所以**没有** latency/usage。"""
        headers = {
            "X-Vibe-Route-Target": self.target,
            "X-Vibe-Route-Task": self.decision.task,
            "X-Vibe-Route-Sensitive": "1" if self.decision.sensitive else "0",
        }
        if self.decision.sensitive:
            headers["X-Vibe-Route-Reason"] = "sensitive-content"
        elif self.decision.forced_by_policy:
            headers["X-Vibe-Route-Reason"] = "policy"
        else:
            headers["X-Vibe-Route-Reason"] = "task-tier"
        if self.degraded_from:
            headers["X-Vibe-Degraded-From"] = self.degraded_from
        return headers


# --------------------------------------------------------------------------- 正文提取
def outbound_text(body: dict[str, Any], *, limit: int = 400_000) -> str:
    """把待发正文拼出来，供敏感扫描用。

    只取 content / tool 相关文本，**跳过 base64 图片等大字段** ——
    扫图片不仅没意义，还会把内存打爆。
    截断上限的存在同上：这是安全扫描，不是全文索引。
    """
    chunks: list[str] = []
    total = 0
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            chunks.append(content)
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
                        total += len(text)
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for item in tool_calls:
                if not isinstance(item, dict):
                    continue
                fn = item.get("function")
                if isinstance(fn, dict):
                    for key in ("name", "arguments"):
                        value = fn.get(key)
                        if isinstance(value, str):
                            chunks.append(value)
                            total += len(value)
        if total >= limit:
            break
    # 工具 schema 与系统提示也可能带路径，但它们在 system message 里，已被 content 覆盖
    return "\n".join(chunks)[:limit]


def count_prompt_tokens(body: dict[str, Any]) -> int:
    """粗略估算输入 token 数（不引入 tokenizer 依赖）。

    `len/4` 对英文与代码近似可用，中文会偏小 —— 所以估值只用做
    「预留多少额度」的输入，而**实际用量以字段为准**（见 `extract_usage`）。
    用它做硬计费是不负责任的，用它做预留在没有 tokenizer 的前提下是合理的。
    """
    text = outbound_text(body)
    tools = body.get("tools")
    if isinstance(tools, list):
        try:
            text += json.dumps(tools, ensure_ascii=False)
        except (TypeError, ValueError):
            pass
    return max(1, len(text) // 4)


def extract_usage(payload: dict[str, Any]) -> Usage | None:
    """从上游响应里取真实的 usage 字段。

    有这个字段就必须用它 —— 它是唯一准确的用量来源。
    """
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return None
    try:
        prompt = int(raw.get("prompt_tokens") or 0)
        completion = int(raw.get("completion_tokens") or 0)
        total = int(raw.get("total_tokens") or (prompt + completion))
    except (TypeError, ValueError):
        return None
    if prompt == 0 and completion == 0 and total == 0:
        return None
    return Usage(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=total, estimated=False
    )


# --------------------------------------------------------------------------- 服务
class GatewayService:
    def __init__(self, *, config, store, clock, secrets, quota) -> None:
        self.config = config
        self.store = store
        self.clock = clock
        self.secrets = secrets
        self.quota = quota

    # ============================================================== 上游调用
    def _resolve_upstream(self, target: str) -> tuple[str, str]:
        url, key_ref = routing.upstream_for(target, self.config)
        return url.rstrip("/"), key_ref

    def _prepare(
        self,
        *,
        target: str,
        path: str,
        body: bytes,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[urllib.request.OpenerDirector, urllib.request.Request, str, SecretRecord | None]:
        """准备好 (opener, request, url, 密钥记录)。

        **返回值里带 SecretRecord 是刻意的**：调用方需要在 finally 里
        显式把它丢掉（`del`），让「密钥的生命周期」在代码里可见，
        而不是藏在某个不可见的栈帧里。
        """
        base, key_ref = self._resolve_upstream(target)
        if not base:
            raise ApiError(
                503,
                "upstream_unconfigured",
                f"{'云端' if target == routing.CLOUD else '本地'}上游未配置。"
                f"服务端需要设置 "
                f"{'CLOUD' if target == routing.CLOUD else 'LOCAL'}_UPSTREAM_URL。",
            )

        url = base + path
        # 只允许 http/https，防止配置写错导致 file:// 之类被打开
        scheme = urllib.parse.urlparse(url).scheme.lower()
        if scheme not in ("http", "https"):
            raise ApiError(500, "bad_upstream", f"上游地址协议不支持: {scheme}")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "vibe-governance/0.1",
        }
        record: SecretRecord | None = None
        if key_ref:
            record = self.secrets.get(key_ref)
            if record is None:
                raise ApiError(
                    503,
                    "secret_missing",
                    f"密钥引用 {key_ref!r} 在托管存储里找不到。"
                    "请确认已写入，或检查引用名拼写。",
                )
            headers["Authorization"] = f"Bearer {record.value}"
        if extra_headers:
            for key, value in extra_headers.items():
                # 不允许客户端自定义 Authorization —— 那等于让客户端自带密钥，
                # ADR-007 就白写了。
                if key.lower() in ("authorization", "host", "content-length"):
                    continue
                headers[key] = value

        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if host in {"127.0.0.1", "localhost", "::1"} or "." not in host:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        else:
            opener = urllib.request.build_opener()
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        return opener, request, url, record

    def call(
        self,
        *,
        target: str,
        path: str,
        body: dict[str, Any],
        timeout: int | None = None,
        streaming: bool = False,
    ) -> UpstreamCall:
        """非流式调用上游。"""
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        timeout = timeout or self.config.upstream_timeout
        started = time.perf_counter()

        url = ""
        record: SecretRecord | None = None
        try:
            opener, request, url, record = self._prepare(
                target=target, path=path, body=raw
            )
            with opener.open(request, timeout=timeout) as response:
                payload = response.read(MAX_UPSTREAM_BYTES)
            elapsed = int((time.perf_counter() - started) * 1000)
            parsed = _safe_json(payload)
            usage = extract_usage(parsed) or Usage(estimated=True)
            return UpstreamCall(
                ok=True,
                status=200,
                body=payload,
                usage=usage,
                target=target,
                upstream_url=url,
                latency_ms=elapsed,
                streaming=streaming,
            )
        except urllib.error.HTTPError as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            try:
                detail = sanitize_error(exc.read(4000).decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                detail = ""
            return UpstreamCall(
                ok=False,
                status=int(exc.code),
                body=b"",
                usage=Usage(estimated=True),
                target=target,
                upstream_url=url,
                latency_ms=elapsed,
                error=f"上游返回 HTTP {exc.code}",
                upstream_message=detail,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            reason = getattr(exc, "reason", exc)
            return UpstreamCall(
                ok=False,
                status=0,
                body=b"",
                usage=Usage(estimated=True),
                target=target,
                upstream_url=url,
                latency_ms=elapsed,
                error=f"无法连接上游: {sanitize_error(str(reason), limit=200)}",
            )
        finally:
            # 显式断开对密钥的引用，缩短它的存活窗口
            del record

    # ============================================================== 主流程
    def chat(
        self,
        *,
        actor,
        body: dict[str, Any],
        touched_paths: tuple[str, ...] = (),
        prefer_local: bool = False,
        policy: dict[str, Any] | None = None,
        path: str = "/chat/completions",
    ) -> GatewayOutcome:
        """完整的网关流程：路由 → 限流配额 → 调用 → 降级 → 结算。

        返回的 `GatewayOutcome.call.body` 就是可以直接回给客户端的字节。
        """
        policy = policy or {}

        # --- 1) 路由决策 ---
        text = outbound_text(body)
        prompt_tokens = count_prompt_tokens(body)
        try:
            max_tokens = int(body.get("max_tokens") or 0)
        except (TypeError, ValueError):
            max_tokens = 0
        tools = body.get("tools")

        request = routing.RouteRequest(
            task=str(policy.get("task") or routing.TASK_UNKNOWN),
            touched_paths=tuple(touched_paths),
            outbound_text=text,
            prompt_tokens=prompt_tokens,
            tool_count=len(tools) if isinstance(tools, list) else 0,
            prefer_local=prefer_local,
            cloud_allowed=bool(policy.get("allowCloud", True)),
        )
        decision = routing.decide(request, policy=policy)

        # --- 2) 配额预留 ---
        team_id = ""
        if getattr(actor, "memberships", None):
            team_id = actor.memberships[0].team_id
        reservation = self.quota.reserve(
            user_id=actor.user_id,
            team_id=team_id,
            is_cloud=(decision.target == routing.CLOUD),
            prompt_tokens=prompt_tokens,
            max_tokens=max_tokens or 4096,
        )

        # --- 3) 调用（含降级）---
        degraded_from = ""
        degrade_reason = ""
        call = self.call(target=decision.target, path=path, body=body)

        if not call.ok and self._should_degrade(call.status) and decision.target == routing.CLOUD:
            degraded_from = routing.CLOUD
            degrade_reason = call.error or f"HTTP {call.status}"
            # 云端配额已预留，但这次没真的用上云端 —— 释放掉，
            # 否则一次云端抖动就白白吃掉一段云端额度。
            self.quota.release(reservation)
            # 降级后的本地调用重新预留（本地不计 token，但仍要过限流）
            reservation = self.quota.reserve(
                user_id=actor.user_id,
                team_id=team_id,
                is_cloud=False,
                prompt_tokens=prompt_tokens,
                max_tokens=max_tokens or 4096,
                apply_rate_limit=False,  # 同一请求不重复计限流
            )
            call = self.call(target=routing.LOCAL, path=path, body=body)

        # --- 4) 结算 ---
        if call.ok:
            self.quota.settle(
                reservation,
                actual_tokens=call.usage.total_tokens,
                is_cloud=(call.target == routing.CLOUD),
            )
        else:
            self.quota.release(reservation)

        meta = {
            "route": decision.describe(),
            "target": call.target,
            "status": call.status,
            "latencyMs": call.latency_ms,
            "usage": call.usage.describe(),
            "promptTokensEstimated": prompt_tokens,
            "degradedFrom": degraded_from,
            "degradeReason": degrade_reason,
            # 敏感命中的**条数**而非内容：审计要能回答「为什么这次没上云」，
            # 但不能因此把命中的路径写进服务端数据库。
            "sensitiveRuleCount": len(decision.sensitive_reasons),
        }

        return GatewayOutcome(
            call=call,
            decision=decision,
            reservation=reservation,
            degraded_from=degraded_from,
            degrade_reason=degrade_reason,
            meta=meta,
        )

    # ============================================================== 流式（M3）
    def open_stream(
        self,
        *,
        actor,
        body: dict[str, Any],
        touched_paths: tuple[str, ...] = (),
        prefer_local: bool = False,
        policy: dict[str, Any] | None = None,
        path: str = "/chat/completions",
        audit_recorder: Any = None,
    ) -> StreamSession:
        """流式版主流程：路由 → 预留 → 打开上游连接（含降级）。

        与 chat() 共用同一路由、配额、降级判定 —— 两条路径只在
        「连接怎么打开、用量怎么结算」上不同。任何一处判定分叉，
        都会出现「同一请求流式被拒、非流式放行」这种无法解释的行为。
        """
        policy = policy or {}
        text = outbound_text(body)
        prompt_tokens = count_prompt_tokens(body)
        try:
            max_tokens = int(body.get("max_tokens") or 0)
        except (TypeError, ValueError):
            max_tokens = 0
        tools = body.get("tools")

        request = routing.RouteRequest(
            task=str(policy.get("task") or routing.TASK_UNKNOWN),
            touched_paths=tuple(touched_paths),
            outbound_text=text,
            prompt_tokens=prompt_tokens,
            tool_count=len(tools) if isinstance(tools, list) else 0,
            prefer_local=prefer_local,
            cloud_allowed=bool(policy.get("allowCloud", True)),
        )
        decision = routing.decide(request, policy=policy)

        team_id = ""
        if getattr(actor, "memberships", None):
            team_id = actor.memberships[0].team_id
        reservation = self.quota.reserve(
            user_id=actor.user_id,
            team_id=team_id,
            is_cloud=(decision.target == routing.CLOUD),
            prompt_tokens=prompt_tokens,
            max_tokens=max_tokens or 4096,
        )

        degraded_from = ""
        degrade_reason = ""
        opened = self._open_stream_connection(decision.target, path, body)
        if (
            not opened.ok
            and self._should_degrade(opened.status)
            and decision.target == routing.CLOUD
        ):
            # 降级发生在**下行开始之前**：连接没建起来时状态码还没发出去，
            # 可以干净地换本地重来。流中途坏掉就换不了了（见 StreamSession）。
            degraded_from = routing.CLOUD
            degrade_reason = opened.error or f"HTTP {opened.status}"
            self.quota.release(reservation)
            reservation = self.quota.reserve(
                user_id=actor.user_id,
                team_id=team_id,
                is_cloud=False,
                prompt_tokens=prompt_tokens,
                max_tokens=max_tokens or 4096,
                apply_rate_limit=False,
            )
            opened = self._open_stream_connection(routing.LOCAL, path, body)

        if not opened.ok:
            self.quota.release(reservation)
            raise ApiError(
                opened.status if opened.status >= 400 else 502,
                "upstream_error",
                opened.error or "上游调用失败",
            )

        return StreamSession(
            gateway=self,
            decision=decision,
            reservation=reservation,
            prompt_tokens=prompt_tokens,
            response=opened.response,
            target=opened.target,
            degraded_from=degraded_from,
            degrade_reason=degrade_reason,
            open_latency_ms=opened.latency_ms,
            audit_recorder=audit_recorder,
        )

    def _open_stream_connection(self, target: str, path: str, body: dict[str, Any]):
        """打开一条到上游的流式连接。只建连接不读正文。"""
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        started = time.perf_counter()
        opener, request, url, record = self._prepare(
            target=target, path=path, body=raw,
            extra_headers={"Accept": "text/event-stream"},
        )
        try:
            try:
                response = opener.open(request, timeout=self.config.upstream_timeout)
            except urllib.error.HTTPError as exc:
                try:
                    detail = sanitize_error(exc.read(4000).decode("utf-8", "replace"))
                except Exception:  # noqa: BLE001
                    detail = ""
                return _OpenedStream(
                    ok=False,
                    status=int(exc.code),
                    target=target,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error=f"上游返回 HTTP {exc.code}",
                    upstream_message=detail,
                )
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                return _OpenedStream(
                    ok=False,
                    status=0,
                    target=target,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error=f"无法连接上游: {sanitize_error(str(reason), limit=200)}",
                )
            return _OpenedStream(
                ok=True,
                response=response,
                status=200,
                target=target,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        finally:
            del record  # 密钥离开作用域（与 call() 同一纪律）

    @staticmethod
    def _should_degrade(status: int) -> bool:
        """只有「上游服务坏了」才降级；请求本身有问题时不降级（见文件头）。"""
        return status == 0 or status in _DEGRADABLE_STATUS

    # ============================================================== 传递头
    @staticmethod
    def downstream_headers(outcome: GatewayOutcome) -> dict[str, str]:
        """回给客户端的路由元信息。

        **只回决策结果，不回正文与密钥。** 客户端据此可以在 UI 上
        显示「本次由本地模型处理」或「云端不可用已降级」。
        """
        headers = {
            "X-Vibe-Route-Target": outcome.call.target,
            "X-Vibe-Route-Task": outcome.decision.task,
            "X-Vibe-Route-Sensitive": "1" if outcome.decision.sensitive else "0",
            "X-Vibe-Upstream-Latency-Ms": str(outcome.call.latency_ms),
        }
        if outcome.decision.sensitive:
            headers["X-Vibe-Route-Reason"] = "sensitive-content"
        elif outcome.decision.forced_by_policy:
            headers["X-Vibe-Route-Reason"] = "policy"
        else:
            headers["X-Vibe-Route-Reason"] = "task-tier"
        if outcome.degraded_from:
            headers["X-Vibe-Degraded-From"] = outcome.degraded_from
        if outcome.call.usage.estimated:
            headers["X-Vibe-Usage-Estimated"] = "1"
        return headers

    # ============================================================== 模型清单
    def list_models(self, *, actor) -> dict[str, Any]:
        """告知客户端当前可用的模型与路由能力。

        故意**不返回上游真实模型列表**：那需要拿密钥去问上游，
        且会把内网模型名暴露给所有登录用户。返回的是能力声明。
        """
        local_url, _ = self._resolve_upstream(routing.LOCAL)
        cloud_url, _ = self._resolve_upstream(routing.CLOUD)
        return {
            "local": {
                "configured": bool(local_url),
                "models": list(getattr(self.config, "local_models", []) or []),
            },
            "cloud": {
                "configured": bool(cloud_url),
                "models": list(getattr(self.config, "cloud_models", []) or []),
            },
            "defaults": {
                "accessTtl": self.config.access_ttl,
                "upstreamTimeout": self.config.upstream_timeout,
            },
        }


def _safe_json(payload: bytes | str) -> dict[str, Any]:
    """尽力解析成 dict，失败给空 dict。

    bytes 与 str 都要收：非流式路径拿到的是响应体字节，
    流式路径拿到的已经是按行解码过的文本。签名若只写 bytes，
    流式那一侧会在**每一帧**上抛 AttributeError ——
    而它只在真正跑一次流式时才会暴露。
    """
    try:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="replace")
        data = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def require_json_object(body: bytes) -> dict[str, Any]:
    """网关入口的请求体校验。返回 dict，或抛 400。"""
    if not body:
        raise BadRequest("请求体为空")
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise BadRequest(f"请求体不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise BadRequest("请求体必须是 JSON 对象")
    if "messages" not in data:
        raise BadRequest("请求体缺少 messages 字段")
    # stream=true 在这里**不被拒绝**：M3 起网关支持 SSE 透传，由调用方
    # （api/routes.py）转入 open_stream() 分支，用量按上游 usage 或估算结算。
    # 不要再在这里加回拒绝 —— 那会让 routes 的流式分支变成永远走不到的死代码。
    return data


__all__ = [
    "GatewayOutcome",
    "GatewayService",
    "UpstreamCall",
    "Usage",
    "count_prompt_tokens",
    "extract_usage",
    "outbound_text",
    "require_json_object",
]
