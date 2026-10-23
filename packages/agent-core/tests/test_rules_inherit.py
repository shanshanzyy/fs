"""全局规则继承（M9-B）测试。

验收要点：
1. 全局级是最泛的一层，排在最前；合并顺序 = 全局 < 用户 < 仓库 < 附加 < 目录；
2. 合计超 64KB 时**先丢最泛的层**（全局最先被丢）；
3. 目录级就近（深）规则仍优先（排在最后）；
4. 单层时正文与旧版逐字节一致（无来源抬头），回归保护；
5. ``global_rules_path=None`` 时合并结果与改造前逐字节一致。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.project_rules import (  # noqa: E402
    load_project_instructions,
)


class _WsHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self._global = Path(self._tmp.name) / "global-rules"
        self._global.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, rel: str, text: str) -> Path:
        path = self.ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def _write_global(self, name: str, text: str) -> Path:
        path = self._global / name
        path.write_text(text, encoding="utf-8", newline="")
        return path


class GlobalLayerMergeTests(_WsHarness):
    def test_global_is_most_general_layer(self) -> None:
        self._write_global("01-conventions.md", "全局规范")
        user = self._write("user-rules.md", "用户级规则")
        self._write("VIBE.md", "仓库级规则")

        rules = load_project_instructions(
            self.ws, user_rules_path=user, global_rules_path=self._global
        )

        self.assertEqual(rules.layers, 3)
        # 全局层来源带 `global:` 前缀，排最前
        self.assertTrue(rules.sources[0].startswith("global:"))
        body = rules.text
        self.assertLess(body.index("全局规范"), body.index("用户级规则"))
        self.assertLess(body.index("用户级规则"), body.index("仓库级规则"))
        # 多层带来源抬头（全局层也带）
        self.assertIn("### 规则来源：global:01-conventions.md", body)

    def test_global_absent_is_byte_identical(self) -> None:
        """未设 global_rules_path 时，结果与只含用户/仓库层时完全一致。"""
        user = self._write("user-rules.md", "用户级规则")
        self._write("VIBE.md", "仓库级规则")

        with_global = load_project_instructions(
            self.ws, user_rules_path=user, global_rules_path=self._global
        )
        without_global = load_project_instructions(
            self.ws, user_rules_path=user, global_rules_path=None
        )

        self.assertEqual(with_global.text, without_global.text)
        self.assertEqual(with_global.layers, without_global.layers)
        self.assertEqual(with_global.sources, without_global.sources)

    def test_global_dropped_first_on_total_overflow(self) -> None:
        """总量超预算时，最泛的全局层最先被丢。"""
        # 单份上限 MAX_RULES_BYTES = 32KB，两份各写满 32KB 后总量 64KB 恰好卡边，
        # 因此这里再加一层用户级，使总量必然超出 TOTAL_RULES_BYTES。
        big = "x" * (32 * 1024)
        self._write_global("01-big.md", big)
        self._write("VIBE.md", big)  # 仓库层也写满 32KB
        user = self._write("user-rules.md", "用户级规则")

        rules = load_project_instructions(
            self.ws, user_rules_path=user, global_rules_path=self._global
        )

        self.assertTrue(rules.truncated)
        # 全局层被丢，只剩仓库层与用户级（全局层不再在场）
        self.assertNotIn("global:", rules.sources)
        self.assertFalse(any(s.startswith("global:") for s in rules.sources))

    def test_global_max_bytes_drops_tail_files(self) -> None:
        """全局层专用上限（global_rules_max_bytes）超了丢弃文件名排序后的队尾。"""
        self._write_global("01-first.md", "第一条规则")
        self._write_global("02-second.md", "第二条规则")
        self._write_global("03-third.md", "第三条规则")

        # 上限很小，只够放第一条
        rules = load_project_instructions(
            self.ws, global_rules_path=self._global, global_rules_max_bytes=20
        )

        global_sources = [s for s in rules.sources if s.startswith("global:")]
        self.assertEqual(global_sources, ["global:01-first.md"])
        self.assertIn("第一条规则", rules.text)
        self.assertNotIn("第三条规则", rules.text)
        self.assertTrue(rules.truncated)

    def test_dir_level_still_wins_over_global(self) -> None:
        """目录级就近规则仍排最后（优先级最高），不受全局层影响。"""
        self._write_global("01-conventions.md", "全局规范")
        self._write("VIBE.md", "仓库级规则")
        self._write("src/VIBE.md", "src 目录规则")

        rules = load_project_instructions(
            self.ws,
            global_rules_path=self._global,
            current_files=["src/foo.py"],
        )

        body = rules.text
        self.assertLess(body.index("全局规范"), body.index("仓库级规则"))
        self.assertLess(body.index("仓库级规则"), body.index("src 目录规则"))
        # 目录级来源是相对路径，不带 global: 前缀
        self.assertIn("src/VIBE.md", rules.sources)

    def test_single_global_layer_no_header(self) -> None:
        """只有全局层、没有其它层时，单层正文不带来源抬头（逐字节兼容）。"""
        self._write_global("01-only.md", "只有全局规则")

        rules = load_project_instructions(
            self.ws, global_rules_path=self._global
        )

        self.assertEqual(rules.layers, 1)
        self.assertEqual(rules.text, "只有全局规则")
        self.assertNotIn("### 规则来源", rules.text)

    def test_global_dir_missing_is_silent(self) -> None:
        """全局目录不存在时按「没有全局层」处理，不报错、不影响其它层。"""
        self._write("VIBE.md", "仓库级规则")
        missing = self.ws / "does-not-exist"

        rules = load_project_instructions(
            self.ws, global_rules_path=missing
        )

        self.assertEqual(rules.layers, 1)
        self.assertEqual(rules.text, "仓库级规则")


if __name__ == "__main__":
    unittest.main()
