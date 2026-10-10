"""LLM 抽象层的数据结构与工具解析。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

_FENCE = re.compile(r"```[a-zA-Z_]*\s*\n(.*?)```", re.DOTALL)


@dataclass
class ToolCall:
    """一次工具调用请求。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""


@dataclass
class ChatResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLMClient(Protocol):
    """所有模型客户端都要满足的最小接口。"""

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> ChatResponse: ...


def _strip_fences(text: str) -> str:
    match = _FENCE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


# ---- 内联补全（D4）：FIM 不可用时的提示词与文本清洗 ------------------------
_COMPLETION_INSTRUCTIONS = (
    "你是代码补全引擎。根据上下文，只输出应当插入到 <CURSOR> 处的代码。\n"
    "硬性要求：\n"
    "1. 只输出要插入的那段代码本身，不要解释、不要 Markdown 围栏、不要重复已有代码；\n"
    "2. 保持与上下文一致的缩进与代码风格；\n"
    "3. 若没有合适的补全，输出空字符串。"
)


def build_completion_prompt(prefix: str, suffix: str) -> str:
    """构造回退 chat 补全的提示。FIM 形态不可用时用它兜底。

    把光标前后的代码都给出（而不是只给 prefix），是因为补全经常需要「向后看」
    才能决定该写什么（比如闭合括号、补全参数列表）。
    """
    return (
        f"{_COMPLETION_INSTRUCTIONS}\n\n"
        "<PREFIX>\n" + prefix + "\n</PREFIX>\n"
        "<CURSOR></CURSOR>\n"
        "<SUFFIX>\n" + suffix + "\n</SUFFIX>"
    )


def clean_completion_text(text: str) -> str:
    """去掉模型习惯性加的代码围栏，保留缩进。"""
    return _strip_fences(text)


def window_completion_context(
    prefix: str,
    suffix: str,
    *,
    prefix_chars: int,
    suffix_chars: int,
) -> tuple[str, str, bool]:
    """把补全上下文截到光标附近（**行对齐**），返回 ``(prefix, suffix, truncated)``。

    大文件整份上送会让 token 不可控、首字变慢，因此只保留光标前 ``prefix_chars``
    与光标后 ``suffix_chars`` 个字符；两者为 ``0`` 时各自不截断。截断按行对齐：
    前缀丢掉被切断的**首行残段**（保留紧邻光标的整行）、后缀丢掉**尾部残行**
    （保留从光标起的完整行），避免把半行代码喂给模型。

    刻意不做 token 估算 —— 那需要第三方分词器，与「核心链路零依赖」红线冲突；
    按字符数切是 O(1) 且足够稳。
    """
    truncated = False
    if prefix_chars > 0 and len(prefix) > prefix_chars:
        start = len(prefix) - prefix_chars
        newline = prefix.find("\n", start)
        if newline != -1:
            start = newline + 1  # 从下一整行开始，丢弃首行残段
        prefix = prefix[start:]
        truncated = True
    if suffix_chars > 0 and len(suffix) > suffix_chars:
        end = suffix_chars
        newline = suffix.rfind("\n", 0, end)
        if newline != -1:
            end = newline + 1  # 只保留完整行，丢弃尾部残行
        suffix = suffix[:end]
        truncated = True
    return prefix, suffix, truncated


# ---- 行内改写（D8）：把选区按指令改写，只产替换文本 ------------------------
_REWRITE_INSTRUCTIONS = (
    "你是代码改写引擎。按 <INSTRUCTION> 改写 <SELECTION> 中的代码。\n"
    "硬性要求：\n"
    "1. 只输出改写后的代码本身，不要解释、不要 Markdown 围栏；\n"
    "2. 绝不要改动 <SELECTION> 之外的任何代码；\n"
    "3. 保持选区原有的缩进风格与相对缩进。"
)


def build_rewrite_prompt(
    instruction: str,
    selection: str,
    *,
    before: str = "",
    after: str = "",
) -> str:
    """构造行内改写的提示。

    选区前后的少量上下文（``before`` / ``after``）是**只读参考**，让模型在
    需要闭合括号、推断变量类型时有依据；它们不会被模型改动，模型只负责选区。
    """
    parts = [
        _REWRITE_INSTRUCTIONS,
        f"\n<INSTRUCTION>\n{instruction}\n</INSTRUCTION>",
    ]
    if before:
        parts.append(f"\n<BEFORE>\n{before}\n</BEFORE>")
    parts.append(f"\n<SELECTION>\n{selection}\n</SELECTION>")
    if after:
        parts.append(f"\n<AFTER>\n{after}\n</AFTER>")
    return "".join(parts)


def _extract_json_object(text: str) -> str | None:
    """从一段文本里抠出最外层平衡的 JSON 对象。

    小模型经常在 JSON 前后絮叨（"好的，我来读取文件：{...} 以上就是我的调用"），
    所以必须容忍脏文本，而不是直接 json.loads 失败。
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def parse_arguments(raw: Any) -> dict[str, Any]:
    """把模型给的 arguments 解析成 dict，尽力容忍各种畸形输出。

    实际会遇到的形态：标准 JSON、JSON 字符串、被 ```json 包裹的 JSON、
    JSON 外面还有解释文字、单引号、尾随逗号、Python 字面量风格。
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}

    text = raw.strip()
    if not text:
        return {}

    candidates = [text, _strip_fences(text)]
    stripped = _extract_json_object(text)
    if stripped:
        candidates.append(stripped)

    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed

    # 最后一招：容忍单引号与尾随逗号（小模型高频错误）
    for candidate in candidates:
        if not candidate:
            continue
        repaired = candidate.replace("'", '"')
        repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
        try:
            parsed = json.loads(repaired)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed

    return {}


def parse_text_tool_calls(text: str) -> list[ToolCall]:
    """从纯文本回复里解析工具调用（原生 function calling 不可用时的兜底）。

    约定格式（写在系统提示里）：
        ```tool
        {"name": "read_file", "arguments": {"path": "app.py"}}
        ```
    只认 tool / tool_call / function 这几种围栏标签，避免把回答里正常的
    ```json 示例代码块误当成调用。
    """
    if not text:
        return []

    calls: list[ToolCall] = []
    for match in re.finditer(
        r"```(tool|tool_call|function)\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE
    ):
        payload = _extract_json_object(match.group(2)) or match.group(2)
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue

        name = data.get("name") or data.get("tool") or data.get("function")
        if isinstance(name, dict):  # {"function": {"name": ..., "arguments": ...}}
            args_raw = name.get("arguments", {})
            name = name.get("name")
        else:
            args_raw = data.get("arguments", data.get("parameters", {}))
        if not isinstance(name, str) or not name.strip():
            continue

        calls.append(
            ToolCall(
                id=f"textcall_{len(calls)}",
                name=name.strip(),
                arguments=parse_arguments(args_raw),
                raw_arguments=json.dumps(args_raw, ensure_ascii=False)
                if not isinstance(args_raw, str)
                else args_raw,
            )
        )
    return calls
