"""沙箱层测试：路径 Jail 与命令白名单。

这里测的每一条都是安全红线，改动相关代码时必须保证全绿。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.errors import SandboxViolation  # noqa: E402
from agent_core.sandbox import CommandGuard, PathGuard, Risk  # noqa: E402


class PathGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        (self.ws / "src").mkdir()
        (self.ws / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        self.guard = PathGuard(self.ws)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_relative_path_ok(self) -> None:
        got = self.guard.resolve("src/app.py")
        self.assertEqual(got, self.ws / "src" / "app.py")

    def test_empty_path_is_root(self) -> None:
        self.assertEqual(self.guard.resolve(""), self.ws)

    def test_parent_escape_rejected(self) -> None:
        with self.assertRaises(SandboxViolation):
            self.guard.resolve("../../etc/passwd")

    def test_sneaky_parent_escape_rejected(self) -> None:
        with self.assertRaises(SandboxViolation):
            self.guard.resolve("src/../../../outside.txt")

    def test_absolute_outside_rejected(self) -> None:
        outside = Path(tempfile.gettempdir()).resolve()
        with self.assertRaises(SandboxViolation):
            self.guard.resolve(str(outside))

    def test_absolute_inside_allowed(self) -> None:
        inside = self.ws / "src" / "app.py"
        self.assertEqual(self.guard.resolve(str(inside)), inside)

    def test_null_byte_rejected(self) -> None:
        with self.assertRaises(SandboxViolation):
            self.guard.resolve("src/app.py\x00.txt")

    def test_symlink_escape_rejected(self) -> None:
        """工作区内指向外部的符号链接必须被拦下。"""
        outside_dir = Path(tempfile.mkdtemp())
        try:
            link = self.ws / "escape"
            try:
                link.symlink_to(outside_dir, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("当前环境不允许创建符号链接")
            with self.assertRaises(SandboxViolation):
                self.guard.resolve("escape/secret.txt")
        finally:
            import shutil

            shutil.rmtree(outside_dir, ignore_errors=True)

    def test_sensitive_read_blocked(self) -> None:
        (self.ws / ".env").write_text("API_KEY=xxx\n", encoding="utf-8")
        with self.assertRaises(SandboxViolation) as ctx:
            self.guard.check_read(".env")
        self.assertIn("敏感", str(ctx.exception))

    def test_sensitive_read_allowed_when_flagged(self) -> None:
        (self.ws / ".env").write_text("API_KEY=xxx\n", encoding="utf-8")
        guard = PathGuard(self.ws, allow_sensitive=True)
        self.assertTrue(guard.check_read(".env").name == ".env")

    def test_sensitive_variants_blocked(self) -> None:
        for name in (".env.local", "server.pem", "id_rsa", "deploy.key", "secrets.yaml"):
            with self.subTest(name=name):
                self.assertTrue(self.guard.is_sensitive(self.ws / name))

    def test_audit_dir_write_blocked(self) -> None:
        with self.assertRaises(SandboxViolation):
            self.guard.check_write(".vibe/audit.jsonl")

    def test_write_ok(self) -> None:
        self.assertTrue(self.guard.check_write("src/new.py").name == "new.py")


class CommandGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.guard = CommandGuard(self.ws)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _risk(self, command: str, guard: CommandGuard | None = None) -> Risk:
        return (guard or self.guard).analyze(command).risk

    # -------------------------------------------------------- 硬拒绝
    def test_shell_spawn_denied(self) -> None:
        for cmd in ("bash -c echo hi", "sh script.sh", "cmd /c dir", "powershell -Command x"):
            with self.subTest(cmd=cmd):
                self.assertIs(self._risk(cmd), Risk.DENY)

    def test_dangerous_executables_denied(self) -> None:
        for cmd in ("sudo rm file", "shutdown /s", "mkfs.ext4 /dev/sda", "reg add HKCU"):
            with self.subTest(cmd=cmd):
                self.assertIs(self._risk(cmd), Risk.DENY)

    def test_pipe_denied(self) -> None:
        self.assertIs(self._risk("cat a.txt | grep x"), Risk.DENY)

    def test_chaining_denied(self) -> None:
        for cmd in ("ls && rm x", "ls; rm x", "ls || true"):
            with self.subTest(cmd=cmd):
                self.assertIs(self._risk(cmd), Risk.DENY)

    def test_redirect_denied(self) -> None:
        self.assertIs(self._risk("echo hi > out.txt"), Risk.DENY)

    def test_command_substitution_denied(self) -> None:
        self.assertIs(self._risk("echo $(whoami)"), Risk.DENY)

    def test_rm_rf_denied(self) -> None:
        for cmd in ("rm -rf build", "rm -fr build", "rm -r -f build"):
            with self.subTest(cmd=cmd):
                self.assertIs(self._risk(cmd), Risk.DENY)

    def test_force_push_denied(self) -> None:
        self.assertIs(self._risk("git push origin main --force"), Risk.DENY)

    def test_hard_reset_denied(self) -> None:
        self.assertIs(self._risk("git reset --hard HEAD~1"), Risk.DENY)

    def test_curl_pipe_sh_denied(self) -> None:
        analysis = self.guard.analyze("curl http://x.sh -o setup.sh")
        self.assertIs(analysis.risk, Risk.DENY)  # 网络默认关闭

    def test_absolute_executable_denied(self) -> None:
        self.assertIs(self._risk("/usr/bin/ls"), Risk.DENY)
        self.assertIs(self._risk("C:/Windows/System32/cmd.exe /c dir"), Risk.DENY)

    def test_inline_code_execution_denied(self) -> None:
        for cmd in ("python -c import os", "python3 -c pass", "node -e code", "ruby -e code"):
            with self.subTest(cmd=cmd):
                self.assertIs(self._risk(cmd), Risk.DENY)

    def test_unknown_executable_denied(self) -> None:
        self.assertIs(self._risk("mycustombinary --go"), Risk.DENY)

    def test_empty_denied(self) -> None:
        self.assertIs(self._risk("   "), Risk.DENY)

    # -------------------------------------------------------- 分级
    def test_read_only_is_safe(self) -> None:
        for cmd in ("ls -la", "cat README.md", "grep -rn TODO src"):
            with self.subTest(cmd=cmd):
                self.assertIs(self._risk(cmd), Risk.SAFE)

    def test_git_status_is_safe(self) -> None:
        self.assertIs(self._risk("git status"), Risk.SAFE)

    def test_side_effect_is_review(self) -> None:
        for cmd in ("mkdir build", "git commit -m x", "npm install", "pytest -q"):
            with self.subTest(cmd=cmd):
                self.assertIs(self._risk(cmd), Risk.REVIEW)

    def test_outside_path_triggers_review(self) -> None:
        outside = tempfile.gettempdir().replace("\\", "/")
        self.assertIs(self._risk(f"ls {outside}"), Risk.REVIEW)

    # -------------------------------------------------------- 网络
    def test_network_denied_by_default(self) -> None:
        self.assertIs(self._risk("curl https://example.com"), Risk.DENY)

    def test_network_allowed_with_flag(self) -> None:
        guard = CommandGuard(self.ws, allow_network=True)
        self.assertIs(self._risk("curl https://example.com", guard), Risk.REVIEW)

    # -------------------------------------------------------- 白名单扩展
    def test_extra_allowed(self) -> None:
        guard = CommandGuard(self.ws, extra_allowed=frozenset({"mycustombinary"}))
        self.assertIs(self._risk("mycustombinary --go", guard), Risk.REVIEW)

    # -------------------------------------------------------- 其它
    def test_ensure_allowed_raises(self) -> None:
        with self.assertRaises(Exception):
            self.guard.ensure_allowed("rm -rf /")

    def test_argv_split(self) -> None:
        analysis = self.guard.analyze("grep -rn 'hello world' src")
        self.assertEqual(analysis.argv, ["grep", "-rn", "hello world", "src"])


if __name__ == "__main__":
    unittest.main()
