"""M4：进程内指标与成本统计。

## 为什么这组必须有测试

这两个功能的失败模式都是「**有数，但数不对**」，而数不对不会报错：

1. **指标接线在 HTTP 层**（`VibeHandler._dispatch`）。只测 `Metrics` 类只能
   证明「计数逻辑对」，证明不了请求真的走到它 —— 漏记比记错更隐蔽。
   更要紧的是**路由模板映射**：若把 `/devices/xxx/revoke` 原样当标签，
   标签基数会被路径参数撑爆，先崩的是监控系统而不是应用。
2. **用量汇总的作用域校验**漏一条，就是「任何登录用户能看全公司开销」。
   而这是**读**接口：越权读不会留下任何异常，只会留下一份没人知道的报表。
3. **`days` 是客户端可控的查询参数**，没有上限等于让人用一个 URL
   把整张 `usage_counters` 拉进内存。

所以下面三类断言分别钉：Prometheus 文本形状、真实请求驱动计数、
汇总的作用域与截断。
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest import mock

from vibe_server import __version__, rbac
from vibe_server import metrics as metrics_module
from vibe_server.errors import Forbidden
from vibe_server.gateway import quota as quota_module
from vibe_server.gateway.quota import MAX_SUMMARY_DAYS
from vibe_server.httpd import AUTH_PUBLIC, AUTH_REQUIRED
from vibe_server.metrics import Metrics, render_prometheus

from tests.support import TempDirTestCase, frozen, http_get, owner_actor, serve_app


# --------------------------------------------------------------------------- 渲染
class PrometheusRenderTests(unittest.TestCase):
    """文本格式的形状：抓取方能解析、告警规则能直接写。"""

    def _render(self, metrics: Metrics | None = None, *, version: str = "0.1.0") -> str:
        source = metrics or Metrics()
        return render_prometheus(
            source.snapshot(), version=version, uptime_seconds=7, store_ok=True
        )

    def test_format_is_shaped_for_a_scrape(self) -> None:
        text = self._render()
        self.assertIn("# TYPE vibe_requests_total counter", text)
        self.assertIn('vibe_build_info{version="0.1.0"} 1', text)
        self.assertIn("vibe_uptime_seconds 7", text)
        self.assertIn("vibe_store_up 1", text)
        # 末尾换行：某些抓取器按行解析，最后一行没有换行会被吞
        self.assertTrue(text.endswith("\n"))

    def test_store_down_is_reported_as_zero(self) -> None:
        text = render_prometheus(
            Metrics().snapshot(), version="0.1.0", uptime_seconds=1, store_ok=False
        )
        self.assertIn("vibe_store_up 0", text)

    def test_quota_rejections_are_taken_from_the_429_bucket(self) -> None:
        """429 单独给一条指标：否则告警规则得自己从状态码维度里挑，容易漏。"""
        m = Metrics()
        m.observe(method="POST", template="/api/v1/gateway/chat", status=429, elapsed_ms=3)
        m.observe(method="POST", template="/api/v1/gateway/chat", status=200, elapsed_ms=3)
        text = self._render(m)
        self.assertIn("vibe_quota_rejections_total 1", text)
        self.assertIn('vibe_requests_total{code="429"} 1', text)

    def test_unmatched_requests_collapse_into_one_label(self) -> None:
        """404/405 没有路由模板，必须落到同一个标签上，不能各占一个。"""
        m = Metrics()
        for _ in range(3):
            m.observe(method="GET", template="", status=404, elapsed_ms=1)
        text = self._render(m)
        self.assertIn('vibe_route_requests_total{method="GET",route="(unmatched)"} 3', text)

    def test_label_overflow_is_collapsed_and_flagged(self) -> None:
        """超过上限的新标签归到 `(other)`，并把自己标出来。

        上限存在的意义就是「宁可少几个标签，也不能让标签集合无界增长」——
        溢出时静默改名很难查，所以额外给一条 gauge。
        """
        m = Metrics()
        for index in range(metrics_module._MAX_ROUTES + 5):
            m.observe(method="GET", template=f"/r{index}", status=200, elapsed_ms=1)
        text = self._render(m)
        self.assertIn('route="(other)"', text)
        self.assertIn("vibe_metrics_label_overflow 1", text)

    def test_label_values_with_quotes_or_newlines_are_escaped(self) -> None:
        """版本号来自外部（构建注入），带引号或换行会把整个文本格式弄坏。"""
        text = self._render(version='1.0"bad\nline')
        self.assertIn('version="1.0\\"bad\\nline"', text)
        # 转义之后不该真的有换行落在标签里
        self.assertNotIn("bad\nline", text.replace('\\n', ""))


# --------------------------------------------------------------------------- HTTP
class MetricsOverHttpTests(TempDirTestCase):
    """指标由**真实请求**驱动 —— 手搓快照证明不了接线是通的。"""

    def setUp(self) -> None:
        super().setUp()
        self.app = self.make_app()
        self.base = serve_app(self, self.app)

    def _get(self, path: str):
        return http_get(self.base, path)

    def test_metrics_endpoint_is_public_and_prometheus_shaped(self) -> None:
        """不带 Authorization 也能爬：要求认证的结果往往是「改用管理员令牌去爬」。"""
        status, body, headers = self._get("/metrics")
        self.assertEqual(status, 200)
        self.assertIn("text/plain", headers.get("Content-Type") or "")
        self.assertIn(f'vibe_build_info{{version="{__version__}"}} 1', body)
        self.assertIn("vibe_store_up 1", body)
        # 假时钟不前进，所以 uptime 恰好 0。若是负数，说明起点用的是别的
        # 时间源（真实 time.time()）而不是注入的时钟
        self.assertIn("vibe_uptime_seconds 0", body)

    def test_served_requests_show_up_in_the_next_scrape(self) -> None:
        self._get("/healthz")
        self._get("/definitely-not-a-route")
        _, body, _ = self._get("/metrics")
        self.assertIn('vibe_requests_total{code="200"}', body)
        self.assertIn('vibe_requests_total{code="404"}', body)
        # 模板映射生效的证据：路径不认识 → 归到同一个标签
        self.assertIn('vibe_route_requests_total{method="GET",route="(unmatched)"} 1', body)
        self.assertIn('route="/healthz"', body)

    def test_scrapes_count_themselves_on_the_following_scrape(self) -> None:
        """指标在响应**发出之后**才记，所以第一次抓取看不到自己。

        这不是缺陷而是顺序：`observe` 不该影响这次请求本身的成败。
        第二次抓取能看到它，正说明「统计不改变被统计的东西」。
        """
        _, first, _ = self._get("/metrics")
        self.assertNotIn('route="/metrics"', first)
        _, second, _ = self._get("/metrics")
        self.assertIn('vibe_route_requests_total{method="GET",route="/metrics"} 1', second)


# --------------------------------------------------------------------------- 汇总
class UsageSummaryTests(TempDirTestCase):
    """按天汇总：作用域、窗口上限、截断标记。"""

    def setUp(self) -> None:
        super().setUp()
        self.clock = frozen(1_700_000_000)
        self.app = self.make_app(clock=self.clock)
        self.quota = self.app.quota_service
        self.store = self.app.store
        self._owner = None

    @property
    def owner(self):
        """惰性构造组织所有者（首次调用才付 scrypt 口令哈希的代价）。"""
        if self._owner is None:
            self._owner = owner_actor(self.app)
        return self._owner

    def _spend(
        self,
        *,
        day: str,
        user_id: str,
        team_id: str = "",
        cloud: int = 0,
        local: int = 0,
        counted: bool = True,
    ) -> None:
        """往某一天的账上记一笔。

        直接调存储层的 `settle_usage`（`estimate=0`）：这一组测的是**汇总**，
        走一遍 reserve→settle 只会把预留逻辑也拖进被测面。
        """
        self.store.settle_usage(
            day,
            user_id,
            team_id,
            estimate=0,
            actual_tokens=cloud or local,
            is_cloud=bool(cloud),
            counted_request=counted,
            now=self.clock.now(),
        )

    # ------------------------------------------------------------------ 基本形状
    def test_empty_window_reports_zeroes_not_a_missing_field(self) -> None:
        result = self.quota.usage_summary(actor=self.owner, days=7)
        self.assertEqual(result["series"], [])
        self.assertEqual(
            result["totals"], {"cloudTokens": 0, "localTokens": 0, "requests": 0}
        )
        self.assertFalse(result["truncated"])
        self.assertEqual(result["days"], 7)
        self.assertEqual(result["scope"], {"userId": self.owner.user_id, "teamId": ""})

    def test_days_are_aggregated_in_order_and_summed(self) -> None:
        day_one = self.clock.today()
        self._spend(day=day_one, user_id=self.owner.user_id, cloud=100)
        self.clock.advance(86400)
        day_two = self.clock.today()
        self._spend(day=day_two, user_id=self.owner.user_id, cloud=250)
        # 本地推理不吃云额度，也不计请求数
        self._spend(day=day_two, user_id=self.owner.user_id, local=50, counted=False)

        result = self.quota.usage_summary(actor=self.owner, days=30)
        self.assertEqual([item["day"] for item in result["series"]], [day_one, day_two])
        self.assertEqual(
            result["totals"], {"cloudTokens": 350, "localTokens": 50, "requests": 2}
        )
        self.assertEqual(result["untilDay"], day_two)

    def test_window_is_bounded_by_days(self) -> None:
        """窗口外的账不能算进来 —— 否则「近 7 天」会包含上月。"""
        old_day = self.clock.today()
        self._spend(day=old_day, user_id=self.owner.user_id, cloud=999)
        self.clock.advance(86400 * 40)
        self._spend(day=self.clock.today(), user_id=self.owner.user_id, cloud=1)

        result = self.quota.usage_summary(actor=self.owner, days=7)
        self.assertEqual(result["totals"]["cloudTokens"], 1)
        self.assertNotIn(old_day, [item["day"] for item in result["series"]])

    # ------------------------------------------------------------------ 窗口上限
    def test_days_is_clamped_to_the_maximum(self) -> None:
        result = self.quota.usage_summary(actor=self.owner, days=100000)
        self.assertEqual(result["days"], MAX_SUMMARY_DAYS)
        # 只回显被 clamp 的数字不算数：窗口本身也不能跟着放大
        expected_since = (
            date.fromisoformat(result["untilDay"]) - timedelta(days=MAX_SUMMARY_DAYS - 1)
        ).isoformat()
        self.assertEqual(result["sinceDay"], expected_since)

    def test_days_below_one_becomes_a_single_day(self) -> None:
        for raw in (0, -3):
            with self.subTest(days=raw):
                result = self.quota.usage_summary(actor=self.owner, days=raw)
                self.assertEqual(result["days"], 1)
                self.assertEqual(result["sinceDay"], result["untilDay"])

    def test_truncation_is_reported_rather_than_hidden(self) -> None:
        """取满上限时必须自报截断：少报几天比不报危险得多。

        这里把「每天最多取多少行」压到 1，用同一天的两条记录逼出截断 ——
        真按 500 行去构造，测的是 SQLite 的 LIMIT，不是这里的语义。
        """
        day = self.clock.today()
        self._spend(day=day, user_id="u-a", team_id="t1", cloud=10)
        self._spend(day=day, user_id="u-b", team_id="t1", cloud=20)

        full = self.quota.usage_summary(actor=self.owner, team_id="t1", days=1)
        self.assertEqual(full["totals"]["cloudTokens"], 30)
        self.assertFalse(full["truncated"])

        with mock.patch.object(quota_module, "_MAX_ROWS_PER_DAY", 1):
            clipped = self.quota.usage_summary(actor=self.owner, team_id="t1", days=1)
        self.assertTrue(clipped["truncated"])
        self.assertLess(clipped["totals"]["cloudTokens"], 30)

    # ------------------------------------------------------------------ 作用域
    def test_team_scope_keeps_only_that_team(self) -> None:
        day = self.clock.today()
        self._spend(day=day, user_id="u-a", team_id="t1", cloud=10)
        self._spend(day=day, user_id="u-b", team_id="t2", cloud=20)

        result = self.quota.usage_summary(actor=self.owner, team_id="t1", days=7)
        self.assertEqual(result["totals"]["cloudTokens"], 10)
        self.assertEqual(result["scope"], {"userId": "", "teamId": "t1"})

    def test_another_users_usage_needs_a_shared_scope(self) -> None:
        outsider = rbac.build_actor(
            user_id="u-out", email="o@x", display_name="O", memberships=[]
        )
        with self.assertRaises(Forbidden):
            self.quota.usage_summary(actor=outsider, user_id="u-someone-else", days=7)

    def test_org_wide_actor_may_read_any_user(self) -> None:
        day = self.clock.today()
        self._spend(day=day, user_id="u-a", cloud=5)
        result = self.quota.usage_summary(actor=self.owner, user_id="u-a", days=7)
        self.assertEqual(result["totals"]["cloudTokens"], 5)

    def test_another_teams_usage_is_refused(self) -> None:
        outsider = rbac.build_actor(
            user_id="u-out",
            email="o@x",
            display_name="O",
            memberships=[
                {"team_id": "t-other", "team_slug": "o", "team_name": "O", "role": rbac.ROLE_TEAM_ADMIN}
            ],
        )
        with self.assertRaises(Forbidden):
            self.quota.usage_summary(actor=outsider, team_id="t1", days=7)


# --------------------------------------------------------------------------- 声明
class EndpointDeclarationTests(TempDirTestCase):
    """两个新端点的**路由级**属性。

    这类声明漏了不会报错：少一个 `permissions` 就是少一道闸门，
    少一个 `auth=AUTH_PUBLIC` 就是监控再也爬不到。所以用断言钉住，
    而不是靠 review 时记得看一眼。
    """

    def setUp(self) -> None:
        super().setUp()
        self.app = self.make_app()

    def test_usage_route_requires_usage_read(self) -> None:
        matched = self.app.router.resolve("GET", "/api/v1/usage")
        self.assertIsNotNone(matched, "未注册 /api/v1/usage")
        route = matched[0]
        self.assertEqual(route.permissions, (rbac.PERM_USAGE_READ,))
        self.assertEqual(route.auth, AUTH_REQUIRED)

    def test_metrics_route_is_public(self) -> None:
        matched = self.app.router.resolve("GET", "/metrics")
        self.assertIsNotNone(matched, "未注册 /metrics")
        route = matched[0]
        self.assertEqual(route.auth, AUTH_PUBLIC)
        self.assertEqual(route.permissions, ())


if __name__ == "__main__":
    unittest.main()