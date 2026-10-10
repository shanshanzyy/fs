"""密钥托管（E4 定档：内网没有 Vault，以 `file` provider 为准）。

## 为什么这组必须有测试

`secrets.py` 有三种 provider，但过去**没有任何测试**碰过它 ——
连 `env` / `file` 也只在别的测试里被顺带间接用到。定档结论是「内网无 Vault，
用 file + 目录权限」，那么 `file` 分支就必须真的被钉住，否则
「换了密钥不重启不生效」「0644 密钥文件当没事」这类事故没人拦。

`vault` 分支**未验证**（从未对着真实 Vault 跑过），所以这里**只钉配置闸门**
（缺 addr/token 必须在启动前失败），不假装测过协议层。切换条件见类文档
与 `apps/server/README.md` §边界。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vibe_server.__main__ import main
from vibe_server.errors import ConfigError
from vibe_server.gateway import secrets as secrets_mod
from vibe_server.gateway.secrets import (
    EnvSecretStore,
    FileSecretStore,
    VaultSecretStore,
    open_secret_store,
)

from tests.support import TempDirTestCase


class FileSecretStoreTests(TempDirTestCase):
    def _write(self, payload, *, mode: int = 0o600) -> Path:
        path = self.tmp / "secrets.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(path, mode)
        return path

    @staticmethod
    def _force_posix():
        """让权限分支在 Windows 上也被跑到。

        `secrets.py` 里 `os.name == "nt"` 会直接 return，于是「0644 必须启动即失败」
        这条最要紧的语义在 Windows 开发机上永远不执行。把它这**一个模块**看到的
        `os.name` 临时改成 posix，走的就是真实文件的真实 `st_mode`。
        """
        return mock.patch.object(secrets_mod.os, "name", "posix")

    # ---------------------------------------------------------------- 正常读取
    def test_get_and_names(self) -> None:
        path = self._write({"cloud-main": "sk-abcdef1234567890", "local-main": "localkey-1234"})
        store = FileSecretStore(path)

        record = store.get("cloud-main")
        self.assertIsNotNone(record)
        self.assertEqual(record.value, "sk-abcdef1234567890")
        self.assertEqual(record.source, "file")
        # names() 只报名字，永不返回值
        self.assertEqual(store.names(), ["cloud-main", "local-main"])
        self.assertNotIn("sk-abcdef1234567890", json.dumps(store.describe()))

    def test_unknown_name_returns_none(self) -> None:
        store = FileSecretStore(self._write({"a": "1"}))
        self.assertIsNone(store.get("nope"))

    def test_repr_never_leaks_plaintext(self) -> None:
        store = FileSecretStore(self._write({"cloud-main": "sk-abcdef1234567890"}))
        self.assertNotIn("sk-abcdef1234567890", repr(store.get("cloud-main")))

    def test_reload_picks_up_rotation(self) -> None:
        """轮换后不重启服务就要生效 —— 缓存必须能主动失效。"""
        path = self._write({"cloud-main": "old-key-1234567890"})
        store = FileSecretStore(path)
        self.assertEqual(store.get("cloud-main").value, "old-key-1234567890")

        path.write_text(json.dumps({"cloud-main": "new-key-0987654321"}), encoding="utf-8")
        # 未 reload：仍读到旧值（缓存生效，不是每次都读盘）
        self.assertEqual(store.get("cloud-main").value, "old-key-1234567890")

        store.reload()
        self.assertEqual(store.get("cloud-main").value, "new-key-0987654321")

    # ---------------------------------------------------------------- 失败即启动失败
    def test_missing_file_reports_actionable_fix(self) -> None:
        missing = self.tmp / "nope.json"
        with self.assertRaises(ConfigError) as ctx:
            FileSecretStore(missing)
        message = str(ctx.exception)
        self.assertIn("密钥文件不存在", message)
        # doctor 会把这段原文打进「发现 N 个问题」，必须自带修复动作
        self.assertIn("修复", message)

    def test_invalid_json_rejected(self) -> None:
        path = self.tmp / "secrets.json"
        path.write_text("{not json", encoding="utf-8")
        # POSIX 上构造会**先**查权限：不显式收成 0600，umask 给的 0644 会让
        # 这条用例撞上「权限过宽」而不是它真正要测的 JSON 解析。
        os.chmod(path, 0o600)
        store = FileSecretStore(path)
        # 解析是**惰性**的：构造只查存在性与权限，内容在首次读取时才解析。
        # doctor 走 names()，所以这条路径同样会在自检里被拦住。
        with self.assertRaises(ConfigError) as ctx:
            store.names()
        self.assertIn("不是合法 JSON", str(ctx.exception))

    def test_top_level_must_be_object(self) -> None:
        store = FileSecretStore(self._write(["a", "b"]))
        with self.assertRaises(ConfigError) as ctx:
            store.names()
        self.assertIn("顶层必须是对象", str(ctx.exception))

    def test_values_must_be_non_empty_string(self) -> None:
        with self.assertRaises(ConfigError):
            FileSecretStore(self._write({"a": "1", "b": ""})).names()
        with self.assertRaises(ConfigError):
            FileSecretStore(self._write({"a": 3})).names()

    def test_oversized_file_rejected(self) -> None:
        path = self.tmp / "secrets.json"
        path.write_text(json.dumps({"a": "x" * (secrets_mod.MAX_SECRET_BYTES + 1)}), encoding="utf-8")
        os.chmod(path, 0o600)  # 同 test_invalid_json_rejected：先过权限关，才轮到体积关
        store = FileSecretStore(path)
        with self.assertRaises(ConfigError) as ctx:
            store.names()
        self.assertIn("过大", str(ctx.exception))

    # ---------------------------------------------------------------- 权限
    def test_wide_permissions_are_rejected(self) -> None:
        # 构造时先放行（allow_insecure），再单独把检查那一步拉出来跑 ——
        # 否则在 POSIX 上构造本身就会抛，测试反而只在 Windows 上成立。
        store = FileSecretStore(self._write({"a": "1"}, mode=0o644), allow_insecure=True)
        with self._force_posix(), self.assertRaises(ConfigError) as ctx:
            store._check_permissions(allow_insecure=False)
        self.assertIn("权限过宽", str(ctx.exception))
        self.assertIn("chmod 600", str(ctx.exception))

    def test_insecure_flag_opts_out_of_permission_check(self) -> None:
        store = FileSecretStore(self._write({"a": "1"}, mode=0o644), allow_insecure=True)
        with self._force_posix():
            store._check_permissions(allow_insecure=True)  # 不应抛
        self.assertEqual(store.get("a").value, "1")


class EnvSecretStoreTests(unittest.TestCase):
    def test_reads_prefixed_env_and_lists_names(self) -> None:
        store = EnvSecretStore(env={"VIBE_SRV_SECRET_CLOUD_MAIN": "sk-1", "PATH": "/x"})
        self.assertEqual(store.get("cloud-main").value, "sk-1")
        # `cloud-main` 与 `cloud_main` 等价
        self.assertEqual(store.get("cloud_main").value, "sk-1")
        self.assertEqual(store.names(), ["cloud_main"])
        self.assertIsNone(store.get("missing"))


class FactoryTests(unittest.TestCase):
    def test_env_is_the_default(self) -> None:
        self.assertIsInstance(open_secret_store(SimpleNamespace()), EnvSecretStore)

    def test_file_without_path_reports_fix(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            open_secret_store(SimpleNamespace(secrets_provider="file", secrets_file=None))
        message = str(ctx.exception)
        self.assertIn("SECRETS_FILE", message)
        self.assertIn("修复", message)

    def test_unknown_provider_lists_choices(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            open_secret_store(SimpleNamespace(secrets_provider="magic"))
        message = str(ctx.exception)
        self.assertIn("未知的密钥托管方式", message)
        self.assertIn("env / file / vault", message)


class VaultConfigGateTests(unittest.TestCase):
    """vault 分支**未验证**（E4 定档）。

    这里刻意只测「缺配置要在启动前失败」—— 那是当前唯一被真跑过的部分。
    协议层（KV v2 路径、字段取值、LIST 降级）从未对着真实 Vault 跑过，
    所以**不写**假装通过的测试；切换条件：内网接入 Vault 时补假 Vault 测试。
    """

    def test_requires_addr_and_token(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            VaultSecretStore(addr="", token="t")
        self.assertIn("VAULT_ADDR", str(ctx.exception))

        with self.assertRaises(ConfigError) as ctx:
            VaultSecretStore(addr="http://vault:8200", token="")
        self.assertIn("VAULT_TOKEN", str(ctx.exception))

    def test_factory_requires_addr(self) -> None:
        with self.assertRaises(ConfigError):
            open_secret_store(
                SimpleNamespace(
                    secrets_provider="vault", vault_addr="", vault_token="", vault_mount="secret"
                )
            )


class DoctorSecretTests(TempDirTestCase):
    """`doctor` 对密钥托管的缺失配置必须 FAIL + 一行可执行建议。"""

    def _run_doctor(self, **env: str) -> tuple[int, str]:
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False):
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                code = main(
                    [
                        "--data-dir", str(self.tmp / "data"),
                        "--db", str(self.tmp / "gov.db"),
                        "doctor",
                    ]
                )
        return code, buf.getvalue()

    @staticmethod
    def _line(output: str, keyword: str) -> str:
        return next((line for line in output.splitlines() if keyword in line), "")

    def test_missing_secret_file_fails_with_hint(self) -> None:
        code, output = self._run_doctor(
            VIBE_SRV_SECRETS_PROVIDER="file",
            VIBE_SRV_SECRETS_FILE=str(self.tmp / "nope.json"),
        )
        self.assertEqual(code, 1, output)
        self.assertTrue(self._line(output, "密钥托管").startswith("  [FAIL]"), output)
        self.assertIn("密钥文件不存在", output)
        self.assertIn("修复", output)

    def test_valid_secret_file_reports_count_without_values(self) -> None:
        path = self.tmp / "secrets.json"
        path.write_text(
            json.dumps({"cloud-main": "sk-abcdef1234567890", "local-main": "localkey-1234"}),
            encoding="utf-8",
        )
        # doctor 自检会读真实文件权限：POSIX 上必须收成 0600，否则会被判「权限过宽」。
        os.chmod(path, 0o600)
        # 只关心密钥托管这一项；全局策略没发布会让 doctor 整体退出 1，故不断言退出码。
        _, output = self._run_doctor(
            VIBE_SRV_SECRETS_PROVIDER="file",
            VIBE_SRV_SECRETS_FILE=str(path),
        )
        self.assertTrue(self._line(output, "密钥托管").startswith("  [OK ]"), output)
        self.assertIn("已知密钥 2 个", output)
        # 台账只报数量，不得把明文打进自检输出
        self.assertNotIn("sk-abcdef1234567890", output)


if __name__ == "__main__":
    unittest.main()