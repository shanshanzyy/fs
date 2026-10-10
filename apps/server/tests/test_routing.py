"""路由决策：敏感内容强制本地（红线）、内容扫描、策略只能收紧。

## 为什么这组必须有测试

路由的失败模式**不会报错** —— 它会安静地把代码送出去。所以这里的每一条
断言都是在钉一个「不许发生的事」，而不是在验证一个功能：

1. 命中敏感路径 → 必须本地，**即使策略允许上云**（红线优先级最高）。
2. 客户端没申报的敏感内容，也要靠**扫描实际外发正文**发现 ——
   客户端的申报是「配合」而不是「保证」，不能作为防线。
3. 策略说 `allowCloud=false` 之后，后面任何一层都不能把它翻回来。
"""

from __future__ import annotations

import unittest

from vibe_server.gateway.routing import (
    CLOUD,
    LOCAL,
    TASK_REASONING,
    RouteRequest,
    decide,
    scan_text,
)


class SensitivePathTests(unittest.TestCase):
    def test_sensitive_path_forces_local_even_when_cloud_allowed(self) -> None:
        """红线：命中敏感路径就走本地，策略允许上云也不行。"""
        decision = decide(
            RouteRequest(task=TASK_REASONING, touched_paths=(".env",)),
            policy={"allowCloud": True},
        )
        self.assertEqual(decision.target, LOCAL)
        self.assertTrue(decision.sensitive)
        self.assertTrue(decision.sensitive_reasons)
        # 理由要能进审计，但**不能**回正文
        self.assertNotIn("SECRET", str(decision.describe()))

    def test_config_directory_with_prod_segment_is_sensitive(self) -> None:
        """`config/prod/db.yaml` 必须命中。

        这是一处真实回归：最初的实现只匹配**最后一段**路径，
        于是 `config/prod/db.yaml` 被当成「一个叫 db.yaml 的普通文件」放行了 ——
        而它恰恰是生产库连接串最可能出现的位置。
        """
        for path in (
            "config/prod/db.yaml",
            "config/production/app.yml",
            "config/staging/app.yaml",
            "conf/live/secrets.json",
        ):
            with self.subTest(path=path):
                decision = decide(
                    RouteRequest(task=TASK_REASONING, touched_paths=(path,)),
                    policy={"allowCloud": True},
                )
                self.assertEqual(decision.target, LOCAL, f"{path} 没被识别为敏感")

    def test_lookalike_paths_are_not_sensitive(self) -> None:
        """反过来：长得像但不能误伤。误报会让这条红线被当成噪音关掉。"""
        for path in (
            "docs/config.md",             # 文档里讲配置
            "src/config_parser.py",       # 解析配置的代码
            "src/production_utils.py",    # 名字里带 production
        ):
            with self.subTest(path=path):
                decision = decide(
                    RouteRequest(task=TASK_REASONING, touched_paths=(path,)),
                    policy={"allowCloud": True},
                )
                self.assertFalse(
                    decision.sensitive, f"{path} 被误判为敏感 —— 误报会让人关掉这条红线"
                )


class OutboundScanTests(unittest.TestCase):
    """不信任客户端的申报：扫真正要发出去的那段文字。"""

    def test_prompt_text_is_scanned(self) -> None:
        hits = scan_text("这是我的私钥 -----BEGIN RSA PRIVATE KEY----- ")
        self.assertTrue(hits, "外发正文里的私钥没被扫出来")

    def test_scan_finds_credentials(self) -> None:
        for text in (
            "aws_secret_access_key = AKIAIOSFODNN7EXAMPLE",
            "password = hunter2  # 生产库",
            "连接串 postgres://user:pw@10.0.0.5:5432/prod",
        ):
            with self.subTest(text=text[:30]):
                self.assertTrue(scan_text(text), f"没扫出凭据：{text!r}")

    def test_scan_does_not_flag_ordinary_code(self) -> None:
        """普通代码不该被拦 —— 否则每次请求都走本地，云端路由等于不存在。"""
        for text in (
            "def add(a, b):\n    return a + b\n",
            "请解释这个函数的作用，并给出三个测试用例。",
            "SELECT id, name FROM users WHERE id = 1",
            # 下面三行是「凭据赋值」规则的误报陷阱：关键词都在，
            # 但右边不是字面量，而是参数回填、环境变量、函数调用。
            # 拦下它们等于把每一份用户登录代码都压回本地。
            "self.password = password",
            'token = os.environ["GITHUB_TOKEN"]',
            "pwd = get_pwd()",
        ):
            with self.subTest(text=text[:30]):
                self.assertEqual(scan_text(text), [], f"普通内容被误报：{text!r}")

    def test_sensitive_text_forces_local_without_any_declared_path(self) -> None:
        """关键：客户端一个敏感路径都没申报，也要被拦下。"""
        decision = decide(
            RouteRequest(
                task=TASK_REASONING,
                touched_paths=(),
                outbound_text="把我的 API key sk-abcdefghijklmnopqrstuvwxyz 发上去",
            ),
            policy={"allowCloud": True},
        )
        self.assertEqual(decision.target, LOCAL)
        self.assertTrue(decision.sensitive)


class PolicyTighteningTests(unittest.TestCase):
    def test_policy_can_only_tighten(self) -> None:
        """策略说不行就是不行，后面几层不许翻回来。"""
        request = RouteRequest(task=TASK_REASONING)   # 本来是最该上云的档次
        self.assertEqual(decide(request, policy={"allowCloud": True}).target, CLOUD)

        decision = decide(request, policy={"allowCloud": False})
        self.assertEqual(decision.target, LOCAL)
        self.assertTrue(decision.forced_by_policy)
        self.assertFalse(decision.sensitive, "这是策略压回来的，不是敏感判定")

    def test_client_cannot_override_policy(self) -> None:
        """客户端声明 cloud_allowed=True 也盖不过服务端策略。"""
        decision = decide(
            RouteRequest(task=TASK_REASONING, cloud_allowed=True),
            policy={"allowCloud": False},
        )
        self.assertEqual(decision.target, LOCAL)

    def test_absent_policy_defaults_to_local(self) -> None:
        """没有任何策略时，默认本地。

        默认值选「上云」是最坏的一种：它把「忘了配」变成「已经在往外发」。
        """
        decision = decide(RouteRequest(task="completion"))  # 轻任务
        self.assertEqual(decision.target, LOCAL)
        self.assertFalse(decision.sensitive)

    def test_light_task_stays_local_even_when_cloud_allowed(self) -> None:
        decision = decide(RouteRequest(task="completion"), policy={"allowCloud": True})
        self.assertEqual(decision.target, LOCAL)


if __name__ == "__main__":
    unittest.main()
