"""CLI 首跑链路：`bootstrap` / `doctor`。

## 为什么这组必须有测试

这条链路的失败**不会报错**：`bootstrap` 建好了管理员、打印了成功，
然后在你准备拉人进来时才发现注册令牌压根没签出来。
内网首次部署正好卡在这一步，而「第一次就失败」的体验最伤。

具体回归：`cmd_bootstrap` 曾用一枚 `device_id="cli-bootstrap"` 的访问令牌
去调 `authenticate()`，但服务端从来没有这条设备记录，于是被当成
「设备已被撤销」—— 命令返回 1，管理员建好了、令牌没有。
所以这里的断言不止「打印了一行字」，而是**真的拿那枚令牌去注册一个开发者**。
"""

from __future__ import annotations

import contextlib
import io
import unittest

from vibe_server.__main__ import main

from tests.support import TempDirTestCase, make_config


class CliBootstrapTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        # 让 CLI 与应用共享同一份 data_dir（含 jwt.key）与同一个 db，
        # 否则两边会各自生成密钥、各自建库，「CLI 建的」和「应用看的」不是一份东西。
        self.db = self.tmp / "gov.db"
        self.data_dir = self.tmp
        self.email = "owner@corp.example"
        self.password = "Str0ng!Passw0rd"

    # ------------------------------------------------------------------ 工具
    def run_cli(self, *argv: str) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = main(["--data-dir", str(self.data_dir), "--db", str(self.db), *argv])
        return code, buf.getvalue()

    def bootstrap(self, **extra: str) -> tuple[int, str]:
        argv = ["bootstrap", "--email", self.email, "--password", self.password]
        for key, value in extra.items():
            argv += [f"--{key.replace('_', '-')}", value]
        return self.run_cli(*argv)

    def make_shared_app(self):
        """在与 CLI 相同的 data_dir / db 上建一个应用（共用 jwt.key 与库）。"""
        from vibe_server.app import build_app
        from vibe_server.clock import FrozenClock

        config = make_config(self.tmp, data_dir=self.data_dir, db_path=self.db)
        app = build_app(config, clock=FrozenClock.at(1_700_000_000))
        self.addCleanup(lambda: _close(app))
        return app

    def extract_enrollment_token(self, output: str) -> str:
        """取「已签发注册令牌」标记之后的第一个非空行。"""
        lines = output.splitlines()
        for index, line in enumerate(lines):
            if "已签发注册令牌" not in line:
                continue
            for candidate in lines[index + 1:]:
                if candidate.strip():
                    return candidate.strip()
        self.fail(f"没从输出里找到注册令牌：\n{output}")


def _close(app) -> None:
    try:
        app.store.close()
    except Exception:
        pass


class BootstrapFlowTests(CliBootstrapTests):
    def test_bootstrap_creates_owner_and_a_usable_enrollment_token(self) -> None:
        """核心：`bootstrap` 必须真的签出一枚**能用**的注册令牌。

        只断言「输出里有令牌」是不够的 —— 之前那版连输出都没有。
        """
        code, output = self.bootstrap()
        self.assertEqual(code, 0, f"bootstrap 没成功：{output}")
        self.assertNotIn("操作失败", output)
        self.assertIn("已创建组织所有者", output)
        self.assertIn("已签发注册令牌", output)

        token = self.extract_enrollment_token(output)

        # 拿这枚令牌真的开一个开发者账号 —— 这才证明它有效
        app = self.make_shared_app()
        session = app.auth_service.register_with_enrollment(
            enrollment_token=token,
            email="dev@corp.example",
            password="An0ther!Str0ngPwd",
            device_name="dev-laptop",
            platform="test",
            fingerprint="fp-dev-1",
        )
        self.assertTrue(session["accessToken"])
        self.assertEqual(session["user"]["email"], "dev@corp.example")
        self.assertIn("developer", session["user"]["roles"])

        # 而且这个新账号能正常登录
        again = app.auth_service.login(
            email="dev@corp.example",
            password="An0ther!Str0ngPwd",
            device_name="dev-laptop",
            platform="test",
            fingerprint="fp-dev-1",
        )
        self.assertTrue(again["refreshToken"])

    def test_enrollment_token_cannot_be_reused(self) -> None:
        """默认只签 1 次：拉第二个人要重新签发。"""
        code, output = self.bootstrap()
        self.assertEqual(code, 0)
        token = self.extract_enrollment_token(output)

        app = self.make_shared_app()
        app.auth_service.register_with_enrollment(
            enrollment_token=token,
            email="dev1@corp.example",
            password="An0ther!Str0ngPwd",
            device_name="d1", platform="test", fingerprint="fp1",
        )
        with self.assertRaises(Exception):
            app.auth_service.register_with_enrollment(
                enrollment_token=token,
                email="dev2@corp.example",
                password="An0ther!Str0ngPwd",
                device_name="d2", platform="test", fingerprint="fp2",
            )

    def test_no_enrollment_flag_skips_token_issuance(self) -> None:
        code, output = self.bootstrap(enrollment_uses="0")
        self.assertEqual(code, 0)
        self.assertIn("已创建组织所有者", output)
        self.assertNotIn("已签发注册令牌", output)

    def test_bootstrap_is_refused_once_the_org_exists(self) -> None:
        self.assertEqual(self.bootstrap()[0], 0)
        code, output = self.bootstrap()
        self.assertEqual(code, 1, "组织已存在时 bootstrap 仍然成功了")
        self.assertIn("已初始化", output)


class DoctorTests(CliBootstrapTests):
    def test_doctor_flags_a_missing_global_policy_then_passes_after_publish(self) -> None:
        """`doctor` 的价值在于把「环境少配了一件事」前置到启动之前。

        首次自检应当明确指出「还没发布全局策略」，发布之后转为通过。
        """
        self.assertEqual(self.bootstrap()[0], 0)

        code, output = self.run_cli("doctor")
        self.assertEqual(code, 1, "缺策略时 doctor 应当报出问题")
        self.assertIn("未发布全局策略", output)

        # 用 CLI 建的所有者登录，发布一份全局策略
        app = self.make_shared_app()
        session = app.auth_service.login(
            email=self.email,
            password=self.password,
            device_name="admin", platform="test", fingerprint="fp-admin",
        )
        actor = app.auth_service.authenticate(session["accessToken"])
        app.policy_service.publish(
            actor=actor,
            scope_type="global",
            scope_id="",
            document={"allowCloud": False, "maxIterations": 30},
        )

        code, output = self.run_cli("doctor")
        self.assertEqual(code, 0, f"发布策略后 doctor 仍报错：\n{output}")
        self.assertIn("自检通过", output)
        self.assertIn("全局策略版本 1", output)


if __name__ == "__main__":
    unittest.main()
