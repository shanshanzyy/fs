"""LLM 抽象层：把厂商差异收敛到一套极小的接口。

对应 ADR-003 —— 这里就是「LiteLLM 的位置」，只是 M0 用标准库自己实现了一层最小抽象。
只要上游提供 OpenAI 兼容的 /chat/completions，Ollama / vLLM / DeepSeek / Claude 网关 /
公司自建推理服务都能直接接入，业务代码完全不感知厂商差异。
"""

from .base import (
    ChatResponse,
    LLMClient,
    ToolCall,
    build_completion_prompt,
    build_rewrite_prompt,
    clean_completion_text,
    parse_arguments,
    parse_text_tool_calls,
    window_completion_context,
)
from .openai_compat import OpenAICompatClient, StreamInterrupted, probe_endpoint
from .router import ModelRouter, infer_intent

__all__ = [
    "ChatResponse",
    "LLMClient",
    "ModelRouter",
    "OpenAICompatClient",
    "StreamInterrupted",
    "ToolCall",
    "build_completion_prompt",
    "build_rewrite_prompt",
    "clean_completion_text",
    "infer_intent",
    "parse_arguments",
    "parse_text_tool_calls",
    "probe_endpoint",
    "window_completion_context",
]
