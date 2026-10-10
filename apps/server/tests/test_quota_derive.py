"""配额动态推导：预算 → 每日额度、解析优先级、作用域与权限。

## 为什么这组必须有测试

配额推导最容易出的两类错，都不会报错：

1. **推导覆盖了显式策略**。管理员明明把某个成员的额度设成 5000，
   系统却因为团队有预算把它算成 800 —— 界面显示「已设置」，
   实际一点没生效。所以这里用断言把解析链钉死：
   用户显式 > 团队显式 > 全局显式 > 推导 > config 默认。
2. **取整方向反了**。向上取整会让「预算 / 天数」在边界上多给一天的量，
   日积月累就是超发。推导一律**向下**取整，宁可少给。

另外：推导**只补默认**、不是硬上限 —— 这条语义用「显式策略存在时推导值被忽略」
来钉，而不是靠文档里的一句话。
"""

from __future__ import annotations

import json
import time
import unittest
import urllib.error
import urllib.request
from typing import Any

from vibe_server import rbac
from vibe_server.errors import ApiError, BadRequest, Forbidden, QuotaExceeded
from vibe_server.gateway.quota import derive_daily_tokens, month_days

from tests.support import (
    TempDirTestCase,
    actor_of,
    bootstrap_and_login,
    frozen,
    owner_actor,
    serve_app,
)


# --------------------------------------------------------------------------- HTTP 工具
def http_post(base: str, path: str, payload: dict, *, token: str = "") -> tuple[int, Any]:
    """POST 一个 JSON 体，4xx/5xx 也当正常结果返回（权限拒绝正是被测对象）。"""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw


def http_get_json(base: str, path: str, *, token: str = "") -> tuple[int, Any]:
    request = urllib.request.Request(base + path)
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw


# --------------------------------------------------------------------------- 纯推导数学
class DeriveMathTests(unittest.TestCase):
    """`derive_daily_tokens` 是纯函数，先把它本身钉死，再去测接线。"""

    def test_token_budget_spread_over_month(self) -> None:
        self.assertEqual(
            derive_daily_tokens(budget=30_000, unit_price=0, window=0, members=1, month=30),
            1000,
        )

    def test_explicit_window_overrides_month(self) -> None:
        self.assertEqual(
            derive_daily_tokens(budget=30_000, unit_price=0, window=7, members=1, month=30),
            4285,  # 30000 / 7 = 4285.71 → 向下
        )

    def test_cost_budget_is_converted_by_unit_price(self) -> None:
        """unit_price>0 时 budget 是成本预算：先算 token 预算，再摊到天。"""
        self.assertEqual(
            derive_daily_tokens(budget=1000, unit_price=5, window=10, members=1),
            20,  # (1000 / 5) / 10 = 20
        )

    def test_team_budget_is_split_across_members(self) -> None:
        self.assertEqual(
            derive_daily_tokens(budget=90_000, unit_price=0, window=30, members=3, month=30),
            1000,  # 90000 / 30 = 3000（团队） / 3 人 = 1000
        )

    def test_rounding_is_always_down(self) -> None:
        """100 / 3 = 33.33 —— 取 33。向上取整会在边界上超发。"""
        self.assertEqual(
            derive_daily_tokens(budget=100, unit_price=0, window=3, members=1),
            33,
        )

    def test_zero_or_negative_budget_derives_nothing(self) -> None:
        self.assertEqual(derive_daily_tokens(budget=0, unit_price=0, window=30), 0)
        self.assertEqual(derive_daily_tokens(budget=-100, unit_price=0, window=30), 0)

    def test_members_floor_is_one(self) -> None:
        """成员数为 0 不能让除法爆掉，也不能推导出「无限」。"""
        self.assertEqual(
            derive_daily_tokens(budget=3000, unit_price=0, window=30, members=0, month=30),
            100,
        )

    def test_month_days_tracks_leap_years(self) -> None:
        leap = int(time.mktime((2024, 2, 15, 12, 0, 0, 0, 0, -1)))
        plain = int(time.mktime((2023, 2, 15, 12, 0, 0, 0, 0, -1)))
        self.assertEqual(month_days(leap), 29)
        self.assertEqual(month_days(plain), 28)


