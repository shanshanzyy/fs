"""多模态图片输入（N / P1-10）：把工作区里的图片读成 OpenAI 兼容的 parts。

## 为什么单独一个模块

`read_file` 明确拒二进制（见 `tools/fs.py`），图片不能塞进现有读文件工具；
而 `chat()` 的 `content` 本就原样透传给上游，把一条 user 消息的 `content`
扩成 parts 列表**不需要改客户端协议**。图片通道因此独立成这一层。

## 三条纪律

1. **纯函数、可单测** —— 不碰全局状态；输入「路径 + PathGuard」，输出 part dict。
2. **路径一律走 PathGuard** —— 与其它引用同一处校验；越权 / 敏感文件在
   `check_read` 被拦下（抛 `SandboxViolation`），由调用方降级为一行提示。
3. **只发往用户配置的端点** —— 图片以 base64 data URL 随请求离开本机；
   `vision_mode` 默认 off 正是因为这个（见 `config.vision_mode`）。

## 为什么按**魔数**而不是后缀判类型

后缀是用户能随手改的，且上传通道与本地文件都可能被改后缀。魔数（文件头）
才是真实类型：既用它决定 data URL 的 MIME，也用它把「改了后缀的假图片」
挡在外面。两者必须一致 —— 只看后缀会让 `x.png` 里塞一段非图片字节蒙混过关。
"""

from __future__ import annotations

import base64
from typing import Any

from ..errors import ToolError
from ..sandbox import PathGuard

#: 允许的图片后缀（小写）。与下面的魔数白名单**互为交叉校验**：
#: 后缀在集合内、且魔数能识别出类型，才放行。
ALLOWED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})

#: 默认单张图片字节上限（与 config.vision_max_image_bytes 的默认值一致）。
DEFAULT_MAX_IMAGE_BYTES = 4_000_000


def _sniff_mime(data: bytes) -> str | None:
    """按文件头识别图片类型，返回 MIME；识别不出返回 None。

    只认白名单内的五种（png/jpeg/webp/gif）—— 与其它的 svg/bmp 等都排除，
    因为多模态上游普遍只吃这几种（svg 更是文本，可能内嵌脚本）。
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


#: MIME → 落盘用的扩展名。落盘一律用**魔数识别出的**类型，不信任上传时给的名字。
_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def identify_image(data: bytes) -> tuple[str, str] | None:
    """按文件头识别图片，返回 ``(扩展名, MIME)``；识别不出返回 None。"""
    mime = _sniff_mime(data)
    if mime is None:
        return None
    return _MIME_EXT[mime], mime


def load_image(
    paths: PathGuard, path: str, *, max_bytes: int = DEFAULT_MAX_IMAGE_BYTES
) -> dict[str, Any]:
    """把一张工作区图片读成 OpenAI 兼容的 image part。

    流程：`check_read` → 后缀白名单 → 读字节 → 大小上限 → 魔数识别 →
    base64 data URL。任一步不通过都抛 `ToolError`（或 `SandboxViolation`），
    由 `context.references` 逐条降级为提示，绝不弄死整条任务。
    """
    checked = paths.check_read(path)
    rel = paths.relative(checked)
    if checked.is_dir():
        raise ToolError(f"{rel} 是目录，不能作为图片引用")
    if not checked.exists():
        raise ToolError(f"图片不存在：{rel}")

    suffix = checked.suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ToolError(
            f"{rel} 不是受支持的图片类型（仅 {'/'.join(sorted(ALLOWED_SUFFIXES))}）"
        )

    try:
        data = checked.read_bytes()
    except OSError as exc:
        raise ToolError(f"读取图片失败：{exc}") from exc

    limit = max(1, int(max_bytes))
    if len(data) > limit:
        raise ToolError(
            f"{rel} 过大（{len(data)} 字节，上限 {limit}）。"
            "请压缩后重试，或调大 VIBE_VISION_MAX_BYTES。"
        )

    mime = _sniff_mime(data)
    if mime is None:
        raise ToolError(f"{rel} 的文件头不是受支持的图片格式（后缀与实际内容不符）")

    encoded = base64.b64encode(data).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{encoded}"},
    }


def to_message_parts(
    text: str, images: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """把「正文 + 若干图片 part」组装成一条 user 消息的 `content` parts 列表。

    顺序固定为「先文字、后图片」：模型据此把文字当指令、把图片当同一轮的素材。
    调用方保证 `images` 非空 —— 无图片时应当继续用纯字符串 content，
    让「不带图片时请求体逐字节不变」这条不变式成立（见 host/session.submit）。
    """
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    parts.extend(images or [])
    return parts


def message_text(content: Any) -> str:
    """从一条消息的 `content`（字符串或 parts 列表）里取出**纯文本**部分。

    审计与检查点只要「长度与指纹」这一份文本：图片的 base64 既不该进指纹
    （否则同一张图重传会算出不同指纹），也不该被 `len()` 当成正文长度。
    非字符串、非列表的畸形 content 一律退化成空串，不抛。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
        return "\n".join(chunks)
    return ""


__all__ = [
    "ALLOWED_SUFFIXES",
    "DEFAULT_MAX_IMAGE_BYTES",
    "identify_image",
    "load_image",
    "message_text",
    "to_message_parts",
]