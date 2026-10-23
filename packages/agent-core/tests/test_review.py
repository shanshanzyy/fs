"""代码审阅工作流（M9-H）测试。

验证六条纪律：
1. `review_changes` 恒 SAFE、不改盘、不执行、不派子 Agent；
2. 审阅材料结构化（含 diff + 文件上下文 + 审阅模板，模板覆盖严重级别/类别/定位/建议）；
3. 缺省审「工作区相对 HEAD」的改动，可用 paths 收窄；
4. `VIBE_REVIEW` 未设时不注册、行为逐字节一致；
5. 审计只记计数（files / issues / severity_high / ms），diff 与文件正文不出机；
6. 结果 `wrap_untrusted` 包裹（审阅材料是工作区读到的自由文本）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.audit import AuditLog  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.sandbox import CommandGuard, PathGuard, Risk  # noqa: E402
from agent_core.tools import ToolContext, build_default_registry, build_readonly_registry  # noqa: E402
from agent_core.tools.base import UNTRUSTED_BEGIN  # noqa: E402
from agent_core.tools.review import _changed_paths, REVIEW_CHANGES_SPEC  # noqa: E402


def _git(args: list[str], cwd: Path) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8"
    )
    return out.stdout


def _init_repo(ws: Path, dirty: bool = True) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(ws), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=str(ws), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "tester"], cwd=str(ws), check=True
    )
    (ws / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=str(ws), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(ws), check=True)
    if dirty:
        (ws / "a.py").write_text("def f():\n    return 2\n", encoding="utf-8")


class RegistryTests(unittest.TestCase):
    def test_disabled_by_default(self) -> None:
        reg = build_default_registry()
        self.assertNotIn("review_changes", reg.names())

    def test_enabled_registers_review(self) -> None:
        reg = build_default_registry(review=True)
        self.assertIn("review_changes", reg.names())
        spec = reg.get("review_changes")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.risk, Risk.SAFE)

    def test_not_in_readonly_registry(self) -> None:
        ro = build_readonly_registry(lsp=False)
        self.assertNotIn("review_changes", ro.names())


class ReviewToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.audit = AuditLog(self.ws / "audit.jsonl", session_id="t", enabled=True)
        self.ctx = ToolContext(
            config=Config.load(self.ws),
            paths=PathGuard(self.ws),
            commands=CommandGuard(self.ws),
            audit=self.audit,
        )
        self.reg = build_default_registry(review=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_clean_workspace_reports_nothing_to_review(self) -> None:
        _init_repo(self.ws, dirty=False)
        result = self.reg.execute(self.ctx, "review_changes", {})
        self.assertTrue(result.ok, result.content)
        self.assertIn("无需自审", result.content)

    def test_dirty_workspace_assembles_diff_and_template(self) -> None:
        _init_repo(self.ws, dirty=True)
        result = self.reg.execute(self.ctx, "review_changes", {})
        self.assertTrue(result.ok, result.content)
        # 材料包含 diff、审阅模板、结构化意见字段、不可信包裹
        self.assertIn(UNTRUSTED_BEGIN, result.content)
        self.assertIn("diff", result.content)
        self.assertIn("严重级别", result.content)
        self.assertIn("类别", result.content)
        self.assertIn("定位", result.content)
        self.assertIn("建议", result.content)
        # 恒 SAFE：不产出补丁、不提示改代码
        self.assertNotIn("补丁已", result.content)

    def test_audit_records_counts_only(self) -> None:
        _init_repo(self.ws, dirty=True)
        self.reg.execute(self.ctx, "review_changes", {})
        lines = [
            json.loads(l)
            for l in self.ws.joinpath("audit.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        review_events = [e for e in lines if e["event"] == "review.run"]
        self.assertEqual(len(review_events), 1)
        ev = review_events[0]
        self.assertEqual(ev["files"], 1)
        # 计数与耗时字段存在，diff/文件正文不出机
        for key in ("issues", "severity_high", "ms"):
            self.assertIn(key, ev)
        payload = json.dumps(ev, ensure_ascii=False)
        self.assertNotIn("return 2", payload)  # 文件正文
        self.assertNotIn("--- a/a.py", payload)  # diff 正文

    def test_non_repo_reports_unavailable(self) -> None:
        result = self.reg.execute(self.ctx, "review_changes", {})
        self.assertFalse(result.ok)
        self.assertIn("不是 git 仓库", result.content)

    def test_paths_narrowing_accepts_relative(self) -> None:
        _init_repo(self.ws, dirty=True)
        result = self.reg.execute(self.ctx, "review_changes", {"paths": "a.py"})
        self.assertTrue(result.ok, result.content)

    def test_is_readonly_no_write_no_exec(self) -> None:
        # review_changes 不写盘：源码文件内容不变，且除审计日志外无新增用户文件。
        _init_repo(self.ws, dirty=True)
        before_content = (self.ws / "a.py").read_text(encoding="utf-8")
        before = {p.name for p in self.ws.iterdir()}
        self.reg.execute(self.ctx, "review_changes", {})
        after_content = (self.ws / "a.py").read_text(encoding="utf-8")
        after = {p.name for p in self.ws.iterdir()}
        self.assertEqual(after_content, before_content)
        # 唯一允许新增的是审计日志（AuditLog 的正常落盘，非 review 工具的写盘副作用）
        new_files = after - before
        self.assertTrue(new_files <= {"audit.jsonl"}, new_files)


class ChangedPathsTests(unittest.TestCase):
    def test_extracts_paths_from_diff(self) -> None:
        diff = (
            "diff --git a/src/a.py b/src/a.py\n"
            "--- a/src/a.py\n+++ b/src/a.py\n"
            "diff --git a/docs/x.md b/docs/x.md\n"
            "new file mode 100644\n--- /dev/null\n+++ b/docs/x.md\n"
        )
        self.assertEqual(_changed_paths(diff), ["src/a.py", "docs/x.md"])

    def test_skips_dev_null(self) -> None:
        diff = "diff --git a/foo b/foo\n--- /dev/null\n+++ b/foo\n"
        self.assertEqual(_changed_paths(diff), ["foo"])

    def test_empty_diff_returns_empty(self) -> None:
        self.assertEqual(_changed_paths(""), [])


if __name__ == "__main__":
    unittest.main()
