"""示例测试：目前 average([]) 会失败，正好用来演示 vibe-agent 修 bug。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from utils import average, parse_date  # noqa: E402


class UtilsTest(unittest.TestCase):
    def test_parse_date_normal(self) -> None:
        self.assertEqual(parse_date("2026-09-23").year, 2026)

    def test_average_empty_should_not_crash(self) -> None:
        # 这条当前会失败：ZeroDivisionError
        self.assertEqual(average([]), 0)

    def test_average_values(self) -> None:
        self.assertEqual(average([1, 2, 3]), 2)


if __name__ == "__main__":
    unittest.main()
