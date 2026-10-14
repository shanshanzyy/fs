"""策略下发与合并：只收紧、交换律、敏感红线、客户端映射。

## 为什么这组必须有测试

治理面最容易被绕过的地方不是认证，而是**策略合并**。典型错误：
团队策略说「允许上云」、全局策略说「禁止上云」，实现里写「团队覆盖全局」——
于是团队一个设置就把公司红线绕过了。

所以这里的断言几乎全是「**不许发生的事**」：

1. 任何一份策略都不能放宽另一份（团队放大不了全局）。
2. 合并必须与顺序无关 —— 否则「先加团队还是先加全局」会改变结果，
   运维迟早踩到；而且 etag 会对同一份策略算出两个值，客户端反复重拉。
3. `routing.sensitiveForceLocal` 不可配置，写成 false 要**整份拒收**，
   不是静默忽略（忽略的话，写文档的人会以为生效了）。
4. 过期策略要降级到受限模式，而不是「拿着旧策略继续跑」。
"""

from __future__ import annotations

import itertools
import unittest

from vibe_server import rbac
from vibe_server.errors import BadRequest, Forbidden
from vibe_server.policy import (
    DEFAULT_POLICY,
    PolicyService,
    apply_to_client_config,
    etag_for,
    merge_documents,
    validate_document,
)

from tests.support import TempDirTestCase, frozen, make_config


def owner() -> rbac.Actor:
    """一个廉价的组织所有者主体（不走口令哈希，因此快得多）。"""
    return rbac.build_actor(
        user_id="u-owner",
        email="owner@corp.example",
        display_name="Owner",
        memberships=[],
        roles=[rbac.ROLE_OWNER],
    )


class MergeTightenOnlyTests(unittest.TestCase):
    def test_boolean_switches_use_and(self) -> None:
        merged = merge_documents([{"allowCloud": True}, {"allowCloud": False}])
        self.assertFalse(merged["allowCloud"], "任一层说不许就是不许")

        both_on = merge_documents([{"allowWrite": True}, {"allowWrite": True}])
        self.assertTrue(both_on["allowWrite"])

    def test_numeric_limits_use_min(self) -> None:
        merged = merge_documents([{"maxIterations": 40}, {"maxIterations": 10}])
        self.assertEqual(merged["maxIterations"], 10, "没有取最紧的那一档")

    def test_deny_lists_use_union(self) -> None:
        merged = merge_documents(
            [{"deniedPaths": ["secrets/"]}, {"deniedPaths": ["keys/"]}]
        )
        self.assertEqual(set(merged["deniedPaths"]), {"secrets/", "keys/"})

    def test_allow_lists_use_intersection(self) -> None:
        merged = merge_documents(
            [{"allowedModels": ["gpt-4", "local-7b"]}, {"allowedModels": ["gpt-4", "claude"]}]
        )
        self.assertEqual(set(merged["allowedModels"]), {"gpt-4"})

    def test_empty_allow_list_means_unrestricted_not_forbidden(self) -> None:
        """交集不能把「允许所有」变成「全部禁止」 —— 那是收紧过头且没人预期。"""
        merged = merge_documents([{"allowedModels": ["gpt-4"]}, {"allowedModels": []}])
        self.assertEqual(set(merged["allowedModels"]), {"gpt-4"})

    def test_a_later_document_cannot_loosen_an_earlier_one(self) -> None:
        """**核心**：把「团队覆盖全局」这条最常见的绕过路径钉死。"""
        merged = merge_documents(
            [
                {"allowCloud": False, "maxIterations": 5},
                {"allowCloud": True, "maxIterations": 100},   # 想放松
            ]
        )
        self.assertFalse(merged["allowCloud"])
        self.assertEqual(merged["maxIterations"], 5)

    def test_merging_nothing_returns_the_conservative_default(self) -> None:
        merged = merge_documents([])
        self.assertFalse(merged["allowCloud"])
        self.assertEqual(merged, validate_document({}))


