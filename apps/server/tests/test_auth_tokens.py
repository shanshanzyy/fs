"""认证：刷新令牌轮换、重放检测、撤销、口令强度。

## 为什么「轮换」必须配「重放检测」

刷新令牌是一次性的：每次刷新作废旧的、发新的。这把「令牌被盗」的窗口
从「直到过期」缩短到「直到下一次刷新」。

但一次性令牌有一个**危险的失败模式**：如果实现只是「作废旧的」而没有把
重放当成入侵信号 —— 攻击者先用掉旧令牌拿到新令牌，合法用户随后拿同一个
旧令牌来刷新，只看到一句「无效」，于是重新登录，一切照常。
攻击者手上那份新令牌**永远不会被发现**。

所以重放必须判为入侵：撤销整个令牌家族（连同已经发出的新令牌）**以及该设备**。
代价是合法用户也要重新登录 —— 这是刻意的：已知有入侵时，让双方都重新认证，
比让攻击者安静地留在里面便宜得多。
"""

from __future__ import annotations

import json
import threading
import unittest

from tests.support import (
    TempDirTestCase,
    actor_of,
    bootstrap_and_login,
    http_get,
    serve_app,
)

from vibe_server import rbac
from vibe_server.errors import Forbidden, Unauthenticated
from vibe_server.auth import passwords


