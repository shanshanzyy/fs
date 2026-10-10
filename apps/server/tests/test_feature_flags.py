"""功能灰度（M4）：分桶、名单、只收紧合并，以及随策略下发。

## 为什么这组必须有测试

灰度的失败模式有三种，全都不报错：

1. **分桶不稳定**。用随机数或时间分桶，用户会看到功能忽有忽无 ——
   报障时无法复现，最后运维只能「干脆全量开」，灰度白做。
   所以这里对同一个 key 反复评估，断言结果**逐次相同**。
2. **名单优先级搞反**。`denylist` 命中时仍被百分比放行，等于出事了
   摘不掉某个人，只能回滚整个功能。
3. **下层凭空开开关**。若团队策略能开一个全局没定义的功能，
   任何团队管理员都能绕过全公司的灰度节奏。

另外「百分比确实在切分人群」这件事必须真跑一遍：`is_on` 恒返回 True
也能让「设置 100%」的用例通过。
"""

from __future__ import annotations

import unittest

from vibe_server.errors import BadRequest
from vibe_server.features import (
    MAX_FLAGS,
    FeatureFlag,
    bucket_of,
    evaluate,
    is_on,
    merge_flags,
    normalize_flags,
    to_document,
)
from vibe_server.policy import (
    PolicyService,
    merge_documents,
    validate_document,
)

from tests.support import (
    TempDirTestCase,
    actor_of,
    bootstrap_and_login,
    frozen,
    http_get,
    owner_actor,
    serve_app,
)


# --------------------------------------------------------------------------- 评估
class FeatureEvaluationTests(unittest.TestCase):
    def test_disabled_flag_is_off_even_at_full_rollout(self) -> None:
        """`enabled=false` 是总闸：百分比 100% 与白名单都压不过它。"""
        flag = FeatureFlag(enabled=False, rollout_percent=100, allowlist=("u-1",))
        self.assertFalse(is_on("f", flag, user_id="u-1"))

    def test_denylist_beats_allowlist_and_full_rollout(self) -> None:
        flag = FeatureFlag(
            enabled=True, rollout_percent=100, allowlist=("u-1",), denylist=("u-1",)
        )
        self.assertFalse(is_on("f", flag, user_id="u-1"))

    def test_denylist_matches_the_device_too(self) -> None:
        """要能只摘掉一台机器，而不是把整个人排除。"""
        flag = FeatureFlag(enabled=True, rollout_percent=100, denylist=("dev-9",))
        self.assertFalse(is_on("f", flag, user_id="u-1", device_id="dev-9"))
        self.assertTrue(is_on("f", flag, user_id="u-1", device_id="dev-8"))

    def test_allowlist_lets_a_named_user_in_at_zero_percent(self) -> None:
        flag = FeatureFlag(enabled=True, rollout_percent=0, allowlist=("u-1",))
        self.assertTrue(is_on("f", flag, user_id="u-1"))
        self.assertFalse(is_on("f", flag, user_id="u-2"))

    def test_missing_identity_stays_off(self) -> None:
        """没有稳定标识就分不了桶。宁可不开 —— 反过来是「身份缺失即全开」。"""
        flag = FeatureFlag(enabled=True, rollout_percent=100)
        self.assertTrue(is_on("f", flag, user_id=""))
        # 100% 不需要分桶，所以上面是 True；下面才是关键：
        partial = FeatureFlag(enabled=True, rollout_percent=50)
        self.assertFalse(is_on("f", partial, user_id=""))

    def test_bucket_is_stable_for_the_same_identity(self) -> None:
        first = bucket_of("checkpointV2", "u-1")
        for _ in range(5):
            self.assertEqual(bucket_of("checkpointV2", "u-1"), first)

    def test_buckets_are_independent_per_flag(self) -> None:
        """同一个 key 在不同 flag 上不能总是落同一个桶，否则几个实验样本重合。"""
        keys = [f"u-{i}" for i in range(200)]
        a = [bucket_of("flagA", key) for key in keys]
        b = [bucket_of("flagB", key) for key in keys]
        self.assertNotEqual(a, b)

    def test_rollout_percent_actually_partitions_the_population(self) -> None:
        keys = [f"u-{i}" for i in range(1000)]
        flag = FeatureFlag(enabled=True, rollout_percent=30)
        on = [key for key in keys if is_on("f", flag, user_id=key)]
        # 三成左右。区间给得宽：这条断言要抓的是「恒真/恒假/严重偏斜」，
        # 不是哈希分布的质量（那由 hashlib 保证）。
        self.assertGreater(len(on), 200)
        self.assertLess(len(on), 400)

    def test_evaluate_returns_sorted_flags(self) -> None:
        flags = {
            "zeta": FeatureFlag(enabled=True, rollout_percent=100),
            "alpha": FeatureFlag(enabled=False),
        }
        result = evaluate(flags, user_id="u-1")
        self.assertEqual(list(result), ["alpha", "zeta"])
        self.assertEqual(result, {"alpha": False, "zeta": True})


