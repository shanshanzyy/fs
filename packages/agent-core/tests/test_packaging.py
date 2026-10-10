"""桌面端打包（D1）的静态契约测试。

打包本身要靠 `scripts/e2e_package.py` 冒烟（跑真实分发包），这里只守住
那些「一旦两边写歪就静默出错」的约定：

1. 版本号只有一处真源（`agent_core.__version__`），打包脚本必须从那里取；
2. 内嵌后端的文件名，Python 侧与 Electron 侧必须逐字一致 ——
   名字对不上时不会报错，只会悄悄回退到系统 Python，「无 Python 也能用」就失效了；
3. 后端启动逻辑只存在于 `src/backend.js`，`main.js` 不得再复制一份。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
AGENT_CORE = ROOT / "packages" / "agent-core"
DESKTOP = ROOT / "apps" / "desktop"
sys.path.insert(0, str(AGENT_CORE))
sys.path.insert(0, str(AGENT_CORE / "scripts"))

import build_desktop  # noqa: E402

from agent_core import __version__  # noqa: E402


class BuildScriptTests(unittest.TestCase):
    def test_version_has_single_source_of_truth(self) -> None:
        self.assertEqual(build_desktop.core_version(), __version__)

    def test_embedded_binary_name_per_platform(self) -> None:
        self.assertEqual(build_desktop.embedded_binary_name("win32"), "vibe-agent-core.exe")
        self.assertEqual(build_desktop.embedded_binary_name("linux"), "vibe-agent-core")

    def test_backend_binary_lands_inside_desktop_resources(self) -> None:
        path = build_desktop.backend_binary_path()
        self.assertEqual(path.parent, DESKTOP / "resources" / "backend")
        # Electron 侧只认 <resources>/backend/<name>，dev 与打包后同一相对位置
        self.assertEqual(
            path.relative_to(DESKTOP).as_posix(),
            "resources/backend/vibe-agent-core.exe",
        )

    def test_dist_dir_name_carries_version(self) -> None:
        name = build_desktop.dist_dir_name("0.9.9", "win32", "AMD64")
        self.assertEqual(name, "VibeAgent-0.9.9-win32-AMD64")

    def test_app_payload_includes_backend_and_smoke(self) -> None:
        files = set(build_desktop.APP_FILES)
        self.assertIn("main.js", files)
        self.assertIn("backend.js", files)
        self.assertIn("smoke.js", files)
        for name in files:
            self.assertTrue((DESKTOP / "src" / name).is_file(), f"缺少 {name}")

    def test_js_embedded_binary_name_matches_python(self) -> None:
        """backend.js 里的文件名常量必须与 Python 侧一致（防两边漂移）。"""
        source = (DESKTOP / "src" / "backend.js").read_text(encoding="utf-8")
        self.assertIn(f"'{build_desktop.embedded_binary_name('win32')}'", source)

    def test_js_app_files_are_ascii_safe_paths(self) -> None:
        """Windows 上用 npm 装出来的 Electron 运行时不能有中文文件名。"""
        for name in build_desktop.APP_FILES:
            self.assertTrue(name.isascii(), name)


class BackendLauncherTests(unittest.TestCase):
    def test_backend_launcher_lives_only_in_backend_js(self) -> None:
        main_js = (DESKTOP / "src" / "main.js").read_text(encoding="utf-8")
        backend_js = (DESKTOP / "src" / "backend.js").read_text(encoding="utf-8")
        self.assertIn("function pickPython", backend_js)
        self.assertIn("class Backend", backend_js)
        self.assertNotIn("function pickPython", main_js)  # 不得复制第二份
        self.assertNotIn("class Backend", main_js)
        self.assertIn("require('./backend')", main_js)

    def test_smoke_entry_reuses_shared_launcher(self) -> None:
        smoke_js = (DESKTOP / "src" / "smoke.js").read_text(encoding="utf-8")
        self.assertIn("require('./backend')", smoke_js)

    def test_desktop_package_json_version_matches_core(self) -> None:
        data = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(data["version"], __version__)


if __name__ == "__main__":
    unittest.main()