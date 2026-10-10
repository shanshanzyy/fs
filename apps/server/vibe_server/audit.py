"""审计接收服务。

**链的链**是这里的核心设计。客户端的哈希链只能证明「我本地的记录
一条没被改」；它证明不了「你收到的和发出的是同一批」——
上传过程可能被截断（丢掉最后几条）或重放（重复提交）。
所以客户端每批上传时，额外算一个「批次链头」：

```
客户端本地链：  e1 → e2 → e3 → ... → eN        （逐条链式）
批次链：        b1 → b2 → b3 → ...             （每个批次的链头再串一条链）
```

服务端保存两条链并都能校验，于是「哪一条被改过、哪个批次缺了」
都能被精确定位。这是审计系统里唯一值得多花的那点复杂度。

另外三个必须做对的地方：

1. **幂等靠唯一约束，不靠应用层去重。** `UNIQUE(device_id, session_id, seq)`
   让重传天然安全；应用层做「查一下有没有、没有就插」在并发下会重复。
2. **断号检测。** 客户端可能因为崩溃、断网丢事件。服务端按
   `seq` 连续性检查并**如实记录缺口**，而不是「补齐占位」——
   占位会让「丢了 3 条」看起来像「有 3 条空记录」。
3. **顺序是「先校验、后入库」。** 混着做的话，一批里前面几条入了库
   后面几条被拒，客户端的链头就对不上了，而它会以为整批成功。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import rbac, redaction
from .errors import BadRequest, Forbidden
from .hashing import GENESIS, link
from .ids import new_id


@dataclass
class BatchResult:
    """一批审计的接收结果。

    **这里有两个不同的链头，绝不能用同一个名字。** 这曾经是一个真实的
    生产缺陷：`chainHead` 一个字段承载了「批次链头」，而客户端把它当成
    「事件链头」用，于是第二批起链头必然对不上 → 客户端按设计拒绝推进
    水位线 → 审计上报从第二批开始**永久卡死**，而运维看到的是
    「数据可能被篡改」这个假告警。

    两个字段各自的用途：

    - `event_chain_head`：本批处理完后**事件链**的链头。客户端拿它作为
      下一批 `start_hash`。这才是客户端需要的东西。
    - `batch_chain_head`：本批在**批次链（链的链）**上的链头。用来证明
      「批次之间没被截断、没被重排」，客户端不需要也不该用它续链。

    刻意**不提供**一个含糊的 `chainHead`：一个字段名同时能指两条链，
    就是那个缺陷的成因。有歧义的兼容别名比破坏性变更更危险。
    """

    accepted: int = 0
    duplicates: int = 0
    rejected: list[dict[str, Any]] = field(default_factory=list)
    batch_id: str = ""
    # 事件链头：客户端下一批的起点
    event_chain_head: str = ""
    # 批次链头：只在服务端内部的批次链上使用
    batch_chain_head: str = ""
    gaps: list[dict[str, int]] = field(default_factory=list)
    first_seq: int = 0
    last_seq: int = 0
    # None = 客户端没声明链头（老客户端或刻意省略），True/False = 比对结果
    client_head_match: bool | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "rejectedCount": len(self.rejected),
            "batchId": self.batch_id,
            # 客户端用这个
            "eventChainHead": self.event_chain_head,
            # 排障用（`audit verify` 会顺着这条链查批次连续性）
            "batchChainHead": self.batch_chain_head,
            "firstSeq": self.first_seq,
            "lastSeq": self.last_seq,
            "gaps": self.gaps,
            "clientHeadMatch": self.client_head_match,
            # 只回前若干条拒绝原因，避免一个坏客户端把响应撑爆
            "rejected": self.rejected[:20],
        }


class AuditService:
    def __init__(self, *, store, config, clock) -> None:
        self.store = store
        self.config = config
        self.clock = clock

    # ============================================================== 接收
    def ingest(self, *, actor, body: dict[str, Any]) -> BatchResult:
        """接收一批审计事件。

        事件来自**客户端进程**，因此整批都要当作不可信输入处理 ——
        包括「客户端上报的 user_id / team_id」。**不信客户端自称的身份**：
        team/user 一律取自 `actor`（已认证），客户端传的同名字段被忽略。
        这是很容易漏的一处：审计里写了别人的 id，等于往别人的账上记东西。
        """
        session_id = str(body.get("sessionId") or "").strip()
        if not session_id or len(session_id) > 64:
            raise BadRequest("缺少合法的 sessionId")
        device_id = str(body.get("deviceId") or actor.device_id or "").strip()
        if not device_id:
            raise BadRequest("缺少 deviceId")
        if actor.device_id and device_id != actor.device_id:
            # 用 A 设备的令牌给 B 设备上报 —— 要么客户端有 bug，要么在被利用
            raise Forbidden("上报的 deviceId 与当前设备不匹配")

        raw_events = body.get("events")
        if not isinstance(raw_events, list):
            raise BadRequest("events 必须是数组")
        if not raw_events:
            # 空批：事件链没动，所以事件链头就是「该会话已存的最后一条」。
            # 不能图省事回批次链头 —— 两者是不同的链，客户端会拿它续错链。
            return BatchResult(
                batch_id="",
                event_chain_head=self._last_hash(device_id, session_id),
                batch_chain_head=self.store.get_batch_chain_head(device_id),
            )
        limit = int(getattr(self.config, "audit_max_batch_events", redaction.MAX_EVENTS_PER_BATCH))
        if len(raw_events) > limit:
            raise BadRequest(f"单批事件数 {len(raw_events)} 超过上限 {limit}，请分批上报")

        # --- 1) 先全量脱敏校验（一道不通过就整条拒，见 redaction 文件头）---
        stats = redaction.redaction_stats(raw_events)
        accepted_events: list[dict[str, Any]] = stats["accepted"]
        rejected: list[dict[str, Any]] = stats["rejected"]

        # --- 2) 组行（身份取自 actor，不取自事件体）---
        team_id = actor.memberships[0].team_id if actor.memberships else ""
        now = self.clock.now()
        batch_id = new_id("bat")
        rows: list[dict[str, Any]] = []
        for event in accepted_events:
            try:
                seq = int(event.get("seq"))
            except (TypeError, ValueError):
                rejected.append({"index": -1, "reason": "事件缺少整数 seq"})
                continue
            try:
                ts = int(event.get("ts") or now)
            except (TypeError, ValueError):
                ts = now
            payload = event.get("payload") or {}
            rows.append(
                {
                    "device_id": device_id,
                    "user_id": actor.user_id,      # 不信客户端自称
                    "team_id": team_id,            # 同上
                    "session_id": session_id,
                    "seq": seq,
                    "event_type": str(event.get("type")),
                    "ts": ts,
                    "payload": payload,
                    "batch_id": batch_id,
                    "received_at": now,
                }
            )

        if not rows:
            return BatchResult(
                rejected=rejected,
                batch_id=batch_id,
                # 一条都没能组行 → 事件链没动，链头仍是该会话已存的最后一条
                event_chain_head=self._last_hash(device_id, session_id),
                batch_chain_head=self.store.get_batch_chain_head(device_id),
            )

        rows.sort(key=lambda r: r["seq"])

        # --- 3) 断号检测（要在入库前算，入库后就分不清"原本就没有"和"刚插进去"）---
        # 起点是「该会话已入库的最大 seq」，这样跨批次的缺口也能被发现 ——
        # 只查批次内部会漏掉「客户端上传到一半崩了，下一批从很后面接着传」。
        last_stored_seq = int(self.store.get_device_last_seq(device_id, session_id))
        gaps = self._find_gaps(rows, previous_seq=last_stored_seq)

        # --- 4) 只对「服务端还没有的」事件算链 ---
        # 这一步不能省。对已入库的行**重新算链**会得到与服务端已存哈希不同的值
        # （起点已经变成「已存的链头」而不是当初的起点），于是新插入的行会被
        # 链在一个错的前置哈希上 —— 链一旦这样被污染，之后每条都验不过，
        # 而且没法回溯修复（重算会覆盖证据）。
        #
        # 触发条件并不罕见：客户端的 `.upload-state.json` 被清掉（版本升级、
        # 误删）而服务端已有数据时，客户端会从 seq 1 重传；
        # 只要本地日志比服务端多几条，那几条就会被写坏。
        fresh = [row for row in rows if int(row["seq"]) > last_stored_seq]
        already_stored = len(rows) - len(fresh)

        # prev 从「该设备该会话已存的最后一条」开始，这样批次之间也能接上。
        # fresh 为空时，prev_hash 恰好就是真实的事件链头 ——
        # 纯重传返回的就是它，不会是「把老事件再链一遍」得到的凭空的头。
        prev_hash = self._last_hash(device_id, session_id)
        for row in fresh:
            body_text, row_hash = link(prev_hash, self._chain_payload(row))
            row["prev_hash"] = prev_hash
            row["hash"] = row_hash
            prev_hash = row_hash

        # --- 5) 入库（唯一约束负责幂等）---
        inserted = self.store.insert_audit_events(fresh)
        duplicates = len(fresh) - inserted + already_stored

        first_seq = rows[0]["seq"]
        last_seq = rows[-1]["seq"]

        # --- 6) 批次链（链的链）---
        prev_head = self.store.get_batch_chain_head(device_id)

        # 客户端声明的链头，与服务端按同一公式算出的链头必须一致。
        # 这是**唯一能证明「上传过程没被截断或重排」**的检查：
        # 副本链只能证明「我收到的东西之后没被改」，
        # 但如果客户端发了 10 条、网络只送达 7 条，副本链照样是完整的 ——
        # 除非我们拿客户端的承诺去比对。
        # 公式与字段形状必须两端一致（见 hashing.py 的说明），
        # 因此客户端不能直接把本地 JSONL 原样上传，而要先规范化。
        declared_head = str(body.get("eventChainHead") or "")
        head_matches: bool | None = None
        # 纯重放时**不能**做链头比对：这种情况下服务端的链起点已经是这批
        # 事件末条的哈希（它们上一轮就入库了），再拿它去"验证"客户端
        # 从更早起点算出的链头，必然不匹配 —— 于是每次网络重试都报一次
        # 「数据可能被篡改」。假告警会让人很快学会忽略所有告警，
        # 那比不检测更糟。所以这里明确报 None（不可判定），而不是 False。
        if declared_head and inserted > 0:
            head_matches = declared_head == prev_hash

        # 注意变量命名：这是**批次链**的头，与上面的 `prev_hash`（事件链头）
        # 是两条完全不同的链。历史上这两个值共用一个名字，直接导致了
        # 「第二批起审计上报永久卡死」的缺陷 —— 所以这里刻意把名字写满。
        _, batch_chain_head = link(
            prev_head,
            {
                "batch_id": batch_id,
                "device_id": device_id,
                "user_id": actor.user_id,
                "first_seq": first_seq,
                "last_seq": last_seq,
                "count": len(rows),
                "client_chain_head": declared_head,
            },
        )
        self.store.insert_audit_batch(
            {
                "id": batch_id,
                "device_id": device_id,
                "user_id": actor.user_id,
                "team_id": team_id,
                "first_seq": first_seq,
                "last_seq": last_seq,
                "event_count": inserted,
                "client_chain_head": declared_head,
                "prev_chain_head": prev_head,
                "chain_head": batch_chain_head,
                "sequence_gap": len(gaps),
                "received_at": now,
            }
        )

        if head_matches is False:
            # 链头不匹配有三种可能：上传被截断、客户端本地链与上传载荷不一致
            # （客户端 bug）、或者有人在中间改包。都要让人看到。
            self.store.record_auth_event(
                "audit.chain_head_mismatch",
                user_id=actor.user_id,
                detail=f"device={device_id} session={session_id} declared={declared_head[:16]} actual={prev_hash[:16]}",
                now=now,
            )

        return BatchResult(
            accepted=inserted,
            duplicates=duplicates,
            rejected=rejected,
            batch_id=batch_id,
            # prev_hash 就是本批最后一条事件的哈希（没有事件时是会话已存的最后一条）
            event_chain_head=prev_hash,
            batch_chain_head=batch_chain_head,
            gaps=gaps,
            first_seq=first_seq,
            last_seq=last_seq,
            client_head_match=head_matches,
        )

    # ============================================================== 内部
    @staticmethod
    def _chain_payload(row: dict[str, Any]) -> dict[str, Any]:
        """链哈希的输入。

        刻意**不含 `batch_id` 与 `received_at`** —— 它们是服务端侧概念。
        含进去的话，同一条事件被重传（新批次）会算出不同的哈希，
        「幂等」就成了空话。链只需覆盖「事件本身的身份与内容」。
        """
        return {
            "device_id": row["device_id"],
            "session_id": row["session_id"],
            "seq": row["seq"],
            "event_type": row["event_type"],
            "ts": row["ts"],
            "payload": row["payload"],
        }

    def _last_hash(self, device_id: str, session_id: str) -> str:
        """续链的起点：该会话已存的最后一条哈希，没有则创世值。"""
        value = self.store.get_session_last_hash(device_id, session_id)
        return value if value else GENESIS

    @staticmethod
    def _find_gaps(
        rows: list[dict[str, Any]], *, previous_seq: int = 0
    ) -> list[dict[str, int]]:
        """找 seq 缺口：既查批次内部，也查「上一批最后一条」到「本批第一条」。

        只报「缺口位置与大小」，不尝试补齐。补齐会掩盖「丢了多少」这个事实，
        而审计的价值恰恰在于「如实记录丢失」。
        """
        gaps: list[dict[str, int]] = []
        if previous_seq and rows:
            delta = int(rows[0]["seq"]) - int(previous_seq)
            if delta > 1:
                gaps.append(
                    {
                        "afterSeq": int(previous_seq),
                        "beforeSeq": int(rows[0]["seq"]),
                        "missing": delta - 1,
                    }
                )
        for previous, current in zip(rows, rows[1:]):
            delta = int(current["seq"]) - int(previous["seq"])
            if delta > 1:
                gaps.append(
                    {
                        "afterSeq": int(previous["seq"]),
                        "beforeSeq": int(current["seq"]),
                        "missing": delta - 1,
                    }
                )
        return gaps

    # ============================================================== 查询
    def query(self, *, actor, params: dict[str, Any]) -> dict[str, Any]:
        """按作用域过滤查询。

        **作用域是这里唯一的难点**：审计是全公司最敏感的数据，
        而它天然「一个人能看到所有人的记录」。所以过滤必须在
        「谁能看哪些 team / user」这一层做对，且默认只给自己。
        """
        rbac.require_permission(actor, rbac.PERM_AUDIT_READ)

        team_id = str(params.get("teamId") or "")
        user_id = str(params.get("userId") or "")

        if team_id:
            actor.scope_team(team_id, rbac.PERM_AUDIT_READ)
        elif user_id:
            actor.scope_user(user_id, rbac.PERM_AUDIT_READ)
        elif actor.is_org_wide:
            pass  # 组织级：不加过滤
        elif actor.can(rbac.PERM_USAGE_READ_TEAM) and actor.team_ids:
            # 团队管理员：限定在自己团队
            team_id = actor.team_ids[0]
        else:
            # 普通开发者：只能看自己的
            user_id = actor.user_id

        # 再兜一层：即便上面选定了 team，也要确认它真在自己范围内
        if team_id and not actor.is_org_wide and team_id not in actor.team_ids:
            raise Forbidden("无权查询该团队的审计")

        limit = min(max(1, int(params.get("limit") or 100)), 1000)
        offset = max(0, int(params.get("offset") or 0))

        rows = self.store.query_audit_events(
            team_id=team_id,
            user_id=user_id,
            device_id=str(params.get("deviceId") or ""),
            session_id=str(params.get("sessionId") or ""),
            event_type=str(params.get("eventType") or ""),
            since=int(params.get("since") or 0),
            until=int(params.get("until") or 0),
            limit=limit,
            offset=offset,
        )
        return {
            "events": [
                {
                    "deviceId": str(r.get("device_id") or ""),
                    "userId": str(r.get("user_id") or ""),
                    "teamId": str(r.get("team_id") or ""),
                    "sessionId": str(r.get("session_id") or ""),
                    "seq": int(r.get("seq") or 0),
                    "type": str(r.get("event_type") or ""),
                    "ts": int(r.get("ts") or 0),
                    "payload": r.get("payload") or {},
                    "hash": str(r.get("hash") or ""),
                }
                for r in rows
            ],
            "limit": limit,
            "offset": offset,
            "scope": {"teamId": team_id, "userId": user_id, "orgWide": actor.is_org_wide},
        }

    def summary(self, *, actor, team_id: str = "", since: int = 0) -> dict[str, Any]:
        rbac.require_permission(actor, rbac.PERM_AUDIT_READ)
        if team_id:
            actor.scope_team(team_id, rbac.PERM_AUDIT_READ)
        elif not actor.is_org_wide:
            # 不给团队聚合就退化成自己的维度，避免普通开发者拿到全公司概览
            team_id = actor.team_ids[0] if actor.team_ids else ""
        rows = self.store.audit_summary(team_id=team_id, since=since)
        return {
            "teamId": team_id,
            "since": since,
            "byType": [
                {"type": str(r.get("event_type") or ""), "count": int(r.get("n") or 0)}
                for r in rows
            ],
            "total": sum(int(r.get("n") or 0) for r in rows),
        }

    def verify(self, *, actor, device_id: str = "") -> dict[str, Any]:
        """校验审计完整性。

        这是审计系统自我证明的能力：**必须能回答「这些记录可信吗」**。
        检查两条链：批次链（相邻批次的 chain_head 是否首尾相接）
        与逐条链（每条事件的 prev_hash 是否等于上一条的 hash）。
        """
        rbac.require_permission(actor, rbac.PERM_AUDIT_READ)
        if not device_id:
            raise BadRequest("需要指定 deviceId")
        if not actor.is_org_wide:
            # 非组织级只能验自己设备的链
            from .errors import NotFound

            device = self.store.get_device(device_id)
            if device is None or str(device.get("user_id")) != actor.user_id:
                raise NotFound("设备不存在")

        batches = self.store.server_chain(device_id, limit=2000)
        batch_problems: list[dict[str, Any]] = []
        previous_head = GENESIS
        for batch in batches:
            expected_prev = str(batch.get("prev_chain_head") or "")
            if expected_prev != previous_head:
                batch_problems.append(
                    {
                        "batchId": str(batch.get("id") or ""),
                        "kind": "batch_chain_broken",
                        "expectedPrev": previous_head,
                        "actualPrev": expected_prev,
                    }
                )
            previous_head = str(batch.get("chain_head") or "")

        # 逐条链校验：抽一段（全量在设备记录多时会很慢）
        events = self.store.query_audit_events(device_id=device_id, limit=1000)
        event_problems: list[dict[str, Any]] = []
        previous_hash: str | None = None
        for row in sorted(events, key=lambda r: (str(r.get("session_id")), int(r.get("seq") or 0))):
            stored_prev = str(row.get("prev_hash") or "")
            stored_hash = str(row.get("hash") or "")
            if previous_hash is not None and stored_prev != previous_hash:
                event_problems.append(
                    {
                        "sessionId": str(row.get("session_id") or ""),
                        "seq": int(row.get("seq") or 0),
                        "kind": "event_chain_broken",
                    }
                )
            # 重算本条哈希，确认「内容没被改」
            _, recomputed = link(
                stored_prev,
                self._chain_payload(
                    {
                        "device_id": str(row.get("device_id") or ""),
                        "session_id": str(row.get("session_id") or ""),
                        "seq": int(row.get("seq") or 0),
                        "event_type": str(row.get("event_type") or ""),
                        "ts": int(row.get("ts") or 0),
                        "payload": row.get("payload") or {},
                    }
                ),
            )
            if recomputed != stored_hash:
                event_problems.append(
                    {
                        "sessionId": str(row.get("session_id") or ""),
                        "seq": int(row.get("seq") or 0),
                        "kind": "event_hash_mismatch",
                    }
                )
            previous_hash = stored_hash

        ok = not batch_problems and not event_problems
        return {
            "deviceId": device_id,
            "ok": ok,
            "batchCount": len(batches),
            "eventCount": len(events),
            "batchProblems": batch_problems[:20],
            "eventProblems": event_problems[:20],
            "detail": (
                "两条链均完整"
                if ok
                else f"发现 {len(batch_problems)} 处批次链问题、{len(event_problems)} 处事件链问题"
            ),
        }


__all__ = ["AuditService", "BatchResult"]
