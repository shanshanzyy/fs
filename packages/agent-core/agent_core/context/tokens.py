"""token 估算（P2-17）。

## 为什么不能继续按「字符」算预算

`max_context_chars=120000` 是**字符**口径，但模型按 **token** 计费与限窗。
中文一个字约 1 token，英文约 1/4 token —— 同一字符数下 token 量能差 4 倍，
于是「什么时候该裁剪」这件事对中文对话必然失准（要么裁早了丢上下文，
要么裁晚了被上游按超限直接拒掉）。

## 两种估算器，自动择优

1. **tiktoken**（若本机装了）：精确，且与多数 OpenAI 兼容上游同源。
   只**探测**、不打包 —— 词表几十 MB，为省一次估算把它塞进发行物不值当。
2. **脚本加权启发式**（缺 tiktoken 时的兜底）：CJK / 全角按 1 token/字，
   其余按 1/4 token/字。与真实 tokenizer 有偏差，但**保住了量级关系**
   （中文串必然比同长度 ASCII 串「贵」），足以支撑预算判断。

估算器永远返回**正整数或 0**，绝不抛异常：它是预算判断的输入，
不是功能本身 —— 估算失败不该让一次对话直接失败。
"""

from __future__ import annotations

import importlib.util
import json
from typing import Any

#: 单条消息的固定开销（role / 分隔符 / 模板标记）。真实 tokenizer 会把它们
#: 算进去，启发式不补上就会系统性低估 —— 消息条数多时误差会累积。
PER_MESSAGE_TOKENS = 4

#: 一张图片的固定开销。多模态上游普遍按「分辨率分块 + 基础开销」计，
#: 这里取一个保守的常量：目的是让预算条**不要漏掉图片**，而不是精确计费。
IMAGE_TOKENS = 85

#: 需要按「1 字 ≈ 1 token」计的区间：CJK、日韩、全角标点与全角字母数字。
_HEAVY_RANGES = (
    (0x2E80, 0x2FFF),   # 部首扩展、康熙部首
    (0x3000, 0x303F),   # CJK 标点
    (0x3040, 0x33FF),   # 平假名 / 片假名 / 注音 / CJK 兼容
    (0x3400, 0x4DBF),   # 扩展 A
    (0x4E00, 0x9FFF),   # 基本区
    (0xA000, 0xA4CF),   # 彝文
    (0xAC00, 0xD7AF),   # 谚文音节
    (0xF900, 0xFAFF),   # CJK 兼容表意
    (0xFE30, 0xFE4F),   # CJK 兼容形式
    (0xFF00, 0xFFEF),   # 全角 ASCII / 半角片假名
    (0x20000, 0x3FFFF), # 扩展 B 及以后
)


def _is_heavy(ch: str) -> bool:
    code = ord(ch)
    for low, high in _HEAVY_RANGES:
        if low <= code <= high:
            return True
    return False


def heuristic_tokens(text: str) -> int:
    """纯启发式估算：CJK ≈ 1 token/字，其余 ≈ 1/4 token/字（向上取整）。"""
    if not text:
        return 0
    heavy = sum(1 for ch in text if _is_heavy(ch))
    light = len(text) - heavy
    return heavy + (light + 3) // 4


# --------------------------------------------------------------------------- tiktoken
_tiktoken_checked = False
_tiktoken_module: Any = None
_encoders: dict[str, Any] = {}


def _tiktoken() -> Any:
    """探测并缓存 tiktoken 模块。未安装返回 None（**不 import**，只 find_spec）。"""
    global _tiktoken_checked, _tiktoken_module
    if not _tiktoken_checked:
        _tiktoken_checked = True
        if importlib.util.find_spec("tiktoken") is not None:
            try:
                import tiktoken  # noqa: PLC0415 — 可选依赖，只在探测命中后才导入

                _tiktoken_module = tiktoken
            except Exception:  # noqa: BLE001 — 装坏了也不该拖垮估算
                _tiktoken_module = None
    return _tiktoken_module


def _encoder(model: str | None) -> Any:
    """取一个 encoder；模型名不认识时退回 cl100k_base。取不到就返回 None。"""
    tiktoken = _tiktoken()
    if tiktoken is None:
        return None
    key = model or ""
    if key in _encoders:
        return _encoders[key]
    encoder = None
    if key:
        try:
            encoder = tiktoken.encoding_for_model(key)
        except Exception:  # noqa: BLE001 — 模型名不在词表映射里
            encoder = None
    if encoder is None:
        try:
            encoder = tiktoken.get_encoding("cl100k_base")
        except Exception:  # noqa: BLE001 — 词表下载失败（离线）等
            encoder = None
    _encoders[key] = encoder
    return encoder


def estimator_name(model: str | None = None) -> str:
    """当前实际启用的估算器：``"tiktoken"`` 或 ``"heuristic"``。供界面展示。"""
    return "tiktoken" if _encoder(model) is not None else "heuristic"


def estimate_tokens(text: str, *, model: str | None = None) -> int:
    """估一段文本的 token 数。永远返回 >= 0，绝不抛。"""
    if not text:
        return 0
    encoder = _encoder(model)
    if encoder is None:
        return heuristic_tokens(text)
    try:
        return max(0, len(encoder.encode(text)))
    except Exception:  # noqa: BLE001 — 编码失败就退回启发式，不让估算成为故障点
        return heuristic_tokens(text)


def _content_tokens(content: Any, model: str | None) -> int:
    """一条消息 content 的 token 数。content 可能是字符串或**多模态 parts 列表**。"""
    if content is None:
        return 0
    if isinstance(content, str):
        return estimate_tokens(content, model=model)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, str):
                total += estimate_tokens(part, model=model)
            elif isinstance(part, dict):
                ptype = str(part.get("type") or "")
                if ptype in ("image_url", "image", "input_image"):
                    # 图片不按 base64 长度算 —— 那会把预算瞬间拉爆，
                    # 而真实开销取决于分辨率而不是字符串长度。
                    total += IMAGE_TOKENS
                else:
                    text = part.get("text")
                    if isinstance(text, str):
                        total += estimate_tokens(text, model=model)
                    else:
                        total += estimate_tokens(
                            json.dumps(part, ensure_ascii=False), model=model
                        )
        return total
    return estimate_tokens(str(content), model=model)


def estimate_messages(messages: list[dict[str, Any]], *, model: str | None = None) -> int:
    """估一整段 messages 的 token 数（含 tool_calls 参数与每条固定开销）。"""
    total = 0
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        total += PER_MESSAGE_TOKENS
        total += _content_tokens(message.get("content"), model)
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                # 参数是 JSON 字符串（原生协议）或 dict（内部表示），两种都要算上
                arguments = function.get("arguments")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                total += estimate_tokens(name + str(arguments), model=model)
    return total


__all__ = [
    "IMAGE_TOKENS",
    "PER_MESSAGE_TOKENS",
    "estimate_messages",
    "estimate_tokens",
    "estimator_name",
    "heuristic_tokens",
]