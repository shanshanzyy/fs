"""治理面接线（M3）的契约测试。

治理面在 M2 已经「完整」，M3 的工作是把它们**接进启动路径**。接线最容易出错的
地方不是逻辑，而是**条件**与**降级语义**：

1. 没配 `VIBE_SERVER_URL` 的用户必须一行行为都不变（治理是可选增强，
   不是把纯本地用户劫持进一个他们没要求过的依赖）。这条一旦破，
   症状是「我什么都没配，为什么模型请求往外发了」。
2. 配了但没登录 / 服务端不可达时必须**降级继续**，并且把原因放进 notices。
   静默降级比不降级更糟：用户会以为自己在用云端模型或以为自己被策略管着。
3. 桌面壳（Electron）必须真的把地址传下去。壳与后端之间靠环境变量交接，
   这类「两边各写一份字符串」的约定最容易悄悄漂移，所以用静态契约钉死。

本文件**不打任何真实网络**：凭据目录与策略缓存都落在临时目录里，
服务端交互由替身客户端完成。
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
AGENT_CORE = ROOT / "packages" / "agent-core"
DESKTOP = ROOT / "apps" / "desktop"
sys.path.insert(0, str(AGENT_CORE))

from agent_core.config import Config  # noqa: E402
from agent_core.governance import client as gov_client  # noqa: E402
from agent_core.governance import runtime as gov_runtime  # noqa: E402
from agent_core.governance.credentials import CredentialStore, Credentials  # noqa: E402
from agent_core.llm.openai_compat import OpenAICompatClient  # noqa: E402

SERVER = "http://governance.local:8080"


class _FakeResponse:
    def __init__(self, status: int, body=None, headers=None) -> None:
        self.status = status
        self.body = body if body is not None else {}
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")


class _FakeGovernanceClient:
    """`GovernanceClient` 的替身：只实现 `wire()` 真正会调用的那几个方法。

    用替身而不是真发请求，是因为这里要验的是**装配决策**（哪种情况走哪条分支），
    不是 HTTP 细节 —— 那些在服务端测试里已经覆盖过了。
    """

    #: 测试通过它注入行为（setUp 里重置，避免用例之间互相污染）
    behavior: dict = {}
    instances: list = []

    def __init__(self, server: str, *, store=None, credentials=None) -> None:
        self.server = server
        self.store = store
        self.credentials = credentials
        _FakeGovernanceClient.instances.append(self)

    def ensure_token(self):
        if self.behavior.get("token_error"):
            raise gov_client.GovernanceError(self.behavior["token_error"])
        return getattr(self.credentials, "access_token", "token")

    def fetch_policy(self, etag: str | None = None):
        self.policy_etag = etag
        response = self.behavior.get("policy_response")
        if response is None:
            raise gov_client.GovernanceError("服务端不可达")
        return response

    def fetch_audit_schema(self):
        schema = self.behavior.get("audit_schema")
        if schema is None:
            raise gov_client.GovernanceError("事件契约不可达")
        return schema

    def fetch_quota(self, **query: str):
        quota = self.behavior.get("quota")
        if quota is None:
            raise gov_client.GovernanceError("配额不可达")
        return quota

    def gateway_base_url(self) -> str:
        return f"{self.server}/api/v1/gateway/v1"


def _policy_response(**policy) -> _FakeResponse:
    """形如真实 `/api/v1/policy` 的响应：策略文档 + 版本号 + ETag。"""
    return _FakeResponse(
        200,
        body={"policy": policy, "version": 7},
        headers={"ETag": 'W/"7"'},
    )


CLOUD_ON = {"allowCloud": True, "routing": {"allowCloud": True, "sensitiveForceLocal": True}}
AUDIT_SCHEMA = {"fields": {"kind": {"type": "string"}}, "eventTypes": ["tool.call"]}
#: 形如真实 `GET /api/v1/quota` 的返回。三条语义都要能对上：
#: `remaining.cloudTokens` 有值 = 已知额度；`null` = 不限；拉取失败 = 视图整个为 None。
QUOTA_VIEW = {
    "day": "2026-09-24",
    "usage": {"cloudTokens": 8_000, "reservedTokens": 0, "localTokens": 1_000, "requests": 12},
    "limits": {"dailyCloudTokens": 20_000, "dailyRequests": 0, "requestsPerMinute": 60},
    "remaining": {"cloudTokens": 12_000, "requests": None},
    "resetInSeconds": 3_600,
}


class WireStatusTests(unittest.TestCase):
    """`wire()` 的四种状态：off / unauthenticated / degraded / governed。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.ws = self.root / "workspace"
        self.ws.mkdir()
        # 凭据与策略缓存都放临时目录：绝不碰用户真实的 ~/.vibe
        self.store = CredentialStore(
            self.root / "credentials.json", strict_permissions=False
        )

        _FakeGovernanceClient.behavior = {}
        _FakeGovernanceClient.instances = []
        patcher = mock.patch.object(
            gov_runtime, "GovernanceClient", _FakeGovernanceClient
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _config(self, server_url: str = "") -> Config:
        return Config.load(self.ws, server_url=server_url)

    def _login(self, *, server: str = SERVER) -> None:
        self.store.save(
            Credentials(
                server=server,
                user_id="u-1",
                email="dev@example.com",
                device_id="d-1",
                access_token="access-1",
                refresh_token="refresh-1",
                expires_at=9_999_999_999,
            )
        )

    def _write_cache(self, document: dict) -> None:
        """按 PolicyCache 自己的存法写缓存，避免在测试里硬编码 JSON 键名。"""
        cache = gov_runtime.PolicyCache(path=self.store.path.parent / "policy-cache.json")
        cache.store(document=document, etag='W/"1"', version=3, now=1)

    # ---- off -------------------------------------------------------------
    def test_off_when_not_configured(self) -> None:
        """没配地址 = M0/M1 原行为，且**根本不碰凭据文件**。"""
        runtime = gov_runtime.wire(self._config(), store=self.store)
        self.assertEqual(runtime.status, "off")
        self.assertIsNone(runtime.client)
        self.assertIsNone(runtime.governance)
        self.assertEqual(runtime.notices, [])
        self.assertFalse(self.store.path.exists())
        self.assertEqual(_FakeGovernanceClient.instances, [])

    def test_describe_exposes_server_url_only_when_configured(self) -> None:
        """界面靠 `serverUrl` 把「去登录」指向 `<serverUrl>/login`（Q 阶段）。

        没配治理面时必须**不出现**该字段 —— 纯本地用户不该看到治理相关的东西。
        """
        off = gov_runtime.wire(self._config(), store=self.store).describe()
        self.assertNotIn("serverUrl", off)

        view = gov_runtime.wire(self._config(SERVER), store=self.store).describe()
        self.assertEqual(view["serverUrl"], SERVER)

    # ---- unauthenticated --------------------------------------------------
    def test_unauthenticated_without_credentials(self) -> None:
        runtime = gov_runtime.wire(self._config(SERVER), store=self.store)
        self.assertEqual(runtime.status, "unauthenticated")
        self.assertIsNone(runtime.client)
        self.assertTrue(runtime.degraded_reason)
        self.assertTrue(runtime.notices)
        self.assertIn("login", runtime.degraded_reason)

    def test_unauthenticated_when_credentials_belong_to_another_server(self) -> None:
        """换服务端后旧凭据必须失效 —— 否则会拿 A 的令牌去调 B。"""
        self._login(server="http://other.local:8080")
        runtime = gov_runtime.wire(self._config(SERVER), store=self.store)
        self.assertEqual(runtime.status, "unauthenticated")

    # ---- degraded --------------------------------------------------------
    def test_degraded_when_token_refresh_fails(self) -> None:
        """登录被吊销：和「从没登录」区分开，原因要如实说。"""
        self._login()
        _FakeGovernanceClient.behavior = {"token_error": "refresh_token 已失效"}
        runtime = gov_runtime.wire(self._config(SERVER), store=self.store)
        self.assertEqual(runtime.status, "degraded")
        self.assertIsNone(runtime.client)
        self.assertIn("refresh_token 已失效", runtime.degraded_reason)

    def test_degraded_when_policy_unreachable_and_no_cache(self) -> None:
        self._login()
        runtime = gov_runtime.wire(self._config(SERVER), store=self.store)
        self.assertEqual(runtime.status, "degraded")
        self.assertTrue(runtime.notices)

    def test_falls_back_to_cache_when_server_unreachable(self) -> None:
        """有缓存就按缓存继续 —— 这正是 PolicyCache 存在的意义。"""
        self._login()
        self._write_cache(CLOUD_ON)
        runtime = gov_runtime.wire(self._config(SERVER), store=self.store)
        self.assertEqual(runtime.status, "governed")
        self.assertTrue(any("缓存" in note for note in runtime.notices))

    # ---- governed --------------------------------------------------------
    def test_governed_when_policy_fetched(self) -> None:
        self._login()
        _FakeGovernanceClient.behavior = {"policy_response": _policy_response(**CLOUD_ON)}
        runtime = gov_runtime.wire(self._config(SERVER), store=self.store)
        self.assertEqual(runtime.status, "governed")
        # 策略允许上云 → 模型客户端换成走网关的那个（不是 None）
        self.assertIsNotNone(runtime.client)
        self.assertTrue(runtime.constraints.allow_cloud)

    def test_gateway_routed_only_when_policy_allows_cloud(self) -> None:
        """`gateway_routed` 决定界面能不能改模型，必须跟着「是否真的走网关」走。

        策略禁云时请求全部落到本地客户端，本机 settings.json 就是权威；
        此时若仍标记「已接管」，界面就会挡住一次本来有效的保存。
        """
        self._login()
        _FakeGovernanceClient.behavior = {"policy_response": _policy_response(**CLOUD_ON)}
        cloud = gov_runtime.wire(self._config(SERVER), store=self.store)
        self.assertTrue(cloud.gateway_routed)
        self.assertTrue(cloud.locks_local_config)

        _FakeGovernanceClient.behavior = {
            "policy_response": _policy_response(
                allowCloud=False, routing={"allowCloud": False}
            )
        }
        local = gov_runtime.wire(self._config(SERVER), store=self.store)
        self.assertFalse(local.gateway_routed)
        self.assertFalse(local.locks_local_config)
        # 闸门函数与属性同一判据：两个入口（settings / MCP）都只认这一个。
        self.assertFalse(gov_runtime.is_config_locked(local))
        self.assertTrue(gov_runtime.is_config_locked(object()))  # 未知形态 fail-closed

    def test_uploader_absent_when_schema_unavailable(self) -> None:
        """契约拉不到就不上传（fail-closed）：宁可不传，也不传不合契约的事件。"""
        self._login()
        _FakeGovernanceClient.behavior = {"policy_response": _policy_response(**CLOUD_ON)}
        runtime = gov_runtime.wire(self._config(SERVER), store=self.store)
        self.assertIsNone(runtime.uploader)
        self.assertTrue(any("契约" in note for note in runtime.notices))

    def test_uploader_built_when_schema_available(self) -> None:
        self._login()
        _FakeGovernanceClient.behavior = {
            "policy_response": _policy_response(**CLOUD_ON),
            "audit_schema": AUDIT_SCHEMA,
        }
        runtime = gov_runtime.wire(self._config(SERVER), store=self.store)
        self.assertIsNotNone(runtime.uploader)

    def test_local_only_policy_keeps_local_client(self) -> None:
        """策略不允许上云 → 一律走本地，并在 notices 里说明。"""
        self._login()
        _FakeGovernanceClient.behavior = {
            "policy_response": _policy_response(allowCloud=False),
            "audit_schema": AUDIT_SCHEMA,
        }
        runtime = gov_runtime.wire(self._config(SERVER), store=self.store)
        self.assertEqual(runtime.status, "governed")
        self.assertFalse(runtime.constraints.allow_cloud)
        # 禁云时给的是一个**本地**客户端（按收紧后的配置构建的），不走网关
        self.assertIsInstance(runtime.client, OpenAICompatClient)
        self.assertTrue(any("本地" in note for note in runtime.notices))

    # ---- 配额视图 ---------------------------------------------------------
    def test_quota_view_surfaces_when_available(self) -> None:
        """拉到配额 → 进 describe()。界面读的就是这条路径。"""
        self._login()
        _FakeGovernanceClient.behavior = {
            "policy_response": _policy_response(**CLOUD_ON),
            "quota": QUOTA_VIEW,
        }
        view = gov_runtime.wire(self._config(SERVER), store=self.store).describe()
        self.assertEqual(view["quota"]["remaining"]["cloudTokens"], 12_000)
        self.assertEqual(view["quota"]["limits"]["dailyCloudTokens"], 20_000)
        self.assertEqual(view["quotaError"], "")

    def test_quota_unavailable_does_not_degrade_or_warn(self) -> None:
        """配额拉不到 = 「看不到」，不是「不让干活」。

        所以状态仍是 governed、notices 里不许多一条 —— 否则这条噪音会
        混进「策略禁用云端」那类真正需要用户看见的提示里。
        """
        self._login()
        _FakeGovernanceClient.behavior = {"policy_response": _policy_response(**CLOUD_ON)}
        runtime = gov_runtime.wire(self._config(SERVER), store=self.store)
        view = runtime.describe()
        self.assertEqual(runtime.status, "governed")
        self.assertIsNone(view["quota"])
        self.assertIn("配额不可达", view["quotaError"])
        self.assertFalse(any("配额" in note for note in runtime.notices))

    def test_quota_absent_when_not_configured(self) -> None:
        """没接入治理面时配额是 None（而不是 0）—— 「没有这份信息」与「额度为零」不同。"""
        view = gov_runtime.wire(self._config(), store=self.store).describe()
        self.assertIsNone(view["quota"])
        self.assertEqual(view["quotaError"], "")


class HostWiringTests(unittest.TestCase):
    """桌面端后端（host）是否在正确的条件下调用 `wire()`。"""

    def setUp(self) -> None:
        from agent_core.host import session as session_module

        self.session_module = session_module
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name).resolve()

        self.calls: list = []
        patcher = mock.patch.object(self.session_module, "wire", self._fake_wire)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_wire(self, config, **kwargs):
        self.calls.append(config)
        return gov_runtime.GovernanceRuntime(
            config=config,
            constraints=gov_runtime.RuntimeConstraints(),
            notices=["测试用治理通知"],
            status="unauthenticated",
        )

    def test_not_wired_without_server_url(self) -> None:
        session = self.session_module.Session(Config.load(self.ws))
        self.assertEqual(self.calls, [])
        self.assertIsNone(session.governance)
        self.assertIsNone(session.describe()["governance"])
        self.assertNotIn("测试用治理通知", session.describe()["notices"])

    def test_wired_and_notices_surface_when_server_url_set(self) -> None:
        session = self.session_module.Session(Config.load(self.ws, server_url=SERVER))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0].server_url, SERVER)
        self.assertIsNotNone(session.governance)
        # 降级原因必须能被界面看到（否则用户以为策略生效了）
        self.assertIn("测试用治理通知", session.describe()["notices"])
        self.assertEqual(session.describe()["governance"]["status"], "unauthenticated")

    def test_governance_view_carries_quota_fields(self) -> None:
        """界面读的是 `session.describe()['governance']`，配额字段必须在这条路径上。

        否则 runtime 里拉到了配额、界面却永远看不到 —— 这类「接了一半」
        的漂移不会有任何报错，只是功能静静地不生效。
        """
        session = self.session_module.Session(Config.load(self.ws, server_url=SERVER))
        view = session.describe()["governance"]
        self.assertIn("quota", view)
        self.assertIn("quotaError", view)


