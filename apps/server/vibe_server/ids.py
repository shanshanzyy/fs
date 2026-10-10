"""标识符生成。

前缀 + 时间 + 随机，形如 `usr_1a2b3c4d_9f8e7d6c5b4a`。

为什么不用自增整数：ID 会出现在审计导出、URL、日志里，
自增 ID 会顺带泄漏「这个组织有多少人、发了多少令牌」这类信息，
而且将来做多实例部署时自增还会冲突。前缀让「这串东西是什么」
一眼可辨，排查时省掉一次查表。

时间部分只用于**粗略排序**，不作为唯一性依据 —— 唯一性由随机段保证。
"""

from __future__ import annotations

import secrets
import time

_ALPHABET_SAFE = True  # token_hex 已保证 URL 安全，不需要额外处理


def new_id(prefix: str, *, entropy_bytes: int = 8) -> str:
    """生成带前缀的唯一 ID。

    `entropy_bytes=8` → 64 bit 随机。对「同一毫秒内批量创建」也足够：
    单进程每秒百万级才会出现碰撞概率问题，而这是治理面，不是交易撮合。
    """
    stamp = format(int(time.time() * 1000), "x")
    return f"{prefix}_{stamp}_{secrets.token_hex(entropy_bytes)}"


def new_secret(prefix: str = "", *, bytes_: int = 32) -> str:
    """高熵随机串（用于令牌、pepper 等）。"""
    body = secrets.token_urlsafe(bytes_)
    return f"{prefix}{body}" if prefix else body


def trace_id() -> str:
    """用于把「客户端看到的那次失败」与服务端日志对上（不带任何业务含义）。"""
    return secrets.token_hex(6)


__all__ = ["new_id", "new_secret", "trace_id"]