class AllowCloudConsistencyTests(unittest.TestCase):
    """`allowCloud` 的两处表达必须一致，否则是**无声失效**。"""

    def test_top_level_true_propagates_to_routing(self) -> None:
        """只写顶层 true，就应当生效。

        回归：曾经 `_normalize` 让 `routing.allowCloud` 保持默认 false，
        于是发布 `{"allowCloud": true}` 的生效值是 false ——
        接口回显 true、界面显示"已开"，云端却从来没被用过，且不报错。
        """
        normalized = validate_document({"allowCloud": True})
        self.assertTrue(normalized["allowCloud"])
        self.assertTrue(normalized["routing"]["allowCloud"], "routing 没跟随顶层，策略被静默关掉")

    def test_routing_true_propagates_to_top_level(self) -> None:
        normalized = validate_document({"routing": {"allowCloud": True}})
        self.assertTrue(normalized["allowCloud"])
        self.assertTrue(normalized["routing"]["allowCloud"])

    def test_disagreement_takes_the_stricter(self) -> None:
        normalized = validate_document(
            {"allowCloud": True, "routing": {"allowCloud": False}}
        )
        self.assertFalse(normalized["allowCloud"])
        self.assertFalse(normalized["routing"]["allowCloud"])

    def test_normalization_is_idempotent(self) -> None:
        once = validate_document({"allowCloud": True})
        twice = validate_document(once)
        self.assertEqual(once, twice, "规范化不幂等，合并结果会漂")

    def test_merge_of_single_allow_document_enables_cloud(self) -> None:
        merged = merge_documents([{"allowCloud": True}])
        self.assertTrue(merged["allowCloud"], "单份文档写 true 却合并成 false")


class CommutativityTests(unittest.TestCase):
    def test_merge_is_order_independent(self) -> None:
        """六个排列必须得到**逐字节相同**的文档。

        否则 etag 会不一样，客户端以为自己拿到了新策略，反复重拉；
        更糟的是「顺序无关」这个承诺就成了空话。
        """
        docs = [
            {"allowCloud": True, "maxIterations": 40, "deniedPaths": ["a/"]},
            {"allowCloud": True, "maxIterations": 12, "deniedPaths": ["b/"]},
            {"allowCloud": False, "maxIterations": 30, "allowedModels": ["m1", "m2"]},
        ]
        results = {etag_for(merge_documents(list(p))) for p in itertools.permutations(docs)}
        self.assertEqual(len(results), 1, f"合并结果依赖顺序：{results}")

    def test_list_order_is_normalized(self) -> None:
        """同一组元素、不同书写顺序 → 同一份文档（etag 才稳定）。"""
        left = merge_documents([{"deniedPaths": ["b/"]}, {"deniedPaths": ["a/"]}])
        right = merge_documents([{"deniedPaths": ["a/"]}, {"deniedPaths": ["b/"]}])
        self.assertEqual(left["deniedPaths"], right["deniedPaths"])
        self.assertEqual(etag_for(left), etag_for(right))


class SensitiveForceLocalTests(unittest.TestCase):
    def test_explicitly_disabling_it_rejects_the_whole_document(self) -> None:
        """合规红线：显式关闭 = 拒收整份文档。

        「静默忽略」是更糟的选择 —— 写文档的人会以为它生效了。
        """
        with self.assertRaises(BadRequest) as ctx:
            validate_document({"routing": {"sensitiveForceLocal": False}})
        self.assertIn("sensitiveForceLocal", str(ctx.exception))

    def test_it_is_forced_true_after_normalization(self) -> None:
        self.assertTrue(validate_document({"allowCloud": True})["routing"]["sensitiveForceLocal"])
        self.assertTrue(
            validate_document({"routing": {"allowCloud": True}})["routing"]["sensitiveForceLocal"]
        )

    def test_merge_always_ends_with_it_true(self) -> None:
        merged = merge_documents([{"allowCloud": True}, {"allowCloud": True}])
        self.assertTrue(merged["routing"]["sensitiveForceLocal"])

    def test_truthy_values_other_than_true_are_refused(self) -> None:
        """`1`、`"true"` 这类「看起来像真」的值也拒 —— 只认布尔 true。"""
        for bad in (1, "true", "yes", None):
            with self.subTest(value=bad):
                with self.assertRaises(BadRequest):
                    validate_document({"routing": {"sensitiveForceLocal": bad}})