# --------------------------------------------------------------------------- 规范化
class FlagNormalizationTests(unittest.TestCase):
    def test_valid_document_is_parsed(self) -> None:
        flags = normalize_flags(
            {
                "checkpointV2": {
                    "enabled": True,
                    "rolloutPercent": 25,
                    "allowlist": ["u-1"],
                    "denylist": ["u-2"],
                }
            }
        )
        self.assertEqual(
            flags["checkpointV2"],
            FeatureFlag(
                enabled=True,
                rollout_percent=25,
                allowlist=("u-1",),
                denylist=("u-2",),
            ),
        )

    def test_minimal_spec_defaults_to_off(self) -> None:
        """只写 `{}` 或什么都不写 = 关。默认必须是保守的那一档。"""
        self.assertFalse(normalize_flags({"f": {}})["f"].enabled)
        self.assertFalse(normalize_flags({"f": None})["f"].enabled)

    def test_illegal_names_are_refused(self) -> None:
        for name in ("has space", "1leading", "x" * 65, "a/b", "带中文"):
            with self.subTest(name=name):
                with self.assertRaises(BadRequest):
                    normalize_flags({name: {}})

    def test_unknown_fields_are_refused(self) -> None:
        with self.assertRaises(BadRequest):
            normalize_flags({"f": {"rollout": 10}})

    def test_percent_must_be_an_int_in_range(self) -> None:
        for spec in ({"rolloutPercent": True}, {"rolloutPercent": "10"}, {"rolloutPercent": 101}, {"rolloutPercent": -1}):
            with self.subTest(spec=spec):
                with self.assertRaises(BadRequest):
                    normalize_flags({"f": spec})

    def test_list_items_must_be_one_line_strings(self) -> None:
        for spec in ({"denylist": "u-1"}, {"denylist": [""]}, {"denylist": ["a\nb"]}):
            with self.subTest(spec=spec):
                with self.assertRaises(BadRequest):
                    normalize_flags({"f": spec})

    def test_too_many_flags_are_refused(self) -> None:
        with self.assertRaises(BadRequest):
            normalize_flags({f"f{i}": {} for i in range(MAX_FLAGS + 1)})

    def test_lenient_mode_skips_bad_entries_instead_of_failing(self) -> None:
        """合并路径上不能因为一个坏值让「算出生效策略」整个失败 ——
        那会让所有客户端都拿不到策略，代价远大于忽略一个 flag。"""
        flags = normalize_flags(
            {
                "good": {"enabled": True, "rolloutPercent": 100},
                "has space": {},
                "badField": {"percent": 1},
            },
            strict=False,
        )
        self.assertEqual(list(flags), ["good"])

    def test_to_document_round_trips(self) -> None:
        source = {"f": {"enabled": True, "rolloutPercent": 40, "allowlist": ["u-1"]}}
        # 缺的字段会被补成全量形态：文档形状必须**唯一**，
        # 否则同一份定义写成两种字节，etag 就不稳定了
        self.assertEqual(
            to_document(normalize_flags(source)),
            {
                "f": {
                    "enabled": True,
                    "rolloutPercent": 40,
                    "allowlist": ["u-1"],
                    "denylist": [],
                }
            },
        )
        # 已经是全量形态的则原样往返
        self.assertEqual(to_document(normalize_flags(to_document(normalize_flags(source)))), to_document(normalize_flags(source)))


