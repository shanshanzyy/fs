"""哈希与指纹工具。

单独抽出来是因为审计链的公式必须**客户端与服务端逐字节一致** ——
两边各写一份是最容易出的错：一旦某一边的序列化方式变了（比如
`sort_keys` 或 `ensure_ascii` 不同），链校验就会全线失败，
而且报出来的是「记录被篡改」，排查方向完全被带偏。

所以公式只在这里定义一次，两边都从这里引。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

GENESIS = "0" * 64


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def fingerprint(text: str, length: int = 16) -> str:
    """短指纹，用于替代正文入库（ADR-006）。"""
    return sha256_hex(text)[:length]


def canonical(payload: dict[str, Any]) -> str:
    """规范化序列化。

    `sort_keys` 保证字段顺序无关，`separators` 去掉空白差异，
    `ensure_ascii=False` 让中文按原样参与计算 —— 这三条任意一条不一致，
    两端的链就对不上。
    """
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def chain_hash(prev_hash: str, body: str) -> str:
    """链式指纹：hash(prev + body)。

    为什么用「上一条的哈希 + 本条内容」而不是简单的 Merkle 树：
    这样任何一条被改动或删除，从它之后的所有记录都会失配，
    **篡改的位置能被精确定位到第一条不符的行**，而不是只知道「整体不可信」。
    """
    return sha256_hex(prev_hash + body)


def link(prev_hash: str, payload: dict[str, Any]) -> tuple[str, str]:
    """返回 (规范化正文, 本条链哈希)。"""
    body = canonical(payload)
    return body, chain_hash(prev_hash, body)


__all__ = ["GENESIS", "canonical", "chain_hash", "fingerprint", "link", "sha256_hex"]
