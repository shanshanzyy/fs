"""Git 集成（阶段 C）测试。

验收口径（P0-FEATURES-PLAN §5-C7）：

1. **非仓库返回 `available=False`** —— 而不是抛异常（界面靠它显示提示）；
2. `status` / `diff` / `branches` 的形状正确（分支、改动码、+/- 计数）；
3. **冲突（`UU`）能被列出** —— 面板要把冲突单独标红。

所有用例都在 `tempfile` 里现建仓库；本机没装 git 时整组 `skipTest`
（不装 git 不算缺陷，「降级后不失效」正是这条能力的验收点之一）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core import gitinfo  # noqa: E402
from agent_core.audit import AuditLog  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.sandbox import CommandGuard, PathGuard  # noqa: E402
from agent_core.tools import build_default_registry  # noqa: E402
from agent_core.tools.base import ToolContext  # noqa: E402
from agent_core.tools.git import _git_diff  # noqa: E402

GIT = shutil.which("git")


def _run(root: Path, *argv: str) -> str:
    """测试自身的 helper：跑一条 git 命令并断言它成功。

    刻意**不复用** gitinfo 的内部 `_git` —— 测试要独立于被测代码，
    否则被测代码的解析 bug 会同时污染「准备仓库」这一步，用例红得莫名其妙。
    """
    proc = subprocess.run(  # noqa: S603
        ["git", *argv],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(argv)} 失败：{proc.stderr.strip()}")
    return proc.stdout


@unittest.skipIf(GIT is None, "本机没有 git，跳过 Git 集成用例")
class GitRepoTestCase(unittest.TestCase):
    """公共夹具：一个已 init 的临时仓库。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        _run(self.ws, "init", "-q")
        # 用 -c 传身份，不写全局配置 —— 测试不该改开发机的 git 配置
        self._identity = (
            "-c", "user.email=dev@example.com", "-c", "user.name=Dev",
            "-c", "commit.gpgsign=false",
        )

    def git(self, *argv: str) -> str:
        return _run(self.ws, *argv)

    def commit_all(self, message: str) -> str:
        self.git("add", "-A")
        self.git(*self._identity, "commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def write(self, rel: str, text: str) -> Path:
        path = self.ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def ctx(self) -> ToolContext:
        config = Config.load(self.ws)
        return ToolContext(
            config=config,
            paths=PathGuard(self.ws),
            commands=CommandGuard(self.ws),
            audit=AuditLog(
                config.audit_path,
                session_id=config.session_id,
                enabled=config.audit_enabled,
            ),
        )


class NonRepoTests(unittest.TestCase):
    """非 git 目录：所有入口都必须「平静地」说不可用。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_all_entries_report_unavailable_without_raising(self) -> None:
        self.assertFalse(gitinfo.is_repo(self.ws))
        for name, data in (
            ("status", gitinfo.status(self.ws)),
            ("diff", gitinfo.diff(self.ws)),
            ("branches", gitinfo.branches(self.ws)),
            ("log", gitinfo.log(self.ws)),
        ):
            with self.subTest(entry=name):
                self.assertFalse(data["available"], f"{name} 在非仓库里应报不可用")
                self.assertTrue(data.get("reason"), f"{name} 必须给出原因")

    def test_missing_directory_is_also_just_unavailable(self) -> None:
        ghost = self.ws / "nope"
        self.assertFalse(gitinfo.is_repo(ghost))
        self.assertFalse(gitinfo.status(ghost)["available"])

    def test_unmerged_is_empty_list(self) -> None:
        self.assertEqual(gitinfo.unmerged(self.ws), [])

    def test_tool_returns_failure_hint_instead_of_raising(self) -> None:
        result = _git_diff(self.ctx(), {"mode": "status"})
        self.assertFalse(result.ok)
        self.assertFalse(result.meta["available"])
        self.assertIn("不是 git 仓库", result.content)

    def ctx(self) -> ToolContext:
        config = Config.load(self.ws)
        return ToolContext(
            config=config,
            paths=PathGuard(self.ws),
            commands=CommandGuard(self.ws),
            audit=AuditLog(
                config.audit_path,
                session_id=config.session_id,
                enabled=config.audit_enabled,
            ),
        )


@unittest.skipIf(GIT is None, "本机没有 git，跳过 Git 集成用例")
class StatusTests(GitRepoTestCase):
    def test_unborn_repo_is_flagged(self) -> None:
        self.write("a.txt", "hello\n")
        data = gitinfo.status(self.ws)
        self.assertTrue(data["available"])
        self.assertTrue(data["unborn"])
        self.assertFalse(data["clean"])
        self.assertEqual([item["status"] for item in data["entries"]], ["??"])
        self.assertEqual(data["counts"]["untracked"], 1)

    def test_clean_after_commit(self) -> None:
        self.write("a.txt", "hello\n")
        self.commit_all("init")
        data = gitinfo.status(self.ws)
        self.assertTrue(data["clean"])
        self.assertEqual(data["entries"], [])
        self.assertEqual(data["counts"]["changed"], 0)

    def test_modified_and_untracked_are_listed_with_codes(self) -> None:
        self.write("a.txt", "hello\n")
        self.commit_all("init")
        self.write("a.txt", "hello world\n")
        self.write("b.txt", "new\n")
        data = gitinfo.status(self.ws)
        codes = {item["path"]: item["status"] for item in data["entries"]}
        self.assertEqual(codes.get("a.txt"), "M")
        self.assertEqual(codes.get("b.txt"), "??")
        self.assertEqual(data["counts"]["untracked"], 1)
        self.assertEqual(data["conflicts"], [])

    def test_rename_records_original_path(self) -> None:
        self.write("old.txt", "hello\n")
        self.commit_all("init")
        self.git("mv", "old.txt", "new.txt")
        data = gitinfo.status(self.ws)
        entry = next(item for item in data["entries"] if item["path"].endswith("new.txt"))
        self.assertEqual(entry["status"], "R")
        self.assertTrue(entry["orig"].endswith("old.txt"))

    def test_branch_name_and_no_upstream(self) -> None:
        self.write("a.txt", "hello\n")
        self.commit_all("init")
        data = gitinfo.status(self.ws)
        self.assertIn(data["branch"], ("main", "master"))
        self.assertEqual(data["upstream"], "")
        self.assertFalse(data["detached"])


@unittest.skipIf(GIT is None, "本机没有 git，跳过 Git 集成用例")
class DiffTests(GitRepoTestCase):
    def test_diff_against_head_counts_lines(self) -> None:
        self.write("a.txt", "one\ntwo\n")
        self.commit_all("init")
        self.write("a.txt", "one\ntwo\nthree\n")

        data = gitinfo.diff(self.ws)
        self.assertTrue(data["available"])
        self.assertEqual(data["files"], 1)
        self.assertEqual(data["added"], 1)
        self.assertEqual(data["removed"], 0)
        self.assertIn("+three", data["diff"])
        self.assertFalse(data["truncated"])

    def test_unborn_repo_diffs_against_empty_tree(self) -> None:
        """没有 HEAD 时基准换成空树 —— 否则 `git diff HEAD` 直接报错。

        注意：未跟踪文件不进 index，任何 diff 都看不见它（这是 git 的语义，
        不是这里的缺陷）—— 所以这里先 `add`，走的才是 diff 能表达的范围。
        """
        self.write("a.txt", "hello\n")
        self.git("add", "a.txt")
        data = gitinfo.diff(self.ws)
        self.assertTrue(data["available"])
        self.assertEqual(data["ref"], gitinfo._EMPTY_TREE)
        self.assertIn("+hello", data["diff"])

    def test_staged_diff_only_shows_index(self) -> None:
        self.write("a.txt", "one\n")
        self.commit_all("init")
        self.write("a.txt", "one\ntwo\n")
        self.write("b.txt", "brand new\n")
        self.git("add", "b.txt")

        staged = gitinfo.diff(self.ws, staged=True)
        self.assertTrue(staged["staged"])
        self.assertEqual(staged["files"], 1)
        self.assertIn("b.txt", staged["diff"])
        self.assertNotIn("two", staged["diff"])

    def test_path_filter_narrows_the_diff(self) -> None:
        self.write("a.txt", "one\n")
        self.write("b.txt", "one\n")
        self.commit_all("init")
        self.write("a.txt", "one\naaa\n")
        self.write("b.txt", "one\nbbb\n")

        data = gitinfo.diff(self.ws, path="a.txt")
        self.assertEqual(data["files"], 1)
        self.assertIn("aaa", data["diff"])
        self.assertNotIn("bbb", data["diff"])


@unittest.skipIf(GIT is None, "本机没有 git，跳过 Git 集成用例")
class BranchesAndLogTests(GitRepoTestCase):
    def test_branches_marks_current_and_kind(self) -> None:
        self.write("a.txt", "hello\n")
        self.commit_all("init")
        self.git("branch", "feature/x")

        data = gitinfo.branches(self.ws)
        self.assertTrue(data["available"])
        names = {item["name"]: item for item in data["branches"]}
        self.assertIn("feature/x", names)
        self.assertEqual(names["feature/x"]["kind"], "local")
        self.assertTrue(names[data["current"]]["current"])

    def test_log_returns_short_and_subject(self) -> None:
        self.write("a.txt", "hello\n")
        self.commit_all("第一个提交")
        data = gitinfo.log(self.ws, 5)
        self.assertTrue(data["available"])
        self.assertEqual(data["commits"][0]["subject"], "第一个提交")

    def test_log_on_unborn_repo_is_empty_not_error(self) -> None:
        data = gitinfo.log(self.ws)
        self.assertTrue(data["available"])
        self.assertEqual(data["commits"], [])

    def test_snapshot_bundles_all_three(self) -> None:
        self.write("a.txt", "hello\n")
        self.commit_all("init")
        data = gitinfo.snapshot(self.ws)
        self.assertEqual(set(data), {"status", "branches", "log"})
        self.assertTrue(data["status"]["available"])


@unittest.skipIf(GIT is None, "本机没有 git，跳过 Git 集成用例")
class ConflictTests(GitRepoTestCase):
    """冲突必须能被单独识别 —— 界面把它标红，模型也不该把 UU 当普通改动。"""

    def _make_conflict(self) -> None:
        self.write("a.txt", "base\n")
        self.commit_all("base")
        self.git("checkout", "-q", "-b", "other")
        self.write("a.txt", "from other\n")
        self.commit_all("other side")
        self.git("checkout", "-q", "-")
        self.write("a.txt", "from main\n")
        self.commit_all("main side")
        # merge 自身也需要身份（git 会先做「能不能提交」的检查），
        # 所以 `-c user.*` 必须带在 merge 上 —— 否则报的是「身份未知」，
        # 而不是真正的冲突，用例就测不到想看的东西。
        merge = subprocess.run(  # noqa: S603
            ["git", *self._identity, "merge", "other"],
            cwd=str(self.ws), capture_output=True, text=True, check=False,
        )
        # 合并必须真的失败（有冲突），否则这条用例测的就不是冲突了
        self.assertNotEqual(merge.returncode, 0, "预期合并冲突，但合并成功了")

    def test_conflict_listed_and_flagged(self) -> None:
        self._make_conflict()
        data = gitinfo.status(self.ws)
        self.assertIn("a.txt", data["conflicts"])
        entry = next(item for item in data["entries"] if item["path"] == "a.txt")
        self.assertTrue(entry["conflicted"])
        self.assertEqual(entry["status"], "UU")
        self.assertEqual(data["counts"]["conflicts"], 1)

    def test_unmerged_matches_status_conflicts(self) -> None:
        self._make_conflict()
        self.assertEqual(gitinfo.unmerged(self.ws), gitinfo.status(self.ws)["conflicts"])

    def test_tool_status_mentions_conflict(self) -> None:
        self._make_conflict()
        result = _git_diff(self.ctx(), {"mode": "status"})
        self.assertTrue(result.ok)
        self.assertEqual(result.meta["conflicts"], 1)
        self.assertIn("合并冲突未解决", result.content)


@unittest.skipIf(GIT is None, "本机没有 git，跳过 Git 集成用例")
class GitToolTests(GitRepoTestCase):
    def test_diff_mode_renders_summary(self) -> None:
        self.write("a.txt", "one\n")
        self.commit_all("init")
        self.write("a.txt", "one\ntwo\n")

        result = _git_diff(self.ctx(), {})
        self.assertTrue(result.ok)
        self.assertIn("+1 -0", result.content)
        self.assertEqual(result.meta["files"], 1)
        self.assertIn("+two", result.diff)

    def test_unknown_mode_is_rejected(self) -> None:
        result = build_default_registry().execute(self.ctx(), "git_diff", {"mode": "push"})
        self.assertFalse(result.ok)
        self.assertIn("mode", result.content)

    def test_escaping_path_is_blocked_by_path_guard(self) -> None:
        """越权 path 必须被 PathGuard 拦下 —— 走真实的注册表漏斗。"""
        self.write("a.txt", "one\n")
        self.commit_all("init")
        result = build_default_registry().execute(self.ctx(), "git_diff", {"path": "../outside.txt"})
        self.assertFalse(result.ok)
        self.assertIn("SandboxViolation", result.content)

    def test_audit_entry_records_metadata(self) -> None:
        """每次调用落一条 `tool.git_diff`，记的是元数据（mode/ref/±）。

        diff 原文留在**本机**审计文件里（与 `tool.grep` 记 pattern 同一约定），
        上传时由 `governance/vocabulary.py` 换成 digest + 长度 ——
        那一侧的契约由 `apps/server/tests/test_chain_compat.py` 钉住。
        """
        self.write("a.txt", "one\n")
        self.commit_all("init")
        self.write("a.txt", "one\ntwo\n")
        ctx = self.ctx()
        _git_diff(ctx, {})

        entries = [
            json.loads(line)
            for line in ctx.config.audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        record = next(item for item in entries if item.get("event") == "tool.git_diff")
        self.assertEqual(record["mode"], "diff")
        self.assertEqual(record["ref"], "HEAD")
        self.assertEqual(record["files"], 1)
        self.assertEqual(record["added"], 1)
        self.assertFalse(record["staged"])


if __name__ == "__main__":
    unittest.main()