# --------------------------------------------------------------------------- 合并
class FlagMergeTests(unittest.TestCase):
    def _layers(self, *specs: dict) -> list[dict]:
        return [normalize_flags(spec) for spec in specs]

    def test_any_layer_saying_off_wins(self) -> None:
        merged = merge_flags(
            self._layers(
                {"f": {"enabled": True, "rolloutPercent": 100}},
                {"f": {"enabled": False}},
            )
        )
        self.assertFalse(merged["f"].enabled)

    def test_percent_takes_the_minimum(self) -> None:
        merged = merge_flags(
            self._layers(
                {"f": {"enabled": True, "rolloutPercent": 50}},
                {"f": {"enabled": True, "rolloutPercent": 10}},
            )
        )
        self.assertEqual(merged["f"].rollout_percent, 10)

    def test_lists_are_unioned_and_intersected(self) -> None:
        merged = merge_flags(
            self._layers(
                {
                    "f": {
                        "enabled": True,
                        "rolloutPercent": 100,
                        "allowlist": ["u-1", "u-2"],
                        "denylist": ["u-9"],
                    }
                },
                {
                    "f": {
                        "enabled": True,
                        "rolloutPercent": 100,
                        "allowlist": ["u-2", "u-3"],
                        "denylist": ["u-8"],
                    }
                },
            )
        )
        flag = merged["f"]
        # 白名单交集：任一层没列的不可用
        self.assertEqual(flag.allowlist, ("u-2",))
        # 排除清单并集：任一层禁的都禁
        self.assertEqual(set(flag.denylist), {"u-8", "u-9"})

    def test_a_flag_only_defined_below_stays_off(self) -> None:
        """上层没定义 → 下层按默认（关）参与合并，于是下层开不出来。"""
        merged = merge_flags(
            self._layers(
                {"global.only": {"enabled": True, "rolloutPercent": 100}},
                {"team.only": {"enabled": True, "rolloutPercent": 100}},
            )
        )
        self.assertFalse(merged["team.only"].enabled)

    def test_merge_is_order_independent(self) -> None:
        """顺序无关是承诺：否则「先加团队还是先加全局」会改变灰度结果。"""
        a = {"f": {"enabled": True, "rolloutPercent": 50, "denylist": ["u-1"]}}
        b = {"f": {"enabled": True, "rolloutPercent": 30, "denylist": ["u-2"]}}
        first = merge_flags(self._layers(a, b))["f"]
        second = merge_flags(self._layers(b, a))["f"]
        self.assertEqual(first, second)


