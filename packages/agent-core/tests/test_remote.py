"""远程设备子系统测试。

覆盖面刻意偏重**安全语义**而不是功能走通：连接字段校验（含 ssh 选项注入）、
远端路径 Jail、写回字节校验、三层门控、审计不含正文。
这些每一条都是这类「把执行面伸到另一台机器」的功能上最容易出错、
且出错代价最高的地方。

真实 SSH 连接不在单测范围内（需要一个远端主机）。取而代之的是
`FakeTransport`：它实现与 `SSHTransport` 相同的上层接口，在内存里模拟一个
远端文件系统。这样「jail → 补丁引擎 → 写回 → 留底」这条完整链路可以被
端到端验证，而不依赖网络。**argv 拼装**单独由 `TransportArgvTest` 覆盖。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.audit import AuditLog  # noqa: E402
from agent_core.checkpoint import CheckpointManager  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.errors import PatchError, SandboxViolation, ToolError  # noqa: E402
from agent_core.remote.connection import RemoteConnection  # noqa: E402
from agent_core.remote.errors import RemoteConfigError, RemoteError  # noqa: E402
from agent_core.remote.pathguard import RemotePathGuard  # noqa: E402
from agent_core.remote.store import RemoteStore  # noqa: E402
from agent_core.remote.transport import (  # noqa: E402
    Capabilities,
    RemoteResult,
    SSHTransport,
    _classify_ssh_error,
    _last_int,
)
from agent_core.remote.workspace import RemoteWorkspace  # noqa: E402
from agent_core.sandbox import CommandGuard, PathGuard, Risk  # noqa: E402
from agent_core.tools import build_default_registry, build_readonly_registry  # noqa: E402
from agent_core.tools.base import ToolContext  # noqa: E402
from agent_core.tools.remote import (  # noqa: E402
    _outside_remote_paths,
    _safe_segment,
    locate_remote_turn,
    restore_turn,
)


def make_conn(**overrides) -> RemoteConnection:
    fields = {
        "name": "devbox",
        "host": "10.0.0.12",
        "user": "dev",
        "root": "/srv/app",
    }
    fields.update(overrides)
    return RemoteConnection(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 连接模型
# --------------------------------------------------------------------------- #
class ConnectionModelTest(unittest.TestCase):
    def test_valid_connection_passes(self) -> None:
        conn = make_conn()
        conn.validate()
        self.assertEqual(conn.target, "dev@10.0.0.12")
        self.assertEqual(conn.root_normalized, "/srv/app")
        self.assertEqual(conn.display, "dev@10.0.0.12:22 → /srv/app")

    def test_host_starting_with_dash_is_rejected(self) -> None:
        """这是本文件最重要的一条：`-oProxyCommand=…` 当主机名会被 ssh 吃掉。

        一旦放开，用户在连接名里塞一个「主机名」，就能整体推翻我们拼好的
        加固选项（含指纹校验）。正则强制首字符为字母数字，从根上堵死。
        """
        with self.assertRaises(RemoteConfigError):
            make_conn(host="-oProxyCommand=/bin/sh").validate()

    def test_user_with_dash_prefix_rejected(self) -> None:
        with self.assertRaises(RemoteConfigError):
            make_conn(user="-l").validate()

    def test_host_with_whitespace_rejected(self) -> None:
        for bad in ("a b", "n;rm -rf /", "h$(id)", "h|other"):
            with self.subTest(host=bad):
                with self.assertRaises(RemoteConfigError):
                    make_conn(host=bad).validate()

    def test_ipv6_requires_brackets(self) -> None:
        make_conn(host="[fe80::1]").validate()
        with self.assertRaises(RemoteConfigError):
            make_conn(host="fe80::1").validate()

    def test_bad_name_rejected(self) -> None:
        for bad in ("", "has space", "-lead", "名前", "a" * 33):
            with self.subTest(name=bad):
                with self.assertRaises(RemoteConfigError):
                    make_conn(name=bad).validate()

    def test_root_must_be_absolute(self) -> None:
        with self.assertRaises(RemoteConfigError):
            make_conn(root="srv/app").validate()

    def test_root_with_parent_segment_rejected(self) -> None:
        with self.assertRaises(RemoteConfigError):
            make_conn(root="/srv/../../etc").validate()

    def test_port_range(self) -> None:
        with self.assertRaises(RemoteConfigError):
            make_conn(port=0).validate()
        with self.assertRaises(RemoteConfigError):
            make_conn(port=70000).validate()

    def test_host_key_policy_enum(self) -> None:
        make_conn(host_key_policy="strict").validate()
        with self.assertRaises(RemoteConfigError):
            make_conn(host_key_policy="off").validate()

    def test_problems_reports_all_issues_at_once(self) -> None:
        """一次把问题报全，不让用户挤牙膏式地改。"""
        problems = make_conn(host="-bad", port=0, host_key_policy="nope").problems()
        self.assertGreaterEqual(len(problems), 3)

    def test_proxy_jump_validation(self) -> None:
        make_conn(proxy_jump="jump@10.0.0.1:2222").validate()
        make_conn(proxy_jump="[::1]:22").validate()
        with self.assertRaises(RemoteConfigError):
            make_conn(proxy_jump="-oProxyCommand=x").validate()
        with self.assertRaises(RemoteConfigError):
            make_conn(proxy_jump="host:notaport").validate()

    def test_round_trip_serialisation(self) -> None:
        conn = make_conn(proxy_jump="j@h:22", host_key_policy="strict",
                         allow_write=True, allow_shell=True, note="内网")
        restored = RemoteConnection.from_dict(conn.to_dict())
        self.assertEqual(restored, conn)

    def test_unknown_keys_dropped_on_load(self) -> None:
        """前向兼容：更新版本写过的字段不该让旧客户端崩掉。"""
        raw = make_conn().to_dict()
        raw["future_field"] = {"nested": True}
        conn = RemoteConnection.from_dict(raw)
        self.assertEqual(conn.name, "devbox")

    def test_bad_types_fall_back_to_defaults(self) -> None:
        conn = RemoteConnection.from_dict(
            {"name": "x", "host": "h", "port": "22", "allow_write": "yes"}
        )
        self.assertEqual(conn.port, 22)
        self.assertFalse(conn.allow_write)

    def test_frozen(self) -> None:
        conn = make_conn()
        with self.assertRaises(Exception):
            conn.host = "other"  # type: ignore[misc]

    def test_write_and_shell_default_off(self) -> None:
        conn = make_conn()
        self.assertFalse(conn.allow_write)
        self.assertFalse(conn.allow_shell)


# --------------------------------------------------------------------------- #
# 连接清单落盘
# --------------------------------------------------------------------------- #
class ConnectionStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name).resolve() / "remote.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_file_is_empty_not_error(self) -> None:
        store = RemoteStore.load(self.path)
        self.assertEqual(store.names(), [])
        self.assertEqual(store.default_name, "")

    def test_upsert_and_reload(self) -> None:
        store = RemoteStore.load(self.path)
        store.upsert(make_conn(allow_write=True))
        reloaded = RemoteStore.load(self.path)
        self.assertEqual(reloaded.names(), ["devbox"])
        self.assertTrue(reloaded.get("devbox").allow_write)  # type: ignore[union-attr]
        self.assertEqual(reloaded.default_name, "devbox")

    def test_first_connection_becomes_default(self) -> None:
        store = RemoteStore.load(self.path)
        store.upsert(make_conn(name="a"))
        store.upsert(make_conn(name="b"))
        self.assertEqual(store.default_name, "a")

    def test_removing_default_clears_it(self) -> None:
        """删掉默认连接后，默认位必须清空 —— 而不是静默改指到别的机器上。

        清空之后的兜底行为要分清：只剩一条时可以自动选它（低门槛），
        还剩两条时**拒绝猜测**（可能把改动写到错的机器上）。
        """
        store = RemoteStore.load(self.path)
        store.upsert(make_conn(name="a"))
        store.upsert(make_conn(name="b"))
        store.remove("a")
        self.assertEqual(store.default_name, "")
        # 只剩一条时自动选它是合理的（低门槛），但默认位本身必须已经清空 ——
        # 关键在于「不指向一台已删除的机器」。
        self.assertEqual(store.resolve().name, "b")
        # 再多一条且无默认 → 拒绝猜测
        store.upsert(make_conn(name="c"))
        store.default_name = ""
        with self.assertRaises(RemoteConfigError):
            store.resolve()

    def test_resolve_prefers_explicit_then_default(self) -> None:
        store = RemoteStore.load(self.path)
        store.upsert(make_conn(name="a"))
        store.upsert(make_conn(name="b"))
        store.set_default("b")
        self.assertEqual(store.resolve().name, "b")
        self.assertEqual(store.resolve("a").name, "a")

    def test_resolve_single_connection_without_default(self) -> None:
        store = RemoteStore.load(self.path)
        store.upsert(make_conn(name="only"))
        store.default_name = ""
        self.assertEqual(store.resolve().name, "only")

    def test_resolve_ambiguous_refuses_to_guess(self) -> None:
        store = RemoteStore.load(self.path)
        store.upsert(make_conn(name="a"))
        store.upsert(make_conn(name="b"))
        store.default_name = ""
        with self.assertRaises(RemoteConfigError) as ctx:
            store.resolve()
        self.assertIn("拒绝猜测", str(ctx.exception))

    def test_require_lists_available_names(self) -> None:
        store = RemoteStore.load(self.path)
        store.upsert(make_conn(name="alpha"))
        with self.assertRaises(RemoteConfigError) as ctx:
            store.require("beta")
        self.assertIn("alpha", str(ctx.exception))

    def test_corrupt_json_raises_not_silently_empty(self) -> None:
        """清单是配置不是缓存：坏了必须说，而不是让连接「凭空消失」。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(RemoteConfigError):
            RemoteStore.load(self.path)

    def test_future_version_rejected(self) -> None:
        self.path.write_text(
            json.dumps({"version": 99, "connections": []}), encoding="utf-8"
        )
        with self.assertRaises(RemoteConfigError):
            RemoteStore.load(self.path)

    def test_dangling_default_is_dropped_not_fatal(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "default": "ghost",
                    "connections": [make_conn(name="real").to_dict()],
                }
            ),
            encoding="utf-8",
        )
        store = RemoteStore.load(self.path)
        self.assertEqual(store.default_name, "")
        self.assertEqual(store.names(), ["real"])

    @unittest.skipIf(os.name == "nt", "Windows 上 chmod 无 POSIX 语义")
    def test_file_mode_is_0600(self) -> None:
        store = RemoteStore.load(self.path)
        store.upsert(make_conn())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_save_is_atomic_no_leftover_temp(self) -> None:
        store = RemoteStore.load(self.path)
        store.upsert(make_conn())
        leftovers = [p.name for p in self.path.parent.iterdir() if "vibetmp" in p.name]
        self.assertEqual(leftovers, [])

    def test_created_at_preserved_on_update(self) -> None:
        store = RemoteStore.load(self.path)
        first = store.upsert(make_conn())
        updated = store.upsert(make_conn(allow_write=True))
        self.assertEqual(updated.created_at, first.created_at)