class ValidationTests(unittest.TestCase):
    def test_unknown_field_is_rejected(self) -> None:
        with self.assertRaises(BadRequest):
            validate_document({"allowCloudd": True})

    def test_boolean_fields_require_booleans(self) -> None:
        with self.assertRaises(BadRequest):
            validate_document({"allowCloud": "yes"})

    def test_numeric_fields_reject_bools_and_out_of_range(self) -> None:
        for bad in (True, 0, -5, 99_999_999):
            with self.subTest(value=bad):
                with self.assertRaises(BadRequest):
                    validate_document({"maxIterations": bad})

    def test_list_items_cannot_smuggle_code(self) -> None:
        """往拒绝清单里塞代码是常见的「顺手」行为，要明确挡住。"""
        with self.assertRaises(BadRequest):
            validate_document({"deniedPaths": ["def f():\n    pass"]})

    def test_list_items_must_be_nonempty_strings(self) -> None:
        with self.assertRaises(BadRequest):
            validate_document({"deniedPaths": [""]})
        with self.assertRaises(BadRequest):
            validate_document({"deniedPaths": [123]})

    def test_routing_allow_cloud_must_be_boolean(self) -> None:
        with self.assertRaises(BadRequest):
            validate_document({"routing": {"allowCloud": "nope"}})

    def test_valid_document_is_normalized_with_defaults(self) -> None:
        normalized = validate_document({"maxIterations": 7})
        self.assertEqual(normalized["maxIterations"], 7)
        self.assertEqual(normalized["maxShell"] if "maxShell" in normalized else 7, 7)
        for key in DEFAULT_POLICY:
            self.assertIn(key, normalized, f"规范化后缺少字段 {key}")


class ClientMappingTests(unittest.TestCase):
    """把服务端策略套到客户端本地配置上（两端共用同一份合并实现）。"""

    def test_snake_case_mapping_is_applied(self) -> None:
        """不做显式映射而「按名字猜」，会让合并逻辑全程跑空 ——
        策略看着下发了但一点没生效，这是最难发现的一类失效。"""
        effective, notes = apply_to_client_config(
            {"allowCloud": False, "maxIterations": 12},
            client={"allow_cloud": True, "max_iterations": 100},
            stale=False,
        )
        self.assertFalse(effective["allow_cloud"])
        self.assertEqual(effective["max_iterations"], 12)
        self.assertTrue(notes)

    def test_missing_client_key_gets_policy_value_written_in(self) -> None:
        """客户端没这个字段时，策略说不允许**必须显式写进去**。

        不写的话客户端会用它的默认值（很可能是 True），策略等于没下发。
        """
        effective, _ = apply_to_client_config(
            {"allowCloud": False}, client={}, stale=False
        )
        self.assertIs(effective["allow_cloud"], False)

    def test_numeric_limits_are_only_lowered(self) -> None:
        effective, _ = apply_to_client_config(
            {"maxIterations": 40}, client={"max_iterations": 5}, stale=False
        )
        self.assertEqual(effective["max_iterations"], 5, "客户端更紧的设置被放松了")

    def test_deny_lists_union_with_the_clients_own(self) -> None:
        effective, _ = apply_to_client_config(
            {"deniedPaths": ["keys/"]},
            client={"denied_paths": ["mine/"]},
            stale=False,
        )
        self.assertEqual(set(effective["denied_paths"]), {"mine/", "keys/"})

    def test_allowed_models_are_narrowed(self) -> None:
        effective, _ = apply_to_client_config(
            {"allowedModels": ["gpt-4"]},
            client={"allowed_models": ["gpt-4", "local-7b"]},
            stale=False,
        )
        self.assertEqual(effective["allowed_models"], ["gpt-4"])

    def test_client_cannot_loosen_a_policy(self) -> None:
        effective, _ = apply_to_client_config(
            {"allowCloud": True}, client={"allow_cloud": False}, stale=False
        )
        self.assertFalse(effective["allow_cloud"], "客户端把自己的紧设置放开了")

    def test_sensitive_force_local_is_always_recorded(self) -> None:
        effective, _ = apply_to_client_config({}, client={}, stale=False)
        self.assertTrue(effective["sensitive_force_local"])

    def test_stale_policy_degrades_to_restricted_mode(self) -> None:
        """过期不沿用：降级到「仅本地 + 强制审批」，而不是裸奔。"""
        effective, notes = apply_to_client_config(
            {"allowCloud": True, "commandTimeout": 600},
            client={"allow_cloud": True, "auto_approve_review": True, "command_timeout": 600},
            stale=True,
            max_staleness=3600,
        )
        self.assertFalse(effective["allow_cloud"])
        self.assertFalse(effective["auto_approve_review"])
        self.assertLessEqual(effective["command_timeout"], 60)
        self.assertTrue(any("受限模式" in n for n in notes))

    def test_client_config_is_not_mutated_in_place(self) -> None:
        client = {"allow_cloud": True}
        apply_to_client_config({"allowCloud": False}, client=client, stale=False)
        self.assertTrue(client["allow_cloud"], "原地改掉了调用方的字典")