# --------------------------------------------------------------------------- 策略接线
class PolicyFeatureWiringTests(TempDirTestCase):
    """灰度必须真的随策略文档走通「发布 → 合并 → 下发 → 按人评估」。"""

    def setUp(self) -> None:
        super().setUp()
        self.clock = frozen(1_700_000_000)
        self.app = self.make_app(clock=self.clock)
        self.policy = self.app.policy_service
        self._owner = None

    @property
    def owner(self):
        if self._owner is None:
            self._owner = owner_actor(self.app)
        return self._owner

    def test_validate_accepts_features_and_refuses_bad_ones(self) -> None:
        document = validate_document(
            {"features": {"checkpointV2": {"enabled": True, "rolloutPercent": 100}}}
        )
        self.assertEqual(
            document["features"],
            {"checkpointV2": {"enabled": True, "rolloutPercent": 100, "allowlist": [], "denylist": []}},
        )
        with self.assertRaises(BadRequest):
            validate_document({"features": {"has space": {}}})

    def test_unknown_top_level_field_listing_includes_features(self) -> None:
        """`features` 必须出现在「可用字段」提示里 —— 否则管理员写错时
        看到的是一份漏掉它的清单，会以为这个功能不存在。"""
        with self.assertRaises(BadRequest) as caught:
            validate_document({"feature": {}})
        self.assertIn("features", str(caught.exception))

    def test_merged_document_keeps_flags_only_tightened(self) -> None:
        merged = merge_documents(
            [
                {"features": {"f": {"enabled": True, "rolloutPercent": 60}}},
                {"features": {"f": {"enabled": True, "rolloutPercent": 20}}},
            ]
        )
        self.assertEqual(merged["features"]["f"]["rolloutPercent"], 20)
        self.assertNotIn("g", merged["features"])

    def test_bundle_carries_definitions_and_this_subjects_results(self) -> None:
        """`policy.features` 是定义，顶层 `features` 是「这个人此刻的结果」。

        两者都要下发：客户端只看结果，界面要解释「你为什么在这个灰度里」
        得看定义。
        """
        self.policy.publish(
            actor=self.owner,
            scope_type="global",
            scope_id="",
            document={
                "features": {
                    "checkpointV2": {"enabled": True, "rolloutPercent": 0, "allowlist": [self.owner.user_id]},
                    "treeSitter": {"enabled": True, "rolloutPercent": 0},
                }
            },
        )
        bundle = self.policy.resolve(actor=self.owner)
        self.assertTrue(bundle.features["checkpointV2"], "白名单里的用户没被放行")
        self.assertFalse(bundle.features["treeSitter"])
        described = bundle.describe()
        self.assertEqual(described["features"]["checkpointV2"], True)
        self.assertEqual(
            described["policy"]["features"]["checkpointV2"]["rolloutPercent"], 0
        )

    def test_team_layer_cannot_introduce_a_flag_the_global_layer_never_defined(self) -> None:
        """真实接线的版本：全局没定义，团队开了也不算数。"""
        self.policy.publish(
            actor=self.owner, scope_type="global", scope_id="", document={}
        )
        self.policy.publish(
            actor=self.owner,
            scope_type="team",
            scope_id="t1",
            document={"features": {"sneaky": {"enabled": True, "rolloutPercent": 100}}},
        )
        bundle = self.policy.resolve(actor=self.owner, team_id="t1")
        self.assertFalse(bundle.features["sneaky"])

    def test_policy_endpoint_delivers_the_gate_decisions(self) -> None:
        """端到端：客户端 HTTP 拿到的 JSON 里必须有 `features`。

        这条是这组里唯一不可省的 —— 前面几条直接调 service，
        而「下发」这个动作发生在 HTTP 层。
        """
        session = bootstrap_and_login(self.app)
        owner = actor_of(self.app, session["accessToken"])
        self.policy.publish(
            actor=owner,
            scope_type="global",
            scope_id="",
            document={"features": {"checkpointV2": {"enabled": True, "rolloutPercent": 100}}},
        )
        base = serve_app(self, self.app)
        status, body, _headers = http_get(
            base, "/api/v1/policy", token=session["accessToken"]
        )
        self.assertEqual(status, 200)
        self.assertIn('"features"', body)
        self.assertIn('"checkpointV2": true', body)
        self.assertIn('"rolloutPercent": 100', body)


class PolicyServiceDefaultsTests(unittest.TestCase):
    """`policy_required=False` 的路径也要给出一份（空的）灰度结果。

    漏掉的话客户端拿到的是「没有 features 字段」，而它的默认行为很可能是
    全开 —— 静默降级成「全量灰度」比不给灰度更糟。
    """

    def test_disabled_policy_still_reports_an_empty_gate_map(self) -> None:
        from vibe_server.config import ServerConfig

        config = ServerConfig.load(policy_required=False)
        service = PolicyService(store=_NullStore(), config=config, clock=frozen())
        bundle = service.resolve(actor=owner_shaped_actor())
        self.assertEqual(bundle.features, {})
        self.assertIn("features", bundle.describe())


class _NullStore:
    """`policy_required=False` 时 store 不会被读；给个空壳把依赖摆明。"""

    def __getattr__(self, name: str):  # pragma: no cover - 被调用即失败
        raise AssertionError(f"这一路径不该访问存储：{name}")


def owner_shaped_actor():
    from vibe_server import rbac

    return rbac.build_actor(
        user_id="u-owner",
        email="o@x",
        display_name="O",
        memberships=[],
        roles=[rbac.ROLE_OWNER],
    )


if __name__ == "__main__":
    unittest.main()