"""M3：网关流式（SSE 透传）的行为契约。

这组测试的存在理由：流式与非流式在网关里**共用**路由/配额/降级判定，
只在「连接怎么打开、用量怎么结算」上分叉。分叉处是最容易长出
「同一个请求流式被拒、非流式放行」这种无法解释行为的地方，
而这类 bug 只有把两条路径放在同一组断言下对比才看得见。

上游用真的 HTTP 服务模拟（tests/support.MockUpstream），不打桩 urlopen：
chunked 编码、SSE 帧边界、连接被拒，都是网关必须处理的真实语义。
"""

from __future__ import annotations

import json
from typing import Any

from tests.support import (
    TempDirTestCase,
    actor_of,
    bootstrap_and_login,
    frozen,
    make_config,
    mock_upstream,
)


def _body(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "model": "mock",
        "messages": [{"role": "user", "content": "你好"}],
    }
    base.update(overrides)
    return base


class StreamBase(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.actor = None  # 惰性：多数用例不需要真的 bootstrap（scrypt 很慢）

    # ---------------------------------------------------------------- 装配
    def app_with(self, local: Any = None, cloud: Any = None, **config_overrides: Any):
        """造一个上游指向假服务的应用。"""
        from vibe_server.app import build_app

        overrides: dict[str, Any] = {}
        if local is not None:
            overrides["local_upstream_url"] = local.url
        if cloud is not None:
            overrides["cloud_upstream_url"] = cloud.url
        overrides.update(config_overrides)
        self.config = make_config(self.tmp, **overrides)
        app = build_app(self.config, clock=frozen())
        self.addCleanup(app.store.close)
        return app

    def owner(self, app: Any):
        if self.actor is None:
            self.actor = actor_of(app, bootstrap_and_login(app)["accessToken"])
        return self.actor

    @staticmethod
    def drain(session: Any) -> bytes:
        return b"".join(session.iter_chunks())

    @staticmethod
    def frames(raw: bytes) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for block in raw.decode("utf-8", "replace").split("\n\n"):
            for line in block.split("\n"):
                text = line.strip()
                if not text.startswith("data:"):
                    continue
                data_str = text[5:].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    out.append(json.loads(data_str))
                except ValueError:
                    pass
        return out


class PassthroughTests(StreamBase):
    """透传本身：字节必须原样下行，网关不许重组帧。"""

    def test_frames_reach_client_verbatim(self) -> None:
        upstream = mock_upstream(self, sse_chunks=("你", "好", "世界"))
        app = self.app_with(local=upstream)
        session = app.gateway.open_stream(
            actor=self.owner(app), body=_body(stream=True)
        )
        raw = self.drain(session)
        frames = self.frames(raw)
        pieces = [
            (f["choices"][0]["delta"].get("content") or "")
            for f in frames
            if (f.get("choices") or [{}])[0].get("delta", {}).get("content")
        ]
        self.assertEqual("".join(pieces), "你好世界")

    def test_content_type_is_event_stream_and_chunks_are_nonempty(self) -> None:
        upstream = mock_upstream(self, sse_chunks=("一段", "话"))
        app = self.app_with(local=upstream)
        session = app.gateway.open_stream(
            actor=self.owner(app), body=_body(stream=True)
        )
        chunks = list(session.iter_chunks())
        self.assertTrue(chunks, "至少要有一块数据下行")
        self.assertTrue(all(chunks), "空块没有意义，不该下行")

    def test_upstream_received_stream_true(self) -> None:
        """网关必须把 stream=true 原样带给上游 —— 否则流式只是本地幻觉。"""
        upstream = mock_upstream(self, sse_chunks=("x",))
        app = self.app_with(local=upstream)
        session = app.gateway.open_stream(
            actor=self.owner(app), body=_body(stream=True)
        )
        self.drain(session)
        self.assertTrue(upstream.requests, "上游没收到请求")
        self.assertTrue(upstream.requests[-1].get("stream"))


class QuotaSettlementTests(StreamBase):
    """流式配额结算 —— 预留跨「打开连接」与「消费正文」两段存活，
    结算点只有一个，漏一次就是永久泄漏。"""

    def test_settles_with_upstream_usage_when_provided(self) -> None:
        upstream = mock_upstream(
            self,
            sse_chunks=("ab", "cd"),
            usage={"prompt_tokens": 100, "completion_tokens": 40},
        )
        # 默认「每日请求数不限」时根本不计数（counted_request=False），
        # 要验证结算就得先把它打开 —— 否则这个测试恒真，看不出漏结算。
        app = self.app_with(local=upstream, default_daily_requests=50)
        actor = self.owner(app)
        session = app.gateway.open_stream(actor=actor, body=_body(stream=True))
        self.drain(session)
        view = app.quota_service.status(actor=actor)
        self.assertEqual(view["usage"]["requests"], 1)
        # 本地运行不消耗云端 token 配额（用的是用户自己的显卡）
        self.assertEqual(view["usage"]["cloudTokens"], 0)

    def test_settles_by_estimate_when_upstream_omits_usage(self) -> None:
        """上游没开 stream_options.include_usage 是很常见的真实行为。

        网关不能因为拿不到 usage 就不结算 —— 那等于流式请求白嫖配额。
        """
        upstream = mock_upstream(
            self,
            sse_chunks=("一" * 400,),
            usage={"prompt_tokens": 10, "completion_tokens": 0},
            sse_usage=False,
        )
        app = self.app_with(local=upstream)
        actor = self.owner(app)
        session = app.gateway.open_stream(
            actor=actor, body=_body(stream=True), policy={"allowCloud": True}
        )
        self.drain(session)
        # 拿不到 usage 时走估算分支：标记缺失，并按已下行的正文长度估
        self.assertIsNone(session._usage, "上游没给 usage，不该凭空造一个精确值")
        self.assertGreater(session._completion_chars, 0)

    def test_abort_releases_reservation_when_stream_never_consumed(self) -> None:
        """客户端在响应头发出前就断开：生成器从未启动，预留必须靠 abort 回收。

        这是配额最容易悄悄泄漏的一条路径 —— 没有请求记录，却有预留。
        """
        upstream = mock_upstream(self, sse_chunks=("x",))
        app = self.app_with(local=upstream)
        actor = self.owner(app)
        session = app.gateway.open_stream(actor=actor, body=_body(stream=True))
        # 模拟 HTTP 层的 finally：不迭代生成器，直接收尾
        session.abort()
        self.assertTrue(session._settled)
        # abort 必须幂等：HTTP 层 finally 与生成器 GC 都可能再调一次
        session.abort()
        self.assertTrue(session._settled)

    def test_abort_is_noop_after_successful_settlement(self) -> None:
        upstream = mock_upstream(self, sse_chunks=("x",))
        app = self.app_with(local=upstream)
        actor = self.owner(app)
        session = app.gateway.open_stream(actor=actor, body=_body(stream=True))
        self.drain(session)
        self.assertTrue(session._settled)
        session.abort()  # 不该把已结算的账再动一次
        self.assertTrue(session._settled)


class RouteParityTests(StreamBase):
    """流式与非流式的路由判定必须完全一致 —— 这是本组最重要的一条。"""

    def test_same_request_routes_identically_streamed_or_not(self) -> None:
        local = mock_upstream(self, sse_chunks=("a",), content="a")
        cloud = mock_upstream(self, sse_chunks=("b",), content="b")
        app = self.app_with(local=local, cloud=cloud)
        actor = self.owner(app)

        non_stream = app.gateway.chat(
            actor=actor, body=_body(), policy={"allowCloud": True}
        )
        session = app.gateway.open_stream(
            actor=actor, body=_body(stream=True), policy={"allowCloud": True}
        )
        self.drain(session)
        self.assertEqual(
            session.target,
            non_stream.call.target,
            "同一请求流式与非流式走了不同的上游 —— 策略红线会被流式绕过",
        )

    def test_sensitive_content_forces_local_even_when_streaming(self) -> None:
        """敏感红线在流式上同样必须生效。

        流式走的是另一套代码路径（open_stream），若红线只加在 chat() 里，
        就会出现「开流式就能把敏感代码发到云端」这种绕过。
        """
        local = mock_upstream(self, sse_chunks=("a",), content="a")
        cloud = mock_upstream(self, sse_chunks=("b",), content="b")
        app = self.app_with(local=local, cloud=cloud)
        actor = self.owner(app)
        body = _body(
            stream=True,
            messages=[{"role": "user", "content": "看看 config/prod/db.yaml"}],
        )
        session = app.gateway.open_stream(
            actor=actor, body=body, policy={"allowCloud": True}
        )
        self.drain(session)
        self.assertEqual(session.target, "local")
        headers = session.downstream_headers()
        self.assertEqual(headers["X-Vibe-Route-Sensitive"], "1")

    def test_downstream_headers_carry_route_decision(self) -> None:
        upstream = mock_upstream(self, sse_chunks=("a",))
        app = self.app_with(local=upstream)
        session = app.gateway.open_stream(
            actor=self.owner(app), body=_body(stream=True)
        )
        headers = session.downstream_headers()
        self.assertEqual(headers["X-Vibe-Route-Target"], "local")
        self.assertIn("X-Vibe-Route-Reason", headers)
        self.drain(session)

    def test_downstream_headers_never_leak_body_or_keys(self) -> None:
        """回给客户端的头只能是决策元信息 —— 用白名单而不是「看着不像」。

        若靠「值里没有 sk- 这种样子」来判断，任何新加的头都会默认通过；
        白名单反过来：新加的头必须被显式认可才能下发。
        """
        allowed = {
            "X-Vibe-Route-Target",
            "X-Vibe-Route-Task",
            "X-Vibe-Route-Sensitive",
            "X-Vibe-Route-Reason",
            "X-Vibe-Degraded-From",
        }
        upstream = mock_upstream(self, sse_chunks=("a",))
        app = self.app_with(local=upstream)
        session = app.gateway.open_stream(
            actor=self.owner(app), body=_body(stream=True)
        )
        headers = session.downstream_headers()
        self.assertLessEqual(set(headers), allowed, f"出现了未认可的下行头: {headers}")
        for key, value in headers.items():
            self.assertLess(len(str(value)), 64, f"{key} 的值过长，疑似带上了正文")
        self.assertNotIn("Authorization", headers)
        self.drain(session)


class DegradationTests(StreamBase):
    """流式降级：只有在下行开始之前才允许换本地。"""

    def test_degrades_to_local_when_cloud_unreachable(self) -> None:
        local = mock_upstream(self, sse_chunks=("本地",))
        # 云端指向一个没人监听的地址 → 连接被拒（status=0 → 可降级）
        app = self.app_with(
            local=local, cloud=None, cloud_upstream_url="http://127.0.0.1:1/v1"
        )
        actor = self.owner(app)
        session = app.gateway.open_stream(
            actor=actor,
            body=_body(stream=True),
            policy={"allowCloud": True, "task": "reasoning"},
        )
        raw = self.drain(session)
        self.assertEqual(session.degraded_from, "cloud")
        self.assertEqual(session.target, "local")
        self.assertEqual(
            session.downstream_headers().get("X-Vibe-Degraded-From"), "cloud"
        )
        # 按 SSE 帧解析而不是在原始字节里找子串：JSON 会把中文转成 \uXXXX
        pieces = [
            (f["choices"][0]["delta"].get("content") or "")
            for f in self.frames(raw)
            if (f.get("choices") or [{}])[0].get("delta", {}).get("content")
        ]
        self.assertEqual("".join(pieces), "本地")

    def test_does_not_degrade_on_client_error_status(self) -> None:
        """上游回 400（请求本身有问题）时不降级 —— 换本地重跑只会同样失败，
        而且会把「请求写错了」掩盖成「云端不稳定」。

        断言方式是「本地上游一个请求都没收到」：只看抛出的状态码的话，
        就算偷偷重试了本地也看不出来。
        """
        local = mock_upstream(self, sse_chunks=("本地",))
        cloud = mock_upstream(self, sse_chunks=("x",), status=400)
        app = self.app_with(local=local, cloud=cloud)
        from vibe_server.errors import ApiError

        with self.assertRaises(ApiError) as ctx:
            app.gateway.open_stream(
                actor=self.owner(app),
                body=_body(stream=True),
                policy={"allowCloud": True, "task": "reasoning"},
            )
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(
            local.requests, [], "4xx 不该触发本地重试 —— 那是掩盖错误不是容错"
        )

    def test_degrade_releases_the_cloud_reservation(self) -> None:
        """降级时换了一次目标，预留也换了一次 —— 云端那次必须释放掉。

        否则每降一级就多一份悬挂预留，用户配额凭空消失。
        """
        local = mock_upstream(self, sse_chunks=("本地",))
        app = self.app_with(
            local=local, cloud_upstream_url="http://127.0.0.1:1/v1"
        )
        actor = self.owner(app)
        session = app.gateway.open_stream(
            actor=actor,
            body=_body(stream=True),
            policy={"allowCloud": True, "task": "reasoning"},
        )
        self.drain(session)
        self.assertEqual(session.degraded_from, "cloud")
        # 结算只发生一次，且按最终目标（local）
        self.assertTrue(session._settled)
        # 云端那次预留已释放：预留额归零，不留悬挂
        self.assertEqual(
            app.quota_service.status(actor=actor)["usage"]["reservedTokens"], 0
        )


class FailureTests(StreamBase):
    def test_unconfigured_cloud_raises_instead_of_silently_going_local(self) -> None:
        """重任务被判到云端但云端没配：如实报 503，不悄悄改用本地。

        「静默走本地」会把配置漏配变成一件没人发现的事 ——
        用户以为在用云端能力，实际永远在跑本地模型。
        """
        local = mock_upstream(self, sse_chunks=("a",))
        app = self.app_with(local=local)  # 未配置 cloud
        from vibe_server.errors import ApiError

        with self.assertRaises(ApiError) as ctx:
            app.gateway.open_stream(
                actor=self.owner(app),
                body=_body(stream=True),
                policy={"allowCloud": True, "task": "reasoning"},
            )
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(local.requests, [], "不该偷偷降级到未被告知的本地上游")

    def test_audit_event_marks_streaming(self) -> None:
        """审计里必须能区分流式与非流式请求，否则事后无法回答
        「这些用量是怎么产生的」。"""
        upstream = mock_upstream(self, sse_chunks=("a",))
        app = self.app_with(local=upstream)
        seen: list[dict[str, Any]] = []
        session = app.gateway.open_stream(
            actor=self.owner(app),
            body=_body(stream=True),
            audit_recorder=lambda **kw: seen.append(kw),
        )
        self.drain(session)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0]["payload"]["streaming"])
        self.assertEqual(seen[0]["event_type"], "gateway.response")