class BootstrapTests(TempDirTestCase):
    def test_owner_gets_a_working_actor(self) -> None:
        """引导出来的所有者必须**立刻能用**。

        曾经的缺陷：只建用户不建 membership，而角色全部由 membership 推导，
        于是「管理员建好了，登录进去每个接口都 403」。注册接口却返回成功，
        所以这个坑很隐蔽。
        """
        app = self.make_app()
        session = bootstrap_and_login(app)
        actor = actor_of(app, session["accessToken"])
        self.assertTrue(actor.is_org_wide)
        self.assertIn(rbac.ROLE_OWNER, actor.roles)
        self.assertTrue(actor.team_ids, "所有者没有任何团队，说明基线 membership 没建")

    def test_bootstrap_is_refused_once_org_exists(self) -> None:
        """库非空之后不能再引导 —— 否则任何人都能给自己开 owner。"""
        app = self.make_app()
        bootstrap_and_login(app)
        with self.assertRaises(Forbidden):
            app.auth_service.bootstrap_org(
                email="attacker@corp.example", password="Str0ng!Passw0rd"
            )

    def test_concurrent_bootstrap_creates_exactly_one_owner(self) -> None:
        """并发 bootstrap 只能有一个成功。

        「先 list_users 再 create_user」是先查后写：两个请求会同时读到空表，
        各建一个 owner —— 等于开了两条未认证的提权路径。
        现在判定与写入压在同一条 SQL 里，断言「恰好一个成功、库内只有一个用户」。
        """
        app = self.make_app()
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def attempt(email: str) -> None:
            barrier.wait(timeout=10)
            try:
                app.auth_service.bootstrap_org(
                    email=email, password="Str0ng!Passw0rd", display_name="Owner"
                )
                outcomes.append("ok")
            except Forbidden:
                outcomes.append("forbidden")

        threads = [
            threading.Thread(target=attempt, args=(f"owner{index}@corp.example",))
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(outcomes.count("ok"), 1, f"应恰好一个成功，实际 {outcomes}")
        self.assertEqual(
            outcomes.count("forbidden"), 1, f"另一个必须被拒，实际 {outcomes}"
        )
        self.assertEqual(
            len(app.store.list_users(limit=10)), 1, "库里只能有一个用户"
        )

    def test_weak_password_is_refused(self) -> None:
        app = self.make_app()
        with self.assertRaises(Exception) as ctx:
            app.auth_service.bootstrap_org(email="a@corp.example", password="123456")
        message = str(ctx.exception)
        self.assertTrue(
            "口令" in message or "密码" in message,
            f"弱口令拒绝的理由应当是「口令怎么不行」，实际：{message}",
        )


class PasswordStrengthTests(unittest.TestCase):
    def test_common_passwords_rejected(self) -> None:
        for weak in ("password", "12345678", "qwertyuiop", "admin12345"):
            with self.subTest(password=weak):
                with self.assertRaises(Exception):
                    passwords.check_strength(weak, email="u@corp.example")

    def test_short_email_prefix_does_not_block_strong_password(self) -> None:
        """邮箱前缀只取足够长的一段来比对。

        拿单字符前缀（`a@x.com` 的 `a`）去检查，几乎任何密码都「包含用户名」，
        于是规则变成了「必须绕开它」。一条用户必须绕开的规则，
        结果是密码更好猜，而不是更安全。
        """
        passwords.check_strength("Str0ng!Passw0rd", email="a@x.com")

    def test_long_email_prefix_still_checked(self) -> None:
        """但足够长的前缀必须被拦住 —— 那是真的可猜。"""
        with self.assertRaises(Exception):
            passwords.check_strength("Zyxwvu!Secure9", email="zyxwvu@corp.example")

    def test_hash_is_salted_and_verifiable(self) -> None:
        first = passwords.hash_password("Str0ng!Passw0rd")
        second = passwords.hash_password("Str0ng!Passw0rd")
        self.assertNotEqual(first, second, "同一口令两次哈希相同 —— 没有加盐")
        self.assertTrue(passwords.verify_password("Str0ng!Passw0rd", first))
        self.assertFalse(passwords.verify_password("Str0ng!Passw0rd ", first))


class RefreshRotationTests(TempDirTestCase):
    def test_refresh_returns_a_new_pair(self) -> None:
        app = self.make_app()
        session = bootstrap_and_login(app)
        rotated = app.auth_service.refresh(refresh_token=session["refreshToken"])
        self.assertNotEqual(
            rotated["refreshToken"], session["refreshToken"],
            "刷新没有换发新令牌 —— 那它就不是一次性的",
        )
        # 新令牌能用
        actor = actor_of(app, rotated["accessToken"])
        self.assertEqual(actor.user_id, session["user"]["id"])

    def test_old_refresh_token_is_rejected_after_rotation(self) -> None:
        app = self.make_app()
        session = bootstrap_and_login(app)
        app.auth_service.refresh(refresh_token=session["refreshToken"])
        with self.assertRaises(Unauthenticated):
            app.auth_service.refresh(refresh_token=session["refreshToken"])

    def test_replay_revokes_the_whole_family_and_the_device(self) -> None:
        """核心断言：重放旧令牌 = 入侵信号，不是一句「无效」了事。"""
        app = self.make_app()
        session = bootstrap_and_login(app)
        old = session["refreshToken"]
        stolen = app.auth_service.refresh(refresh_token=old)   # 攻击者先用掉旧令牌

        # 合法用户拿着同一个旧令牌来刷新 → 被识别为重放
        with self.assertRaises(Unauthenticated):
            app.auth_service.refresh(refresh_token=old)

        # 关键：攻击者刚拿到的那份**也必须失效**。否则重放检测毫无意义 ——
        # 攻击者已经在里面了，而合法用户只看到一次「令牌无效」。
        with self.assertRaises(Unauthenticated):
            app.auth_service.refresh(refresh_token=stolen["refreshToken"])

        # 设备也要下线：否则它还能继续用访问令牌
        with self.assertRaises(Unauthenticated):
            actor_of(app, stolen["accessToken"])

    def test_replay_is_recorded_as_an_auth_event(self) -> None:
        """入侵信号必须留痕 —— 没有留痕的重放检测只挡住了这一次。"""
        app = self.make_app()
        session = bootstrap_and_login(app)
        app.auth_service.refresh(refresh_token=session["refreshToken"])
        with self.assertRaises(Unauthenticated):
            app.auth_service.refresh(refresh_token=session["refreshToken"])

        kinds = {
            str(row.get("kind") or "")
            for row in app.store.list_auth_events(limit=200)
        }
        self.assertTrue(
            any("reuse" in k or "replay" in k for k in kinds),
            f"重放没有被记成认证事件，实际记录：{sorted(kinds)}",
        )

    def test_logout_revokes_the_access_token(self) -> None:
        """登出必须让访问令牌立刻失效，而不是「等它自然过期」。"""
        app = self.make_app()
        session = bootstrap_and_login(app)
        app.auth_service.logout(
            access_token=session["accessToken"], refresh_token=session["refreshToken"]
        )
        with self.assertRaises(Unauthenticated):
            actor_of(app, session["accessToken"])


class LoginHygieneTests(TempDirTestCase):
    def test_unknown_user_and_wrong_password_look_identical(self) -> None:
        """两种失败必须给出**同样的**响应。

        区分开就等于送给攻击者一个用户枚举接口：先问「这个邮箱存在吗」，
        再集中爆破存在的那几个。
        """
        app = self.make_app()
        bootstrap_and_login(app)

        with self.assertRaises(Unauthenticated) as wrong:
            app.auth_service.login(email="owner@corp.example", password="Wrong!Passw0rd")
        with self.assertRaises(Unauthenticated) as missing:
            app.auth_service.login(email="nobody@corp.example", password="Wrong!Passw0rd")

        self.assertEqual(
            str(wrong.exception), str(missing.exception),
            "两种失败的消息不同 —— 这等于一个用户枚举接口",
        )

    def test_login_is_case_insensitive_on_email(self) -> None:
        app = self.make_app()
        bootstrap_and_login(app)
        session = app.auth_service.login(
            email="Owner@Corp.Example", password="Str0ng!Passw0rd"
        )
        self.assertTrue(session["accessToken"])


class DeviceListTests(TempDirTestCase):
    def test_list_devices_exposes_device_id_and_current_marker(self) -> None:
        """`GET /api/v1/devices` 必须回 deviceId（与登录响应同名）和 current。

        曾经的缺陷：服务端返回键是 `id`，而客户端 `governance devices`
        读的是 `deviceId`，于是设备列表渲染成一片空白；且服务端从不返回
        `current`，「（本机）」标记永远不出现。这条用例把两件事一起钉住。
        """
        app = self.make_app()
        session = bootstrap_and_login(app)
        base = serve_app(self, app)

        status, body, _ = http_get(base, "/api/v1/devices", token=session["accessToken"])
        self.assertEqual(status, 200)
        devices = json.loads(body)["devices"]
        self.assertEqual(len(devices), 1)
        device = devices[0]

        self.assertTrue(device.get("deviceId"), "deviceId 为空 —— 客户端会渲染成空白")
        self.assertEqual(device["deviceId"], session["deviceId"])
        self.assertIs(device["current"], True, "发起请求的设备未被标成 current")
        self.assertNotIn("id", device, "旧的 `id` 键会让两套命名长期并存")


if __name__ == "__main__":
    unittest.main()