class DesktopShellContractTests(unittest.TestCase):
    """Electron 壳与后端之间靠环境变量交接，名字漂移不会有任何报错。"""

    def _read(self, name: str) -> str:
        return (DESKTOP / "src" / name).read_text(encoding="utf-8")

    def test_backend_passes_governance_env_vars(self) -> None:
        backend = self._read("backend.js")
        self.assertIn("env.VIBE_SERVER_URL = this.config.serverUrl", backend)
        self.assertIn("env.VIBE_GATEWAY_URL = this.config.gatewayUrl", backend)

    def test_env_var_names_match_python_side(self) -> None:
        """壳里写死的环境变量名必须与 `Config.load()` 读的完全一致。"""
        config_py = (AGENT_CORE / "agent_core" / "config.py").read_text(encoding="utf-8")
        for env_name in ("VIBE_SERVER_URL", "VIBE_GATEWAY_URL"):
            with self.subTest(env=env_name):
                self.assertIn(f'"{env_name}"', config_py)
                self.assertIn(env_name, self._read("backend.js"))

    def test_desktop_config_exposes_governance_fields(self) -> None:
        main_js = self._read("main.js")
        for field in ("serverUrl", "gatewayUrl"):
            with self.subTest(field=field):
                self.assertIn(f"{field}: ''", main_js)

    def test_ui_labels_cover_every_non_off_status(self) -> None:
        """界面文案要覆盖 `wire()` 可能产出的每个状态。

        漏一个的后果是静默的：徽标显示成兜底文案，用户读到的是错的信息。
        """
        runtime_py = (
            AGENT_CORE / "agent_core" / "governance" / "runtime.py"
        ).read_text(encoding="utf-8")
        statuses = set(re.findall(r'status="([a-z_]+)"', runtime_py)) - {"off"}
        self.assertEqual(statuses, {"unauthenticated", "degraded", "governed"})

        app_js = (AGENT_CORE / "agent_core" / "host" / "ui" / "app.js").read_text(
            encoding="utf-8"
        )
        for status in statuses:
            with self.subTest(status=status):
                self.assertIn(f"{status}:", app_js)

    def test_ui_offers_login_entry_when_unauthenticated(self) -> None:
        """治理面已配置但未登录/降级时，界面必须给出通往服务端登录页的入口。

        薄壳纪律（Q 阶段）：界面只负责打开 `<serverUrl>/login`，
        登录逻辑在服务端（/login + /api/v1/auth/login），壳里不实现任何认证。
        """
        app_js = (AGENT_CORE / "agent_core" / "host" / "ui" / "app.js").read_text(
            encoding="utf-8"
        )
        for token in ("serverUrl", "/login", "去登录", "chip-link"):
            with self.subTest(token=token):
                self.assertIn(token, app_js)

    def test_ui_separates_the_three_quota_states(self) -> None:
        """配额三态（已知 / 未知 / 用尽）在界面上必须分开。

        合并成一句「没有额度」的后果是静默的：用户会把「没拉到」
        读成「不限」，然后把超额的锅算到别处。
        """
        app_js = (AGENT_CORE / "agent_core" / "host" / "ui" / "app.js").read_text(
            encoding="utf-8"
        )
        for token in ("quotaError", "remaining.cloudTokens", "未能获取", "配额用尽"):
            with self.subTest(token=token):
                self.assertIn(token, app_js)


if __name__ == "__main__":
    unittest.main()