# --------------------------------------------------------------------------- 解析链
class ResolveChainTests(TempDirTestCase):
    """预算推导与显式策略的优先级 —— 本组的核心。"""

    def setUp(self) -> None:
        super().setUp()
        self.clock = frozen(1_700_000_000)
        self.app = self.make_app(clock=self.clock)
        self.quota = self.app.quota_service
        self.store = self.app.store
        self._owner = None
        self.now = self.clock.now()

    @property
    def owner(self):
        if self._owner is None:
            self._owner = owner_actor(self.app)
        return self._owner

    # ------------------------------------------------------------------ 夹具
    def add_team(self, team_id: str = "t1", members: tuple[str, ...] = ("u1", "u2", "u3")) -> None:
        self.store.create_team(team_id, f"slug-{team_id}", f"Team {team_id}", now=self.now)
        for uid in members:
            self.store.create_user(uid, f"{uid}@x.com", uid, "hash", now=self.now)
            self.store.add_membership(f"mem-{uid}", uid, team_id, rbac.ROLE_DEVELOPER, now=self.now)

    def set_budget(self, scope_type: str, scope_id: str, **kw: Any) -> None:
        params = {"budget": 0, "unit_price": 0, "window": 0}
        params.update(kw)
        self.store.upsert_budget_policy(
            f"bdg-{scope_type}-{scope_id}",
            scope_type,
            scope_id,
            budget=int(params["budget"]),
            unit_price=int(params["unit_price"]),
            window=int(params["window"]),
            updated_by="test",
            now=self.now,
        )

    def set_policy(self, scope_type: str, scope_id: str, tokens: int) -> None:
        self.store.upsert_quota_policy(
            f"qta-{scope_type}-{scope_id}",
            scope_type,
            scope_id,
            daily_cloud_tokens=tokens,
            daily_requests=0,
            requests_per_minute=0,
            updated_by="test",
            now=self.now,
        )

    # ------------------------------------------------------------------ 推导生效
    def test_team_budget_derives_a_member_limit(self) -> None:
        self.add_team()
        self.set_budget("team", "t1", budget=90_000, window=30)
        limits = self.quota.limits_for(user_id="u1", team_id="t1")
        self.assertEqual(limits.daily_cloud_tokens, 1000)

    def test_user_budget_is_more_specific_than_team_budget(self) -> None:
        self.add_team()
        self.set_budget("team", "t1", budget=90_000, window=30)   # 每人 1000
        self.set_budget("user", "u1", budget=2000, window=10)     # 200/天
        self.assertEqual(
            self.quota.limits_for(user_id="u1", team_id="t1").daily_cloud_tokens, 200
        )
        # 同团队的另一人仍按团队预算
        self.assertEqual(
            self.quota.limits_for(user_id="u2", team_id="t1").daily_cloud_tokens, 1000
        )

    def test_derived_limit_is_enforced_by_reserve(self) -> None:
        """推导出来的额度必须真的拦得住，否则它只是界面上的一个数字。"""
        self.add_team()
        self.set_budget("team", "t1", budget=90_000, window=30)   # 每人 1000
        with self.assertRaises(QuotaExceeded):
            self.quota.reserve(
                user_id="u1", team_id="t1", is_cloud=True, prompt_tokens=1000, max_tokens=500
            )

    # ------------------------------------------------------------------ 显式恒优先
    def test_user_explicit_policy_beats_derived(self) -> None:
        self.add_team()
        self.set_budget("team", "t1", budget=90_000, window=30)
        self.set_policy("user", "u1", 5555)
        self.assertEqual(
            self.quota.limits_for(user_id="u1", team_id="t1").daily_cloud_tokens, 5555
        )

    def test_team_explicit_policy_beats_derived(self) -> None:
        self.add_team()
        self.set_budget("team", "t1", budget=90_000, window=30)
        self.set_policy("team", "t1", 4321)
        self.assertEqual(
            self.quota.limits_for(user_id="u1", team_id="t1").daily_cloud_tokens, 4321
        )

    def test_global_explicit_policy_beats_derived(self) -> None:
        """全局显式策略在解析链上**高于**推导值（推导只补空缺）。"""
        self.add_team()
        self.set_budget("team", "t1", budget=90_000, window=30)
        self.set_policy("global", "", 7777)
        self.assertEqual(
            self.quota.limits_for(user_id="u1", team_id="t1").daily_cloud_tokens, 7777
        )

    def test_zero_explicit_policy_is_unlimited_not_unset(self) -> None:
        """显式的 0 是「不限」，不能被推导值顶掉。"""
        self.add_team()
        self.set_budget("team", "t1", budget=90_000, window=30)
        self.set_policy("user", "u1", 0)
        self.assertEqual(
            self.quota.limits_for(user_id="u1", team_id="t1").daily_cloud_tokens, 0
        )

    # ------------------------------------------------------------------ 回退
    def test_zero_budget_falls_back_to_config_default(self) -> None:
        self.add_team()
        self.set_budget("team", "t1", budget=0, window=30)   # 0 = 未设
        self.assertEqual(
            self.quota.limits_for(user_id="u1", team_id="t1").daily_cloud_tokens,
            self.app.config.default_daily_cloud_tokens,
        )

    def test_no_budget_at_all_falls_back_to_config_default(self) -> None:
        self.assertEqual(
            self.quota.limits_for(user_id="nobody", team_id="").daily_cloud_tokens,
            self.app.config.default_daily_cloud_tokens,
        )

    # ------------------------------------------------------------------ status 来源标注
    def test_status_labels_derived_source(self) -> None:
        _ = self.owner   # 先建所有者：bootstrap 要求库里一个用户都没有
        self.add_team()
        self.set_budget("team", "t1", budget=90_000, window=30)
        view = self.quota.status(actor=self.owner, user_id="u1", team_id="t1")
        self.assertEqual(view["source"], "derived:team")
        self.assertEqual(view["derived"]["dailyCloudTokens"], 1000)
        self.assertEqual(view["derived"]["scope"], "team")
        self.assertEqual(view["budget"]["scopeType"], "team")
        self.assertEqual(view["budget"]["members"], 3)

    def test_status_labels_explicit_source(self) -> None:
        self.set_policy("user", "u1", 5000)
        view = self.quota.status(actor=self.owner, user_id="u1")
        self.assertEqual(view["source"], "explicit:user")
        self.assertEqual(view["limits"]["dailyCloudTokens"], 5000)

    def test_status_labels_default_source(self) -> None:
        view = self.quota.status(actor=self.owner)
        self.assertEqual(view["source"], "default")
        self.assertEqual(view["derived"]["dailyCloudTokens"], 0)
        self.assertIsNone(view["budget"])

    # ------------------------------------------------------------------ 预算管理（服务层）
    def test_set_budget_rejects_negative_values(self) -> None:
        for field in ("budget", "unit_price", "window"):
            args = {"budget": 0, "unit_price": 0, "window": 0}
            args[field] = -1
            with self.assertRaises(BadRequest):
                self.quota.set_budget(
                    actor=self.owner, scope_type="global", scope_id="", **args
                )

    def test_set_budget_rejects_unknown_scope_type(self) -> None:
        with self.assertRaises(BadRequest):
            self.quota.set_budget(
                actor=self.owner, scope_type="galaxy", scope_id="", budget=1
            )

    def test_set_budget_rejects_unknown_team(self) -> None:
        with self.assertRaises(BadRequest):
            self.quota.set_budget(
                actor=self.owner, scope_type="team", scope_id="ghost", budget=1
            )

    def test_set_budget_requires_quota_write(self) -> None:
        developer = rbac.build_actor(
            user_id="dev",
            email="dev@x.com",
            display_name="Dev",
            memberships=[
                {"team_id": "t1", "team_slug": "t1", "team_name": "T1", "role": rbac.ROLE_DEVELOPER}
            ],
        )
        self.assertFalse(developer.can(rbac.PERM_QUOTA_WRITE), "developer 不该有 quota:write")
        with self.assertRaises(ApiError):
            self.quota.set_budget(
                actor=developer, scope_type="global", scope_id="", budget=1
            )

    def test_get_budget_returns_unset_for_missing_row(self) -> None:
        view = self.quota.get_budget(actor=self.owner, scope_type="global", scope_id="")
        self.assertFalse(view["set"])
        self.assertEqual(view["derivedDailyCloudTokens"], 0)

    def test_get_budget_scope_is_enforced(self) -> None:
        """有 quota:read 但不是该团队的人 —— 必须被作用域挡下，而不是放行。"""
        self.add_team("t1")
        other = rbac.build_actor(
            user_id="dev",
            email="dev@x.com",
            display_name="Dev",
            memberships=[
                {
                    "team_id": "t-else",
                    "team_slug": "else",
                    "team_name": "Else",
                    "role": rbac.ROLE_DEVELOPER,
                }
            ],
        )
        self.assertTrue(other.can(rbac.PERM_QUOTA_READ), "developer 应当有 quota:read")
        with self.assertRaises(Forbidden):
            self.quota.get_budget(actor=other, scope_type="team", scope_id="t1")


