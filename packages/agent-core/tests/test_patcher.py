"""编辑引擎测试：三级匹配策略 + 文件保真 + 安全护栏。

运行：cd packages/agent-core && python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import codecs
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.edit import patcher as P  # noqa: E402
from agent_core.errors import PatchError  # noqa: E402

PY_SOURCE = (
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "\n"
    "def sub(a, b):\n"
    "    result = a - b\n"
    "    return result\n"
)


class PatcherTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str, text: str, **kwargs) -> Path:
        path = self.ws / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="", **kwargs)
        return path

    # ------------------------------------------------------------ 匹配策略
    def test_exact_match(self) -> None:
        path = self._write("a.py", PY_SOURCE)
        plan = P.plan_patch(path, "    return a + b", "    return a + b  # fast path")
        self.assertEqual(plan.strategy, "exact")
        self.assertEqual(plan.added, 1)
        self.assertEqual(plan.removed, 1)
        P.commit_plan(plan)
        self.assertIn("# fast path", path.read_text(encoding="utf-8"))

    def test_normalized_match_ignores_trailing_space(self) -> None:
        path = self._write("a.py", PY_SOURCE)
        plan = P.plan_patch(path, "    return a + b   \n", "    return a + b\n")
        self.assertEqual(plan.strategy, "normalized")

    def test_flexible_match_extra_indent(self) -> None:
        path = self._write("a.py", PY_SOURCE)
        # 模型多缩进了 4 格，前两级都会失败，由 flexible 兜住
        plan = P.plan_patch(path, "        return a + b", "        return a + b  # ok")
        self.assertEqual(plan.strategy, "flexible")

    def test_flexible_reindents_replacement(self) -> None:
        """关键：命中后替换内容必须被平移回目标缩进，不能把 4 空格改成 0 空格。"""
        path = self._write("a.py", PY_SOURCE)
        plan = P.plan_patch(
            path, "        return a + b", "        return a + b  # ok"
        )
        self.assertIn("    return a + b  # ok", plan.new_text)
        self.assertNotIn("\nreturn a + b  # ok", plan.new_text)
        P.commit_plan(plan)
        self.assertIn("    return a + b  # ok", path.read_text(encoding="utf-8"))

    def test_flexible_multi_line_block(self) -> None:
        path = self._write("a.py", PY_SOURCE)
        search = "        result = a - b\n        return result"
        replace = "        result = a - b\n        return result  # noqa"
        plan = P.plan_patch(path, search, replace)
        self.assertEqual(plan.strategy, "flexible")
        self.assertIn("    return result  # noqa", plan.new_text)

    # ------------------------------------------------------------ 安全护栏
    def test_ambiguous_match_rejected(self) -> None:
        path = self._write("a.py", PY_SOURCE)
        with self.assertRaises(PatchError) as ctx:
            P.plan_patch(path, "    return ", "    yield ")
        self.assertIn("命中多处", str(ctx.exception))

    def test_replace_all_flag(self) -> None:
        path = self._write("a.py", "x = 1\ny = 1\nz = 1\n")
        plan = P.plan_patch(path, " = 1", " = 2", replace_all=True)
        self.assertIn("all", plan.strategy)
        P.commit_plan(plan)
        self.assertEqual(path.read_text(encoding="utf-8"), "x = 2\ny = 2\nz = 2\n")

    def test_missing_search_raises_with_diagnosis(self) -> None:
        path = self._write("a.py", PY_SOURCE)
        with self.assertRaises(PatchError) as ctx:
            P.plan_patch(path, "    total = compute(x)", "    total = 0")
        self.assertIn("找不到", str(ctx.exception))
        self.assertIn("read_file", str(ctx.exception))

    def test_empty_search_rejected(self) -> None:
        path = self._write("a.py", PY_SOURCE)
        with self.assertRaises(PatchError) as ctx:
            P.plan_patch(path, "   ", "整个换掉")
        self.assertIn("search 片段为空", str(ctx.exception))

    def test_identical_search_replace_rejected(self) -> None:
        path = self._write("a.py", PY_SOURCE)
        with self.assertRaises(PatchError):
            P.plan_patch(path, "    return a + b", "    return a + b")

    def test_missing_file_rejected(self) -> None:
        with self.assertRaises(PatchError):
            P.plan_patch(self.ws / "nope.py", "a", "b")

    # ------------------------------------------------------------ 文件保真
    def test_crlf_preserved(self) -> None:
        path = self.ws / "b.js"
        path.write_bytes(b"const x = 1;\r\nconst y = 2;\r\n")
        P.apply_patch(path, "const y = 2;", "const y = 3;")
        self.assertEqual(path.read_bytes(), b"const x = 1;\r\nconst y = 3;\r\n")

    def test_lf_preserved(self) -> None:
        path = self.ws / "c.js"
        path.write_bytes(b"const x = 1;\nconst y = 2;\n")
        P.apply_patch(path, "const y = 2;", "const y = 3;")
        self.assertEqual(path.read_bytes(), b"const x = 1;\nconst y = 3;\n")

    def test_bom_preserved(self) -> None:
        path = self.ws / "d.py"
        path.write_bytes(codecs.BOM_UTF8 + "x = 1\n".encode("utf-8"))
        P.apply_patch(path, "x = 1", "x = 2")
        self.assertTrue(path.read_bytes().startswith(codecs.BOM_UTF8))

    def test_missing_trailing_newline_preserved(self) -> None:
        path = self.ws / "e.py"
        path.write_bytes(b"x = 1")
        plan = P.plan_patch(path, "x = 1", "x = 2")
        P.commit_plan(plan)
        self.assertEqual(path.read_bytes(), b"x = 2")

    def test_gbk_file_roundtrip(self) -> None:
        path = self.ws / "f.txt"
        path.write_bytes("第一行\n第二行\n".encode("gbk"))
        P.apply_patch(path, "第二行", "第三行")
        self.assertEqual(path.read_bytes().decode("gbk"), "第一行\n第三行\n")

    def test_unicode_content(self) -> None:
        path = self._write("g.py", 'msg = "你好世界"\n')
        P.apply_patch(path, 'msg = "你好世界"', 'msg = "你好，世界"')
        self.assertIn("你好，世界", path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------ 新建
    def test_plan_create(self) -> None:
        path = self.ws / "new.py"
        plan = P.plan_create(path, "print('hi')\n")
        self.assertTrue(plan.is_create)
        P.commit_plan(plan)
        self.assertEqual(path.read_text(encoding="utf-8"), "print('hi')\n")

    def test_plan_create_guards_existing(self) -> None:
        path = self._write("exist.py", "x = 1\n")
        with self.assertRaises(PatchError) as ctx:
            P.plan_create(path, "y = 2\n")
        self.assertIn("已存在", str(ctx.exception))

    # ------------------------------------------------------------ diff
    def test_diff_is_unified(self) -> None:
        path = self._write("a.py", PY_SOURCE)
        plan = P.plan_patch(path, "    return a + b", "    return a + b  # fast")
        self.assertIn("--- a/a.py", plan.diff)
        self.assertIn("+++ b/a.py", plan.diff)
        self.assertIn("@@", plan.diff)


if __name__ == "__main__":
    unittest.main()
