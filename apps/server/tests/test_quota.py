"""配额与限流：预留-结算、TOCTOU、跨天、限流、作用域。

## 为什么这组必须有测试

配额的失败模式是**超额**，而超额不会报错 —— 它只是让「今天最多花这么多」
这句话变成空话。这里每一条断言都在钉住一个具体的失败：

1. **TOCTOU**：查剩余 → 判断 → 扣减之间的间隙，并发请求会一起看到「够」。
   所以这里不测「累加对不对」，而是**放 20 个线程去抢**，验证成功率恰好等于容量。
2. **跨天**：不测就永远不知道额度是不是真的在零点重置。
   用可注入时钟把「跨天」压进毫秒。
3. **本地请求不吃 token 配额**：吃的话等于「不许你用自己的显卡」。
4. **release 必须存在**：少了它，一次上游挂掉会永久占掉一段额度，
   用户看到「额度没了」但审计里一条调用都没有。
"""

from __future__ import annotations

import threading
import unittest

from vibe_server.errors import BadRequest, QuotaExceeded
from vibe_server.gateway.quota import QuotaService

from tests.support import TempDirTestCase, frozen, owner_actor


class QuotaTestCase(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.clock = frozen(1_700_000_000)
        self.app = self.make_app(clock=self.clock)
        self.quota = self.app.quota_service
        self.store = self.app.store
        # 大多数用例只用 user_id 当一个**记账主键**，不需要一个真的登录主体。
        # 建主体要走一次 scrypt 口令哈希（~0.3s/次），几十个测试叠起来就是十几秒，
        # 所以只在真正需要 actor 的用例里惰性建（见 self.owner）。
        self.user_id = "user-1"
        self._owner = None

    @property
    def owner(self):
        """惰性构造一个组织所有者主体（首次调用时才付 scrypt 的代价）。"""
        if self._owner is None:
            self._owner = owner_actor(self.app)
        return self._owner

    # ------------------------------------------------------------------ 夹具
    def set_global(self, *, tokens: int = 0, requests: int = 0, rpm: int = 0) -> None:
        """写一条全局配额策略。

        默认值全 0 = 不限。0 在这里是「不限」而不是「一个都不许」——
        所以专门有一个测试钉住这个语义（见 LimitsResolutionTests）。
        """
        self.store.upsert_quota_policy(
            "qta-global",
            "global",
            "",
            daily_cloud_tokens=tokens,
            daily_requests=requests,
            requests_per_minute=rpm,
            updated_by="test",
            now=self.clock.now(),
        )

    def reserve(self, *, cloud: bool = True, prompt: int = 1000, maximum: int = 500, **kw):
        return self.quota.reserve(
            user_id=self.user_id,
            team_id="",
            is_cloud=cloud,
            prompt_tokens=prompt,
            max_tokens=maximum,
            **kw,
        )

    def usage(self, day: str = ""):
        return self.store.get_usage(day or self.clock.today(), self.user_id)


class EstimateTests(unittest.TestCase):
    def test_estimate_is_biased_conservative(self) -> None:
        """估算必须偏大：估小了会超发，估大了只是浪费。偏置方向只能是这样。"""
        est = QuotaService.estimate_tokens(prompt_tokens=1000, max_tokens=500)
        self.assertGreater(est, 1500, "估算没有留余量，上游多算一点就超发")

    def test_estimate_has_a_floor(self) -> None:
        """空上下文也不能估成 0：系统提示与工具 schema 也要占额度。"""
        self.assertGreaterEqual(QuotaService.estimate_tokens(prompt_tokens=0, max_tokens=0), 256)

    def test_estimate_ignores_negative_input(self) -> None:
        self.assertGreaterEqual(QuotaService.estimate_tokens(prompt_tokens=-100, max_tokens=-5), 256)


class ReserveSettleTests(QuotaTestCase):
    def test_reserve_occupies_then_settle_accounts_actual(self) -> None:
        reservation = self.reserve()
        self.assertEqual(self.usage()["reserved_tokens"], reservation.estimate)
        self.assertEqual(self.usage()["cloud_tokens"], 0)

        self.quota.settle(reservation, actual_tokens=120, is_cloud=True)
        after = self.usage()
        self.assertEqual(after["reserved_tokens"], 0, "结算后预留没有释放")
        self.assertEqual(after["cloud_tokens"], 120, "没有按实际用量入账")

    def test_settle_records_actual_even_above_estimate(self) -> None:
        """实际超过预估也要**如实记**。

        强制记成预留值等于系统性少记 —— 那配额就永远超不了，也就没意义了。
        """
        reservation = self.reserve()
        actual = reservation.estimate + 1000
        self.quota.settle(reservation, actual_tokens=actual, is_cloud=True)
        self.assertEqual(self.usage()["cloud_tokens"], actual)

    def test_settle_clamps_absurd_actual(self) -> None:
        """但要有防溢出上限：上游报个天文数字不能把账算爆。"""
        reservation = self.reserve()
        self.quota.settle(reservation, actual_tokens=reservation.estimate * 10_000, is_cloud=True)
        self.assertLessEqual(self.usage()["cloud_tokens"], reservation.estimate * 8)

    def test_settle_is_idempotent(self) -> None:
        """同一次预留结算两次，第二次数不起作用 —— 否则一笔会记两遍。"""
        reservation = self.reserve()
        first = self.quota.settle(reservation, actual_tokens=100, is_cloud=True)
        second = self.quota.settle(reservation, actual_tokens=100, is_cloud=True)
        self.assertTrue(first["settled"])
        self.assertFalse(second["settled"])
        self.assertEqual(self.usage()["cloud_tokens"], 100)

    def test_release_returns_reservation_and_records_nothing(self) -> None:
        """上游失败：预留要还回去，且**不入账**（一次调用都没发生）。"""
        reservation = self.reserve()
        self.quota.release(reservation)
        after = self.usage()
        self.assertEqual(after["reserved_tokens"], 0, "预留泄漏 —— 额度会被永久占掉")
        self.assertEqual(after["cloud_tokens"], 0)
        self.assertEqual(after["requests"], 0)

    def test_release_after_settle_is_a_noop(self) -> None:
        reservation = self.reserve()
        self.quota.settle(reservation, actual_tokens=50, is_cloud=True)
        self.quota.release(reservation)
        self.assertEqual(self.usage()["cloud_tokens"], 50, "结算后 release 不该把账冲掉")


class LocalRunsTests(QuotaTestCase):
    def test_local_runs_do_not_consume_token_quota(self) -> None:
        """本地模型跑在用户自己的机器上，收它 token 费既是拦不住也不合理。"""
        self.set_global(tokens=1, requests=0, rpm=0)   # token 配额小到离谱
        reservation = self.reserve(cloud=False)        # 本地不该被它拦住
        self.assertEqual(reservation.estimate, 0)
        after = self.usage()
        self.assertEqual(after["cloud_tokens"], 0)
        self.assertEqual(after["reserved_tokens"], 0)

    def test_local_tokens_are_recorded_separately(self) -> None:
        self.reserve(cloud=False)
        reservation = self.quota.reserve(
            user_id=self.user_id, team_id="", is_cloud=False, prompt_tokens=100, max_tokens=0
        )
        self.quota.settle(reservation, actual_tokens=77, is_cloud=False)
        after = self.usage()
        self.assertEqual(after["local_tokens"], 77)
        self.assertEqual(after["cloud_tokens"], 0, "本地用量混进了云端账")


class ExhaustionTests(QuotaTestCase):
    def test_exceeding_daily_tokens_raises_quota_error(self) -> None:
        self.set_global(tokens=1000, requests=0, rpm=0)
        with self.assertRaises(QuotaExceeded) as ctx:
            self.reserve()   # 估算 1875 > 1000
        details = ctx.exception.details
        self.assertEqual(details["kind"], "quota")
        self.assertEqual(ctx.exception.status, 429, "配额超限必须是 429，客户端要能据它降级")
        self.assertIn("resetInSeconds", details)

    def test_token_headroom_is_respected_across_reservations(self) -> None:
        """第一笔预留之后，第二笔要能被它挡住 —— 预留本来就该占额度。"""
        self.set_global(tokens=2000, requests=0, rpm=0)
        first = self.reserve(prompt=1000, maximum=500)     # 1875
        self.assertGreater(first.estimate, 1000)
        with self.assertRaises(QuotaExceeded):
            self.reserve(prompt=1000, maximum=500)

    def test_release_frees_headroom_again(self) -> None:
        """上游失败后，被占住的额度要能重新用 —— 否则失败会变成净损失。"""
        self.set_global(tokens=2000, requests=0, rpm=0)
        reservation = self.reserve()
        self.quota.release(reservation)
        self.reserve()   # 不该再抛

    def test_repeated_failures_never_leak_capacity(self) -> None:
        """连续 50 次「预留→失败」，额度必须一点不少。"""
        self.set_global(tokens=2000, requests=0, rpm=0)
        for _ in range(50):
            reservation = self.reserve()
            self.quota.release(reservation)
        self.assertEqual(self.usage()["reserved_tokens"], 0)
        self.reserve()   # 仍然可用

    def test_daily_request_limit_blocks_after_settlement(self) -> None:
        """请求数在**结算**时计数，所以两次成功调用之后第三次要被挡。"""
        self.set_global(tokens=0, requests=2, rpm=0)
        for _ in range(2):
            reservation = self.reserve(cloud=False)
            self.quota.settle(reservation, actual_tokens=0, is_cloud=False)
        self.assertEqual(self.usage()["requests"], 2)
        with self.assertRaises(QuotaExceeded) as ctx:
            self.reserve(cloud=False)
        self.assertEqual(ctx.exception.details["kind"], "quota")


class ConcurrencyTests(QuotaTestCase):
    def test_concurrent_reserve_never_oversubscribes(self) -> None:
        """**核心测试**：并发抢配额，成功的数量必须恰好等于容量。

        「先查后加」在单线程下永远是对的，所以只有放并发线程才能暴露它。
        容量 = floor(5000 / 1250) = 4。
        """
        self.set_global(tokens=5000, requests=0, rpm=0)
        # prompt=900, max=100 → base 1000 → 估 1250
        expected_capacity = 4
        successes = 0
        failures = 0
        lock = threading.Lock()

        def worker() -> None:
            nonlocal successes, failures
            try:
                self.reserve(prompt=900, maximum=100, apply_rate_limit=False)
            except QuotaExceeded:
                with lock:
                    failures += 1
            else:
                with lock:
                    successes += 1
            finally:
                # store 是「每线程一条连接」；线程结束前显式关掉，
                # 否则测试结束时会刷一屏 unclosed database 警告。
                self.store.close()

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(successes, expected_capacity, "并发下配额被超发（TOCTOU）")
        self.assertEqual(failures, 20 - expected_capacity)
        self.assertEqual(self.usage()["reserved_tokens"], expected_capacity * 1250)


class CrossDayTests(QuotaTestCase):
    def test_quota_resets_on_the_next_day(self) -> None:
        self.set_global(tokens=2000, requests=0, rpm=0)
        day1 = self.clock.today()

        reservation = self.reserve()   # 1875 ≤ 2000
        self.quota.settle(reservation, actual_tokens=1500, is_cloud=True)
        with self.assertRaises(QuotaExceeded):
            self.reserve()

        # 跨到第二天：同一个「用户」，但换了自然日
        self.clock.advance(2 * 86400)
        day2 = self.clock.today()
        self.assertNotEqual(day1, day2, "时钟推进后自然日没变，跨天测试本身失效了")

        fresh = self.reserve()
        self.assertGreater(fresh.estimate, 0, "新的一天额度没有重置")

        # 昨天的账必须原封不动 —— 重置 ≠ 抹掉
        self.assertEqual(self.store.get_usage(day1, self.user_id)["cloud_tokens"], 1500)
        self.assertEqual(self.store.get_usage(day2, self.user_id)["cloud_tokens"], 0)

    def test_next_midnight_is_positive(self) -> None:
        self.assertGreater(self.clock.next_midnight(), 0)


class RateLimitTests(QuotaTestCase):
    def test_rate_limit_blocks_within_the_same_minute(self) -> None:
        self.set_global(tokens=0, requests=0, rpm=3)
        for _ in range(3):
            self.reserve(cloud=False)
        with self.assertRaises(QuotaExceeded) as ctx:
            self.reserve(cloud=False)
        details = ctx.exception.details
        self.assertEqual(details["kind"], "rate_limit")
        self.assertGreater(details["retryAfter"], 0)

    def test_new_minute_opens_a_new_window(self) -> None:
        self.set_global(tokens=0, requests=0, rpm=1)
        self.reserve(cloud=False)
        with self.assertRaises(QuotaExceeded):
            self.reserve(cloud=False)
        self.clock.advance(60)
        self.reserve(cloud=False)   # 新窗口

    def test_rate_limit_can_be_skipped_explicitly(self) -> None:
        self.set_global(tokens=0, requests=0, rpm=1)
        self.reserve(cloud=False)
        self.reserve(cloud=False, apply_rate_limit=False)   # 不计数，理应放行


class LimitsResolutionTests(QuotaTestCase):
    def test_precedence_user_over_team_over_global(self) -> None:
        """越具体越优先 —— 这本就是「谁被显式设过就听谁的」。

        注意这与路由的「只收紧」是**两件事**：团队配额是成本预算，
        不该被某个成员的宽松设置顶掉，也不该把整个团队卡死。
        """
        for scope_type, scope_id, tokens in (
            ("global", "", 1000),
            ("team", "t1", 2000),
            ("user", "u1", 3000),
        ):
            self.store.upsert_quota_policy(
                f"qta-{scope_type}-{scope_id}",
                scope_type,
                scope_id,
                daily_cloud_tokens=tokens,
                daily_requests=0,
                requests_per_minute=0,
                updated_by="test",
                now=self.clock.now(),
            )

        self.assertEqual(self.quota.limits_for(user_id="u1", team_id="t1").daily_cloud_tokens, 3000)
        self.assertEqual(self.quota.limits_for(user_id="u2", team_id="t1").daily_cloud_tokens, 2000)
        self.assertEqual(self.quota.limits_for(user_id="uX", team_id="tY").daily_cloud_tokens, 1000)

    def test_zero_in_a_specific_scope_means_unlimited_not_unset(self) -> None:
        """0 是「设成不限」，不是「没设」。

        用真值判断（`if value:`）而不是 `is not None`，就会把 0 当成缺省
        继续往上找，得到一条跟管理员意图完全相反的配额。
        """
        self.store.upsert_quota_policy(
            "qta-global", "global", "",
            daily_cloud_tokens=1000, daily_requests=0, requests_per_minute=0,
            updated_by="test", now=self.clock.now(),
        )
        self.store.upsert_quota_policy(
            "qta-user", "user", "u1",
            daily_cloud_tokens=0, daily_requests=0, requests_per_minute=0,
            updated_by="test", now=self.clock.now(),
        )
        self.assertEqual(self.quota.limits_for(user_id="u1", team_id="").daily_cloud_tokens, 0)

    def test_falls_back_to_config_default_when_nothing_is_set(self) -> None:
        limits = self.quota.limits_for(user_id="nobody", team_id="")
        self.assertEqual(limits.daily_cloud_tokens, self.app.config.default_daily_cloud_tokens)


class PolicyAdminTests(QuotaTestCase):
    def test_set_policy_rejects_unknown_user(self) -> None:
        """写进一条永远匹配不上的配额，界面显示「已设置」却一点没生效 ——
        这种沉默的失效比明确报错难查得多。"""
        with self.assertRaises(BadRequest):
            self.quota.set_policy(
                actor=self.owner, scope_type="user", scope_id="ghost",
                daily_cloud_tokens=1, daily_requests=0, requests_per_minute=0,
            )

    def test_set_policy_rejects_unknown_team(self) -> None:
        with self.assertRaises(BadRequest):
            self.quota.set_policy(
                actor=self.owner, scope_type="team", scope_id="ghost-team",
                daily_cloud_tokens=1, daily_requests=0, requests_per_minute=0,
            )

    def test_set_policy_rejects_unknown_scope_type(self) -> None:
        with self.assertRaises(BadRequest):
            self.quota.set_policy(
                actor=self.owner, scope_type="galaxy", scope_id="",
                daily_cloud_tokens=1, daily_requests=0, requests_per_minute=0,
            )

    def test_set_policy_rejects_negative_limits(self) -> None:
        with self.assertRaises(BadRequest):
            self.quota.set_policy(
                actor=self.owner, scope_type="global", scope_id="",
                daily_cloud_tokens=-1, daily_requests=0, requests_per_minute=0,
            )

    def test_set_global_policy_takes_effect(self) -> None:
        self.quota.set_policy(
            actor=self.owner, scope_type="global", scope_id="",
            daily_cloud_tokens=4321, daily_requests=7, requests_per_minute=9,
        )
        limits = self.quota.limits_for(user_id="anyone", team_id="")
        self.assertEqual(limits.daily_cloud_tokens, 4321)
        self.assertEqual(limits.daily_requests, 7)
        self.assertEqual(limits.requests_per_minute, 9)

    def test_status_reports_remaining_after_settlement(self) -> None:
        self.set_global(tokens=2000, requests=0, rpm=0)
        # status 是按主体查的，所以这次的用量必须记在这个主体名下
        self.user_id = self.owner.user_id
        reservation = self.reserve()
        self.quota.settle(reservation, actual_tokens=500, is_cloud=True)
        view = self.quota.status(actor=self.owner)
        self.assertEqual(view["usage"]["cloudTokens"], 500)
        self.assertEqual(view["limits"]["dailyCloudTokens"], 2000)
        self.assertEqual(view["remaining"]["cloudTokens"], 1500)


if __name__ == "__main__":
    unittest.main()