# --------------------------------------------------------------------------- HTTP 接线
class QuotaBudgetHttpTests(TempDirTestCase):
    """端点走真实 HTTP：路由级权限、作用域校验只有在 HTTP 层才自证。"""

    def setUp(self) -> None:
        super().setUp()
        self.clock = frozen(1_700_000_000)
        self.app = self.make_app(clock=self.clock)
        self.base = serve_app(self, self.app)
        self.session = bootstrap_and_login(self.app)
        self.token = self.session["accessToken"]
        self.owner = actor_of(self.app, self.token)
        self.store = self.app.store

    def _developer_token(self) -> str:
        enrollment = self.app.auth_service.create_enrollment(
            actor=self.owner, role=rbac.ROLE_DEVELOPER
        )
        dev = self.app.auth_service.register_with_enrollment(
            enrollment_token=enrollment["enrollmentToken"],
            email="dev@corp.example",
            password="Str0ng!Passw0rd",
            device_name="dev-device",
            platform="test",
            fingerprint="fp-dev-1",
        )
        return dev["accessToken"]

    def test_post_and_get_budget_roundtrip(self) -> None:
        status, body = http_post(
            self.base,
            "/api/v1/quota/budget",
            {"scopeType": "global", "scopeId": "", "budget": 90_000, "window": 30},
            token=self.token,
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(body["set"])
        self.assertGreater(body["derivedDailyCloudTokens"], 0)

        status, got = http_get_json(
            self.base, "/api/v1/quota/budget?scopeType=global", token=self.token
        )
        self.assertEqual(status, 200, got)
        self.assertTrue(got["set"])
        self.assertEqual(got["budget"], 90_000)

    def test_post_budget_requires_quota_write(self) -> None:
        dev_token = self._developer_token()
        status, body = http_post(
            self.base,
            "/api/v1/quota/budget",
            {"scopeType": "global", "scopeId": "", "budget": 1},
            token=dev_token,
        )
        self.assertEqual(status, 403, body)

    def test_get_budget_of_a_foreign_team_is_refused(self) -> None:
        now = self.clock.now()
        self.store.create_team("t-other", "other", "Other", now=now)
        dev_token = self._developer_token()
        status, body = http_get_json(
            self.base, "/api/v1/quota/budget?scopeType=team&scopeId=t-other", token=dev_token
        )
        self.assertEqual(status, 403, body)


if __name__ == "__main__":
    unittest.main()