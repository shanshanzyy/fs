"""跨端链式哈希一致性 —— M2 最不能出错的一条不变量。

**为什么这条最重要：** 客户端算的链头会随着上传请求发到服务端，
服务端按「同一公式」重算一遍再比对。两端的 `canonical()` 只要有一个参数
不同（比如少写一个 `ensure_ascii=False`，或者多一个空格分隔符），
比对就会**永远失败**。而失败的表现是「每次上传都报『数据可能被篡改』」——
一个每天都在喊、但其实是误报的告警，比没有告警更糟：它会训练人去忽略它。

更麻烦的是它**不会**在单端测试里暴露：客户端测试自己算两遍当然一致，
服务端测试也一样。只有把两端放进同一个进程里比对才能发现。

因此这个文件是**唯一**同时 import `vibe_server` 与 `agent_core` 的地方。

第二条不变量也在这里：**客户端上传的字段必须是服务端契约允许的**。
否则服务端会整条拒收，而那条事件此时已经算进客户端的上传链 ——
它永久丢了，且重传不会补上（本地水位线已推进）。
"""

from __future__ import annotations

import json
import unittest

from tests.support import TempDirTestCase

from agent_core import audit as client_audit
from agent_core.governance import protocol as client_protocol
from agent_core.governance import vocabulary
from vibe_server import hashing as server_hashing
from vibe_server import rbac, redaction
from vibe_server.audit import AuditService