# --------------------------------------------------------------------------- #
# 远端路径 Jail
# --------------------------------------------------------------------------- #
class RemotePathGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = RemotePathGuard("/srv/app")

    def test_relative_resolves_under_root(self) -> None:
        self.assertEqual(self.guard.normalize("src/app.py"), "/srv/app/src/app.py")

    def test_dot_segments_collapsed(self) -> None:
        self.assertEqual(self.guard.normalize("./src/./a.py"), "/srv/app/src/a.py")

    def test_empty_is_root(self) -> None:
        self.assertEqual(self.guard.normalize(""), "/srv/app")
        self.assertEqual(self.guard.normalize(None), "/srv/app")

    def test_parent_escape_rejected(self) -> None:
        for bad in ("../etc/passwd", "../../etc/passwd", "src/../../etc/passwd",
                    "a/../../../../etc/shadow"):
            with self.subTest(path=bad):
                with self.assertRaises(SandboxViolation):
                    self.guard.normalize(bad)

    def test_absolute_outside_root_rejected(self) -> None:
        for bad in ("/etc/passwd", "/srv/other", "/srv/appx/file"):
            with self.subTest(path=bad):
                with self.assertRaises(SandboxViolation):
                    self.guard.normalize(bad)

    def test_absolute_inside_root_allowed(self) -> None:
        self.assertEqual(self.guard.normalize("/srv/app/src/a.py"), "/srv/app/src/a.py")

    def test_null_byte_rejected(self) -> None:
        with self.assertRaises(SandboxViolation):
            self.guard.normalize("src/a\x00.py")

    def test_root_itself_is_inside(self) -> None:
        self.assertTrue(self.guard.is_inside("/srv/app"))
        self.assertFalse(self.guard.is_inside("/srv/appx"))

    def test_sensitive_files_blocked_on_read(self) -> None:
        for bad in (".env", "config/.env.local", "keys/id_rsa", "a/server.pem",
                    ".aws/credentials", ".npmrc"):
            with self.subTest(path=bad):
                with self.assertRaises(SandboxViolation) as ctx:
                    self.guard.check_read(bad)
                self.assertEqual(ctx.exception.kind, "sensitive")

    def test_sensitive_allowed_when_explicitly_enabled(self) -> None:
        guard = RemotePathGuard("/srv/app", allow_sensitive=True)
        self.assertEqual(guard.check_read(".env"), "/srv/app/.env")

    def test_write_to_root_rejected(self) -> None:
        with self.assertRaises(SandboxViolation) as ctx:
            self.guard.check_write("")
        self.assertEqual(ctx.exception.kind, "root_write")

    def test_write_to_remote_vibe_rejected(self) -> None:
        """远端 `.vibe` 可能是**另一台机器**上 Agent 的审计目录。"""
        with self.assertRaises(SandboxViolation) as ctx:
            self.guard.check_write(".vibe/tmp/notes.md")
        self.assertEqual(ctx.exception.kind, "audit_dir")

    def test_write_to_remote_audit_log_blocked_twice_over(self) -> None:
        """审计日志同时命中「敏感文件」与 `.vibe` 两条规则 —— 任一命中都该拦下。

        先命中哪条不重要，重要的是**不能放行**；这里钉住这个不变式，
        免得将来有人调整检查顺序时不小心把它放过去。
        """
        with self.assertRaises(SandboxViolation) as ctx:
            self.guard.check_write(".vibe/audit.jsonl")
        self.assertIn(ctx.exception.kind, ("audit_dir", "sensitive"))

    def test_policy_deny_patterns(self) -> None:
        guard = RemotePathGuard("/srv/app", denied_patterns=("secrets", "**/*.prod"))
        with self.assertRaises(SandboxViolation) as ctx:
            guard.check_read("secrets/db.txt")
        self.assertEqual(ctx.exception.kind, "policy_deny")
        with self.assertRaises(SandboxViolation):
            guard.check_write("config/app.prod")

    def test_relative_path_display(self) -> None:
        self.assertEqual(self.guard.relative("/srv/app/src/a.py"), "src/a.py")
        self.assertEqual(self.guard.relative("/srv/app"), ".")

    def test_bad_root_rejected(self) -> None:
        with self.assertRaises(SandboxViolation):
            RemotePathGuard("relative/path")
        with self.assertRaises(SandboxViolation):
            RemotePathGuard("/srv/../../etc")

    def test_trailing_slash_root_normalised(self) -> None:
        self.assertEqual(RemotePathGuard("/srv/app/").root_resolved, "/srv/app")


