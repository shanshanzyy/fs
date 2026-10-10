"""审计上传：把本地 JSONL 审计日志翻译后分批上报。

五个必须做对的点：

1. **上传前先在本地翻译成服务端契约的形状。** 服务端的 `/audit/schema`
   给出「事件类型 → 允许的字段」完整契约，本地按它翻译（见 `vocabulary.py`）。
   不能指望「服务端会拒绝不合规的，所以照原样发就行」——
   客户端是在把事件算进上传链**之后**才发出去的，一旦被拒那条事件
   就永久丢了（本地链已推进，重传不会补上）。服务端的拒绝不是安全网，
   是数据丢失的通道。
2. **没拿到契约就不上传。** 契约缺失时宁可停在这里并明确报出来，
   也不盲发 —— 盲发的失败模式是「看起来在工作，实际每条都在丢」。
3. **水位线（watermark）避免重复上报。** 重复上报不只浪费带宽：
   服务端对「纯重放批次」无法做链头比对（见服务端 audit.py 的说明），
   于是校验能力被白白削弱。
4. **链头取自上次上传的响应，而不是本地推算。** 本地推算看着可行，
   但一旦有一批被服务端部分拒绝，本地与服务端的链就分叉了，
   而分叉之后所有批次的链头都不再匹配 —— 一个早期的小问题会
   放大成「从此以后一直在报篡改」。
5. **链头不匹配时不推进水位线。** 推进了就等于承认「发出去的东西」
   与「记下来的东西」一致，而校验刚刚告诉我们不一致。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import protocol, vocabulary
from .client import GovernanceClient, GovernanceError

DEFAULT_BATCH = 200
MAX_SCAN_LINES = 20_000
# 状态文件版本。1 存的是批次链头（错的），2 存事件链头。
# 见到不认识（更低）的版本一律**丢弃重来**，而不是把它当成可用状态 ——
# 那个起点是错的，接着用等于把已知缺陷继续带着跑。
STATE_VERSION = 2


@dataclass
class UploadState:
    """上报水位线。存本地文件，与服务端幂等约束配合。

    这里存的是**事件链头**，不是批次链头。第一版把服务端响应的
    `chainHead`（实为批次链头）当成事件链起点存在这里，后果是第二批起
    链头必然不符 → 客户端按设计拒绝推进水位线 → 上报永久卡死，
    而运维看到的是「数据可能被篡改」。所以字段名写满了。
    """

    path: Path
    version: int = STATE_VERSION
    uploaded_seq: int = 0
    event_chain_head: str = protocol.GENESIS
    last_attempt_at: int = 0
    last_error: str = ""
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)

    def load(self) -> "UploadState":
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self
        if not isinstance(raw, dict):
            return self
        if int(raw.get("version") or 0) != STATE_VERSION:
            return self
        self.uploaded_seq = int(raw.get("uploadedSeq") or 0)
        self.event_chain_head = str(raw.get("eventChainHead") or protocol.GENESIS)
        self.last_attempt_at = int(raw.get("lastAttemptAt") or 0)
        self.last_error = str(raw.get("lastError") or "")
        sessions = raw.get("sessions")
        self.sessions = sessions if isinstance(sessions, dict) else {}
        return self

    def save(self) -> None:
        payload = {
            "version": STATE_VERSION,
            "uploadedSeq": self.uploaded_seq,
            "eventChainHead": self.event_chain_head,
            "lastAttemptAt": self.last_attempt_at,
            "lastError": self.last_error,
            "sessions": self.sessions,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.path)
        except OSError:
            pass


@dataclass
class UploadResult:
    uploaded: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: list[dict[str, Any]] = field(default_factory=list)
    client_head_match: bool | None = None
    gaps: list[dict[str, int]] = field(default_factory=list)
    skipped_unknown: int = 0
    skipped_local: int = 0
    dropped_fields: list[str] = field(default_factory=list)
    message: str = ""

    def describe(self) -> dict[str, Any]:
        return {
            "uploaded": self.uploaded,
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "rejectedCount": len(self.rejected),
            "clientHeadMatch": self.client_head_match,
            "gaps": self.gaps,
            "skippedUnknown": self.skipped_unknown,
            "skippedLocal": self.skipped_local,
            "droppedFields": self.dropped_fields[:20],
            "message": self.message,
            "rejected": self.rejected[:10],
        }


class AuditUploader:
    def __init__(
        self,
        *,
        client: GovernanceClient,
        audit_path: Path,
        state_path: Path,
        device_id: str,
        batch_size: int = DEFAULT_BATCH,
        known_event_types: set[str] | None = None,
        schema: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.client = client
        self.audit_path = Path(audit_path)
        self.state_path = Path(state_path)
        self.device_id = device_id
        self.batch_size = max(1, min(batch_size, 500))
        self.known_event_types = known_event_types or set()
        # 服务端契约：事件类型 → 允许的字段。为空时**不上传**，见 upload()。
        self.schema: dict[str, dict[str, str]] = schema or {}
        self.state = UploadState(path=self.state_path).load()

    # ------------------------------------------------------------------ 读取
    def read_new_entries(self) -> list[dict[str, Any]]:
        """读本地日志里水位线之后的新条目。

        本地日志是 append-only 的 JSONL，所以「水位线之后」就是
        seq 大于 uploaded_seq 的那些。这里按 seq 判断而不是按行号：
        文件可能被人工截断或轮转过，行号不可靠，而 seq 是记录自己的标识。
        """
        if not self.audit_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        tried = 0
        try:
            with self.audit_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    tried += 1
                    if tried > MAX_SCAN_LINES:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue  # 坏行跳过，不让一行脏数据卡住整批上传
                    if not isinstance(entry, dict):
                        continue
                    try:
                        seq = int(entry.get("seq") or 0)
                    except (TypeError, ValueError):
                        continue
                    if seq > self.state.uploaded_seq:
                        entries.append(entry)
        except OSError:
            return []
        entries.sort(key=lambda item: int(item.get("seq") or 0))
        return entries

    # ------------------------------------------------------------------ 规范化
    def normalize(self, entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
        """把本地记录翻译成上传事件。

        返回 (上传事件, 跳过数, 纯本地数)。

        跳过计数分两类，因为它们指向**完全不同**的问题：
        - `skipped_unknown`：事件名没有登记在词汇表里 → 有人加了
          `audit.record("...")` 却没在 `vocabulary._RULES` 里分类。
          这是代码缺陷，会静默缩小审计覆盖，所以它出现在结果里。
        - `skipped_local`：明确标记为「只留本机」的事件 → 正常，不是问题。
          混在一起会让上面那个缺陷永远看不见。
        """
        now = int(time.time())
        events: list[dict[str, Any]] = []
        skipped = 0
        local_only = 0
        for entry in entries:
            local_event = str(entry.get("event") or entry.get("type") or "").strip()
            translated = vocabulary.translate(
                entry,
                session_id=str(entry.get("session") or ""),
                sequence=int(entry.get("seq") or 0),
                fallback_ts=now,
                schema=self.schema or None,
            )
            if translated is None:
                # 区分「没分类」和「分类为纯本地」
                rule = vocabulary.rule_for(local_event)
                if rule is None:
                    skipped += 1
                else:
                    local_only += 1
                continue
            events.append(translated.event)
        return events, skipped, local_only

    def dropped_report(self, entries: list[dict[str, Any]]) -> dict[str, list[str]]:
        """哪些字段因为 ADR-006 没有上传。给 `vibe-agent governance status` 用。

        存在的意义是让「少传了什么」是**可见的**。不可见的话，
        审计查不到某条信息时，人的第一反应会是「服务端丢数据了」，
        而不是「这类信息本来就不上传」。
        """
        report: dict[str, list[str]] = {}
        for entry in entries:
            local_event = str(entry.get("event") or entry.get("type") or "").strip()
            rule = vocabulary.rule_for(local_event)
            if rule is None or not rule.drop:
                continue
            report.setdefault(local_event, [])
            for field_name, reason in rule.drop.items():
                if field_name in entry and reason not in report[local_event]:
                    report[local_event].append(reason)
        return report

    # ------------------------------------------------------------------ 上传
    def upload(
        self,
        *,
        session_id: str = "",
        max_batches: int = 5,
    ) -> UploadResult:
        """上传若干批。返回汇总结果。

        `session_id` 为空时，按本地日志里的 session 字段分组上传 ——
        服务端的幂等约束是 (device, session, seq)，混着不同 session 上传
        会让 seq 出现"回退"，从而被误判成断号。
        """
        # 契约缺失时**不上传**。这不是保守，是唯一安全的选择：
        # 没有契约就只能照本地形状盲发，而盲发在服务端的表现是
        # 「每条都被键白名单拒收，而本地上传链已经推进」——
        # 数据静默丢失，且不可回溯。宁可停在这里报一个明确的错误。
        if not self.schema:
            return UploadResult(
                message=(
                    "未取得服务端的审计契约（/api/v1/audit/schema），本次不上报。"
                    "盲发会导致事件被服务端拒收后永久丢失，因此这里选择停住。"
                    "请先完成 enroll，或检查 audit:read 权限。"
                )
            )

        entries = self.read_new_entries()
        if not entries:
            return UploadResult(message="没有新的审计记录需要上报")

        groups: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            key = session_id or str(entry.get("session") or "unknown")
            groups.setdefault(key, []).append(entry)

        result = UploadResult(dropped_fields=sorted(self._dropped_field_names(entries)))
        batches = 0
        for key, group in groups.items():
            for start in range(0, len(group), self.batch_size):
                if batches >= max_batches:
                    result.message = "已达到本次上传批次数上限，剩余记录下次继续"
                    return result
                chunk = group[start : start + self.batch_size]
                outcome = self._upload_one(session=key, entries=chunk)
                if outcome is None:
                    return result  # 网络/服务端问题，保留水位线下次再试
                batches += 1
                result.uploaded += outcome.uploaded
                result.accepted += outcome.accepted
                result.duplicates += outcome.duplicates
                result.rejected.extend(outcome.rejected)
                result.skipped_unknown += outcome.skipped_unknown
                result.skipped_local += outcome.skipped_local
                result.gaps.extend(outcome.gaps)
                if outcome.client_head_match is not None:
                    result.client_head_match = outcome.client_head_match
                    if outcome.client_head_match is False:
                        result.message = (
                            "链头校验不通过：上传内容与服务端收到的不一致，"
                            "已停止推进水位线。请检查网络代理或客户端版本。"
                        )
                        return result
        return result

    def _dropped_field_names(self, entries: list[dict[str, Any]]) -> set[str]:
        """这批记录里有哪些字段名因为 ADR-006 被丢掉了（去重后的字段名）。"""
        names: set[str] = set()
        for entry in entries:
            local_event = str(entry.get("event") or entry.get("type") or "").strip()
            rule = vocabulary.rule_for(local_event)
            if rule is None:
                continue
            for field_name in rule.drop:
                if field_name in entry:
                    names.add(f"{local_event}.{field_name}")
        return names

    def _upload_one(self, *, session: str, entries: list[dict[str, Any]]) -> UploadResult | None:
        events, skipped, local_only = self.normalize(entries)
        last_seq = int(entries[-1].get("seq") or 0)
        if not events:
            # 全是本地事件或未知类型：直接推进水位线，否则它们会被永远重读
            self.state.uploaded_seq = max(self.state.uploaded_seq, last_seq)
            self.state.save()
            return UploadResult(skipped_unknown=skipped, skipped_local=local_only)

        start_hash = self._session_head(session)
        envelope = protocol.build_envelope(
            session_id=session,
            device_id=self.device_id,
            events=events,
            start_hash=start_hash,
        )
        self.state.last_attempt_at = int(time.time())

        try:
            response = self.client.upload_audit(envelope)
        except GovernanceError as exc:
            self.state.last_error = str(exc)[:200]
            self.state.save()
            return None

        match = response.get("clientHeadMatch")
        # 取**事件链**的头作为下批起点（服务端字段名 `eventChainHead`）。
        # 服务端响应里还有一个 `batchChainHead`，那是「链的链」的头，
        # 拿它来续事件链会让下批必然对不上 —— 这正是 v1 的缺陷。
        # 这里刻意不写 `or response.get("chainHead")` 这类兜底：
        # 有歧义的兜底正是当初出错的方式。
        server_head = str(response.get("eventChainHead") or "")
        accepted = int(response.get("accepted") or 0)

        if match is False and accepted > 0:
            # 校验失败：不推进水位线，也不采纳服务端链头
            self.state.last_error = "chain_head_mismatch"
            self.state.save()
            return UploadResult(
                uploaded=len(events),
                accepted=accepted,
                rejected=_rejected_list(response),
                client_head_match=False,
                skipped_unknown=skipped,
                skipped_local=local_only,
            )

        if accepted > 0 and not server_head:
            # 服务端收下了事件，却没给出事件链头 —— 下批就无从接续。
            # 此时推进水位线 = 承认「下批能从某个起点接上」，而我们并不知道
            # 那个起点是什么，于是从下批开始每批都报链头不符。
            # 停在这里，把原因说清楚，比制造一串假告警好。
            self.state.last_error = "missing_event_chain_head"
            self.state.save()
            return UploadResult(
                uploaded=len(events),
                accepted=accepted,
                rejected=_rejected_list(response),
                skipped_unknown=skipped,
                skipped_local=local_only,
                message=(
                    "服务端未返回事件链头（eventChainHead），已停止推进水位线。"
                    "多半是客户端与服务端版本不匹配：请确认服务端版本不低于本客户端。"
                ),
            )

        self.state.uploaded_seq = max(self.state.uploaded_seq, last_seq)
        if server_head:
            self.state.event_chain_head = server_head
            self.state.sessions[session] = {"eventChainHead": server_head, "lastSeq": last_seq}
        self.state.last_error = ""
        self.state.save()

        return UploadResult(
            uploaded=len(events),
            accepted=accepted,
            duplicates=int(response.get("duplicates") or 0),
            rejected=_rejected_list(response),
            client_head_match=match if isinstance(match, bool) else None,
            gaps=response.get("gaps") if isinstance(response.get("gaps"), list) else [],
            skipped_unknown=skipped,
            skipped_local=local_only,
        )

    def _session_head(self, session: str) -> str:
        """本会话的续链起点：上次服务端确认过的事件链头。"""
        record = self.state.sessions.get(session)
        if isinstance(record, dict) and record.get("eventChainHead"):
            return str(record["eventChainHead"])
        return protocol.GENESIS

    # ------------------------------------------------------------------ 自检
    def local_chain_status(self) -> dict[str, Any]:
        """不联网的本地状态摘要，供 `status` 命令展示。"""
        entries = self.read_new_entries()
        return {
            "auditFile": str(self.audit_path),
            "exists": self.audit_path.exists(),
            "uploadedSeq": self.state.uploaded_seq,
            "eventChainHead": self.state.event_chain_head,
            "pending": len(entries),
            "lastAttemptAt": self.state.last_attempt_at,
            "lastError": self.state.last_error,
            "knownEventTypes": len(self.known_event_types),
            "contractLoaded": bool(self.schema),
        }


def _rejected_list(response: dict[str, Any]) -> list[dict[str, Any]]:
    rejected = response.get("rejected")
    return rejected if isinstance(rejected, list) else []


__all__ = ["AuditUploader", "UploadResult", "UploadState"]
