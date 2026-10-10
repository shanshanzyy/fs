"""逐 hunk 拆分与应用（D7）的验收。

关键用例是「勾选子集」：只接受一部分 hunk 时，未勾中的部分必须**逐字节保持原样**。
反向验证方式：临时在 ``apply_hunks`` 里忽略 ``accepted``（无条件用 new_lines），
``test_subset_only_applies_accepted`` 必须变红。
"""

from __future__ import annotations

import unittest

from agent_core.edit.hunks import apply_hunks, split_hunks


class SplitTests(unittest.TestCase):
    def test_single_replace(self) -> None:
        old = "a\nb\nc"
        new = "a\nB\nc"
        hunks = split_hunks(old, new)
        self.assertEqual(len(hunks), 1)
        self.assertEqual(hunks[0].tag, "replace")
        self.assertEqual(hunks[0].old_lines, ["b"])
        self.assertEqual(hunks[0].new_lines, ["B"])

    def test_two_separated_changes_make_two_hunks(self) -> None:
        old = "a\nb\nc\nd\ne"
        new = "a\nB\nc\nD\ne"
        hunks = split_hunks(old, new)
        self.assertEqual([h.tag for h in hunks], ["replace", "replace"])
        self.assertEqual(hunks[0].old_start, 1)
        self.assertEqual(hunks[1].old_start, 3)

    def test_insert_and_delete(self) -> None:
        hunks = split_hunks("a\nc", "a\nb\nc")
        self.assertEqual(len(hunks), 1)
        self.assertEqual(hunks[0].tag, "insert")
        self.assertEqual(hunks[0].old_lines, [])
        self.assertEqual(hunks[0].new_lines, ["b"])

        hunks = split_hunks("a\nb\nc", "a\nc")
        self.assertEqual(len(hunks), 1)
        self.assertEqual(hunks[0].tag, "delete")
        self.assertEqual(hunks[0].old_lines, ["b"])

    def test_no_change(self) -> None:
        self.assertEqual(split_hunks("a\nb", "a\nb"), [])

    def test_trailing_newline_is_faithful(self) -> None:
        """末尾换行的增删必须被表达出来（join 后逐字节可还原）。"""
        self.assertEqual(apply_hunks("a\n", split_hunks("a\n", "a"), set()), "a\n")
        hunks = split_hunks("a", "a\n")
        self.assertTrue(hunks)
        self.assertEqual(apply_hunks("a", hunks, {h.index for h in hunks}), "a\n")


class ApplyTests(unittest.TestCase):
    def test_empty_accepted_returns_old_verbatim(self) -> None:
        old = "a\nb\nc\nd\ne"
        new = "a\nB\nc\nD\ne"
        hunks = split_hunks(old, new)
        self.assertEqual(apply_hunks(old, hunks, set()), old)

    def test_subset_only_applies_accepted(self) -> None:
        """只勾第 0 个 hunk：第 1 处改动必须原样保留。"""
        old = "a\nb\nc\nd\ne"
        new = "a\nB\nc\nD\ne"
        hunks = split_hunks(old, new)
        result = apply_hunks(old, hunks, {0})
        self.assertEqual(result, "a\nB\nc\nd\ne")

        result = apply_hunks(old, hunks, {1})
        self.assertEqual(result, "a\nb\nc\nD\ne")

    def test_all_accepted_equals_new(self) -> None:
        cases = [
            ("a\nb\nc", "a\nB\nc"),
            ("x", "x\ny\nz"),
            ("p\nq\nr", "p\nr"),
            ("one\ntwo", "uno\ndos\nthree"),
        ]
        for old, new in cases:
            hunks = split_hunks(old, new)
            accepted = {h.index for h in hunks}
            self.assertEqual(apply_hunks(old, hunks, accepted), new, (old, new))

    def test_api_shape(self) -> None:
        hunks = split_hunks("a\nb\nc", "a\nB\nc")
        payload = hunks[0].to_api()
        for key in ("index", "tag", "header", "oldStart", "oldEnd", "oldLines", "newLines", "diff", "added", "removed"):
            self.assertIn(key, payload)
        self.assertIn("B", payload["diff"])


if __name__ == "__main__":
    unittest.main()