# --------------------------------------------------------------------------- #
# ssh argv 拼装（不发起连接）
# --------------------------------------------------------------------------- #
class TransportArgvTest(unittest.TestCase):
    def test_hardening_flags_present(self) -> None:
        argv = SSHTransport(make_conn(), multiplex=False).base_argv()
        joined = " ".join(argv)
        self.assertIn("BatchMode=yes", joined)
        self.assertIn("NumberOfPasswordPrompts=0", joined)
        self.assertIn("StrictHostKeyChecking=accept-new", joined)
        self.assertEqual(argv[-1], "dev@10.0.0.12")
        self.assertIn("-p", argv)

    def test_strict_policy(self) -> None:
        argv = SSHTransport(
            make_conn(host_key_policy="strict"), multiplex=False
        ).base_argv()
        self.assertIn("StrictHostKeyChecking=yes", " ".join(argv))

    def test_insecure_policy_discards_known_hosts(self) -> None:
        argv = SSHTransport(
            make_conn(host_key_policy="insecure"), multiplex=False
        ).base_argv()
        joined = " ".join(argv)
        self.assertIn("StrictHostKeyChecking=no", joined)
        self.assertIn("UserKnownHostsFile=/dev/null", joined)

    def test_identity_file_sets_identities_only(self) -> None:
        conn = make_conn(identity_file="~/.ssh/id_ed25519")
        argv = SSHTransport(conn, multiplex=False).base_argv()
        self.assertIn("-i", argv)
        # 限定只用指定密钥，避免 agent 里的密钥逐个试到 MaxAuthTries
        self.assertIn("IdentitiesOnly=yes", " ".join(argv))

    def test_proxy_jump_flag(self) -> None:
        argv = SSHTransport(
            make_conn(proxy_jump="jump@10.0.0.1"), multiplex=False
        ).base_argv()
        self.assertIn("-J", argv)

    def test_wrap_cwd_quotes_every_part(self) -> None:
        conn = make_conn(root="/srv/my app")
        transport = SSHTransport(conn, multiplex=False)
        command = transport._wrap_cwd(["git", "log", "--oneline"], cwd=None)
        # 带空格的目录必须被引号包住，否则远端 shell 会把它切成两个词
        self.assertIn("'/srv/my app'", command)
        self.assertTrue(command.startswith("cd '/srv/my app' && exec "))

    def test_wrap_cwd_neutralises_metacharacters(self) -> None:
        transport = SSHTransport(make_conn(), multiplex=False)
        command = transport._wrap_cwd(["echo", "a; rm -rf /"], cwd="/srv/app")
        # 参数被整体引号包裹 → 远端 shell 不会把 `;` 当分隔符执行
        self.assertIn("'a; rm -rf /'", command)
        self.assertNotIn("; rm -rf / $\'", command)

    def test_multiplexing_disabled_on_windows(self) -> None:
        transport = SSHTransport(make_conn(), multiplex=False)
        self.assertNotIn("ControlMaster=auto", " ".join(transport.base_argv()))

    def test_classify_ssh_errors(self) -> None:
        cases = {
            "Permission denied (publickey).": "auth",
            "Host key verification failed.": "hostkey",
            "REMOTE HOST IDENTIFICATION HAS CHANGED!": "hostkey",
            "ssh: connect to host x port 22: Connection refused": "unreachable",
            "Could not resolve hostname x": "unreachable",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(_classify_ssh_error(text), expected)
        self.assertIsNone(_classify_ssh_error("远端程序的普通报错"))

    def test_last_int_parsing(self) -> None:
        self.assertEqual(_last_int("123\n"), 123)
        self.assertEqual(_last_int("log line\n42\n"), 42)
        self.assertIsNone(_last_int("no digits"))


# --------------------------------------------------------------------------- #
# 内存版远端文件系统
# --------------------------------------------------------------------------- #
class FakeTransport:
    """与 SSHTransport 同接口的内存实现，用于端到端验证上层逻辑。"""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files: dict[str, bytes] = dict(files or {})
        self.writes: list[tuple[str, bytes]] = []
        self.realpath_map: dict[str, str] = {}

    def capabilities(self) -> Capabilities:
        return Capabilities(
            os_name="Linux", kernel="6.1", arch="x86_64", user="dev",
            cwd="/srv/app", shell="/bin/bash", git_version="git version 2.40",
            python_version="3.11.4", find_printf=True, stat_gnu=True,
            head_c=True, ls_a1=True, root_writable=True, multiplexed=True,
        )

    def read_bytes(self, path: str, *, max_bytes: int, timeout: int | None = None) -> bytes:
        if path not in self.files:
            raise RemoteError(f"读取远端文件失败：{path}", kind="notfound")
        data = self.files[path]
        if len(data) > max_bytes:
            raise RemoteError("远端文件超过上限", kind="remote")
        return data

    def write_bytes(self, path: str, data: bytes, *, mode: str | None = None,
                    timeout: int | None = None) -> dict[str, object]:
        created = path not in self.files
        self.files[path] = data
        self.writes.append((path, data))
        return {"bytes": len(data), "created": created, "mode": mode or "644"}

    def exists(self, path: str) -> bool:
        return path in self.files

    def delete_file(self, path: str, *, timeout: int | None = None) -> dict[str, object]:
        existed = path in self.files
        self.files.pop(path, None)
        return {"deleted": existed, "path": path}

    def is_dir(self, path: str) -> bool:
        return path.rstrip("/") == "/srv/app"

    def list_dir(self, path: str) -> list[dict[str, object]]:
        prefix = path.rstrip("/") + "/"
        return [
            {"name": key[len(prefix):], "type": "f", "size": len(value)}
            for key, value in sorted(self.files.items())
            if key.startswith(prefix) and "/" not in key[len(prefix):]
        ]

    def realpath(self, path: str) -> str | None:
        return self.realpath_map.get(path)

    def grep(self, pattern: str, *, path: str, ignore_case: bool = False,
             max_hits: int = 200, timeout: int | None = None) -> RemoteResult:
        hits = [f"{key}:1:match" for key in self.files if pattern in key]
        return RemoteResult(0, "\n".join(hits), "", 0.01)

    def run(self, argv: list[str], *, cwd: str | None = None,
            timeout: int | None = None, stdin_bytes: bytes | None = None) -> RemoteResult:
        return RemoteResult(0, "ok", "", 0.01)


class _UnchangedPlan:
    """最小可用的 PatchPlan 替身：old == new，因此 `changed` 为 False。

    真实的 `plan_patch` 会在 search == replace 时直接抛错，所以「计划存在但
    无改动」这条路径没法用真实引擎走到 —— 它在 `edit()` 里是防御性分支。
    用替身把它单独钉住，比留一段永远不被覆盖的代码要好。
    """

    def __init__(self) -> None:
        self.rel_path = "src/app.py"
        self.old_text = "x = 1\n"
        self.new_text = "x = 1\n"
        self.diff = ""
        self.strategy = "exact"
        self.added = 0
        self.removed = 0
        self.is_create = False
        self.doc = None

    @property
    def changed(self) -> bool:
        return self.old_text != self.new_text


class RemoteWorkspaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.files = {"/srv/app/src/app.py": b"x = 1\ny = 2\n"}
        self.transport = FakeTransport(self.files)
        self.ws = RemoteWorkspace(make_conn(), transport=self.transport)  # type: ignore[arg-type]

    def test_read_text(self) -> None:
        result = self.ws.read("src/app.py")
        self.assertEqual(result.text, "x = 1\ny = 2\n")
        self.assertEqual(result.rel_path, "src/app.py")
        self.assertEqual(result.encoding, "utf-8")
        self.assertEqual(result.newline_label, "LF")

    def test_read_escape_rejected(self) -> None:
        with self.assertRaises(SandboxViolation):
            self.ws.read("../../../etc/passwd")

    def test_edit_applies_structured_patch(self) -> None:
        result = self.ws.edit("src/app.py", "y = 2", "y = 42")
        self.assertTrue(result.changed)
        self.assertTrue(result.written)
        self.assertIn("y = 42", result.diff)
        self.assertEqual(self.transport.files["/srv/app/src/app.py"], b"x = 1\ny = 42\n")

    def test_edit_preserves_crlf(self) -> None:
        """换行风格保真是 ADR-004 的一部分，远端同样适用。"""
        self.transport.files["/srv/app/src/win.py"] = b"a = 1\r\nb = 2\r\n"
        self.ws.edit("src/win.py", "b = 2", "b = 3")
        self.assertEqual(
            self.transport.files["/srv/app/src/win.py"], b"a = 1\r\nb = 3\r\n"
        )

    def test_edit_preserves_bom(self) -> None:
        self.transport.files["/srv/app/src/bom.py"] = b"\xef\xbb\xbfa = 1\n"
        self.ws.edit("src/bom.py", "a = 1", "a = 2")
        self.assertTrue(self.transport.files["/srv/app/src/bom.py"].startswith(b"\xef\xbb\xbf"))

    def test_edit_ambiguous_search_rejected(self) -> None:
        self.transport.files["/srv/app/src/dup.py"] = b"v = 1\nv = 1\n"
        with self.assertRaises(PatchError):
            self.ws.edit("src/dup.py", "v = 1", "v = 2")

    def test_edit_replace_all(self) -> None:
        self.transport.files["/srv/app/src/dup.py"] = b"v = 1\nv = 1\n"
        self.ws.edit("src/dup.py", "v = 1", "v = 2", replace_all=True)
        self.assertEqual(self.transport.files["/srv/app/src/dup.py"], b"v = 2\nv = 2\n")

    def test_edit_no_change_does_not_write(self) -> None:
        """无改动就必须不写回。

        为什么这件事值得一条测试：`TextDoc` 的换行是「按多数判定」的，
        混合换行的文件在 load → render 往返后字节可能不同。若「没改也写回」，
        会把整份文件的换行悄悄统一掉 —— 一个没有任何提示的破坏性副作用。
        """
        before = len(self.transport.writes)
        patch = mock.patch(
            "agent_core.remote.workspace.plan_patch",
            lambda *a, **kw: _UnchangedPlan(),
        )
        with patch:
            result = self.ws.edit("src/app.py", "x = 1", "x = 1")
        self.assertFalse(result.changed)
        self.assertFalse(result.written)
        self.assertEqual(len(self.transport.writes), before)

    def test_edit_symlink_escape_rejected(self) -> None:
        """远端 jail 的第二道防线：realpath 揭示软链逃逸。"""
        self.transport.realpath_map["/srv/app/link"] = "/etc/passwd"
        self.transport.files["/srv/app/link"] = b"root:x:0:0\n"
        with self.assertRaises(SandboxViolation) as ctx:
            self.ws.read("link")
        self.assertEqual(ctx.exception.kind, "escape")

    def test_write_new_file(self) -> None:
        info = self.ws.write("docs/new.md", "# hi\n")
        self.assertTrue(info["created"])
        self.assertEqual(self.transport.files["/srv/app/docs/new.md"], b"# hi\n")

    def test_write_to_sensitive_path_rejected(self) -> None:
        with self.assertRaises(SandboxViolation):
            self.ws.write(".env", "SECRET=1\n")

    def test_list_dir(self) -> None:
        entries = self.ws.list_dir("src")
        self.assertEqual([e["name"] for e in entries], ["app.py"])

    def test_capabilities_report_includes_degradation(self) -> None:
        caps = self.transport.capabilities()
        self.assertEqual(caps.degraded, [])

    def test_report_mentions_flags(self) -> None:
        report = self.ws.report()
        self.assertIn("allow_write=off", report)
        self.assertIn("allow_shell=off", report)


# --------------------------------------------------------------------------- #
# 工具注册与门控
# --------------------------------------------------------------------------- #
class RemoteToolRegistrationTest(unittest.TestCase):
    REMOTE_NAMES = {
        "remote_connections", "remote_probe", "remote_read_file", "remote_list_dir",
        "remote_grep", "remote_edit", "remote_write_file", "remote_run_command",
        "remote_review_changes",
    }

    def test_disabled_by_default(self) -> None:
        """默认关：工具列表必须与未实现前**逐字节一致**。"""
        names = set(build_default_registry(lsp=False).names())
        self.assertFalse(names & self.REMOTE_NAMES)

    def test_enabled_registers_all_nine(self) -> None:
        names = set(build_default_registry(lsp=False, remote=True).names())
        self.assertEqual(names & self.REMOTE_NAMES, self.REMOTE_NAMES)

    def test_readonly_registry_never_has_remote(self) -> None:
        """子 Agent 不得触达其他机器 —— 结构上就让它选不到。"""
        names = set(build_readonly_registry(lsp=False).names())
        self.assertFalse(names & self.REMOTE_NAMES)

    def test_risk_levels(self) -> None:
        registry = build_default_registry(lsp=False, remote=True)
        for name in ("remote_connections", "remote_probe", "remote_read_file",
                     "remote_list_dir", "remote_review_changes"):
            with self.subTest(tool=name):
                self.assertEqual(registry.get(name).risk, Risk.SAFE)  # type: ignore[union-attr]
        for name in ("remote_grep", "remote_edit", "remote_write_file",
                     "remote_run_command"):
            with self.subTest(tool=name):
                self.assertEqual(registry.get(name).risk, Risk.REVIEW)  # type: ignore[union-attr]

    def test_edit_and_write_have_previewers(self) -> None:
        """审批界面要看到真实 diff / 全文，两个写工具都必须有预演器。"""
        registry = build_default_registry(lsp=False, remote=True)
        self.assertIsNotNone(registry.get("remote_edit").previewer)  # type: ignore[union-attr]
        self.assertIsNotNone(registry.get("remote_write_file").previewer)  # type: ignore[union-attr]

    def test_run_command_has_assessor(self) -> None:
        registry = build_default_registry(lsp=False, remote=True)
        self.assertIsNotNone(registry.get("remote_run_command").assessor)  # type: ignore[union-attr]


class RemoteToolBehaviourTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        (self.ws / ".vibe").mkdir()
        self.transport = FakeTransport({"/srv/app/src/app.py": b"value = 1\n"})
        self.config = Config.load(
            self.ws, remote_enabled=True, session_id="testsess", audit_enabled=False
        )
        self.store = RemoteStore.load(self.ws / ".vibe" / "remote.json")
        self.store.upsert(make_conn(allow_write=True, allow_shell=True))

        # 把传输层换成内存实现：仓库路径 → 假实现
        self._patch = unittest.mock.patch(
            "agent_core.remote.workspace.transport_for",
            lambda conn: self.transport,
        )
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def _ctx(self) -> ToolContext:
        return ToolContext(
            config=self.config,
            paths=PathGuard(self.ws, allow_sensitive=False),
            commands=CommandGuard(self.ws, allow_network=False),
            audit=AuditLog(
                self.config.audit_path, session_id=self.config.session_id, enabled=False
            ),
        )

    def _registry(self):
        return build_default_registry(lsp=False, remote=True)

    def test_connections_listed(self) -> None:
        result = self._registry().execute(self._ctx(), "remote_connections", {})
        self.assertTrue(result.ok)
        self.assertIn("devbox", result.content)
        self.assertIn("★", result.content)

    def test_read_file_wraps_untrusted(self) -> None:
        result = self._registry().execute(
            self._ctx(), "remote_read_file", {"path": "src/app.py"}
        )
        self.assertTrue(result.ok)
        self.assertIn("UNTRUSTED_DATA", result.content)
        self.assertIn("value = 1", result.content)

    def test_read_file_escape_denied(self) -> None:
        result = self._registry().execute(
            self._ctx(), "remote_read_file", {"path": "../../etc/passwd"}
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.denied, "path")

    def test_edit_requires_approval_and_writes(self) -> None:
        ctx = self._ctx()
        result = self._registry().execute(
            ctx, "remote_edit",
            {"path": "src/app.py", "search": "value = 1", "replace": "value = 2"},
        )
        self.assertTrue(result.ok)
        self.assertEqual(self.transport.files["/srv/app/src/app.py"], b"value = 2\n")
        # 改动登记必须带 remote: 前缀，避免与本地同名文件在改动列表里串味
        self.assertIn("remote:devbox:src/app.py", ctx.written_files)

    def test_edit_leaves_local_backup(self) -> None:
        """远端改动落地前必须在本机留底 —— 这是「能回滚」的前提。"""
        self._registry().execute(
            self._ctx(), "remote_edit",
            {"path": "src/app.py", "search": "value = 1", "replace": "value = 2"},
        )
        backup = (
            self.config.checkpoint_dir / "remote" / "devbox" / "testsess" / "src" / "app.py"
        )
        self.assertTrue(backup.exists(), f"未找到改前副本: {backup}")
        self.assertEqual(backup.read_bytes(), b"value = 1\n")

    def test_edit_preview_shows_diff_before_approval(self) -> None:
        registry = self._registry()
        spec = registry.get("remote_edit")
        preview = spec.preview(  # type: ignore[union-attr]
            self._ctx(),
            {"path": "src/app.py", "search": "value = 1", "replace": "value = 2"},
        )
        self.assertEqual(preview.kind, "diff")
        self.assertIn("-value = 1", preview.body)
        self.assertIn("+value = 2", preview.body)

    def test_edit_preview_blocks_when_patch_cannot_land(self) -> None:
        spec = self._registry().get("remote_edit")
        preview = spec.preview(  # type: ignore[union-attr]
            self._ctx(),
            {"path": "src/app.py", "search": "不存在的片段", "replace": "x"},
        )
        self.assertEqual(preview.kind, "error")
        self.assertTrue(preview.blocking, "定位失败应在审批前就拦下，别打扰用户")

    def test_readonly_connection_refuses_write(self) -> None:
        self.store.upsert(make_conn(allow_write=False))
        result = self._registry().execute(
            self._ctx(), "remote_edit",
            {"path": "src/app.py", "search": "value = 1", "replace": "value = 2"},
        )
        self.assertFalse(result.ok)
        self.assertIn("allow_write=off", result.content)

    def test_readonly_connection_preview_blocks(self) -> None:
        self.store.upsert(make_conn(allow_write=False))
        spec = self._registry().get("remote_edit")
        preview = spec.preview(  # type: ignore[union-attr]
            self._ctx(),
            {"path": "src/app.py", "search": "value = 1", "replace": "value = 2"},
        )
        self.assertTrue(preview.blocking)

    def test_run_command_denied_without_conn_permission(self) -> None:
        self.store.upsert(make_conn(allow_shell=False))
        registry = self._registry()
        ctx = self._ctx()
        spec = registry.get("remote_run_command")
        risk, reason = spec.assess(ctx, {"command": "pytest -q"})  # type: ignore[union-attr]
        self.assertEqual(risk, Risk.DENY)
        self.assertIn("allow_shell=off", reason)

    def test_run_command_dangerous_command_denied_by_sandbox(self) -> None:
        registry = self._registry()
        spec = registry.get("remote_run_command")
        risk, _reason = spec.assess(  # type: ignore[union-attr]
            self._ctx(), {"command": "rm -rf /srv/app"}
        )
        self.assertEqual(risk, Risk.DENY)

    def test_run_command_shell_syntax_denied(self) -> None:
        registry = self._registry()
        spec = registry.get("remote_run_command")
        risk, _reason = spec.assess(  # type: ignore[union-attr]
            self._ctx(), {"command": "pytest -q && rm -rf build"}
        )
        self.assertEqual(risk, Risk.DENY)

    def test_run_command_readonly_subcommand_is_safe(self) -> None:
        registry = self._registry()
        spec = registry.get("remote_run_command")
        risk, _reason = spec.assess(  # type: ignore[union-attr]
            self._ctx(), {"command": "git status"}
        )
        self.assertEqual(risk, Risk.SAFE)

    def test_run_command_outside_path_escalates(self) -> None:
        registry = self._registry()
        spec = registry.get("remote_run_command")
        risk, reason = spec.assess(  # type: ignore[union-attr]
            self._ctx(), {"command": "cat /etc/passwd"}
        )
        self.assertEqual(risk, Risk.REVIEW)
        self.assertIn("远端工作区外", reason)

    def test_run_command_executes_remotely(self) -> None:
        result = self._registry().execute(
            self._ctx(), "remote_run_command", {"command": "pytest -q"}
        )
        self.assertTrue(result.ok)
        self.assertIn("退出码: 0", result.content)
        self.assertIn("UNTRUSTED_DATA", result.content)

    def test_grep_always_review(self) -> None:
        registry = self._registry()
        spec = registry.get("remote_grep")
        risk, reason = spec.assess(self._ctx(), {"pattern": "secret_token"})  # type: ignore[union-attr]
        self.assertEqual(risk, Risk.REVIEW)
        self.assertIn("远端", reason)

    def test_unknown_connection_name_fails_clearly(self) -> None:
        result = self._registry().execute(
            self._ctx(), "remote_read_file",
            {"path": "src/app.py", "connection": "nope"},
        )
        self.assertFalse(result.ok)
        self.assertIn("devbox", result.content)


# --------------------------------------------------------------------------- #
# P4：留底按轮次分层 + 轮次 manifest + 远端恢复
# --------------------------------------------------------------------------- #
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RemoteTurnRollbackTest(unittest.TestCase):
    """一轮一个留底目录：第二轮不能再吃掉第一轮的成果。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        (self.ws / ".vibe").mkdir()
        self.transport = FakeTransport(
            {"/srv/app/src/app.py": b"value = 1\n", "/srv/app/docs/old.md": b"old\n"}
        )
        self.config = Config.load(
            self.ws, remote_enabled=True, session_id="testsess", audit_enabled=False
        )
        self.store = RemoteStore.load(self.ws / ".vibe" / "remote.json")
        self.store.upsert(make_conn(allow_write=True, allow_shell=True))
        self._patch = mock.patch(
            "agent_core.remote.workspace.transport_for", lambda conn: self.transport
        )
        self._patch.start()
        self.manager = CheckpointManager(
            workspace=self.ws,
            checkpoint_dir=self.config.checkpoint_dir,
            session_id="testsess",
        )
        self.manager.begin()
        self.turn = self.manager.current_id
        self.assertNotEqual(self.turn, "", "begin() 必须立刻给出轮次 id")

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def _ctx(self) -> ToolContext:
        return ToolContext(
            config=self.config,
            paths=PathGuard(self.ws, allow_sensitive=False),
            commands=CommandGuard(self.ws, allow_network=False),
            audit=AuditLog(
                self.config.audit_path, session_id=self.config.session_id, enabled=False
            ),
            checkpoints=self.manager,
        )

    def _registry(self):
        return build_default_registry(lsp=False, remote=True)

    def _turn_dir(self) -> Path:
        return (
            self.config.checkpoint_dir
            / "remote"
            / "devbox"
            / "testsess"
            / self.turn
        )

    def _manifest(self) -> dict:
        return json.loads(
            (self._turn_dir() / "manifest.json").read_text(encoding="utf-8")
        )

    def test_edit_records_turn_dir_and_manifest(self) -> None:
        result = self._registry().execute(
            self._ctx(), "remote_edit",
            {"path": "src/app.py", "search": "value = 1", "replace": "value = 2"},
        )
        self.assertTrue(result.ok)
        # 留底必须落在「这一轮的目录」下，而不是会话级平铺
        snapshot = self._turn_dir() / "src" / "app.py"
        self.assertEqual(snapshot.read_bytes(), b"value = 1\n")
        manifest = self._manifest()
        self.assertEqual(manifest["turn"], self.turn)
        self.assertEqual(manifest["connection"], "devbox")
        entry = manifest["files"][0]
        self.assertEqual(entry["path"], "src/app.py")
        self.assertTrue(entry["existed"])
        self.assertEqual(entry["sha256Before"], _sha(b"value = 1\n"))
        self.assertEqual(entry["sha256After"], _sha(b"value = 2\n"))
        self.assertEqual(entry["bytes"], len(b"value = 2\n"))

    def test_new_file_is_recorded_even_without_snapshot(self) -> None:
        """纯新建文件没有旧字节，但 manifest 必须记下它（existed=false）。"""
        result = self._registry().execute(
            self._ctx(), "remote_write_file",
            {"path": "docs/new.md", "content": "# hi\n"},
        )
        self.assertTrue(result.ok)
        entries = {f["path"]: f for f in self._manifest()["files"]}
        self.assertIn("docs/new.md", entries)
        self.assertFalse(entries["docs/new.md"]["existed"])
        self.assertEqual(entries["docs/new.md"]["sha256After"], _sha(b"# hi\n"))
        self.assertFalse((self._turn_dir() / "docs" / "new.md").exists())

    def test_restore_brings_original_bytes_and_backs_up_current(self) -> None:
        self._registry().execute(
            self._ctx(), "remote_edit",
            {"path": "src/app.py", "search": "value = 1", "replace": "value = 2"},
        )
        self.assertEqual(self.transport.files["/srv/app/src/app.py"], b"value = 2\n")

        report = restore_turn(self._ctx(), self.turn)
        self.assertEqual(report["restored"], ["src/app.py"])
        self.assertEqual(self.transport.files["/srv/app/src/app.py"], b"value = 1\n")
        # 回退前内容留在 rollback-<时间戳>/ 轮次目录里
        self.assertTrue(report["redoId"].startswith("rollback-"))
        redo = (
            self.config.checkpoint_dir
            / "remote" / "devbox" / "testsess" / report["redoId"]
        )
        self.assertEqual((redo / "src" / "app.py").read_bytes(), b"value = 2\n")
        self.assertTrue((redo / "manifest.json").is_file())

    def test_restore_deletes_file_created_in_turn(self) -> None:
        self._registry().execute(
            self._ctx(), "remote_write_file",
            {"path": "docs/new.md", "content": "# hi\n"},
        )
        self.assertIn("/srv/app/docs/new.md", self.transport.files)

        report = restore_turn(self._ctx(), self.turn)
        self.assertEqual(report["deleted"], ["docs/new.md"])
        self.assertNotIn("/srv/app/docs/new.md", self.transport.files)
        self.assertEqual(report["redoId"], "", "删除没有内容可留底，不该伪造 redo")

    def test_stale_existing_file_rejects_whole_restore(self) -> None:
        """远端当前内容与该轮写入后不一致 → 整体拒绝，一个文件都不写。"""
        self._registry().execute(
            self._ctx(), "remote_edit",
            {"path": "src/app.py", "search": "value = 1", "replace": "value = 2"},
        )
        self.transport.files["/srv/app/src/app.py"] = b"value = 3\n"  # 外部改动
        self.transport.writes.clear()  # 只看恢复阶段有没有写

        with self.assertRaises(ValueError) as ctx:
            restore_turn(self._ctx(), self.turn)
        self.assertIn("整体取消", str(ctx.exception))
        self.assertEqual(self.transport.files["/srv/app/src/app.py"], b"value = 3\n")
        self.assertEqual(self.transport.writes, [])

    def test_externally_changed_new_file_is_skipped_not_deleted(self) -> None:
        self._registry().execute(
            self._ctx(), "remote_write_file",
            {"path": "docs/new.md", "content": "# hi\n"},
        )
        self.transport.files["/srv/app/docs/new.md"] = b"# changed\n"

        report = restore_turn(self._ctx(), self.turn)
        self.assertEqual(report["deleted"], [])
        self.assertEqual(len(report["skipped"]), 1)
        self.assertEqual(report["skipped"][0]["path"], "docs/new.md")
        self.assertIn("/srv/app/docs/new.md", self.transport.files)

    def test_restore_rejected_when_connection_readonly(self) -> None:
        self._registry().execute(
            self._ctx(), "remote_edit",
            {"path": "src/app.py", "search": "value = 1", "replace": "value = 2"},
        )
        self.store.upsert(make_conn(allow_write=False))
        with self.assertRaises(ValueError) as ctx:
            restore_turn(self._ctx(), self.turn)
        self.assertIn("allow_write=off", str(ctx.exception))
        self.assertEqual(self.transport.files["/srv/app/src/app.py"], b"value = 2\n")

    def test_checkpoint_id_cannot_traverse(self) -> None:
        with self.assertRaises(ValueError):
            locate_remote_turn(self._ctx(), "../../etc")
        with self.assertRaises(ValueError):
            locate_remote_turn(self._ctx(), "")

    def test_review_changes_is_grouped_by_turn(self) -> None:
        ctx = self._ctx()
        registry = self._registry()
        registry.execute(
            ctx, "remote_edit",
            {"path": "src/app.py", "search": "value = 1", "replace": "value = 2"},
        )
        registry.execute(
            ctx, "remote_write_file", {"path": "docs/new.md", "content": "# hi\n"}
        )
        result = registry.execute(self._ctx(), "remote_review_changes", {})
        self.assertTrue(result.ok)
        self.assertIn(f"轮次 {self.turn}", result.content)
        self.assertIn("docs/new.md", result.content)
        self.assertIn("新建", result.content)
        # 内部目录名不该原样甩给用户
        self.assertNotIn(f"{self.turn}/src/app.py", result.content)


# --------------------------------------------------------------------------- #
# 配置门控与内部工具
# --------------------------------------------------------------------------- #
class ConfigGatingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_remote_disabled_by_default(self) -> None:
        self.assertFalse(Config.load(self.ws).remote_enabled)

    def test_env_enables_remote(self) -> None:
        os.environ["VIBE_REMOTE"] = "1"
        try:
            self.assertTrue(Config.load(self.ws).remote_enabled)
        finally:
            os.environ.pop("VIBE_REMOTE", None)

    def test_env_default_and_config_path(self) -> None:
        os.environ["VIBE_REMOTE_DEFAULT"] = "devbox"
        os.environ["VIBE_REMOTE_CONFIG"] = "/tmp/x/remote.json"
        try:
            cfg = Config.load(self.ws)
            self.assertEqual(cfg.remote_default, "devbox")
            self.assertEqual(cfg.remote_config, "/tmp/x/remote.json")
        finally:
            os.environ.pop("VIBE_REMOTE_DEFAULT", None)
            os.environ.pop("VIBE_REMOTE_CONFIG", None)


class InternalHelpersTest(unittest.TestCase):
    def test_safe_segment_neutralises_traversal(self) -> None:
        """连接名/会话 ID 会被拼进本地路径，这里必须挡住穿越。"""
        self.assertNotIn("/", _safe_segment("../../etc"))
        self.assertNotIn("..", _safe_segment("../../etc"))
        self.assertEqual(_safe_segment(""), "unnamed")
        self.assertLessEqual(len(_safe_segment("a" * 200)), 64)

    def test_outside_remote_paths_uses_remote_root(self) -> None:
        conn = make_conn(root="/srv/app")
        self.assertEqual(_outside_remote_paths(["git", "status"], conn), [])
        self.assertEqual(
            _outside_remote_paths(["pytest", "-q", "tests/"], conn), []
        )
        # 远端路径按远端 root 判定，而不是本地工作区
        self.assertEqual(
            _outside_remote_paths(["cat", "/etc/passwd"], conn), ["/etc/passwd"]
        )
        self.assertEqual(_outside_remote_paths(["cat", "/srv/app/README"], conn), [])

    def test_write_requires_write_flag(self) -> None:
        from agent_core.tools.remote import _require_write

        with self.assertRaises(ToolError):
            _require_write(make_conn(allow_write=False))
        _require_write(make_conn(allow_write=True))


if __name__ == "__main__":
    unittest.main()
