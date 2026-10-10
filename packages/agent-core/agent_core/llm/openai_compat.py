"""OpenAI 兼容协议的模型客户端（标准库实现，零第三方依赖）。

只要上游暴露 /chat/completions，就能直接接入：
    Ollama   http://127.0.0.1:11434/v1
    vLLM     http://<gpu-host>:8000/v1
    DeepSeek https://api.deepseek.com/v1
    OpenAI   https://api.openai.com/v1
    公司自建网关 https://llm-gateway.internal/v1

两个容易被忽略但很关键的细节：
1. **本地地址必须绕过代理**。公司内网机器常设 HTTP_PROXY，若不显式绕过，
   发往 127.0.0.1 的推理请求会被代理拦掉，报出极难排查的错。
2. **工具调用要双通道**。本地模型对原生 function calling 的支持参差不齐，
   因此除了原生 tool_calls，还支持从纯文本里解析 ```tool 代码块（见 base.py）。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from ..errors import LLMError
from .base import (
    ChatResponse,
    ToolCall,
    build_completion_prompt,
    clean_completion_text,
    parse_arguments,
    parse_text_tool_calls,
)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

# FIM（fill-in-the-middle）不可用的判据：这些状态码（或缺失的 /completions 路由）
# 说明上游没有补全能力，而非一次偶发失败 —— 命中即把能力缓存置否，避免每次请求
# 都白试一遍。超时 / 5xx / 连接失败**不**在此列：那是可恢复的，不能污染缓存。
_FIM_UNSUPPORTED_MARKERS = ("HTTP 400", "HTTP 404", "HTTP 405", "HTTP 422", "HTTP 501")

# prompt caching（P2-17）前缀键的字段名。多数 OpenAI 兼容上游会直接忽略未知字段，
# 但也存在**严格模式**的上游（自建网关常见）把未知字段当请求非法直接 400 ——
# 因此注入前要先探明，不能闷头发。
_CACHE_FIELD = "prompt_cache_key"

# 多模态「不支持」的判据（N / P1-10）：上游以 400/422 拒收，**且**报错文本里
# 出现图片相关词。状态码与关键词两个条件都满足才算数 —— 只看 400 会把「消息
# 格式不对」这类真错误误判成「不支持图片」然后静默剥图重发，那才是最难查的 bug
# （与 _cache_rejected 同一纪律）。
_VISION_STATUS = ("HTTP 400", "HTTP 422")
_VISION_MARKERS = ("image", "vision", "multimodal", "image_url")


class OpenAICompatClient:
    """极简的 OpenAI 兼容客户端。"""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: int = 300,
        max_retries: int = 2,
        supports_native_tools: bool = True,
        prompt_cache: str = "off",
        cache_key: str = "",
        vision_mode: str = "off",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.supports_native_tools = supports_native_tools
        # FIM 能力探测缓存（D4）：None = 未探测；True/False = 已探明。
        # 补全每次都试 FIM 会让不支持的上游白吃一次失败 —— 因此第一次探明后就记住。
        self._fim_supported: bool | None = None
        # prompt caching（P2-17）：默认 "off" —— 直接构造本类的场景（测试、脚本、
        # 治理网关）请求体与改造前逐字节一致；只有 Agent 按 Config 显式传值。
        # `_cache_support`：None = 未探明；True/False = 已探明（auto 模式下生效）。
        self._prompt_cache = str(prompt_cache or "off").lower()
        self._cache_key = str(cache_key or "")
        self._cache_support: bool | None = None
        # 多模态能力探测（N / P1-10）：三态同 FIM —— None = 未探明；True/False = 已探明。
        # `_vision_notice`：auto 模式回退后攒一条提示，由 Agent 取出后转成 run.notice
        # （客户端不认识界面，不该自己推事件）。
        self._vision_mode = str(vision_mode or "off").lower()
        self._vision_supported: bool | None = None
        self._vision_notice: str = ""
        self._opener = _build_opener(self.base_url)

    # ------------------------------------------------------------------ 缓存（P2-17）
    @property
    def cache_state(self) -> str:
        """供界面显示的三态：``"on"`` / ``"off"`` / ``"unsupported"``。

        auto 且**尚未探明**时按 "off" 报 —— 界面表达的是「现在有没有在注入」，
        而不是「未来可能注入」。不去主动探测：这是个只读状态查询，不该发请求。
        """
        if self._prompt_cache == "on":
            return "on"
        if self._prompt_cache == "off" or not self._cache_key:
            return "off"
        if self._cache_support is True:
            return "on"
        if self._cache_support is False:
            return "unsupported"
        return "off"

    def _cache_active(self) -> bool:
        key = self._cache_key
        if self._prompt_cache == "off" or not key:
            return False
        if self._prompt_cache == "on":
            return True
        # auto：未探明时先试一次；一旦被拒就永久不再注入（见 _post_with_cache）
        return self._cache_support is not False

    @staticmethod
    def _cache_rejected(exc: LLMError) -> bool:
        """上游是否在说「不认识 prompt_cache_key」。

        状态码与字段名**两个条件都要满足**：只看 400 会把「消息格式不对」这类
        真错误也误判成「不支持 caching」，然后静默降级 —— 那才是最难查的 bug。
        """
        text = str(exc)
        return ("HTTP 400" in text or "HTTP 422" in text) and _CACHE_FIELD in text

    def _post_with_cache(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """带前缀键的 POST；auto 模式下探测失败即永久回退不注入。"""
        if not self._cache_active():
            return self._post(path, payload)
        payload = {**payload, _CACHE_FIELD: self._cache_key}
        try:
            data = self._post(path, payload)
        except LLMError as exc:
            if self._prompt_cache != "auto" or not self._cache_rejected(exc):
                raise
            # 探明：上游严格拒收未知字段 → 永久关闭，且这次请求**重发一次**（不带键）
            self._cache_support = False
            payload.pop(_CACHE_FIELD, None)
            return self._post(path, payload)
        self._cache_support = True
        return data

    # ------------------------------------------------------------------ 多模态（N）
    @staticmethod
    def _has_images(messages: list[dict[str, Any]]) -> bool:
        """这段消息里是否带了图片 part。用于决定要不要走能力探测。"""
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, list) and any(
                isinstance(part, dict) and part.get("type") == "image_url"
                for part in content
            ):
                return True
        return False

    @staticmethod
    def _strip_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把图片 part 换成一行文字说明 —— 回退为纯文本后仍告诉模型「这里曾有图」。

        不回退成「删掉图片、什么都不留」：那样模型会看到一条只有指令、没有素材的
        消息，答非所问且不知道为什么。留一句占位是更诚实的降级。
        """
        stripped: list[dict[str, Any]] = []
        for message in messages or []:
            if not isinstance(message, dict):
                stripped.append(message)
                continue
            content = message.get("content")
            if not isinstance(content, list):
                stripped.append(message)
                continue
            texts: list[str] = []
            images = 0
            for part in content:
                if isinstance(part, str):
                    texts.append(part)
                elif isinstance(part, dict):
                    if part.get("type") == "image_url":
                        images += 1
                    elif isinstance(part.get("text"), str):
                        texts.append(part["text"])
            if images:
                texts.append(f"（本次消息原本附了 {images} 张图片，但当前模型不支持图片输入，已略去）")
            stripped.append({**message, "content": "\n".join(texts)})
        return stripped

    @staticmethod
    def _vision_rejected(exc: LLMError) -> bool:
        """上游是否在说「我不认识图片」。状态码与关键词都要命中（见模块常量）。"""
        text = str(exc).lower()
        return any(s in text for s in ("http 400", "http 422")) and any(
            m in text for m in _VISION_MARKERS
        )

    def _vision_should_probe(self, messages: list[dict[str, Any]]) -> bool:
        """auto 模式、尚未探明「不支持」、且这次确实带了图片时才探测。"""
        return (
            self._vision_mode == "auto"
            and self._vision_supported is not False
            and self._has_images(messages)
        )

    def _mark_vision_unsupported(self) -> None:
        """探明「上游不支持图片」：永久回退，并攒一条提示。"""
        self._vision_supported = False
        self._vision_notice = (
            "当前模型不支持图片输入（VIBE_VISION=auto 已探测到），"
            "本次已自动回退为纯文本重试；如需据图改代码请改用支持多模态的模型。"
        )

    def pop_vision_notice(self) -> str:
        """取走并清空「已回退为纯文本」的一次性提示（供 Agent 转成 run.notice）。"""
        note = self._vision_notice
        self._vision_notice = ""
        return note

    # ------------------------------------------------------------------ 主接口
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

        try:
            data = self._post_with_cache("/chat/completions", payload)
        except LLMError as exc:
            # 能力探测（N / P1-10）：auto 且这次带了图，上游以 400/422 明确拒收图片
            # → 永久回退为纯文本并**重发一次**（剥图后重试；图片换成一行占位说明）。
            if not (self._vision_should_probe(messages) and self._vision_rejected(exc)):
                raise
            self._mark_vision_unsupported()
            payload = {**payload, "messages": self._strip_images(messages)}
            data = self._post_with_cache("/chat/completions", payload)
        else:
            if self._vision_mode != "off" and self._has_images(messages):
                self._vision_supported = True
        return self._to_response(data)

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatResponse:
        """流式版 chat()：SSE 逐帧接收，聚合后返回与 chat() 完全一致的 ChatResponse。

        循环逻辑不需要感知流式 —— 聚合结果走同一条解析路径（含文本协议的
        ```tool 围栏兜底），因此任何一轮在流式与一次性之间切换都是安全的。

        on_delta 只收到「安全」的纯文本增量：
        - 原生 tool_calls 走 delta.tool_calls 分片，从不进入 content，天然安全；
        - 文本协议的工具调用是 content 里的 ```tool 围栏 —— _DeltaGuard 嗅到
          围栏开头就永久静默，绝不让半截 JSON 推给界面（见 A1.2 的边界约定）。
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

        url = self._endpoint("/chat/completions")
        injected = self._cache_active()
        if injected:
            payload[_CACHE_FIELD] = self._cache_key
        try:
            response = self._open_stream(payload, url)
        except LLMError as exc:
            # auto 模式探明「上游严格拒收未知字段」：永久关闭并**重发一次**（不带键）。
            # 与一次性路径同一判据（_cache_rejected），否则流式与非流式会给出
            # 两种不同的降级结果，那比不支持 caching 更难查。
            if injected and self._prompt_cache == "auto" and self._cache_rejected(exc):
                self._cache_support = False
                payload.pop(_CACHE_FIELD, None)
                response = self._open_stream(payload, url)
            # 能力探测（N / P1-10）：与一次性路径同一判据与同一回退动作，
            # 否则流式与一次性会给出两种不同的「是否支持图片」结论。
            elif self._vision_should_probe(messages) and self._vision_rejected(exc):
                self._mark_vision_unsupported()
                payload["messages"] = self._strip_images(messages)
                response = self._open_stream(payload, url)
            else:
                raise
        else:
            if injected:
                self._cache_support = True
            if self._vision_mode != "off" and self._has_images(messages):
                self._vision_supported = True

        with response:
            content_type = str(response.headers.get("Content-Type") or "")
            if "text/event-stream" not in content_type:
                # 上游无视 stream=True 回了普通 JSON —— 静默按一次性处理
                raw = response.read().decode("utf-8", errors="replace")
                try:
                    return self._to_response(json.loads(raw) if raw.strip() else {})
                except json.JSONDecodeError as exc:
                    raise LLMError(f"模型服务返回的不是合法 JSON: {exc}") from exc

            try:
                content, tool_calls_raw, finish_reason, usage = consume_chat_sse(
                    response, on_delta=on_delta
                )
            except socket.timeout as exc:
                raise LLMError(
                    f"流式读取模型响应超时（{self.timeout}s）。\n"
                    "  生成中途断流：可提高 VIBE_REQUEST_TIMEOUT 后重试。"
                ) from exc

        return self._build_response(
            content=content,
            tool_calls_raw=tool_calls_raw,
            finish_reason=finish_reason,
            usage=usage,
        )

    def complete(
        self,
        prefix: str,
        suffix: str = "",
        max_tokens: int | None = None,
        *,
        fim: bool = True,
    ) -> tuple[str, str]:
        """内联补全（D4）：返回 ``(补全文本, 后端标识)``。

        两形态，前者优先：

        1. **FIM**（``/completions`` + ``suffix``）。真正的补全接口，只产「要插入
           的那段」，最省 token、最贴合编辑器语义。
        2. **chat 回退**。上游没有 ``/completions``（Ollama 早期版本、只暴露
           ``/chat/completions`` 的自建网关）时，用一次 ``chat()`` 兜底，提示词
           要求「只输出要插入的代码」。``fim`` 形态不可用会**缓存**下来，后续请求
           直接走 chat，不再白试。

        ``fim=False``（配置 ``VIBE_FIM=0``）时跳过 FIM，直接 chat。两种形态都返回
        纯文本（chat 回退会剥掉模型习惯性加的围栏），调用方不感知差异。
        """
        limit = max_tokens if max_tokens and max_tokens > 0 else 256

        if fim and self._fim_supported is not False:
            try:
                data = self._post(
                    "/completions",
                    {
                        "model": self.model,
                        "prompt": prefix,
                        "suffix": suffix,
                        "max_tokens": limit,
                        "temperature": 0.0,
                        "stream": False,
                    },
                )
            except LLMError as exc:
                if any(marker in str(exc) for marker in _FIM_UNSUPPORTED_MARKERS):
                    # 上游没有补全路由/字段 —— 永久降级，别再试
                    self._fim_supported = False
                # 其余（超时、5xx、连接失败）不缓存，下次仍可试 FIM
            else:
                self._fim_supported = True
                return _extract_completion_text(data), "fim"

        response = self.chat(
            [{"role": "user", "content": build_completion_prompt(prefix, suffix)}],
            temperature=0.0,
        )
        return clean_completion_text(response.content or ""), "chat"

    def list_models(self) -> list[str]:
        """列出可用模型。**会吞掉错误返回空列表**，只用于「有则展示」的场景。

        需要判断服务是否可达时请用 probe()，否则连接失败会被误判成「可达但无模型」，
        让自检给出假阳性结论。
        """
        try:
            data = self._request("GET", "/models", None)
        except LLMError:
            return []
        return _extract_model_names(data)

    def probe(self) -> tuple[bool, str]:
        """探测服务可达性。与 list_models 不同，这里**不吞异常**。"""
        try:
            data = self._request("GET", "/models", None)
        except LLMError as exc:
            return False, str(exc).splitlines()[0]

        names = _extract_model_names(data)
        if names:
            preview = ", ".join(names[:5])
            tail = "" if len(names) <= 5 else f" 等共 {len(names)} 个"
            return True, f"服务可达，可用模型：{preview}{tail}"
        return (
            True,
            "服务可达，但没有可用模型。若使用 Ollama，请先执行 "
            "`ollama pull qwen2.5-coder:7b` 拉取模型。",
        )

    # ------------------------------------------------------------------ HTTP
    def _open_stream(self, payload: dict[str, Any], url: str):
        """发起一次流式请求并返回打开的响应。

        单独抽出来是因为 caching 的 auto 探测需要「同一次调用里重发一次」
        （不带前缀键），而错误翻译必须两条路径完全一致。
        """
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, headers=self._headers(), method="POST"
        )
        request.add_header("Accept", "text/event-stream")
        try:
            return self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            raise LLMError(f"模型服务返回 HTTP {exc.code}\n  地址: {url}\n  返回: {detail}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise LLMError(
                f"无法连接模型服务（流式）: {reason}\n  地址: {url}\n"
                "  若推理端不支持 SSE，可设置 VIBE_STREAM=0 回退一次性请求。"
            ) from exc
        except socket.timeout as exc:
            raise LLMError(
                f"请求模型超时（{self.timeout}s，流式）。\n"
                "  本地小模型首 token 可能很慢，可提高 VIBE_REQUEST_TIMEOUT。"
            ) from exc

    def _endpoint(self, path: str) -> str:
        if not self.base_url.endswith("/v1") and "/v1" not in self.base_url:
            return f"{self.base_url}/v1{path}"
        return f"{self.base_url}{path}"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "vibe-agent-core/0.1",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request(self, method: str, path: str, payload: dict | None) -> dict:
        url = self._endpoint(path)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
        request = urllib.request.Request(
            url, data=body, headers=self._headers(), method=method
        )

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:800]
                if exc.code in _RETRYABLE_STATUS and attempt < self.max_retries:
                    last_error = LLMError(f"HTTP {exc.code}: {detail}")
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise LLMError(
                    f"模型服务返回 HTTP {exc.code}\n"
                    f"  地址: {url}\n"
                    f"  返回: {detail}\n"
                    + (
                        "  提示：401/403 通常是 API Key 不对；"
                        "404 通常是 base_url 少写或多写了 /v1。"
                        if exc.code in (401, 403, 404)
                        else ""
                    )
                ) from exc
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", exc)
                raise LLMError(
                    f"无法连接模型服务: {reason}\n"
                    f"  地址: {url}\n"
                    "  排查建议：\n"
                    "    1) 本地模型：确认 Ollama/vLLM 已启动（ollama serve）\n"
                    "    2) 确认 base_url 与端口正确，Ollama 默认 11434\n"
                    "    3) 公司内网可能设了 HTTP_PROXY，本地地址需绕过代理\n"
                    "    4) 用内置自检命令核对：vibe-agent --check"
                ) from exc
            except socket.timeout as exc:
                raise LLMError(
                    f"请求模型超时（{self.timeout}s）。\n"
                    "  本地小模型在长上下文时首 token 可能很慢，"
                    "可提高 VIBE_REQUEST_TIMEOUT，或先缩小上下文再重试。"
                ) from exc
            except json.JSONDecodeError as exc:
                raise LLMError(f"模型服务返回的不是合法 JSON: {exc}") from exc

        raise last_error or LLMError("模型调用失败")

    def _post(self, path: str, payload: dict) -> dict:
        return self._request("POST", path, payload)

    # ------------------------------------------------------------------ 解析
    def _to_response(self, data: dict) -> ChatResponse:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(
                "模型返回中没有 choices 字段，响应结构不符合 OpenAI 协议。\n"
                f"  原始响应片段: {json.dumps(data, ensure_ascii=False)[:400]}"
            )

        choice = choices[0] or {}
        message = choice.get("message") or {}
        return self._build_response(
            content=message.get("content") or "",
            tool_calls_raw=message.get("tool_calls") or [],
            finish_reason=str(choice.get("finish_reason") or ""),
            usage=data.get("usage") or {},
            raw=data,
        )

    def _build_response(
        self,
        *,
        content: Any,
        tool_calls_raw: Any,
        finish_reason: str,
        usage: dict[str, Any],
        raw: dict[str, Any] | None = None,
    ) -> ChatResponse:
        """把「content + 原始 tool_calls」转成 ChatResponse。

        一次性与流式两条路径都收敛到这里 —— 双通道兜底（文本协议围栏解析）
        只写一份，保证两条路径的解析行为永远一致。
        """
        if isinstance(content, list):  # 某些服务返回分片数组
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )

        tool_calls: list[ToolCall] = []
        for index, item in enumerate(tool_calls_raw or []):
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
                    raw_arguments=raw_args if isinstance(raw_args, str) else json.dumps(raw_args, ensure_ascii=False),
                )
            )

        # 双通道：没有原生 tool_calls 时，尝试从文本里解析 ```tool 代码块
        if not tool_calls and content:
            tool_calls = parse_text_tool_calls(content)

        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage or {},
            raw=raw or {},
        )


def _extract_completion_text(data: dict) -> str:
    """从 ``/completions`` 响应里取补全文本。

    补全接口的正文在 ``choices[0].text``；但个别兼容实现会把它塞进
    ``choices[0].message.content``（复用了 chat 的结构），两种都要认。
    """
    choices = data.get("choices") or []
    if not choices:
        return ""
    choice = choices[0] or {}
    text = choice.get("text")
    if text is None:
        text = (choice.get("message") or {}).get("content")
    if isinstance(text, list):  # 分片数组
        return "".join(p.get("text", "") for p in text if isinstance(p, dict))
    return str(text or "")


def _extract_model_names(data: dict) -> list[str]:
    items = data.get("data") or []
    if isinstance(items, dict):
        items = items.get("models") or []
    names: list[str] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            name = item.get("id") or item.get("name") or item.get("model")
            if name:
                names.append(str(name))
        elif isinstance(item, str):
            names.append(item)
    return names


def _build_opener(base_url: str) -> urllib.request.OpenerDirector:
    """本地地址强制绕过系统代理，其余地址沿用默认行为。"""
    host = (urllib.parse.urlparse(base_url).hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


# parse_text_tool_calls 认可的围栏标签（tool / tool_call / function，大小写不敏感）
# 全部以这两个前缀之一开头 —— 守卫只需要盯住它们。
_TOOL_FENCE_PREFIXES = ("```tool", "```function")


class _DeltaGuard:
    """过滤流式文本增量：嗅到工具围栏的开头就永久静默。

    为什么需要它：文本协议下模型的工具调用是 content 里的 ```tool 围栏。
    若把 content 原样逐段推给界面，用户就会眼睁睁看着半截 JSON 打出来，
    这既难看也违反 A1 的边界约定（「含工具调用的响应不实时上报」）。

    围栏可能跨增量断开（某一段恰好以 "```to" 结尾），因此不能只查单段：
    守卫维护全量缓冲，始终扣留末尾 HOLD_BACK 个字符 —— 这个长度保证了
    任何完整围栏前缀出现的那一刻必然还完整地留在扣留区里，绝无泄漏。
    """

    HOLD_BACK = len("```function") + 1  # 12：比最长围栏前缀多留 1 字符余量

    def __init__(self, on_delta: Callable[[str], None] | None) -> None:
        self._on_delta = on_delta
        self._buffer = ""
        self._emitted = 0
        self._dead = False

    def feed(self, piece: str) -> None:
        if self._dead or not piece or self._on_delta is None:
            return
        self._buffer += piece
        self._drain(final=False)

    def flush(self) -> None:
        """流结束时冲刷扣留的尾部。围栏一旦判定则保持静默。"""
        self._drain(final=True)

    def _drain(self, *, final: bool) -> None:
        text = self._buffer
        lowered = text.lower()
        cut = len(text)
        for prefix in _TOOL_FENCE_PREFIXES:
            pos = lowered.find(prefix)
            if pos != -1:
                cut = min(cut, pos)
                self._dead = True  # 围栏之后不再上报任何内容
        if not final:
            cut = min(cut, max(len(text) - self.HOLD_BACK, 0))
        if cut > self._emitted:
            self._on_delta(text[self._emitted : cut])
            self._emitted = cut


def consume_chat_sse(
    response: Any,
    *,
    on_delta: Callable[[str], None] | None = None,
) -> tuple[str, list[dict[str, str]], str, dict[str, Any]]:
    """消费一条 OpenAI 兼容的 chat SSE 流（直连与经网关共用）。

    之前这段解析只在 OpenAICompatClient 里；M3 给网关客户端加流式时
    提成模块级函数 —— 两边各写一份必然漂移，漂移的表现会是
    「同一个模型直连正常、走网关就丢工具调用」这种最难查的问题。

    返回 (content, tool_calls_raw, finish_reason, usage)。
    """
    guard = _DeltaGuard(on_delta)
    content_parts: list[str] = []
    tool_frags: dict[int, dict[str, str]] = {}
    finish_reason = ""
    usage: dict[str, Any] = {}

    with response:
        for line in response:
            text = line.decode("utf-8", errors="replace").strip()
            if not text or text.startswith(":"):
                continue  # 空行 / SSE 注释（心跳）
            if not text.startswith("data:"):
                continue
            data_str = text[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue  # 容忍个别残缺帧
            if not isinstance(chunk, dict):
                continue

            choices = chunk.get("choices") or []
            if choices:
                choice = choices[0] or {}
                delta = choice.get("delta") or {}
                piece = delta.get("content")
                if isinstance(piece, list):  # 某些服务返回分片数组
                    piece = "".join(
                        p.get("text", "") for p in piece if isinstance(p, dict)
                    )
                if piece:
                    content_parts.append(piece)
                    guard.feed(piece)
                for item in delta.get("tool_calls") or []:
                    if not isinstance(item, dict):
                        continue
                    index = int(item.get("index") or 0)
                    slot = tool_frags.setdefault(
                        index, {"id": "", "name": "", "arguments": ""}
                    )
                    if item.get("id"):
                        slot["id"] = str(item["id"])
                    function = item.get("function") or {}
                    if function.get("name"):
                        slot["name"] = str(function["name"])
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        slot["arguments"] += arguments
                if choice.get("finish_reason"):
                    finish_reason = str(choice["finish_reason"])
            if chunk.get("usage"):
                usage = chunk["usage"]

    guard.flush()
    tool_calls_raw = [
        {
            "id": slot["id"] or f"call_{index}",
            "function": {"name": slot["name"], "arguments": slot["arguments"]},
        }
        for index, slot in sorted(tool_frags.items())
        if slot["name"]
    ]
    return "".join(content_parts), tool_calls_raw, finish_reason, usage


def probe_endpoint(base_url: str, api_key: str, timeout: int = 5) -> tuple[bool, str]:
    """环境自检用：探测服务是否可达。返回 (是否可达, 说明)。

    这里刻意把 max_retries 设为 0 —— 自检应该快速失败，而不是让人等重试。
    """
    client = OpenAICompatClient(
        base_url, model="-", api_key=api_key, timeout=timeout, max_retries=0
    )
    return client.probe()