class WebPolicyTests(unittest.TestCase):
    """联网检索策略（M7）：allowWeb + webDeniedHosts。

    与 allowCloud 同一纪律：BOOL 用 AND（默认最保守）、LIST 并集、
    过期恒关。区别在客户端表达 —— `web_deny_hosts` 是**逗号分隔字符串**
    （见 config.py），并集时必须按 glob 项而非字符处理。
    """

    def test_default_is_most_conservative(self) -> None:
        self.assertFalse(DEFAULT_POLICY["allowWeb"], "默认策略不该默认放开联网")
        self.assertEqual(DEFAULT_POLICY["webDeniedHosts"], [])

    def test_allow_web_uses_and(self) -> None:
        merged = merge_documents([{"allowWeb": True}, {"allowWeb": False}])
        self.assertFalse(merged["allowWeb"], "任一层说不许就是不许")
        both_on = merge_documents([{"allowWeb": True}, {"allowWeb": True}])
        self.assertTrue(both_on["allowWeb"])

    def test_denied_hosts_use_union(self) -> None:
        merged = merge_documents(
            [{"webDeniedHosts": ["a.test"]}, {"webDeniedHosts": ["b.test"]}]
        )
        self.assertEqual(set(merged["webDeniedHosts"]), {"a.test", "b.test"})

    def test_validate_requires_boolean_allow_web(self) -> None:
        self.assertFalse(validate_document({"allowWeb": False})["allowWeb"])
        with self.assertRaises(BadRequest):
            validate_document({"allowWeb": "yes"})

    def test_apply_writes_web_enabled_when_client_lacks_it(self) -> None:
        """客户端没这个字段时要显式写进去 —— 否则客户端用默认值，策略形同虚设。"""
        effective, _ = apply_to_client_config({"allowWeb": False}, client={}, stale=False)
        self.assertIs(effective["web_enabled"], False)

    def test_apply_unions_denied_hosts_into_comma_string(self) -> None:
        """策略下发的拒绝 host 必须并进客户端已有的逗号串，且不拆字符。"""
        effective, notes = apply_to_client_config(
            {"webDeniedHosts": ["evil.test", "meta.internal"]},
            client={"web_deny_hosts": "mine.test"},
            stale=False,
        )
        hosts = [h.strip() for h in effective["web_deny_hosts"].split(",") if h.strip()]
        self.assertEqual(set(hosts), {"mine.test", "evil.test", "meta.internal"})
        self.assertTrue(any("web_deny_hosts" in n for n in notes))

    def test_apply_denied_hosts_dedupes_case_insensitively(self) -> None:
        effective, _ = apply_to_client_config(
            {"webDeniedHosts": ["EVIL.test"]},
            client={"web_deny_hosts": "evil.test"},
            stale=False,
        )
        self.assertEqual(effective["web_deny_hosts"], "evil.test")

    def test_stale_forces_web_off(self) -> None:
        """过期策略降级：allowWeb 恒 False，哪怕文档写 true。"""
        effective, notes = apply_to_client_config(
            {"allowWeb": True},
            client={"web_enabled": True},
            stale=True,
            max_staleness=3600,
        )
        self.assertFalse(effective["web_enabled"])
        self.assertTrue(any("受限模式" in n for n in notes))


class PolicyServiceTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.clock = frozen(1_700_000_000)
        self.app = self.make_app(clock=self.clock)
        self.policy = self.app.policy_service
        self.owner = owner()

    def test_resolve_without_policy_returns_conservative_default(self) -> None:
        bundle = self.policy.resolve(actor=self.owner)
        self.assertFalse(bundle.document["allowCloud"])
        self.assertEqual(bundle.version, 0)
        self.assertEqual(bundle.sources[0]["scope"], "builtin-default")

    def test_publish_then_resolve_returns_the_document(self) -> None:
        self.policy.publish(
            actor=self.owner, scope_type="global", scope_id="",
            document={"allowCloud": True, "maxIterations": 20},
        )
        bundle = self.policy.resolve(actor=self.owner)
        self.assertTrue(bundle.document["allowCloud"])
        self.assertEqual(bundle.document["maxIterations"], 20)
        self.assertEqual(bundle.version, 1)

    def test_team_policy_cannot_loosen_the_global_one(self) -> None:
        self.policy.publish(
            actor=self.owner, scope_type="global", scope_id="",
            document={"allowCloud": False},
        )
        self.policy.publish(
            actor=self.owner, scope_type="team", scope_id="t1",
            document={"allowCloud": True},
        )
        bundle = self.policy.resolve(actor=self.owner, team_id="t1")
        self.assertFalse(bundle.document["allowCloud"], "团队策略把全局红线放松了")

    def test_publish_is_refused_for_a_disallowed_sensitive_setting(self) -> None:
        with self.assertRaises(BadRequest):
            self.policy.publish(
                actor=self.owner, scope_type="global", scope_id="",
                document={"routing": {"sensitiveForceLocal": False}},
            )

    def test_non_org_wide_actor_cannot_publish_global(self) -> None:
        """团队管理员**有** policy:write，但仍是团队级 —— 不能发全局策略。

        （用 developer 测不出这条：它连 policy:write 都没有，会先在
        `require_permission` 上被挡下，压根走不到「是不是组织级」这一步。）
        """
        team_admin = rbac.build_actor(
            user_id="u-ta", email="ta@x", display_name="TA",
            memberships=[
                {"team_id": "t1", "team_slug": "t1", "team_name": "T1", "role": rbac.ROLE_TEAM_ADMIN}
            ],
        )
        self.assertTrue(team_admin.can(rbac.PERM_POLICY_WRITE))
        self.assertFalse(team_admin.is_org_wide)
        with self.assertRaises(Forbidden):
            self.policy.publish(
                actor=team_admin, scope_type="global", scope_id="", document={}
            )

    def test_actor_cannot_read_another_teams_policy(self) -> None:
        outsider = rbac.build_actor(
            user_id="u-out", email="o@x", display_name="Out",
            memberships=[
                {"team_id": "t-other", "team_slug": "o", "team_name": "O", "role": rbac.ROLE_TEAM_ADMIN}
            ],
        )
        with self.assertRaises(Forbidden):
            self.policy.resolve(actor=outsider, team_id="t1")

    def test_versions_increase_and_are_listed(self) -> None:
        for value in (5, 10, 15):
            self.policy.publish(
                actor=self.owner, scope_type="global", scope_id="",
                document={"maxIterations": value},
            )
        bundle = self.policy.resolve(actor=self.owner)
        self.assertEqual(bundle.version, 3)
        versions = self.policy.list_versions(actor=self.owner, scope_type="global")
        self.assertEqual([v["version"] for v in versions], [3, 2, 1])

    def test_policy_required_false_still_returns_a_conservative_default(self) -> None:
        """显式关掉下发时，回的是**保守默认**，不是「全放开」。"""
        config = make_config(self.tmp, policy_required=False)
        service = PolicyService(store=self.app.store, config=config, clock=self.clock)
        bundle = service.resolve(actor=self.owner)
        self.assertFalse(bundle.document["allowCloud"])
        self.assertEqual(bundle.sources[0]["scope"], "builtin-default")

    def test_etag_changes_when_the_effective_document_changes(self) -> None:
        first = self.policy.resolve(actor=self.owner).etag
        self.policy.publish(
            actor=self.owner, scope_type="global", scope_id="",
            document={"maxIterations": 3},
        )
        second = self.policy.resolve(actor=self.owner).etag
        self.assertNotEqual(first, second)
        self.assertGreater(len(second), 0)


if __name__ == "__main__":
    unittest.main()
