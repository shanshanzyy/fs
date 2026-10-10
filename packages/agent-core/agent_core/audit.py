"""审计日志：只可追加（append-only）+ 哈希链防篡改。

对应 docs/ARCHITECTURE.md 的 ADR-006：
    审计只记录「谁 / 何时 / 对哪个文件 / 调了什么模型 / 用了哪些工具」等元数据，
    **绝不记录代码内容、Prompt 原文、模型输出原文**。
    因此这里对正文一律做摘要（sha256 截断 + 长度），原文不出本机。

哈希链设计：每条记录携带 prev 字段（上一条记录的指纹），
    任何对历史记录的修改都会让后续所有 prev 校验失败，实现可检测的防篡改。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def digest(text: str) -> str:
    """对文本取指纹，用于审计时替代原文。"""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def summarize(text: str, limit: int = 160) -> str:
    """截断为一行摘要，避免审计日志里意外混入长正文。"""
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


@dataclass
class AuditLog:
    """append-only JSONL 审计日志。"""

    path: Path
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    enabled: bool = True
    _seq: int = 0
    _last_hash: str = GENESIS
    # 写入必须串行：链的正确性建立在「读到的 _last_hash 就是上一条的 hash」之上，
    # 而写审计的线程不止一个（Agent 工作线程、host 请求线程、索引 watch 线程）。
    # 两个线程同时从同一个 prev 出发，链上就会出现两条同 prev 的记录 ——
    # verify_chain 会判定整条链被篡改。
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 续接已有链：读取最后一条记录恢复 seq 与 hash
        if self.path.exists():
            last = self._read_last()
            if last:
                self._seq = int(last.get("seq", 0))
                self._last_hash = str(last.get("hash", GENESIS))

    def record(self, event: str, **payload: Any) -> None:
        """写入一条审计事件。失败不抛出——审计不能阻断主流程。"""
        if not self.enabled:
            return
        with self._lock:
            self._seq += 1
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                "seq": self._seq,
                "session": self.session_id,
                "event": event,
                "pid": os.getpid(),
                **payload,
            }
            body = json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
            entry["prev"] = self._last_hash
            entry["hash"] = hashlib.sha256(
                (self._last_hash + body).encode("utf-8")
            ).hexdigest()
            self._last_hash = entry["hash"]
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            except OSError:
                pass

    def _read_last(self) -> dict[str, Any] | None:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                last_line = ""
                for line in fh:
                    if line.strip():
                        last_line = line
            return json.loads(last_line) if last_line else None
        except (OSError, json.JSONDecodeError):
            return None


def record_session_start(audit: "AuditLog", config: Any) -> None:
    """记录会话开始。

    CLI 与 host 两条入口共用这一个函数，而不是各自 `record("session.start", ...)`：
    字段名各写一遍的话，改了一处忘了另一处，服务端的键白名单就会开始拒收，
    而拒收的后果是那条事件**永久丢失**（客户端把它算进上传链之后才发出去）。
    """
    audit.record(
        "session.start",
        workspace=str(config.workspace),
        model=str(config.model),
        provider=str(getattr(config, "provider", "") or ""),
    )


def record_session_end(
    audit: "AuditLog",
    *,
    reason: str,
    iterations: int = 0,
    duration_ms: int = 0,
) -> None:
    """记录会话结束（见 record_session_start 的说明）。"""
    audit.record(
        "session.end",
        reason=str(reason)[:64],
        iterations=int(iterations),
        duration_ms=int(duration_ms),
    )


def verify_chain(path: Path) -> tuple[bool, str]:
    """校验哈希链完整性。返回 (是否通过, 说明)。"""
    if not path.exists():
        return True, "审计文件不存在，视为空链"
    prev = GENESIS
    count = 0
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                return False, f"第 {lineno} 行不是合法 JSON，链已损坏"
            stored_prev = entry.pop("prev", None)
            stored_hash = entry.pop("hash", None)
            if stored_prev != prev:
                return False, f"第 {lineno} 行 prev 指纹不匹配，记录被篡改或删除"
            body = json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
            expected = hashlib.sha256((prev + body).encode("utf-8")).hexdigest()
            if stored_hash != expected:
                return False, f"第 {lineno} 行内容指纹不匹配，记录被修改"
            prev = stored_hash
            count += 1
    return True, f"链完整，共 {count} 条记录"
