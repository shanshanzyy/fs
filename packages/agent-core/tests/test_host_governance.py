"""桌面端治理面登录闭环（M3+）的 host 端点测试。

这一层要钉死的不是 HTTP 细节，而是**闭环语义**与**安全边界**：

1. **没配地址 = 400，不是静默失败。** 用户点了「登录」却没填治理面地址时，
   必须当场告诉他，而不是发一个必然失败的请求。
2. **登录成功要「热接入」。** 令牌写进用户级凭证文件后，当前会话的模型客户端与
   策略闸门**立即**换成治理面那一套 —— 不重建会话，对话 / 检查点 / 审计链都保留。
   这条最容易被「接了一半」蒙混：拉到了策略、界面也变了，但 Agent 还用着旧客户端。
3. **令牌绝不外泄。** 响应只回 `describe()` 的掩码视图；把整段响应文本拿出来
   逐字搜令牌是唯一靠得住的检查。
4. **执行中 409。** 登录会换掉模型客户端与策略闸门，与「执行中不可改模型设置」
   同一纪律（ADR-011 的「用户本人动作」范式）。
5. **退不出去是最糟的失败。** 治理面不可达时登出也必须清掉本地凭证 ——
   否则用户永远退不出登录。

全程**不打真实网络**：`wire` 与 `GovernanceClient` 都是替身，凭证落临时目录。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.governance import runtime as gov_runtime  # noqa: E402
from agent_core.governance.client import GovernanceError  # noqa: E402
from agent_core.governance.credentials import CredentialStore, Credentials  # noqa: E402
from agent_core.host import session as session_module  # noqa: E402
from agent_core.host import protocol as P  # noqa: E402
from agent_core.host.server import HostServer  # noqa: E402
from tests.test_agent_loop import FakeClient  # noqa: E402

SERVER = "http://governance.local:8080"
TOKEN = "access-tok-xyz"  # 用于「响应里不许出现它」的断言

#: 策略替身下发的运行时约束：登录后必须落到 Agent 的闸门上。
CONSTRAINTS = gov_runtime.RuntimeConstraints(
    allow_cloud=True,
    denied_paths=("secrets/**",),
    denied_commands=("rm",),
    require_approval=("run_command",),
)


class _FakeGovernanceClient:
    """`GovernanceClient` 替身：只实现登录 / 注册 / 登出。

    用替身而不是真发请求，是因为这里要验的是**会话装配决策**（热接入 / 409 / 凭证），
    不是认证协议本身（那在服务端测试里覆盖）。
    """

    behavior: dict = {}

    def __init__(self, server: str, *, store=None, credentials=None) -> None:
        self.server = server
        self.store = store
        self.credentials = credentials
        self.logout_all: bool | None = None

    def _issue(self, *, user_id: str, email: str, device_id: str, refresh: str) -> Credentials:
        error = self.behavior.get("error")
        if error is not None:
            raise error
        cred = Credentials(
            server=self.server,
            user_id=user_id,
            email=email,
            device_id=device_id,
            access_token=TOKEN,
            refresh_token=refresh,
            expires_at=9_999_999_999,
        )
        if self.store is not None:
            self.store.save(cred)
        self.credentials = cred
        return cred

    def login(self, *, email: str, password: str, device_name: str = "") -> Credentials:
        return self._issue(user_id="u-1", email=email, device_id="d-1", refresh="r-1")

    def register(
        self,
        *,
        enrollment_token: str,
        email: str,
        password: str,
        display_name: str = "",
        device_name: str = "",
    ) -> Credentials:
        return self._issue(user_id="u-2", email=email, device_id="d-2", refresh="r-2")

    def logout(self, *, all_devices: bool = False) -> bool:
        self.logout_all = all_devices
        if self.store is not None:
            self.store.clear()
        return True

    def auth_config(self) -> dict:
        """策略探针替身：行为由 behavior["auth_config"] 控制（默认开放注册）。"""
        error = self.behavior.get("error")
        if error is not None:
            raise error
        return dict(self.behavior.get("auth_config", {"registrationOpen": True}))


class _GovHostFixture(unittest.TestCase):
    """临时工作区 + 真实 host；`wire` 与 `GovernanceClient` 换成替身。"""

    #: 初始配置里的治理面地址（子类可改成空，测「没配地址」）
    server_url = SERVER

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.ws = (root / "ws").resolve()
        self.ws.mkdir()
        # 凭证落临时目录：绝不碰开发机真实的 ~/.vibe
        self.store = CredentialStore(root / "credentials.json", strict_permissions=False)
        _FakeGovernanceClient.behavior = {}

        self._saved_settings = os.environ.get("VIBE_SETTINGS")
        os.environ["VIBE_SETTINGS"] = str(root / "settings.json")
        self.addCleanup(self._restore_env)

        for name, value in (
            ("wire", self._fake_wire),
            ("GovernanceClient", _FakeGovernanceClient),
            ("CredentialStore", lambda: self.store),
        ):
            patcher = mock.patch.object(session_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        config = replace(Config.load(self.ws), server_url=self.server_url)
        self.host = HostServer(config, port=0, client=FakeClient([[("list_dir", {})], "好的。"]))
        self.host.start_background()
        self.base = f"http://127.0.0.1:{self.host.port}"
        self.token = self.host.guard.token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _restore_env(self) -> None:
        if self._saved_settings is None:
            os.environ.pop("VIBE_SETTINGS", None)
        else:
            os.environ["VIBE_SETTINGS"] = self._saved_settings

    def tearDown(self) -> None:
        self.host.shutdown()

    # ---------------------------------------------------------------- 替身
    def _fake_wire(self, config, **kwargs) -> gov_runtime.GovernanceRuntime:
        """按「凭证文件里有没有可用的登录」产出装配结果。"""
        credentials = self.store.load()
        if not config.server_url:
            return gov_runtime.GovernanceRuntime(
                config=config, constraints=gov_runtime.RuntimeConstraints(), status="off"
            )
        if credentials is None:
            return gov_runtime.GovernanceRuntime(
                config=config,
                constraints=gov_runtime.RuntimeConstraints(),
                status="unauthenticated",
                degraded_reason="已配置治理面但本机没有可用登录。",
            )
        client = _FakeGovernanceClient(
            config.server_url, store=self.store, credentials=credentials
        )
        return gov_runtime.GovernanceRuntime(
            config=config,
            constraints=CONSTRAINTS,
            client=None,
            governance=client,
            status="governed",
        )

    # ---------------------------------------------------------------- helpers
    def _request(self, path: str, *, method: str = "POST", body: dict | None = None):
        data = json.dumps(body or {}).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("X-Vibe-Token", self.token)
        request.add_header("Content-Type", "application/json")
        return self._opener.open(request, timeout=15)

    def _call(self, path: str, body: dict | None = None) -> tuple[int, dict, str]:
        """返回 (状态码, JSON, 原始文本)；4xx/5xx 不当异常抛，便于断言错误码。"""
        try:
            with self._request(path, body=body) as response:
                text = response.read().decode("utf-8")
                return response.status, json.loads(text), text
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8")
            return exc.code, json.loads(text), text


class GovernanceLoginTests(_GovHostFixture):
    def test_login_hot_wires_current_session(self) -> None:
        status, data, text = self._call(
            "/api/governance/login", {"email": "dev@example.com", "password": "pw"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["governance"]["status"], "governed")
        # 凭证已落盘（下次启动 wire() 能读到，无需重启）
        saved = self.store.load()
        self.assertIsNotNone(saved)
        self.assertEqual(saved.email, "dev@example.com")
        # 热接入：会话快照、Agent 客户端、策略闸门都换了，且**不重建会话**
        self.assertEqual(self.host.session.describe()["governance"]["status"], "governed")
        self.assertEqual(self.host.session.config.server_url, SERVER)
        self.assertIsNotNone(self.host.session.agent.client)
        self.assertIn("run_command", self.host.session.agent._policy_approval)
        self.assertEqual(self.host.session.agent.paths.denied_patterns, ("secrets/**",))
        self.assertIn("rm", self.host.session.agent.commands.policy_denied)
        # 工具每次调用读的是 ctx 上的引用 —— 必须指向新守卫，否则约束不生效
        self.assertIs(self.host.session.agent.ctx.paths, self.host.session.agent.paths)
        self.assertIs(self.host.session.agent.ctx.commands, self.host.session.agent.commands)

    def test_response_never_leaks_token(self) -> None:
        _status, _data, text = self._call(
            "/api/governance/login", {"email": "dev@example.com", "password": "pw"}
        )
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("refresh_token", text)

    def test_login_rejected_credentials_pass_status_through(self) -> None:
        _FakeGovernanceClient.behavior = {"error": GovernanceError("邮箱或密码不正确", status=401)}
        status, data, _text = self._call(
            "/api/governance/login", {"email": "dev@example.com", "password": "bad"}
        )
        self.assertEqual(status, 401)
        self.assertIn("密码", data["error"])

    def test_login_unreachable_returns_502(self) -> None:
        _FakeGovernanceClient.behavior = {"error": GovernanceError("治理面不可达", status=0)}
        status, _data, _text = self._call(
            "/api/governance/login", {"email": "dev@example.com", "password": "pw"}
        )
        self.assertEqual(status, 502)


class GovernanceAddressTests(_GovHostFixture):
    """会话没配治理面：地址只能来自请求体 —— 这正是同事机器上的首次登录路径。"""

    server_url = ""

    def setUp(self) -> None:
        super().setUp()
        # 未配治理面时整个治理视图为 None（纯本地用户不该看到治理相关的噪音）
        self.assertIsNone(self.host.session.describe()["governance"])

    def test_login_without_address_is_rejected(self) -> None:
        # 会话没配治理面，请求里也不填地址 → 400，且不碰凭证
        status, data, _text = self._call(
            "/api/governance/login", {"email": "dev@example.com", "password": "pw"}
        )
        self.assertEqual(status, 400)
        self.assertIn("治理面地址", data["error"])
        self.assertIsNone(self.store.load())

    def test_login_uses_request_address_when_unconfigured(self) -> None:
        status, data, _text = self._call(
            "/api/governance/login",
            {"email": "dev@example.com", "password": "pw", "serverUrl": SERVER},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["governance"]["status"], "governed")
        self.assertEqual(data["governance"]["serverUrl"], SERVER)

    def test_bad_scheme_is_rejected(self) -> None:
        status, data, _text = self._call(
            "/api/governance/login",
            {"email": "dev@example.com", "password": "pw", "serverUrl": "gov.local"},
        )
        self.assertEqual(status, 400)
        self.assertIn("http://", data["error"])


class GovernanceRegisterTests(_GovHostFixture):
    def test_register_creates_account_and_wires(self) -> None:
        status, data, _text = self._call(
            "/api/governance/register",
            {"enrollmentToken": "invite-1", "email": "new@example.com", "password": "pw"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["governance"]["status"], "governed")
        saved = self.store.load()
        self.assertEqual(saved.email, "new@example.com")
        self.assertEqual(saved.user_id, "u-2")

    def test_register_without_token_passes_through(self) -> None:
        """开放自助注册：邀请码留空也必须能注册（是否放行由服务端开关决定，
        内核只负责如实透传 —— 在 host 层拦「无邀请码」会把开放注册挡死）。"""
        status, data, _text = self._call(
            "/api/governance/register",
            {"enrollmentToken": "", "email": "open@example.com", "password": "pw"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["governance"]["status"], "governed")
        saved = self.store.load()
        self.assertEqual(saved.email, "open@example.com")


class GovernanceConfigProbeTests(_GovHostFixture):
    """`GET /api/governance/config`：登录门的策略探针。

    探针的关键是**永不 409 / 永不写凭证**：执行中也得能探（它只读），
    治理面不可达时回 ok=False 而不是抛错 —— 界面据此退化为「邀请码按需填写」。
    """

    def _get(self, path: str) -> tuple[int, dict]:
        request = urllib.request.Request(self.base + path, method="GET")
        request.add_header("X-Vibe-Token", self.token)
        try:
            with self._opener.open(request, timeout=15) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")

    def test_probe_reports_open_registration(self) -> None:
        status, data = self._get("/api/governance/config")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data["registrationOpen"])

    def test_probe_reports_closed_registration(self) -> None:
        _FakeGovernanceClient.behavior = {"auth_config": {"registrationOpen": False}}
        status, data = self._get("/api/governance/config")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertFalse(data["registrationOpen"])

    def test_probe_survives_unreachable_server(self) -> None:
        _FakeGovernanceClient.behavior = {"error": GovernanceError("不可达", status=0)}
        status, data = self._get("/api/governance/config")
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])

    def test_probe_allowed_while_running(self) -> None:
        self.host.session._state = P.STATE_RUNNING
        status, data = self._get("/api/governance/config")
        self.assertEqual(status, 200)
        self.assertIn("ok", data)


class GovernanceLogoutTests(_GovHostFixture):
    def setUp(self) -> None:
        super().setUp()
        # 先登录，再测退出
        self._call("/api/governance/login", {"email": "dev@example.com", "password": "pw"})
        self.assertIsNotNone(self.store.load())

    def test_logout_clears_credentials_and_drops_to_local(self) -> None:
        status, data, text = self._call("/api/governance/logout", {})
        self.assertEqual(status, 200)
        self.assertIsNone(self.store.load())
        self.assertEqual(data["governance"]["status"], "unauthenticated")
        # 退出后不再持有云端客户端（回到本地直连）
        self.assertNotIn(TOKEN, text)

    def test_logout_survives_unreachable_server(self) -> None:
        """治理面不可达也必须退得出去 —— 否则用户被永久困在登录态。"""
        _FakeGovernanceClient.behavior = {"error": GovernanceError("不可达", status=0)}
        status, _data, _text = self._call("/api/governance/logout", {})
        self.assertEqual(status, 200)
        self.assertIsNone(self.store.load())


class GovernanceBusyTests(_GovHostFixture):
    def test_login_register_logout_return_409_while_running(self) -> None:
        self.host.session._state = P.STATE_RUNNING
        for path, body in (
            ("/api/governance/login", {"email": "a@b.c", "password": "pw"}),
            ("/api/governance/register", {"enrollmentToken": "t", "email": "a@b.c", "password": "pw"}),
            ("/api/governance/logout", {}),
        ):
            with self.subTest(path=path):
                status, data, _text = self._call(path, body)
                self.assertEqual(status, 409)
                self.assertIn("任务执行中", data["error"])


if __name__ == "__main__":
    unittest.main()
