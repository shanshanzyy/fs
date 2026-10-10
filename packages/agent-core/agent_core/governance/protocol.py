"""客户端与治理面之间的协议常量与链公式。

**这里只有一件真正重要的事：链公式必须与服务端逐字节一致。**

服务端用 `apps/server/vibe_server/hashing.py` 的 `canonical()` + `chain_hash()`
算出一份「副本链」。客户端上传时必须用**同一个公式、同一串字段、同样的
序列化参数**算出链头并声明在请求里；服务端拿自己的结果去比对，
从而证明「上传没有被截断或重排」。

只要有一处不同 —— `sort_keys`、`separators`、`ensure_ascii`、
甚至字段名 —— 链头就对不上。而对不上的表现是「校验失败」，
排查方向会被彻底带偏（看起来像有人改了数据，实际只是两端序列化不一致）。
所以这四行代码在两端各存一份是刻意的，且必须有交叉测试锁住。

另一个刻意的选择：**上传的载荷形状与本地审计日志的形状不同**。
本地日志是给人看的（带 pid、可读时间戳），上传载荷是给机器算链的
（epoch 整数时间戳、扁平字段）。多一层规范化看着啰嗦，
但它让「本地日志格式演进」不会连带破坏链的兼容性。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

GENESIS = "0" * 64

# 上传事件的保留字段（不参与 payload，而是提到外层）
RESERVED_KEYS = frozenset({"ts", "seq", "session", "event", "type", "payload", "pid", "prev", "hash"})

# 单条事件里 payload 的字段数上限：与服务端 redaction 的检查口径一致
MAX_PAYLOAD_FIELDS = 24


# --------------------------------------------------------------------------- 链
def canonical(payload: dict[str, Any]) -> str:
    """规范化序列化。参数必须与服务端完全一致。"""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def chain_hash(prev_hash: str, body: str) -> str:
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


def link(prev_hash: str, payload: dict[str, Any]) -> tuple[str, str]:
    body = canonical(payload)
    return body, chain_hash(prev_hash, body)


def chain_payload(
    *,
    device_id: str,
    session_id: str,
    seq: int,
    event_type: str,
    ts: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """链哈希的输入。

    六个字段、一个不多一个不少 —— 服务端 `AuditService._chain_payload`
    是镜像实现。加字段会让历史链全部失效，因此要加字段必须**同时**
    改两端并接受「旧链不可续」的代价（或者引入链的版本号，M3 再说）。
    """
    return {
        "device_id": device_id,
        "session_id": session_id,
        "seq": seq,
        "event_type": event_type,
        "ts": ts,
        "payload": payload,
    }


# --------------------------------------------------------------------------- 规范化
def _to_epoch(value: Any, fallback: int) -> int:
    """把本地日志里的时间戳转成 epoch 秒。

    本地日志写的是 `"%Y-%m-%dT%H:%M:%S"` 可读格式，上传要的是整数 ——
    这是两套形状最明显的差异，也是规范化层存在的直接原因。
    """
    import time

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str) and value:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                return int(time.mktime(time.strptime(value, fmt)))
            except ValueError:
                continue
    return fallback


def _clean_value(value: Any) -> Any:
    """把 payload 值收成服务端能接受的形状（标量）。

    嵌套结构一律**丢弃**而不是序列化后塞进去：服务端的存在性检查
    （不含换行、长度上限、不像代码）是最后一道闸门，
    但客户端不该把「指望服务端兜底」当成默认策略 ——
    到了服务端被拒，这条事件就永久丢了（因为它在本地已经算进链里了）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        # 截断到服务端单字段上限，并去掉换行（服务端会拒绝含换行的值）
        one_line = value.replace("\r", " ").replace("\n", " ")
        return one_line if len(one_line) <= 256 else one_line[:255] + "…"
    return None


def normalize_event(
    raw: dict[str, Any],
    *,
    session_id: str,
    sequence: int,
    fallback_ts: int,
) -> dict[str, Any] | None:
    """把一条本地审计条目转成上传事件。返回 None 表示这条不适合上传。

    本地日志里有些事件是**纯本地**的（比如 `/audit` 自检、会话重置），
    服务端没有对应的事件类型，上传会被拒。与其让它们污染拒绝统计，
    不如在这里基于「事件名是否在已知集合内」直接跳过。
    调用方负责传入已知集合，避免客户端硬编码一份会过期的名单。
    """
    event_type = str(raw.get("event") or raw.get("type") or "").strip()
    if not event_type:
        return None

    # ts 优先用外层 epoch 字段，其次解析可读时间戳
    ts = _to_epoch(raw.get("ts_epoch") or raw.get("ts"), fallback_ts)

    payload: dict[str, Any] = {}
    for key, value in raw.items():
        if key in RESERVED_KEYS:
            continue
        if len(payload) >= MAX_PAYLOAD_FIELDS:
            break
        cleaned = _clean_value(value)
        if cleaned is not None:
            payload[key] = cleaned

    return {
        "type": event_type,
        "seq": int(sequence),
        "ts": int(ts),
        "payload": payload,
    }


def compute_batch(
    events: list[dict[str, Any]],
    *,
    device_id: str,
    session_id: str,
    start_hash: str = GENESIS,
) -> tuple[list[dict[str, Any]], str]:
    """给一批事件算链，返回 (事件列表, 批次结束时的链头)。

    `start_hash` 必须是「上一批结束时服务端持有的链头」。第一次上传用创世值。
    传错的话客户端算出的链头会与服务端的不一致 ——
    所以这个值应该从上次上传的响应里带回来，而不是本地自行推算。
    """
    prev = start_hash
    for event in events:
        _, prev = link(
            prev,
            chain_payload(
                device_id=device_id,
                session_id=session_id,
                seq=int(event["seq"]),
                event_type=str(event["type"]),
                ts=int(event["ts"]),
                payload=event.get("payload") or {},
            ),
        )
    return events, prev


def build_envelope(
    *,
    session_id: str,
    device_id: str,
    events: list[dict[str, Any]],
    start_hash: str = GENESIS,
) -> dict[str, Any]:
    """构造上传请求体。

    字段名是 `eventChainHead`（事件链头），不是含糊的 `chainHead`。
    服务端的响应里同时有 `eventChainHead` 与 `batchChainHead` ——
    两条链、两个名字。曾经它们共用一个名字，直接导致客户端拿批次链头
    去续事件链，从第二批起上报永久卡死（见服务端 `audit.BatchResult`）。
    """
    _, head = compute_batch(
        events, device_id=device_id, session_id=session_id, start_hash=start_hash
    )
    return {
        "sessionId": session_id,
        "deviceId": device_id,
        "eventChainHead": head,
        "events": events,
    }


__all__ = [
    "GENESIS",
    "MAX_PAYLOAD_FIELDS",
    "RESERVED_KEYS",
    "build_envelope",
    "canonical",
    "chain_hash",
    "chain_payload",
    "compute_batch",
    "link",
    "normalize_event",
]
