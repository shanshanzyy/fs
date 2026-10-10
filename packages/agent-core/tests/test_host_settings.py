"""用户级模型设置（host/settings.py）的单元测试。

这层的三条契约值得钉死，因为它们错起来都会很隐蔽：

1. **坏文件自愈** —— 设置文件坏了（半截 JSON / 类型不对）只应「回到默认」，
   绝不能让后端起不来；
2. **优先级算对** —— 环境变量恒为最高优先级。这里的实现难点是
   `Config.load(**overrides)` 的 overrides 会**盖过**环境变量，所以
   `effective_model_overrides` 必须在「环境变量已设」时**不传**该字段 override；
3. **白名单落盘** —— 只有 model / base_url / api_key 能写进文件，且空字符串
   表示「不修改」（界面里 apiKey 留空即「保持原值」）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.host.server import HostServer  # noqa: E402
from agent_core.host.settings import (  # noqa: E402
    effective_model_overrides,
    env_locked_fields,
    load_settings,
    save_settings,
    settings_path,
)
from tests.test_agent_loop import FakeClient  # noqa: E402


class SettingsPathTests(unittest.TestCase):
    def test_default_is_home_vibe_settings(self) -> None:
        import os

        saved = os.environ.pop("VIBE_SETTINGS", None)
        try:
            path = settings_path()
            self.assertEqual(path.name, "settings.json")
            self.assertEqual(path.parent.name, ".vibe")
        finally:
            if saved is not None:
                os.environ["VIBE_SETTINGS"] = saved

    def test_env_overrides_path(self) -> None:
        import os

        saved = os.environ.get("VIBE_SETTINGS")
        try:
            os.environ["VIBE_SETTINGS"] = "E:/tmp/custom-settings.json"
            self.assertEqual(settings_path(), Path("E:/tmp/custom-settings.json"))
        finally:
            if saved is None:
                os.environ.pop("VIBE_SETTINGS", None)
            else:
                os.environ["VIBE_SETTINGS"] = saved


class SettingsReadWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "settings.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_load_missing_file_returns_empty(self) -> None:
        self.assertEqual(load_settings(self.path), {})

    def test_load_bad_json_returns_empty(self) -> None:
        self.path.write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(load_settings(self.path), {})

    def test_load_non_dict_returns_empty(self) -> None:
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(load_settings(self.path), {})

    def test_load_ignores_unknown_and_blank_values(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "model": "qwen2.5-coder:7b",
                    "base_url": "   ",
                    "api_key": "sk-123",
                    "workspace": "/should/be/ignored",
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            load_settings(self.path),
            {"model": "qwen2.5-coder:7b", "api_key": "sk-123"},
        )

    def test_save_then_load_roundtrip(self) -> None:
        saved = save_settings({"model": "gpt-4o-mini", "base_url": "https://api.openai.com/v1"}, self.path)
        self.assertEqual(saved["model"], "gpt-4o-mini")
        self.assertEqual(load_settings(self.path)["model"], "gpt-4o-mini")

    def test_save_only_whitelisted_keys(self) -> None:
        save_settings({"model": "m1", "pythonPath": "E:/python.exe"}, self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("model", raw)
        self.assertNotIn("pythonPath", raw)

    def test_save_blank_value_keeps_previous(self) -> None:
        """空字符串 = 不修改（界面里 apiKey 留空即保持原值）。"""
        save_settings({"model": "keep-me", "api_key": "sk-old"}, self.path)
        save_settings({"api_key": ""}, self.path)
        self.assertEqual(load_settings(self.path)["api_key"], "sk-old")

    def test_save_merges_rather_than_replaces(self) -> None:
        save_settings({"model": "m1"}, self.path)
        save_settings({"base_url": "http://127.0.0.1:11434/v1"}, self.path)
        loaded = load_settings(self.path)
        self.assertEqual(loaded["model"], "m1")
        self.assertEqual(loaded["base_url"], "http://127.0.0.1:11434/v1")

    def test_save_writes_version_marker(self) -> None:
        save_settings({"model": "m1"}, self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], 1)

    def test_save_creates_parent_directory(self) -> None:
        nested = Path(self._tmp.name) / "a" / "b" / "settings.json"
        save_settings({"model": "m1"}, nested)
        self.assertTrue(nested.exists())


class EnvLockTests(unittest.TestCase):
    def test_env_locked_reflects_non_empty_env(self) -> None:
        locked = env_locked_fields(
            {"VIBE_MODEL": "qwen2.5-coder:7b", "VIBE_BASE_URL": "   ", "VIBE_API_KEY": ""}
        )
        self.assertEqual(
            locked, {"model": True, "base_url": False, "api_key": False}
        )


class EffectiveOverridesTests(unittest.TestCase):
    def test_env_wins_and_is_not_overridden(self) -> None:
        """环境变量已设 → 不传该字段 override（否则会盖过环境变量）。"""
        result = effective_model_overrides(
            env={"VIBE_MODEL": "env-model"},
            settings={"model": "settings-model", "base_url": "http://s/v1"},
            cli={"model": "cli-model", "base_url": "http://c/v1"},
        )
        self.assertNotIn("model", result)  # 让 Config.load 自己读环境变量
        self.assertEqual(result["base_url"], "http://s/v1")  # settings 盖过 cli

    def test_settings_wins_over_cli(self) -> None:
        result = effective_model_overrides(
            env={},
            settings={"model": "settings-model"},
            cli={"model": "cli-model"},
        )
        self.assertEqual(result["model"], "settings-model")

    def test_cli_used_when_no_settings(self) -> None:
        result = effective_model_overrides(
            env={}, settings={}, cli={"model": "cli-model", "api_key": "cli-key"}
        )
        self.assertEqual(result["model"], "cli-model")
        self.assertEqual(result["api_key"], "cli-key")

    def test_all_empty_yields_no_overrides(self) -> None:
        self.assertEqual(effective_model_overrides({}, {}, {}), {})

    def test_base_url_trailing_slash_stripped(self) -> None:
        result = effective_model_overrides(
            env={}, settings={}, cli={"base_url": "http://127.0.0.1:11434/v1/"}
        )
        self.assertEqual(result["base_url"], "http://127.0.0.1:11434/v1")


class SettingsEndpointTests(unittest.TestCase):
    """真实 HTTP 层：GET/POST /api/settings、GET /api/models、POST /api/model/test。

    设置文件用 VIBE_SETTINGS 指到临时目录，避免污染开发机的 ~/.vibe/settings.json。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.ws = (root / "ws").resolve()
        self.ws.mkdir()
        self.settings_file = root / "settings.json"

        self._saved_settings_env = os.environ.get("VIBE_SETTINGS")
        os.environ["VIBE_SETTINGS"] = str(self.settings_file)

        config = Config.load(self.ws)
        self.host = HostServer(
            config, port=0, client=FakeClient([[("list_dir", {})], "好的。"])
        )
        self.host.start_background()
        self.base = f"http://127.0.0.1:{self.host.port}"
        self.token = self.host.guard.token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        self.host.shutdown()
        if self._saved_settings_env is None:
            os.environ.pop("VIBE_SETTINGS", None)
        else:
            os.environ["VIBE_SETTINGS"] = self._saved_settings_env
        self._tmp.cleanup()

    def _request(self, path: str, *, method: str = "GET", body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("X-Vibe-Token", self.token)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        return self._opener.open(request, timeout=10)

    def test_get_settings_reports_current_and_path(self) -> None:
        with self._request("/api/settings") as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertEqual(data["settingsPath"], str(self.settings_file))
        self.assertIn("model", data)
        self.assertIn("baseUrl", data)
        # 内核默认就带 api_key（"ollama"），因此这里只验证「不回明文」：
        # 掩码里绝不能出现完整密钥。
        self.assertTrue(data["apiKeySet"])
        self.assertNotEqual(data["apiKeyMasked"], "ollama")  # 只回掩码，绝不回明文
        self.assertEqual(
            data["envLocked"], {"model": False, "base_url": False, "api_key": False}
        )

    def test_post_settings_hot_swaps_and_persists(self) -> None:
        with self._request(
            "/api/settings",
            method="POST",
            body={"model": "new-model", "baseUrl": "http://127.0.0.1:9999/v1/"},
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertFalse(data["switched"])
        # 运行时会话已换成新模型（热替换）
        self.assertEqual(data["session"]["model"], "new-model")
        # 落盘到 settings.json（且 base_url 去尾斜杠）
        stored = load_settings(self.settings_file)
        self.assertEqual(stored["model"], "new-model")
        self.assertEqual(stored["base_url"], "http://127.0.0.1:9999/v1")

    def test_post_settings_empty_body_is_400(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/api/settings", method="POST", body={})
        self.assertEqual(ctx.exception.code, 400)

    def test_get_models_returns_shape(self) -> None:
        with self._request("/api/models") as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertIn("models", data)
        self.assertIsInstance(data["models"], list)

    def test_model_test_unreachable_is_ok_false_not_500(self) -> None:
        """连不上是「如实回 ok:false」，不是 500 —— 界面据此给出可读提示。"""
        with self._request(
            "/api/model/test",
            method="POST",
            body={"baseUrl": "http://127.0.0.1:1/v1", "apiKey": ""},
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertFalse(data["ok"])
        self.assertTrue(data["message"])


if __name__ == "__main__":
    unittest.main()