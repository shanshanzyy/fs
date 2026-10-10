"""多层规则加载（P2-15）测试。

验收要点：
1. 四层（用户级 / 仓库级 / 仓库附加 / 目录级）按「由泛到专」合并，多层加来源抬头；
2. 目录级只对**匹配的路径**生效，就近（深）优先；
3. 合计超 64KB 时**先丢最泛的层**，并把这件事如实标出（truncated）；
4. 单层时正文与旧版逐字节一致（回归保护，见 test_project_rules.py）。
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

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, rel: str, text: str) -> Path:
        path = self.ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return path


class LayerMergeTests(_WsHarness):
    def test_layers_merge_general_to_specific(self) -> None:
        user = self._write("user-rules.md", "用户级规则")
        self._write("VIBE.md", "仓库级规则")
        self._write(".vibe/rules/a.md", "附加规则")

        rules = load_project_instructions(self.ws, user_rules_path=user)

        self.assertEqual(rules.layers, 3)
        # 用户级来源是显式传入的绝对路径；后两层是工作区相对路径
        self.assertTrue(rules.sources[0].endswith("user-rules.md"))
        self.assertEqual(rules.sources[1:], ("VIBE.md", ".vibe/rules/a.md"))
        # 主来源优先取仓库级（UI 徽标展示「这是哪个项目的规则」）
        self.assertEqual(rules.source, "VIBE.md")
        # 合并顺序：用户级 < 仓库级 < 附加（越靠后越专，越优先）
        body = rules.text
        self.assertLess(body.index("用户级规则"), body.index("仓库级规则"))
        self.assertLess(body.index("仓库级规则"), body.index("附加规则"))
        # 多层才加来源抬头
        self.assertIn("### 规则来源：VIBE.md", body)

    def test_repo_root_wins_over_vibe_dir_within_layer(self) -> None:
        self._write("VIBE.md", "根规则")
        self._write(".vibe/VIBE.md", "备选规则")
        rules = load_project_instructions(
            self.ws, user_rules_path=self.ws / "missing.md"
        )
        # 根与 .vibe/VIBE.md 是同一层的备选，不叠加
        self.assertEqual(rules.layers, 1)
        self.assertEqual(rules.text, "根规则")

    def test_single_layer_has_no_header(self) -> None:
        self._write("VIBE.md", "只有仓库规则")
        rules = load_project_instructions(
            self.ws, user_rules_path=self.ws / "missing.md"
        )
        self.assertEqual(rules.text, "只有仓库规则")
        self.assertNotIn("规则来源", rules.text)

    def test_extra_glob_paths(self) -> None:
        self._write("conventions/python.md", "Python 约定")
        self._write("conventions/ts.md", "TS 约定")
        rules = load_project_instructions(
            self.ws,
            user_rules_path=self.ws / "missing.md",
            extra="conventions/*.md",
        )
        self.assertEqual(rules.layers, 2)
        self.assertIn("Python 约定", rules.text)
        self.assertIn("TS 约定", rules.text)


class DirectoryLayerTests(_WsHarness):
    def test_dir_level_nearest_is_last(self) -> None:
        self._write("VIBE.md", "仓库规则")
        self._write("sub/VIBE.md", "子目录规则")
        self._write("sub/deep/VIBE.md", "深层规则")

        rules = load_project_instructions(
            self.ws,
            user_rules_path=self.ws / "missing.md",
            current_files=["sub/deep/file.py"],
        )
        self.assertEqual(
            rules.sources, ("VIBE.md", "sub/VIBE.md", "sub/deep/VIBE.md")
        )
        body = rules.text
        # 就近优先：越深的目录排在越后面
        self.assertLess(body.index("仓库规则"), body.index("子目录规则"))
        self.assertLess(body.index("子目录规则"), body.index("深层规则"))

    def test_dir_level_only_for_matching_paths(self) -> None:
        self._write("sub/VIBE.md", "子目录规则")

        hit = load_project_instructions(
            self.ws,
            user_rules_path=self.ws / "missing.md",
            current_files=["sub/file.py"],
        )
        miss = load_project_instructions(
            self.ws,
            user_rules_path=self.ws / "missing.md",
            current_files=["other/file.py"],
        )
        self.assertIn("子目录规则", hit.text)
        self.assertNotIn("子目录规则", miss.text)
        self.assertEqual(miss.layers, 0)

    def test_dir_level_ignores_outside_workspace(self) -> None:
        outside = self._tmp.name
        # 引用工作区外的绝对路径：不得把外部 VIBE.md 拉进来
        rules = load_project_instructions(
            self.ws,
            user_rules_path=self.ws / "missing.md",
            current_files=[str(Path(outside).parent / "elsewhere" / "x.py")],
        )
        self.assertEqual(rules.layers, 0)


class BudgetTests(_WsHarness):
    def test_total_budget_drops_general_layers_first(self) -> None:
        # 三层都用 3 字节 CJK 撑满单份上限（32KB），合计必然超过 64KB
        big = "用" * 25_000
        user = self._write("user-rules.md", big)
        self._write("VIBE.md", "仓" * 25_000)
        self._write(".vibe/rules/a.md", "附" * 25_000)

        rules = load_project_instructions(self.ws, user_rules_path=user)

        # 最泛的用户级被先丢；剩下的两层仍在预算内（合计 65532 字节）
        self.assertTrue(rules.truncated)
        self.assertGreaterEqual(rules.truncated_count, 1)
        self.assertEqual(rules.layers, 2)
        self.assertNotIn("用", rules.text)
        self.assertIn("仓", rules.text)
        self.assertIn("附", rules.text)


if __name__ == "__main__":
    unittest.main()