"""多实例契约与开关（`config.py` 的 A2/A1/D1/D2 + `__main__` 的 D3）。

这里钉的是「**默认路径逐字节不变**、新校验只在多实例意图下生效」这条边界：
单机（不设开关、不指共享库）时，仍是 SQLite、仍自动生成 jwt.key、仍不要求 TLS。
多实例意图下，三条硬门槛（统一密钥 / 共享存储 / TLS+来源 IP）缺一即拒绝启动。

环境变量用「清空所有 VIBE_SRV_* 再设本次要用的」方式注入，避免上一个测试或
开发机环境串味。
"""

from __future__ import annotations

import io
import os
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tests.support import TempDirTestCase  # noqa: F401 - 触发 sys.path 引导

from vibe_server.config import ServerConfig
from vibe_server.errors import ConfigError

_POSTGRES = "postgres://user:pass@pg.internal:5432/vibe?sslmode=require"
_SECRET = "k" * 48


@contextmanager
def _env(**values: str):
    """清掉所有 VIBE_SRV_* 后注入指定变量（其余环境保留）。"""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("VIBE_SRV_")}
    clean.update(values)
    with patch.dict(os.environ, clean, clear=True):
        yield


class MultiInstanceConfigTests(TempDirTestCase):
    def _load(self, **overrides):
        base = {
            "data_dir": self.tmp / "data",
            "db_path": _POSTGRES,
            "require_tls": True,
            "trust_forwarded_for": True,
        }
        base.update(overrides)
        return ServerConfig.load(**base)

    # ------------------------------------------------------------ 意图判定
    def test_shared_url_implies_multi_instance(self) -> None:
        with _env(VIBE_SRV_JWT_SECRET=_SECRET):
            cfg = self._load()
        self.assertTrue(cfg.multi_instance_intent())
        self.assertEqual(cfg.store_target(), _POSTGRES)
        self.assertTrue(cfg.jwt_secret_from_env)

    def test_flag_alone_marks_intent(self) -> None:
        # multi_instance 为真即视为多实例意图（不经 load，直接构造以单测该方法）
        cfg = ServerConfig(
            db_path=Path(self.tmp) / "x.db", data_dir=Path(self.tmp), multi_instance=True
        )
        self.assertTrue(cfg.multi_instance_intent())
        self.assertEqual(cfg.store_target(), str(Path(self.tmp) / "x.db"))

    # ------------------------------------------------------------ 默认路径不变
    def test_default_single_instance_unchanged(self) -> None:
        with _env():
            cfg = ServerConfig.load(
                data_dir=self.tmp / "data", db_path=self.tmp / "governance.db"
            )
        self.assertFalse(cfg.multi_instance_intent())
        self.assertFalse(cfg.jwt_secret_from_env)
        self.assertEqual(cfg.store_target(), str(self.tmp / "governance.db"))
        # 单机仍自动生成并落盘 jwt.key（行为与改造前一致）
        self.assertTrue((self.tmp / "data" / "jwt.key").exists())

    # ------------------------------------------------------------ 三条硬门槛
    def test_missing_jwt_secret_rejected(self) -> None:
        with _env():
            with self.assertRaises(ConfigError) as ctx:
                self._load()
        self.assertIn("JWT_SECRET", str(ctx.exception))

    def test_short_jwt_secret_rejected(self) -> None:
        with _env(VIBE_SRV_JWT_SECRET="too-short"):
            with self.assertRaises(ConfigError) as ctx:
                self._load()
        self.assertIn("32", str(ctx.exception))

    def test_multi_instance_rejects_sqlite(self) -> None:
        with _env(VIBE_SRV_JWT_SECRET=_SECRET, VIBE_SRV_MULTI_INSTANCE="1"):
            with self.assertRaises(ConfigError) as ctx:
                self._load(db_path=self.tmp / "still-sqlite.db")
        self.assertIn("SQLite", str(ctx.exception))

    def test_multi_instance_requires_tls(self) -> None:
        with _env(VIBE_SRV_JWT_SECRET=_SECRET):
            with self.assertRaises(ConfigError) as ctx:
                self._load(require_tls=False)
        self.assertIn("REQUIRE_TLS", str(ctx.exception))

    def test_multi_instance_requires_forwarded_for(self) -> None:
        with _env(VIBE_SRV_JWT_SECRET=_SECRET):
            with self.assertRaises(ConfigError) as ctx:
                self._load(trust_forwarded_for=False)
        self.assertIn("TRUST_FORWARDED_FOR", str(ctx.exception))

    def test_multi_instance_env_flag_requires_shared_store(self) -> None:
        with _env(VIBE_SRV_JWT_SECRET=_SECRET, VIBE_SRV_MULTI_INSTANCE="1"):
            with self.assertRaises(ConfigError):
                ServerConfig.load(
                    data_dir=self.tmp / "data",
                    db_path=self.tmp / "flag-only.db",
                    require_tls=True,
                    trust_forwarded_for=True,
                )

    # ------------------------------------------------------------ open_store 分派
    def test_open_store_dispatches_postgres(self) -> None:
        """共享 URL 必须真落到 Postgres 驱动，**不能静默退回 SQLite**。

        `connect=False` 只构造不建连：这里钉的是「分派正确」，不是「连得上」。
        静默降级会让「以为在用集群库、实际数据写在本机文件里」这种事故无声发生。
        """
        from vibe_server.store import open_store
        from vibe_server.store.pg import PostgresStore

        store = open_store(_POSTGRES, connect=False)
        try:
            self.assertIsInstance(store, PostgresStore)
        finally:
            store.close()

    def test_open_store_rejects_unknown_scheme(self) -> None:
        from vibe_server.store import open_store

        with self.assertRaises(ConfigError):
            open_store("redis://localhost:6379/0", connect=False)

    # ------------------------------------------------------------ doctor（D3）
    def _doctor(self, argv: list[str]) -> tuple[int, str]:
        from vibe_server.__main__ import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(argv)
        return code, buf.getvalue()

    def test_doctor_single_instance_has_no_multi_line(self) -> None:
        with _env(VIBE_SRV_POLICY_REQUIRED="0"):
            # 全局选项（--data-dir/--db）必须在子命令之前
            code, out = self._doctor(
                ["--data-dir", str(self.tmp / "d1"), "--db", str(self.tmp / "d1.db"), "doctor"]
            )
        self.assertNotIn("多实例", out)
        self.assertEqual(code, 0)

    def test_doctor_reports_multi_instance_when_ready(self) -> None:
        # 共享库指向一个必然连不上的地址：存储项 FAIL，但「多实例」项应判为 OK，
        # 说明三门槛齐备时 doctor 能正确自检（且不会因库不可达而崩溃）。
        with _env(
            VIBE_SRV_MULTI_INSTANCE="1",
            VIBE_SRV_JWT_SECRET=_SECRET,
            VIBE_SRV_REQUIRE_TLS="1",
            VIBE_SRV_TRUST_FORWARDED_FOR="1",
            VIBE_SRV_POLICY_REQUIRED="0",
        ):
            code, out = self._doctor(
                [
                    "--data-dir", str(self.tmp / "d2"),
                    "--db", "postgres://u:p@127.0.0.1:1/vibe",
                    "doctor",
                ]
            )
        self.assertIn("多实例", out)
        self.assertIn("共享存储 + 统一密钥 + TLS/来源 IP 已就绪", out)
        self.assertEqual(code, 1)   # 存储连不上 → 有 problem


if __name__ == "__main__":
    unittest.main()