class CanonicalFormTests(unittest.TestCase):
    """序列化层：两端必须逐字节一致。"""

    def test_canonical_output_is_byte_identical(self) -> None:
        payloads = [
            {"device_id": "d1", "session_id": "s1", "seq": 1, "event_type": "tool.call",
             "ts": 1_700_000_000, "payload": {"tool": "read_file", "ok": True}},
            # 中文：ensure_ascii 不一致会在这里立刻暴露
            {"note": "读取文件", "path": "src/中文目录/a.py"},
            # 键顺序被打乱：sort_keys 不一致会在这里暴露
            {"z": 1, "a": 2, "m": 3},
            # 浮点与 None：default=str 与类型转换的边界
            {"f": 1.5, "b": False, "n": None, "i": 0},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual(
                    client_protocol.canonical(payload),
                    server_hashing.canonical(payload),
                )

    def test_chain_hash_matches(self) -> None:
        for prev in (client_protocol.GENESIS, "abc123", ""):
            for body in ('{"a":1}', "中文内容", ""):
                with self.subTest(prev=prev, body=body):
                    self.assertEqual(
                        client_protocol.chain_hash(prev, body),
                        server_hashing.chain_hash(prev, body),
                    )

    def test_genesis_value_matches(self) -> None:
        """创世值不一致的话，每个会话的第一条就对不上。"""
        self.assertEqual(client_protocol.GENESIS, server_hashing.GENESIS)


class ChainFieldShapeTests(unittest.TestCase):
    """链哈希的输入字段：六个，一个不多一个不少。"""

    def test_chain_payload_keys_are_the_six_fields(self) -> None:
        payload = client_protocol.chain_payload(
            device_id="d", session_id="s", seq=1,
            event_type="tool.call", ts=100, payload={"k": "v"},
        )
        self.assertEqual(
            set(payload),
            {"device_id", "session_id", "seq", "event_type", "ts", "payload"},
        )
        server_payload = {
            "device_id": "d", "session_id": "s", "seq": 1,
            "event_type": "tool.call", "ts": 100, "payload": {"k": "v"},
        }
        self.assertEqual(
            client_protocol.canonical(payload),
            server_hashing.canonical(server_payload),
        )

    def test_server_chain_payload_shape_is_unchanged(self) -> None:
        """服务端 `_chain_payload` 一旦加了字段（比如 batch_id），
        同一条事件重传就会算出不同哈希，「幂等」直接失效。
        这里钉住它的输出字段，让那次改动必须在测试里显式面对。"""
        row = {
            "device_id": "d", "user_id": "u", "team_id": "t",
            "session_id": "s", "seq": 3, "event_type": "tool.call",
            "ts": 100, "payload": {"k": "v"},
            "batch_id": "bat_1", "received_at": 999,
        }
        result = AuditService._chain_payload(row)
        self.assertEqual(
            set(result),
            {"device_id", "session_id", "seq", "event_type", "ts", "payload"},
        )
        for forbidden in ("batch_id", "received_at", "user_id", "team_id"):
            self.assertNotIn(forbidden, result)


class DigestAlgorithmTests(unittest.TestCase):
    """两端的摘要算法必须一致，否则跨设备比对失效。"""

    def test_local_digest_equals_upload_transform(self) -> None:
        for text in ("pytest -q", "src/main.py", "中文路径/文件.py", ""):
            with self.subTest(text=text):
                self.assertEqual(
                    client_audit.digest(text),
                    vocabulary.digest_value(text),
                    "本地日志用的摘要算法与上传翻译层不一致 —— "
                    "同一个值会在本地和服务端算出不同指纹",
                )

    def test_digest_matches_server_regex(self) -> None:
        """服务端 `_DIGEST_RE` 要求 16–64 位 hex。"""
        pattern = redaction._DIGEST_RE
        self.assertIsNotNone(pattern.match(vocabulary.digest_value("anything")))


class VocabularyContractTests(unittest.TestCase):
    """客户端翻译层的输出必须落在服务端契约内。"""

    def test_every_rule_field_is_accepted_by_server(self) -> None:
        """规则表里每个目标字段都必须被服务端对应的事件类型接受。

        写错一个字段名（比如 `matches` 而不是 `hits`）会导致**整条**被拒，
        而这类错误在客户端是完全静默的 —— 只有这个测试能挡住它。
        """
        schema = redaction.event_schema()
        for local_event in sorted(vocabulary.classified_events()):
            rule = vocabulary.rule_for(local_event)
            assert rule is not None
            if not rule.upload:
                continue
            with self.subTest(local_event=local_event):
                self.assertIn(
                    rule.upload, schema,
                    f"规则把 {local_event} 映射到 {rule.upload!r}，服务端没有这个事件类型",
                )
                allowed = set(schema[rule.upload])
                for target in rule.fields:
                    self.assertIn(
                        target, allowed,
                        f"{local_event} → {rule.upload}.{target} 不在服务端契约内。"
                        f"允许的字段：{', '.join(sorted(allowed))}",
                    )

    def test_every_upload_event_is_reachable_from_a_rule(self) -> None:
        """反向检查：服务端登记的每个类型，要么有本地来源，要么是服务端自产。

        这条防的是另一种漂移：服务端加了一类事件、客户端却永远不产生它，
        于是那类审计在真机上永远是空的，而没人知道是「没配」还是「没实现」。
        """
        # 这些是服务端自己产生的（网关调用、策略下发），不指望客户端上报
        server_side = {
            "gateway.request", "gateway.response", "gateway.denied",
            "policy.fetch", "policy.rejected",
        }
        # 这些是 M2 客户端已实现但尚未接线的事件（见 TODO）
        not_yet_wired: set[str] = set()

        produced = {
            vocabulary.rule_for(name).upload
            for name in vocabulary.classified_events()
            if vocabulary.rule_for(name) and vocabulary.rule_for(name).upload
        }
        for event_type in redaction.known_event_types():
            if event_type in server_side or event_type in not_yet_wired:
                continue
            with self.subTest(event_type=event_type):
                self.assertIn(
                    event_type, produced,
                    f"服务端登记了 {event_type}，但客户端词汇表里没有任何本地事件映射到它",
                )

    def test_translate_output_passes_server_validation(self) -> None:
        """把每条规则喂给它声称要读的字段，输出必须通过服务端校验。"""
        schema = redaction.event_schema()
        samples = _sample_locals()
        for local_event, entry in samples.items():
            with self.subTest(local_event=local_event):
                translated = vocabulary.translate(
                    entry, session_id="sess_x", sequence=1, fallback_ts=0, schema=schema
                )
                self.assertIsNotNone(translated, f"{local_event} 无法翻译")
                assert translated is not None
                self.assertEqual(translated.unknown_fields, ())
                verdict = redaction.validate_event(translated.event)
                self.assertTrue(verdict.ok, f"{local_event}: {verdict.reason}")

    def test_content_fields_are_never_uploaded(self) -> None:
        """ADR-006：原文类字段一个都不许出现在上传载荷里。"""
        schema = redaction.event_schema()
        for local_event, entry in _sample_locals().items():
            translated = vocabulary.translate(
                entry, session_id="sess_x", sequence=1, fallback_ts=0, schema=schema
            )
            if translated is None:
                continue
            with self.subTest(local_event=local_event):
                payload = translated.event["payload"]
                for denied in redaction.DENIED_KEYS:
                    self.assertNotIn(denied, payload)
                # 逐值检查：不许有换行，长度不许超过字段上限
                for key, value in payload.items():
                    if isinstance(value, str):
                        self.assertNotIn("\n", value, f"{local_event}.{key} 含换行")
                        self.assertLessEqual(
                            len(value), redaction.MAX_STRING,
                            f"{local_event}.{key} 长度 {len(value)} 超上限",
                        )

    def test_local_only_events_are_not_translated(self) -> None:
        """纯本地事件必须返回 None，不能悄悄上传一个空壳。"""
        for name in ("agent.llm.error", "agent.run.interrupt_requested"):
            with self.subTest(event=name):
                rule = vocabulary.rule_for(name)
                self.assertIsNotNone(rule, f"{name} 未分类")
                assert rule is not None
                self.assertEqual(rule.upload, "")
                self.assertIsNone(vocabulary.translate(
                    {"event": name, "error": "boom"}, session_id="s", sequence=1,
                    fallback_ts=0, schema=redaction.event_schema(),
                ))


class VocabularySourceTests(unittest.TestCase):
    """规则表的**来源端**也必须真实存在。

    反向检查（服务端 schema → 词表）能发现「服务端登记了、没人产出」，
    但发现不了反过来的一种：**词表里写了规则，代码里却从来没人发这个事件**。
    那时那条规则是死的，对应审计在真机上永远是空的 ——
    和「服务端登记了没产出方」是同一个病，只是长在另一端。
    （`session.start` / `session.end` 就曾长期是这个状态。）
    """

    def test_every_rule_source_is_recorded_somewhere(self) -> None:
        recorded = _recorded_event_names()
        missing = sorted(
            name for name in vocabulary.classified_events() if name not in recorded
        )
        self.assertEqual(
            missing, [],
            "词表里有规则，但源码里找不到对应的 audit.record(...) 调用点 —— "
            f"这些规则是死的：{missing}",
        )

    def test_every_recorded_event_is_classified(self) -> None:
        """反过来：源码里发了、词表里没登记。

        后果更隐蔽：`normalize()` 会把它算成 `skipped_unknown`，
        也就是**静默不上传**，同时还把「未知事件」这个真实缺陷信号淹掉。
        """
        unclassified = vocabulary.unclassified(_recorded_event_names())
        self.assertEqual(unclassified, [], f"这些事件发了但没有分类：{unclassified}")


class EndToEndHeadAgreementTests(TempDirTestCase):
    """真跑一遍：客户端翻译 → 算链头 → 服务端重算 → 比对。"""

    def _actor(self, device_id: str) -> object:
        return rbac.build_actor(
            user_id="u_1",
            email="dev@corp.example",
            display_name="Dev",
            memberships=[
                {
                    "team_id": "team_default",
                    "team_slug": "default",
                    "team_name": "Default",
                    "role": rbac.ROLE_DEVELOPER,
                }
            ],
            device_id=device_id,
        )

    def _entries(self, session: str) -> list[dict]:
        """模拟 agent-core 真实写出的本地审计记录（字段名取自调用点）。"""
        entries = []
        for index, sample in enumerate(_sample_locals().values(), start=1):
            entry = dict(sample)
            entry["seq"] = index
            entry["session"] = session
            entry["ts"] = "2026-09-24T10:00:%02d" % index
            entries.append(entry)
        return entries

    def _translate_all(self, entries: list[dict]) -> list[dict]:
        schema = redaction.event_schema()
        events = []
        for entry in entries:
            translated = vocabulary.translate(
                entry,
                session_id=str(entry.get("session") or ""),
                sequence=int(entry["seq"]),
                fallback_ts=0,
                schema=schema,
            )
            if translated is None:
                continue
            events.append(translated.event)
        return events

    def test_client_head_equals_server_head(self) -> None:
        device_id = "dev_1"
        session_id = "sess_abc"

        events = self._translate_all(self._entries(session_id))
        self.assertGreaterEqual(len(events), 5)

        _, client_head = client_protocol.compute_batch(
            events, device_id=device_id, session_id=session_id,
            start_hash=client_protocol.GENESIS,
        )

        app = self.make_app()
        service = AuditService(store=app.store, config=self.config, clock=app.clock)
        result = service.ingest(
            actor=self._actor(device_id),
            body={
                "sessionId": session_id,
                "deviceId": device_id,
                "eventChainHead": client_head,
                "events": events,
            },
        )

        self.assertEqual(result.rejected, [], f"有事件被拒: {result.rejected}")
        self.assertEqual(result.accepted, len(events))
        self.assertEqual(
            result.event_chain_head, client_head,
            "客户端与服务端算出的链头不一致：两端的 canonical/字段形状已经漂移",
        )
        self.assertIs(result.client_head_match, True)

    def test_second_batch_chains_onto_first(self) -> None:
        """第二批必须接得上第一批，否则每批都要重新建立信任。"""
        device_id = "dev_2"
        session_id = "sess_multi"

        app = self.make_app()
        service = AuditService(store=app.store, config=self.config, clock=app.clock)
        actor = self._actor(device_id)

        entries = self._entries(session_id)
        half = len(entries) // 2
        first_events = self._translate_all(entries[:half])
        second_events = self._translate_all(entries[half:])

        _, head = client_protocol.compute_batch(
            first_events, device_id=device_id, session_id=session_id
        )
        r1 = service.ingest(
            actor=actor,
            body={"sessionId": session_id, "deviceId": device_id,
                  "eventChainHead": head, "events": first_events},
        )
        self.assertEqual(r1.event_chain_head, head)
        self.assertIs(r1.client_head_match, True)

        # 关键：第二批的起点是第一批的链头，不是创世值
        _, head2 = client_protocol.compute_batch(
            second_events, device_id=device_id, session_id=session_id, start_hash=head
        )
        r2 = service.ingest(
            actor=actor,
            body={"sessionId": session_id, "deviceId": device_id,
                  "eventChainHead": head2, "events": second_events},
        )
        self.assertEqual(r2.event_chain_head, head2)
        self.assertIs(r2.client_head_match, True)
        self.assertNotEqual(head, head2)

    def test_retransmit_is_idempotent_and_not_a_false_alarm(self) -> None:
        """重传同一批：不能重复入库，也不能报「链头不符」。

        这是最容易被写错的一处：重传时服务端的链起点**已经**是本批的链头了，
        再拿它跟客户端声明的**起始**链头比，必然不符 ——
        于是每次网络抖动导致的正常重试都会报一次「数据可能被篡改」。
        """
        device_id = "dev_3"
        session_id = "sess_retry"

        app = self.make_app()
        service = AuditService(store=app.store, config=self.config, clock=app.clock)
        actor = self._actor(device_id)

        events = self._translate_all(self._entries(session_id))
        _, head = client_protocol.compute_batch(
            events, device_id=device_id, session_id=session_id
        )
        body = {"sessionId": session_id, "deviceId": device_id,
                "eventChainHead": head, "events": events}

        first = service.ingest(actor=actor, body=body)
        self.assertEqual(first.accepted, len(events))
        self.assertIs(first.client_head_match, True)

        again = service.ingest(actor=actor, body=dict(body))
        self.assertEqual(again.accepted, 0, "重传不应重复入库")
        self.assertEqual(again.duplicates, len(events))
        self.assertIsNone(
            again.client_head_match,
            "纯重放被误报成链头不符 —— 这会让重试变成告警噪音",
        )
        self.assertEqual(again.event_chain_head, first.event_chain_head)

    def test_truncated_upload_is_detected(self) -> None:
        """客户端声称 N 条却少发了最后一条 —— 必须被抓住。

        这是链头比对**唯一**能证明的事：副本链只能证明「收到的东西之后没被改」，
        但「发出的和收到的是不是同一批」靠副本链自己是看不出来的。
        """
        device_id = "dev_4"
        session_id = "sess_trunc"

        app = self.make_app()
        service = AuditService(store=app.store, config=self.config, clock=app.clock)
        actor = self._actor(device_id)

        events = self._translate_all(self._entries(session_id))
        _, declared = client_protocol.compute_batch(
            events, device_id=device_id, session_id=session_id
        )
        result = service.ingest(
            actor=actor,
            body={"sessionId": session_id, "deviceId": device_id,
                  "eventChainHead": declared, "events": events[:-1]},
        )
        self.assertEqual(result.accepted, len(events) - 1)
        self.assertIs(
            result.client_head_match, False,
            "截断上传未被发现 —— 这正是链头比对存在的理由",
        )

    def test_two_chain_heads_are_distinct_and_both_returned(self) -> None:
        """钉住 P0：响应必须同时给出**两个不同的**链头，且不得有含糊的 `chainHead`。

        缺陷原貌：`BatchResult.chain_head` 装的是**批次链头**（链的链），
        客户端却把它当**事件链头**存起来当下批起点 → 第二批起必然对不上 →
        客户端按设计拒绝推进水位线 → 审计上报永久卡死，
        而运维看到的是「数据可能被篡改」这个假告警。

        这条测试的价值在于：它检查的是**两个值的语义不同**，
        而不是某个名字存不存在。光改名字、把两个字段都填成同一个值，
        照样是坏的 —— 所以这里断言它们不相等。
        """
        device_id = "dev_heads"
        session_id = "sess_heads"

        events = self._translate_all(self._entries(session_id))
        _, client_head = client_protocol.compute_batch(
            events, device_id=device_id, session_id=session_id
        )

        app = self.make_app()
        service = AuditService(store=app.store, config=self.config, clock=app.clock)
        result = service.ingest(
            actor=self._actor(device_id),
            body={
                "sessionId": session_id,
                "deviceId": device_id,
                "eventChainHead": client_head,
                "events": events,
            },
        )

        # 事件链头 == 客户端算出的那个（客户端下批要用它续链）
        self.assertEqual(
            result.event_chain_head, client_head,
            "事件链头必须等于客户端声明的链头 —— 客户端就是拿它作为下批起点",
        )
        # 批次链头是另一条链，**必然不同**。相同就意味着其中一条是抄来的。
        self.assertNotEqual(
            result.batch_chain_head, result.event_chain_head,
            "两个链头相同 —— 说明有一条链没有真的算，或者又混用了一个字段",
        )
        self.assertTrue(result.batch_chain_head)

        body = result.describe()
        self.assertIn("eventChainHead", body)
        self.assertIn("batchChainHead", body)
        self.assertNotIn(
            "chainHead", body,
            "响应里不许再出现含糊的 chainHead —— 一个名字能指两条链，就是缺陷的成因",
        )

    def test_third_batch_still_matches(self) -> None:
        """连续三批都要接得上。

        只测两批会漏掉一类缺陷：如果「续链起点」取的是**本批末条**的哈希
        而不是跟上批接续，两批的场景在某些构造下也能凑对，
        到第三批才开始漂移。三批能把它逼出来。
        """
        device_id = "dev_6"
        session_id = "sess_three"

        app = self.make_app()
        service = AuditService(store=app.store, config=self.config, clock=app.clock)
        actor = self._actor(device_id)

        entries = self._entries(session_id)
        size = max(1, len(entries) // 3)
        chunks = [entries[i : i + size] for i in range(0, len(entries), size)]

        start = client_protocol.GENESIS
        for index, chunk in enumerate(chunks):
            events = self._translate_all(chunk)
            _, declared = client_protocol.compute_batch(
                events, device_id=device_id, session_id=session_id, start_hash=start
            )
            result = service.ingest(
                actor=actor,
                body={
                    "sessionId": session_id,
                    "deviceId": device_id,
                    "eventChainHead": declared,
                    "events": events,
                },
            )
            with self.subTest(batch=index):
                self.assertEqual(result.rejected, [])
                self.assertEqual(
                    result.event_chain_head, declared,
                    f"第 {index + 1} 批链头不符 —— 客户端与服务端的链已经分叉",
                )
                self.assertIs(
                    result.client_head_match, True,
                    f"第 {index + 1} 批被误判为链头不符",
                )
            # 下一批的起点 = 本批服务端确认的事件链头
            start = result.event_chain_head

    def test_reupload_from_scratch_keeps_chain_intact(self) -> None:
        """上报状态被清掉后从头重传（后端已有前一半）—— 已入库的链不能被打坏。

        这是同一个区域里比「链头取错字段」更深的一处缺陷：

        重传时如果把**已入库的行**也跟着重新算一遍链，它们的起点会变成
        「已存的链头」而不是当初的起点，于是**新插入的那几条**会被链在一个
        错的前置哈希上。链一旦这样被污染，之后每条都验不过，
        而且没法回溯修复 —— 重算会覆盖证据。

        触发条件很常见：客户端 `.upload-state.json` 因版本升级或误删而丢失，
        服务端却已经有数据。
        """
        device_id = "dev_7"
        session_id = "sess_reupload"

        app = self.make_app()
        service = AuditService(store=app.store, config=self.config, clock=app.clock)
        actor = self._actor(device_id)

        entries = self._entries(session_id)
        half = len(entries) // 2

        # 第一批：只传前一半
        first_events = self._translate_all(entries[:half])
        _, head_one = client_protocol.compute_batch(
            first_events, device_id=device_id, session_id=session_id
        )
        r1 = service.ingest(
            actor=actor,
            body={"sessionId": session_id, "deviceId": device_id,
                  "eventChainHead": head_one, "events": first_events},
        )
        self.assertEqual(r1.accepted, len(first_events))

        # 客户端状态被清 → 从头重传「全部」，前一半服务端已经有了
        all_events = self._translate_all(entries)
        _, head_all = client_protocol.compute_batch(
            all_events, device_id=device_id, session_id=session_id
        )
        r2 = service.ingest(
            actor=actor,
            body={"sessionId": session_id, "deviceId": device_id,
                  "eventChainHead": head_all, "events": all_events},
        )

        self.assertEqual(r2.accepted, len(all_events) - len(first_events),
                         "只有新增的那几条应该入库")
        self.assertEqual(r2.duplicates, len(first_events))
        self.assertIs(r2.client_head_match, True, "从头重传被误判成链头不符")
        self.assertEqual(r2.event_chain_head, head_all)

        # 最关键的一条：从库里把整条链重算一遍，逐行确认接续与内容
        stored = app.store.query_audit_events(
            device_id=device_id, session_id=session_id, limit=1000
        )
        ordered = sorted(stored, key=lambda r: int(r.get("seq") or 0))
        self.assertEqual(len(ordered), len(all_events), "事件条数与预期不符")

        previous = server_hashing.GENESIS
        for row in ordered:
            seq = int(row.get("seq") or 0)
            self.assertEqual(
                str(row.get("prev_hash") or ""), previous,
                f"seq={seq} 的前置哈希接不上 —— 链被打坏了",
            )
            _, recomputed = server_hashing.link(
                previous,
                AuditService._chain_payload(
                    {
                        "device_id": str(row.get("device_id") or ""),
                        "session_id": str(row.get("session_id") or ""),
                        "seq": seq,
                        "event_type": str(row.get("event_type") or ""),
                        "ts": int(row.get("ts") or 0),
                        "payload": row.get("payload") or {},
                    }
                ),
            )
            self.assertEqual(
                recomputed, str(row.get("hash") or ""),
                f"seq={seq} 的内容哈希对不上 —— 说明它被链在了错的前置哈希上",
            )
            previous = recomputed

        self.assertEqual(previous, head_all, "整条链的末端与客户端声明的不一致")

    def test_reordered_upload_keeps_head(self) -> None:
        """顺序被打乱：链锚定的是内容，不是传输顺序。"""
        device_id = "dev_5"
        session_id = "sess_reorder"

        app = self.make_app()
        service = AuditService(store=app.store, config=self.config, clock=app.clock)
        actor = self._actor(device_id)

        events = self._translate_all(self._entries(session_id))
        _, declared = client_protocol.compute_batch(
            events, device_id=device_id, session_id=session_id
        )
        shuffled = [events[2], events[0], events[-1], *events[1:-1]]
        # 去重要用**可哈希**的键：payload 是 dict，直接对 items() 做 set / dict.fromkeys
        # 会 TypeError（unhashable type: 'dict'）。规范化 JSON 当键既简单又确定。
        unique: dict[str, dict] = {}
        for item in shuffled:
            unique.setdefault(json.dumps(item, sort_keys=True, ensure_ascii=False), item)
        shuffled = list(unique.values())
        result = service.ingest(
            actor=actor,
            body={"sessionId": session_id, "deviceId": device_id,
                  "chainHead": declared, "events": shuffled},
        )
        self.assertEqual(result.accepted, len(events))
        self.assertEqual(
            result.event_chain_head, declared,
            "同一批事件重排后链头变了 —— 链锚点被传输顺序污染了",
        )


class TimestampNormalizationTests(unittest.TestCase):
    """本地可读时间戳 → epoch 的转换必须确定。"""

    def test_readable_timestamp_is_parsed(self) -> None:
        ts = client_protocol._to_epoch("2026-09-24T10:00:00", fallback=0)
        self.assertGreater(ts, 1_700_000_000)
        self.assertEqual(ts, client_protocol._to_epoch("2026-09-24T10:00:00", fallback=999))

    def test_epoch_passthrough(self) -> None:
        self.assertEqual(client_protocol._to_epoch(1_700_000_000, fallback=0), 1_700_000_000)

    def test_unparseable_falls_back(self) -> None:
        """解析不了就用 fallback，**绝不**返回 0 或当前时间。

        当前时间会让「一条 3 天前的记录」看起来像刚发生，
        在审计场景里这等于伪造时间。
        """
        self.assertEqual(client_protocol._to_epoch("not-a-time", fallback=42), 42)
        self.assertEqual(client_protocol._to_epoch(None, fallback=42), 42)
        self.assertEqual(client_protocol._to_epoch("", fallback=42), 42)


# --------------------------------------------------------------------------- 样本
def _recorded_event_names() -> set[str]:
    """扫出 agent_core 源码里所有 `audit.record("字面量", ...)` 的事件名。

    用 AST 而不是正则：正则会把注释、文档字符串、以及
    `record(name)` 这类变量调用一起算进来，于是测试要么误报要么漏报 ——
    而一个会误报的检查最终会被加豁免，然后彻底失去作用。
    只认字符串字面量，变量调用一律不认（那种情况下人也读不出事件名）。
    """
    import ast
    from pathlib import Path

    package_root = Path(client_audit.__file__).resolve().parent
    found: set[str] = set()
    for path in sorted(package_root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", "") or getattr(func, "id", "")
            if name != "record" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.add(first.value)
    return found


def _sample_locals() -> dict[str, dict]:
    """每条本地事件的真实形状样本（字段名取自 agent_core 的调用点）。

    这个字典是「本地词汇表」在测试里的落点。它不追求覆盖所有本地事件
    （纯本地的那些不需要样本），只覆盖**会上传**的那些 ——
    这样每加一条上传规则，就有一个样本把它的形状钉住。
    """
    return {
        "agent.run.start": {
            "event": "agent.run.start",
            "prompt_chars": 42,
            "prompt_digest": "a" * 16,
        },
        "tool.execute": {
            "event": "tool.execute",
            "tool": "read_file",
            "ok": True,
            "risk": "low",
            "elapsed_ms": 12,
            "output_chars": 512,
            "path": "src/main.py",
        },
        "tool.read_file": {
            "event": "tool.read_file",
            "path": "src/main.py",
            "lines": "1-80",
            "chars": 1200,
        },
        "tool.grep": {
            "event": "tool.grep",
            "pattern": r"def \w+\(",
            "path": ".",
            "hits": 7,
        },
        "tool.find_files": {
            "event": "tool.find_files",
            "pattern": "*.py",
            "hits": 23,
        },
        "tool.list_dir": {"event": "tool.list_dir", "path": "src", "depth": 1, "entries": 9},
        "tool.repo_map": {
            "event": "tool.repo_map",
            "path": ".",
            "files": 120,
            "symbols": 940,
            "truncated": False,
        },
        "tool.semantic_search": {
            "event": "tool.semantic_search",
            "query": "哪里处理用户登录",
            "path": ".",
            "hits": 5,
            "backend": "lexical",
        },
        "index.rebuild": {
            "event": "index.rebuild",
            "added": 1,
            "changed": 2,
            "removed": 0,
            "files_count": 120,
            "ms": 37,
        },
        "tool.git_diff": {
            "event": "tool.git_diff",
            "mode": "diff",
            "path": "src/main.py",
            "ref": "HEAD",
            "staged": False,
            "files": 2,
            "added": 12,
            "removed": 3,
            "diff": (
                "diff --git a/src/main.py b/src/main.py\n"
                "index 1a2b3c4..5d6e7f8 100644\n"
                "--- a/src/main.py\n"
                "+++ b/src/main.py\n"
                "@@ -1,3 +1,4 @@\n"
                "+import os\n"
            ),
        },
        "git.commit_message": {
            "event": "git.commit_message",
            "branch": "main",
            "files": 2,
            "added": 12,
            "removed": 3,
            "diff": "diff --git a/src/main.py b/src/main.py\n+import os\n",
            "message_chars": 68,
        },
        "tool.edit_file": {
            "event": "tool.edit_file",
            "path": "src/main.py",
            "strategy": "exact",
            "added": 3,
            "removed": 1,
            "search_digest": "b" * 16,
            "replace_digest": "c" * 16,
            "search_lines": 4,
        },
        "tool.write_file": {
            "event": "tool.write_file",
            "path": "src/new.py",
            "mode": "create",
            "added": 30,
            "removed": 0,
            "bytes": 800,
            "content_digest": "d" * 16,
        },
        # Repo Wiki（M3+）：与 tools/repo_wiki.py 的 audit.record 调用点形状一致
        "tool.repo_wiki": {
            "event": "tool.repo_wiki",
            "path": "docs/wiki",
            "files": 5,
            "created": 5,
            "updated": 0,
            "bytes": 4096,
            "truncated": False,
        },
        # 本地编辑面（D）：形状与 host/session.py 的 audit.record 调用点一致
        "edit.complete": {
            "event": "edit.complete",
            "path": "src/main.py",
            "prefix_digest": "1" * 16,
            "suffix_digest": "2" * 16,
            "chars": 42,
            "backend": "fim",
        },
        "edit.rewrite.preview": {
            "event": "edit.rewrite.preview",
            "path": "src/main.py",
            "start": 12,
            "end": 18,
            "hunks": 3,
            "selection_digest": "3" * 16,
            "chars": 260,
        },
        "edit.rewrite.apply": {
            "event": "edit.rewrite.apply",
            "path": "src/main.py",
            "strategy": "hunk-subset",
            "applied": 2,
            "total": 3,
            "added": 5,
            "removed": 1,
        },
        "edit.save": {
            "event": "edit.save",
            "path": "src/main.py",
            "strategy": "overwrite",
            "added": 7,
            "removed": 2,
            "chars": 1200,
        },
        "tool.run_command": {
            "event": "tool.run_command",
            "command": "pytest -q tests/",
            "executable": "pytest",
            "risk": "low",
            "exit_code": 0,
            "stdout_bytes": 900,
            "stderr_bytes": 0,
        },
        # 语言服务器（E）：形状与 host/session.py、tools/diagnostics.py 的调用点一致
        "tool.lsp_diagnostics": {
            "event": "tool.lsp_diagnostics",
            "path": "src/main.py",
            "diagnostics": 12,
            "error_count": 3,
            "warning_count": 9,
            "server": "pyright-langserver",
            "truncated": False,
        },
        "lsp.rename.preview": {
            "event": "lsp.rename.preview",
            "path": "src/main.py",
            "line": 12,
            "char": 8,
            "files": 2,
            "edits": 5,
            "new_name_digest": "f" * 16,
            "new_name_chars": 14,
        },
        "lsp.rename.apply": {
            "event": "lsp.rename.apply",
            "path": "src/main.py",
            "edits": 3,
            "added": 3,
            "removed": 3,
            "strategy": "lsp-rename",
        },
        # MCP 客户端（L/P1-6）：形状与 mcp/manager.py、mcp/registry.py 的调用点一致。
        # 参数正文（args_text）只留本机，上传的是它的 digest。
        "mcp.server_start": {
            "event": "mcp.server_start",
            "server": "fs",
            "transport": "stdio",
            "ok": True,
            "tools": 3,
            "ms": 120,
        },
        "mcp.call": {
            "event": "mcp.call",
            "server": "fs",
            "tool": "read_file",
            "ok": True,
            "argc": 2,
            "args_text": '{"path": "src/main.py"}',
            "ms": 35,
        },
        # 计划视图（P2-12）：形状与 host/session.py 的 plan_toggle 调用点一致
        "plan.toggle": {
            "event": "plan.toggle",
            "id": "2",
            "status": "done",
            "steps_count": 4,
            "revision": 3,
        },
        # 多 Agent 编排（M3）：形状与 loop/subagent.py 的 subagent.run 调用点一致。
        # 只记计数与任务指纹；子 Agent 的任务/摘要正文不上传。
        "subagent.run": {
            "event": "subagent.run",
            "ok": True,
            "iterations": 3,
            "tool_calls": 5,
            "answer_chars": 480,
            "task_digest": "a1b2c3d4e5f6a7b8",
        },
        # 测试树（P2-14）：形状与 host/session.py 的 _run_tests 调用点一致。
        # 只记计数与**首个**失败的位置；失败正文（messageHead）不上传。
        "test.run": {
            "event": "test.run",
            "scope": "subset",
            "requested": 3,
            "passed": 2,
            "failed": 1,
            "skipped": 0,
            "ms": 1420,
            "file": "tests/test_x.py",
            "line": 42,
        },
        # 多文件原子事务（P1-8）：形状与 tools/multi_edit.py 的审计调用点一致
        "edit.transaction": {
            "event": "edit.transaction",
            "files_count": 3,
            "changed_count": 3,
            "added": 12,
            "removed": 5,
            "strategy": "atomic",
            "rolled_back": False,
        },
        # 交互式终端（M/P1-7）：形状与 host/session.py 的 term_open/term_close
        # 调用点一致。命令正文（argv）在本地算成 argv_digest，只上传指纹；
        # **逐次键入不记**，所以这里只有 open/close 两条。
        "term.open": {
            "event": "term.open",
            "termId": "term_1",
            "argc": 2,
            "argv_digest": "c" * 16,
            "cols": 80,
            "rows": 24,
            "mode": "pty",
            "ms": 8,
        },
        "term.close": {
            "event": "term.close",
            "termId": "term_1",
            "argc": 0,
            "argv_digest": "",
            "cols": 0,
            "rows": 0,
            "mode": "pty",
            "exit_code": 0,
            "ms": 3,
        },
        # 多模态图片上传（N/P1-10）：形状与 host/session.py 的 upload_image
        # 调用点一致。图片正文（base64/blob）只落本机，**上传的只有元数据**。
        "image.upload": {
            "event": "image.upload",
            "bytes": 20480,
            "ext": ".png",
            "digest": "d" * 16,
        },
        # 网页自检（O/P1-11）：形状与 host/session.py 的 visual_check 调用点一致。
        # URL 在本地算成 url_digest，差异只上传千分比整数；**截图内容不上传**。
        "visual.check": {
            "event": "visual.check",
            "url_digest": "f" * 16,
            "width": 1280,
            "height": 800,
            "diff_ratio": 125,
            "baseline_missing": False,
            "ms": 12,
        },
        "tool.unknown": {"event": "tool.unknown", "tool": "not_a_tool"},
        "tool.preflight_failed": {
            "event": "tool.preflight_failed",
            "tool": "edit_file",
            "reason": "未在文件中找到 search 片段",
        },
        "approval.decision": {
            "event": "approval.decision",
            "tool": "write_file",
            "decision": "approve",
            "reason": "影响范围可控，已复核 diff",
            "arguments_digest": "e" * 16,
        },
        "approval.request": {
            "event": "approval.request",
            "tool": "write_file",
            "risk": "review",
            "reason": "修改 src/main.py",
        },
        "approval.denied_by_policy": {
            "event": "approval.denied_by_policy",
            "tool": "run_command",
            "reason": "策略禁止访问生产环境",
        },
        "approval.no_ui": {
            "event": "approval.no_ui",
            "tool": "write_file",
            "decision": "deny",
        },
        "approval.timeout": {
            "event": "approval.timeout",
            "tool": "write_file",
            "timeout_s": 120,
        },
        "agent.run.finish": {
            "event": "agent.run.finish",
            "iterations": 5,
            "tool_calls": 9,
            "answer_chars": 340,
            "answer_digest": "f" * 16,
        },
        "agent.run.abort": {
            "event": "agent.run.abort",
            "reason": "consecutive_tool_errors",
        },
        "agent.run.exhausted": {"event": "agent.run.exhausted", "iterations": 20},
        "agent.run.interrupted": {
            "event": "agent.run.interrupted",
            "iteration": 3,
            "tool_calls": 4,
        },
        "agent.run.failed": {
            "event": "agent.run.failed",
            "kind": "llm_error",
            "error": "Connection refused: http://127.0.0.1:11434",
        },
        "session.start": {
            "event": "session.start",
            "workspace": "proj",
            "model": "qwen2.5-coder:7b",
            "provider": "ollama",
        },
        "session.end": {
            "event": "session.end",
            "reason": "exit",
            "iterations": 4,
            "duration_ms": 121_000,
        },
        "verify.run": {
            "event": "verify.run",
            "command": "pytest -q tests/",
            "executable": "pytest",
            "exit_code": 0,
            "output_chars": 812,
        },
        "context.compact": {
            "event": "context.compact",
            "compacted_messages": 6,
            "summary_chars": 318,
        },
        "tool.error": {
            "event": "tool.error",
            "tool": "edit_file",
            "kind": "KeyError",
            "detail_chars": 180,
        },
        "sandbox.block": {
            "event": "sandbox.block",
            "kind": "sensitive",
            "tool": "read_file",
            "path": ".env",
            "reason": "文件被策略标记为敏感，拒绝读入上下文：.env",
        },
        "sandbox.deny_command": {
            "event": "sandbox.deny_command",
            "tool": "run_command",
            "executable": "sudo",
            "risk": "deny",
            "reason": "命令被安全策略拒绝：sudo 在硬拒绝清单中",
        },
        "rules.load": {
            "event": "rules.load",
            "layers": 3,
            "chars": 4200,
            "truncated_count": 0,
        },
        "memory.add": {
            "event": "memory.add",
            "scope": "agent",
            "chars": 96,
            "digest": "b" * 16,
        },
        "memory.inject": {
            "event": "memory.inject",
            "count": 3,
            "chars": 280,
        },
    }


if __name__ == "__main__":
    unittest.main()
