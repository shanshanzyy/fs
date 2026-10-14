"""Git 写操作（M7 Part B）的测试：B2 的 gitinfo 写函数 + B3 的 host 端点。

**为什么写操作必须对着真仓库测。** 只读函数可以拿假输出蒙混，写函数不行：
`git add` 有没有真进索引、unstage 在 **unborn** 仓库走的是不是 `rm --cached`
（而不是必然失败的 `reset HEAD`）、`pull --ff-only` 遇到分叉是不是**一个字
都没动**、discard 到底走的是 `checkout --` 还是 `clean -f` —— 这些只有真实的
git 才看得出来。远端一律用 `file://` 裸仓库，**零网络**：CI 里也能跑，且不会
因为网速/凭据把一条本该确定的断言变成 flaky。

两条与安全相关的断言也在这里钉死：

- **路径防线**：`../evil.txt`（越界）与 `.vibe/audit.jsonl`（审计目录）走 git
  端点也必须被 400 拒掉 —— 借 git 动作绕过写保护是这套设计的红线。
- **脱敏**：用户手打的 commit message 只以 `message_digest` 上传，正文不出机
  （ADR-006；与 `test_chain_compat` 的全量扫描互补，这里是聚焦的一条）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core import gitinfo  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.governance import vocabulary  # noqa: E402
from agent_core.host.server import HostServer  # noqa: E402
from tests.test_agent_loop import FakeClient  # noqa: E402


def _git(*args: str, cwd) -> subprocess.CompletedProcess:
    """直接跑 git（测试自带的脚手架，不走 gitinfo —— 免得用被测物搭测试台）。"""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _init_repo(path: Path) -> None:
    # 固定默认分支名，避免机器上的 init.defaultBranch 设置让断言飘。
    _git("-c", "init.defaultBranch=main", "init", cwd=path)
    _git("config", "user.email", "t@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    # 有些开发机全局开了 GPG 签名；关掉本地签名，让 gitinfo 的裸 `git commit` 可跑。
    _git("config", "commit.gpgsign", "false", cwd=path)


def _read_audit(path) -> list[dict]:
    file = Path(path)
    if not file.exists():
        return []
    events: list[dict] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


# ======================================================================== 单测（gitinfo）
class GitRepoCase(unittest.TestCase):
    """每个用例一个真仓库（临时目录里 `git init`）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _init_repo(self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---- 脚手架 ---------------------------------------------------------
    def _write(self, rel: str, text: str) -> Path:
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def _commit_all(self, message: str = "init"):
        _git("add", "-A", cwd=self.repo)
        return _git("commit", "-m", message, cwd=self.repo)

    def _entry(self, rel: str):
        st = gitinfo.status(self.repo)
        return next((e for e in st.get("entries", []) if e["path"] == rel), None)

    def _head(self) -> str:
        return _git("rev-parse", "HEAD", cwd=self.repo).stdout.strip()

    def _add_remote(self) -> Path:
        remote = self.root / "remote.git"
        # 默认分支也要钉成 main，否则裸仓库的 HEAD 指向 master，
        # 克隆方会「HEAD 指向不存在的 ref」—— clone 出来没有分支，push 也就落错地方。
        _git("-c", "init.defaultBranch=main", "init", "--bare", str(remote), cwd=self.root)
        _git("remote", "add", "origin", f"file://{remote.as_posix()}", cwd=self.repo)
        return remote

    def _push_upstream(self, remote: Path):
        return _git("push", "-u", "origin", "main", cwd=self.repo)

    def _clone(self, remote: Path, name: str) -> Path:
        dest = self.root / name
        _git("clone", f"file://{remote.as_posix()}", str(dest), cwd=self.root)
        _git("config", "user.email", "o@example.com", cwd=dest)
        _git("config", "user.name", "Other", cwd=dest)
        _git("config", "commit.gpgsign", "false", cwd=dest)
        return dest


class StageUnstageTests(GitRepoCase):
    def test_stage_then_unstage_is_idempotent(self) -> None:
        self._write("a.txt", "v1\n")
        self._commit_all("init")
        self._write("a.txt", "v2\n")  # 已跟踪文件被改

        r = gitinfo.stage(self.repo, ["a.txt"])
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertEqual(self._entry("a.txt")["index"], "M")

        r = gitinfo.unstage(self.repo, ["a.txt"])
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertEqual(self._entry("a.txt")["index"], " ")  # 索引回到 HEAD
        self.assertEqual(self._entry("a.txt")["worktree"], "M")  # 磁盘仍是改后的

        # 幂等：再取消一次不该报错（pathspec 仍匹配 HEAD 里的 a.txt）
        r = gitinfo.unstage(self.repo, ["a.txt"])
        self.assertTrue(r["ok"], r.get("reason"))

    def test_unstage_in_unborn_repo_uses_cached_removal(self) -> None:
        """仓库一次提交都没有时 unstage 仍要成功 —— 这是最常见的首次使用场景。"""
        self._write("b.txt", "x\n")
        self.assertTrue(gitinfo.stage(self.repo, ["b.txt"])["ok"])
        self.assertEqual(self._entry("b.txt")["index"], "A")

        r = gitinfo.unstage(self.repo, ["b.txt"])
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertEqual(self._entry("b.txt")["status"], "??")

        # all=True 版本的 unborn 分支
        self._write("c.txt", "y\n")
        self.assertTrue(gitinfo.stage(self.repo, all=True)["ok"])
        r = gitinfo.unstage(self.repo, all=True)
        self.assertTrue(r["ok"], r.get("reason"))
        entries = gitinfo.status(self.repo)["entries"]
        self.assertTrue(entries)
        self.assertTrue(all(e["status"] == "??" for e in entries), entries)

    def test_stage_all_true_stages_everything(self) -> None:
        self._write("x.txt", "x\n")
        self._write("y.txt", "y\n")
        self.assertTrue(gitinfo.stage(self.repo, all=True)["ok"])
        staged = {e["path"]: e["index"] for e in gitinfo.status(self.repo)["entries"]}
        self.assertEqual(staged.get("x.txt"), "A")
        self.assertEqual(staged.get("y.txt"), "A")


class CommitTests(GitRepoCase):
    def test_commit_rejects_empty_and_too_long_and_accepts_500(self) -> None:
        r = gitinfo.commit(self.repo, "   ")
        self.assertFalse(r["ok"])
        self.assertIn("不能为空", r["reason"])

        r = gitinfo.commit(self.repo, "x" * 501)
        self.assertFalse(r["ok"])
        self.assertIn("过长", r["reason"])

        # 边界：正好 500 字符要能过
        self._write("a.txt", "one\n")
        self.assertTrue(gitinfo.stage(self.repo, ["a.txt"])["ok"])
        r = gitinfo.commit(self.repo, "x" * 500)
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertTrue(r["oid"])
        self.assertEqual(len(self._head()), 40)

    def test_commit_hook_failure_is_passed_through(self) -> None:
        """用户装的钩子必须照跑（刻意不加 --no-verify）：钩子拒绝 = 提交失败。"""
        self._write("a.txt", "one\n")
        self._commit_all("init")
        before = self._head()

        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho 'hook refuses commit' >&2\nexit 1\n", encoding="utf-8")
        os.chmod(hook, 0o755)

        self._write("a.txt", "two\n")
        self.assertTrue(gitinfo.stage(self.repo, ["a.txt"])["ok"])
        r = gitinfo.commit(self.repo, "second")
        self.assertFalse(r["ok"])
        self.assertIn("hook refuses", r["reason"])
        # 钩子确实拦住了：HEAD 没动
        self.assertEqual(self._head(), before)


class RemoteTests(GitRepoCase):
    def _remote_log(self, remote: Path) -> str:
        return _git("log", "--oneline", "main", cwd=remote).stdout

    def test_push_without_upstream_reports_error(self) -> None:
        self._write("a.txt", "one\n")
        self._commit_all("init")
        r = gitinfo.push(self.repo)
        self.assertTrue(r["available"])
        self.assertFalse(r["ok"])
        self.assertTrue(r["reason"])

    def test_push_succeeds_to_file_remote(self) -> None:
        remote = self._add_remote()
        self._write("a.txt", "one\n")
        self._commit_all("init")
        self.assertEqual(self._push_upstream(remote).returncode, 0)

        self._write("a.txt", "two\n")
        self._commit_all("second")
        r = gitinfo.push(self.repo)
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertIn("second", self._remote_log(remote))

    def test_pull_ff_only_fast_forwards(self) -> None:
        remote = self._add_remote()
        self._write("a.txt", "base\n")
        self._commit_all("base")
        self.assertEqual(self._push_upstream(remote).returncode, 0)

        other = self._clone(remote, "other")
        (other / "a.txt").write_text("remote-new\n", encoding="utf-8")
        _git("add", "a.txt", cwd=other)
        _git("commit", "-m", "remote", cwd=other)
        self.assertEqual(_git("push", cwd=other).returncode, 0)

        r = gitinfo.pull_ff(self.repo)
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertEqual((self.repo / "a.txt").read_text(encoding="utf-8"), "remote-new\n")

    def test_pull_ff_only_rejects_divergence_without_changes(self) -> None:
        """分叉时 `--ff-only` 必须失败，且**本地一个字都不动**（绝不自动合并）。"""
        remote = self._add_remote()
        self._write("a.txt", "base\n")
        self._commit_all("base")
        self.assertEqual(self._push_upstream(remote).returncode, 0)

        other = self._clone(remote, "other")
        (other / "a.txt").write_text("remote-b\n", encoding="utf-8")
        _git("add", "a.txt", cwd=other)
        _git("commit", "-m", "remote", cwd=other)
        self.assertEqual(_git("push", cwd=other).returncode, 0)

        # 本地也前进一个提交 → 与远端分叉
        self._write("a.txt", "local-c\n")
        self._commit_all("local")
        before_head = self._head()
        before_bytes = (self.repo / "a.txt").read_bytes()

        r = gitinfo.pull_ff(self.repo)
        self.assertFalse(r["ok"])
        self.assertTrue(r["reason"])
        self.assertEqual(self._head(), before_head)
        self.assertEqual((self.repo / "a.txt").read_bytes(), before_bytes)


class DiscardTests(GitRepoCase):
    def test_discard_tracked_and_untracked(self) -> None:
        self._write("a.txt", "v1\n")
        self._commit_all("init")
        self._write("a.txt", "v2\n")          # 已跟踪、被改
        self._write("loose.txt", "junk\n")    # 未跟踪

        r = gitinfo.discard(self.repo, ["a.txt", "loose.txt"])
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertEqual((self.repo / "a.txt").read_text(encoding="utf-8"), "v1\n")
        self.assertFalse((self.repo / "loose.txt").exists())

    def test_discard_untracked_in_unborn_repo(self) -> None:
        """没有提交时也要能放弃未跟踪文件（走 clean 而非 checkout）。"""
        self._write("loose.txt", "junk\n")
        r = gitinfo.discard(self.repo, ["loose.txt"])
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertFalse((self.repo / "loose.txt").exists())

    def test_discard_without_paths_is_rejected(self) -> None:
        r = gitinfo.discard(self.repo, [])
        self.assertFalse(r["ok"])
        self.assertIn("未指定", r["reason"])


class BranchTests(GitRepoCase):
    def test_branch_create_and_switch(self) -> None:
        self._write("a.txt", "one\n")
        self._commit_all("init")

        r = gitinfo.branch_create(self.repo, "feature/a")
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertEqual(gitinfo.status(self.repo)["branch"], "feature/a")

        r = gitinfo.branch_switch(self.repo, "main")
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertEqual(gitinfo.status(self.repo)["branch"], "main")

    def test_invalid_refname_is_rejected_without_touching_repo(self) -> None:
        before = self._head()
        for bad in ("../evil", "with space", "-dash", "a..b", "x.lock", ""):
            with self.subTest(bad=bad):
                r = gitinfo.branch_create(self.repo, bad)
                self.assertFalse(r["ok"], f"{bad!r} 应被拒")
                self.assertIn("非法", r["reason"])
        self.assertEqual(self._head(), before)


class ValidRefnameTests(unittest.TestCase):
    def test_accepts_reasonable_names(self) -> None:
        for name in ("main", "feature/x-1", "release/2024.1", "a.b", "A1"):
            with self.subTest(name=name):
                self.assertTrue(gitinfo.valid_refname(name))

    def test_rejects_git_forbidden_forms(self) -> None:
        for bad in (
            "../evil", "with space", "-dash", ".lead", "a..b",
            "trail.", "x.lock", "", "a@{b}", "a\\b", "a//b", "a/", "/lead",
        ):
            with self.subTest(bad=bad):
                self.assertFalse(gitinfo.valid_refname(bad))


class GitCommitUploadRedactionTests(unittest.TestCase):
    """ADR-006：用户手打的 commit message 只上传指纹，正文不出机。"""

    def test_commit_message_is_only_uploaded_as_digest(self) -> None:
        message = "fix: 这里有用户复述的敏感细节 sk-abc123"
        entry = {"event": "git.commit", "ts_epoch": 1_700_000_000, "message": message, "oid": "abc1234"}

        out = vocabulary.translate(entry, session_id="s", sequence=1, fallback_ts=0)
        self.assertIsNotNone(out)
        payload = out.event["payload"]
        self.assertNotIn("message", payload)
        self.assertIn("message_digest", payload)
        self.assertEqual(payload["message_digest"], vocabulary.digest_value(message))
        # 指纹里不能混进原文片段
        self.assertNotIn("sk-abc123", json.dumps(payload, ensure_ascii=False))


# ======================================================================== 端点（host）
class GitWriteEndpointTests(unittest.TestCase):
    """真实 HTTP 层：七个写端点 + 只读 status/branches。

    工作区是一个真 git 仓库（含一次提交），因此 `HEAD` 有值、状态确定。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self._saved_settings = os.environ.get("VIBE_SETTINGS")
        os.environ["VIBE_SETTINGS"] = str(self.root / "settings.json")

        self.ws = self.root / "ws"
        self.ws.mkdir()
        _init_repo(self.ws)
        (self.ws / "a.txt").write_text("v1\n", encoding="utf-8")
        _git("add", "-A", cwd=self.ws)
        _git("commit", "-m", "init", cwd=self.ws)

        self._hosts: list[HostServer] = []
        self.host = self._build_host(self.ws)
        self.base = f"http://127.0.0.1:{self.host.port}"
        self.token = self.host.guard.token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        for host in reversed(self._hosts):
            host.shutdown()
        if self._saved_settings is None:
            os.environ.pop("VIBE_SETTINGS", None)
        else:
            os.environ["VIBE_SETTINGS"] = self._saved_settings
        self._tmp.cleanup()

    def _build_host(self, ws: Path) -> HostServer:
        config = Config.load(ws)
        host = HostServer(config, port=0, client=FakeClient([[("list_dir", {})], "好的。"]))
        host.start_background()
        self._hosts.append(host)
        return host

    def _call(self, path: str, *, method: str = "GET", body: dict | None = None,
              host: HostServer | None = None):
        host = host or self.host
        base = f"http://127.0.0.1:{host.port}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(base + path, data=data, method=method)
        request.add_header("X-Vibe-Token", host.guard.token)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            return exc.code, parsed

    def _entry(self, body: dict, path: str):
        return next((e for e in body.get("entries", []) if e["path"] == path), None)

    # ---- 只读 -----------------------------------------------------------
    def test_get_status_and_branches(self) -> None:
        status, body = self._call("/api/git/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["available"])
        self.assertEqual(body["branch"], "main")

        status, body = self._call("/api/git/branches")
        self.assertEqual(status, 200)
        self.assertEqual(body["current"], "main")

    # ---- 暂存 / 取消暂存 ---------------------------------------------
    def test_stage_then_unstage_via_http(self) -> None:
        (self.ws / "new.txt").write_text("n\n", encoding="utf-8")

        status, body = self._call("/api/git/stage", method="POST", body={"paths": ["new.txt"]})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self._entry(body, "new.txt")["index"], "A")

        status, body = self._call("/api/git/unstage", method="POST", body={"paths": ["new.txt"]})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self._entry(body, "new.txt")["status"], "??")

    def test_stage_all_true_semantics(self) -> None:
        (self.ws / "x.txt").write_text("x\n", encoding="utf-8")
        (self.ws / "y.txt").write_text("y\n", encoding="utf-8")

        status, body = self._call("/api/git/stage", method="POST", body={"all": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self._entry(body, "x.txt")["index"], "A")
        self.assertEqual(self._entry(body, "y.txt")["index"], "A")

        status, body = self._call("/api/git/unstage", method="POST", body={"all": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(all(e["status"] == "??" for e in body["entries"]), body["entries"])

    # ---- 提交 -----------------------------------------------------------
    def test_commit_endpoint_returns_oid_and_advances_head(self) -> None:
        (self.ws / "c.txt").write_text("c\n", encoding="utf-8")
        self._call("/api/git/stage", method="POST", body={"paths": ["c.txt"]})

        status, body = self._call("/api/git/commit", method="POST", body={"message": "feat: 提交"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body.get("oid"))
        log = _git("log", "--oneline", "-1", cwd=self.ws).stdout
        self.assertIn("feat: 提交", log)

    def test_commit_empty_message_is_400(self) -> None:
        status, body = self._call("/api/git/commit", method="POST", body={"message": "   "})
        self.assertEqual(status, 400)
        self.assertIn("不能为空", body["error"])

    def test_commit_with_nothing_staged_is_409(self) -> None:
        """git 自身失败（没东西可提交）→ 409 + stderr 摘要，不是 200。"""
        status, body = self._call("/api/git/commit", method="POST", body={"message": "nothing"})
        self.assertEqual(status, 409)
        self.assertTrue(body["error"])

    # ---- 放弃（破坏性，带检查点）--------------------------------------
    def test_discard_creates_checkpoint_and_restore_brings_file_back(self) -> None:
        """B5 的核心：放弃改动前先落检查点，误丢的内容能从检查点找回。"""
        (self.ws / "a.txt").write_text("v2\n", encoding="utf-8")

        status, body = self._call("/api/git/discard", method="POST", body={"paths": ["a.txt"]})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        checkpoint_id = body.get("checkpointId")
        self.assertTrue(checkpoint_id)
        # 已回滚到提交时的 v1
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "v1\n")

        # 从检查点完整找回 v2 —— 这正是「放弃不会丢工作」的保证
        report = self.host.session.agent.checkpoints.restore(checkpoint_id)
        self.assertIn("a.txt", report.restored)
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "v2\n")

    def test_discard_records_audit_without_path_text(self) -> None:
        (self.ws / "a.txt").write_text("v2\n", encoding="utf-8")
        status, body = self._call("/api/git/discard", method="POST", body={"paths": ["a.txt"]})
        checkpoint_id = body["checkpointId"]

        events = [e for e in _read_audit(self.host.session.agent.config.audit_path)
                  if e.get("event") == "git.discard"]
        self.assertTrue(events)
        last = events[-1]
        self.assertEqual(last["count"], 1)
        self.assertEqual(last["checkpoint_id"], checkpoint_id)
        self.assertIn("paths_digest", last)
        # 审计里不许出现路径原文（ADR-006）
        self.assertNotIn("a.txt", json.dumps(last, ensure_ascii=False))

    # ---- 路径防线 -----------------------------------------------------
    def test_stage_outside_workspace_is_400(self) -> None:
        status, body = self._call("/api/git/stage", method="POST", body={"paths": ["../evil.txt"]})
        self.assertEqual(status, 400)

    def test_discard_sensitive_path_is_400(self) -> None:
        """借 git 放弃动作绕过写保护？`.vibe` 审计目录照样被拒。"""
        status, body = self._call(
            "/api/git/discard", method="POST", body={"paths": [".vibe/audit.jsonl"]}
        )
        self.assertEqual(status, 400)

    # ---- 忙碌态 -------------------------------------------------------
    def test_busy_returns_409(self) -> None:
        self.host.session._state = "running"
        try:
            status, body = self._call(
                "/api/git/stage", method="POST", body={"paths": ["a.txt"]}
            )
            self.assertEqual(status, 409)
        finally:
            self.host.session._state = "idle"

    # ---- 非仓库 -------------------------------------------------------
    def test_non_repo_returns_available_false(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        host = self._build_host(plain)

        status, body = self._call("/api/git/status", host=host)
        self.assertEqual(status, 200)
        self.assertFalse(body["available"])

        # 写端点对非仓库同样返回 200 + available:false（不是 4xx —— 这是正常状态）
        status, body = self._call(
            "/api/git/stage", method="POST", body={"paths": ["a.txt"]}, host=host
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["available"])


if __name__ == "__main__":
    unittest.main()
