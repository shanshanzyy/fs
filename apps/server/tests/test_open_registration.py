# -*- coding: utf-8 -*-
"""开放自助注册（VIBE_SRV_OPEN_REGISTRATION）。

为什么这组必须有测试

「打开软件 → 注册 → 直接用」这条产品形态全靠这个开关。它的失败模式很阴险：
开关关掉时**必须**回到「无邀请码即拒」，一旦漏判，等于把「任何人都能给自己
开号」静默放进了生产；开关打开时落在默认团队 + developer —— 落错团队或角色，
新人登录进去就是「什么都点不动」或「权限过大」。

这里三层都测：配置解析、service 判定、HTTP 探针（客户端登录门靠它决定
「邀请码」是必填还是可留空）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from vibe_server.errors import BadRequest, Conflict

from tests.support import TempDirTestCase, http_get, make_config, serve_app


class OpenRegistrationConfigTests(TempDirTestCase):
    def test_default_is_closed(self) -> None:
        """默认必须关：「任何人都能开号」是要显式承担的风险，不能是出厂状态。"""
        self.assertFalse(self.config.open_registration)

    def test_env_switch_turns_it_on(self) -> None:
        import os

        os.environ["VIBE_SRV_OPEN_REGISTRATION"] = "1"
        self.addCleanup(lambda: os.environ.pop("VIBE_SRV_OPEN_REGISTRATION", None))
        config = make_config(self.tmp / "env-on")
        self.assertTrue(config.open_registration)


class OpenRegistrationServiceTests(TempDirTestCase):
    def make_open_app(self):
        from vibe_server.app import build_app
        from vibe_server.clock import FrozenClock
        from dataclasses import replace

        config = replace(self.config, open_registration=True)
        app = build_app(config, clock=FrozenClock.at(1_700_000_000))
        self.addCleanup(lambda: app.store.close())
        return app

    def test_closed_rejects_tokenless_register(self) -> None:
        """开关关 + 无邀请码 → 400。漏掉这条等于生产环境静默开放注册。"""
        app = self.make_app()
        with self.assertRaises(BadRequest):
            app.auth_service.register_with_enrollment(
                enrollment_token="",
                email="new@corp.example",
                password="An0ther!Str0ngPwd",
            )

    def test_open_allows_tokenless_register_into_default_team(self) -> None:
        """开关开 + 无邀请码 → 落到默认团队、developer，且能直接登录。"""
        app = self.make_open_app()
        session = app.auth_service.register_with_enrollment(
            enrollment_token="",
            email="new@corp.example",
            password="An0ther!Str0ngPwd",
            display_name="Newbie",
        )
        self.assertTrue(session["accessToken"])
        self.assertIn("developer", session["user"]["roles"])
        self.assertNotIn("owner", session["user"]["roles"])

        memberships = app.store.list_memberships(session["user"]["id"])
        self.assertEqual(len(memberships), 1, "开放注册必须落进恰好一个团队（默认团队）")

        again = app.auth_service.login(email="new@corp.example", password="An0ther!Str0ngPwd")
        self.assertTrue(again["refreshToken"])

    def test_open_still_rejects_duplicate_email(self) -> None:
        app = self.make_open_app()
        app.auth_service.register_with_enrollment(
            enrollment_token="", email="dup@corp.example", password="An0ther!Str0ngPwd"
        )
        with self.assertRaises(Conflict):
            app.auth_service.register_with_enrollment(
                enrollment_token="", email="dup@corp.example", password="An0ther!Str0ngPwd"
            )

    def test_open_does_not_break_enrollment_path(self) -> None:
        """开关开时**带邀请码**仍走令牌路径（角色/团队由令牌指定），不被开放注册吞掉。"""
        app = self.make_open_app()
        app.auth_service.bootstrap_org(email="owner@corp.example", password="Str0ng!Passw0rd")
        actor = app.auth_service.login(email="owner@corp.example", password="Str0ng!Passw0rd")
        enrollment = app.auth_service.create_enrollment(
            actor=app.auth_service.authenticate(actor["accessToken"]),
            role="viewer",
            max_uses=1,
        )
        session = app.auth_service.register_with_enrollment(
            enrollment_token=enrollment["enrollmentToken"],
            email="invited@corp.example",
            password="An0ther!Str0ngPwd",
        )
        self.assertIn("viewer", session["user"]["roles"])


class OpenRegistrationHttpTests(TempDirTestCase):
    """探针与注册端点走真实 HTTP：客户端登录门依赖它们的行为。"""

    def _post(self, base: str, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")

    def test_auth_config_probe_is_public_and_reflects_switch(self) -> None:
        from dataclasses import replace
        from vibe_server.app import build_app
        from vibe_server.clock import FrozenClock

        closed = self.make_app()
        base = serve_app(self, closed)
        status, body, _ = http_get(base, "/api/v1/auth/config")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["registrationOpen"])

        config = replace(self.config, open_registration=True, db_path=self.tmp / "b.db",
                         data_dir=self.tmp / "b-data")
        opened = build_app(config, clock=FrozenClock.at(1_700_000_000))
        self.addCleanup(lambda: opened.store.close())
        base2 = serve_app(self, opened)
        status, body, _ = http_get(base2, "/api/v1/auth/config")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["registrationOpen"])

    def test_register_endpoint_roundtrip_without_token(self) -> None:
        from dataclasses import replace
        from vibe_server.app import build_app
        from vibe_server.clock import FrozenClock

        config = replace(self.config, open_registration=True)
        app = build_app(config, clock=FrozenClock.at(1_700_000_000))
        self.addCleanup(lambda: app.store.close())
        base = serve_app(self, app)

        status, body = self._post(
            base,
            "/api/v1/auth/register",
            {"email": "web@corp.example", "password": "An0ther!Str0ngPwd"},
        )
        self.assertEqual(status, 201, f"开放注册下无令牌注册被拒之：{body}")
        self.assertTrue(body["accessToken"])

    def test_register_endpoint_closed_returns_400(self) -> None:
        app = self.make_app()
        base = serve_app(self, app)
        status, _body = self._post(
            base,
            "/api/v1/auth/register",
            {"email": "web@corp.example", "password": "An0ther!Str0ngPwd"},
        )
        self.assertEqual(status, 400)


if __name__ == "__main__":
    import unittest

    unittest.main()
