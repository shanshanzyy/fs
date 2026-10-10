"""SQLite → Postgres 迁移（`store/migrate.py` + `scripts/migrate_sqlite_to_pg.py`）。

本机没有真实 Postgres，所以这里用一个**内存假目标**实现 `migrate.Target` 协议，
把三件与「有没有 PG」无关、但必须做对的**逻辑**钉死：

  · 计数对拍：源/目标逐表行数一致（`dry_run` 只统计源、不写目标）
  · 审计表**整表顺序搬**：`audit_events` 按原插入序（`ORDER BY id`）搬
  · 可重跑幂等：同一目标跑第二遍，`inserted` 全为 0，行数不变

真实 PG 上的写入路径（`PostgresTarget`）在 `test_store_contract.py` 的 PG 支
（`VIBE_SRV_TEST_PG_DSN`）里覆盖。
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

from tests.support import TempDirTestCase  # noqa: F401 - 触发 sys.path 引导

from vibe_server.store.migrate import TABLES, migrate, source_counts

_SPEC = {spec.name: spec for spec in TABLES}
_CLI_PATH = Path(__file__).resolve().parents[1] / "scripts" / "migrate_sqlite_to_pg.py"


class _FakeTarget:
    """内存目标：按 `spec.key` 去重，模拟 `ON CONFLICT DO NOTHING` 的幂等。"""

    def __init__(self) -> None:
        self.rows: dict[str, list[tuple]] = {spec.name: [] for spec in TABLES}
        self._seen: dict[str, set[tuple]] = defaultdict(set)
        self.schema_ready = False

    def ensure_schema(self) -> None:
        self.schema_ready = True

    def count(self, table: str) -> int:
        return len(self.rows[table])

    def insert(self, spec, rows):  # noqa: ANN001
        added = 0
        for row in rows:
            key = tuple(row[spec.columns.index(col)] for col in spec.key)
            if key in self._seen[spec.name]:
                continue
            self._seen[spec.name].add(key)
            self.rows[spec.name].append(row)
            added += 1
        return added

    def close(self) -> None:
        pass


def _seed(path: Path) -> None:
    """造一个有内容的源库，覆盖所有会被搬的表。"""
    from vibe_server.store.sqlite import SQLiteStore

    store = SQLiteStore(path)
    store.init_schema()
    try:
        store.bootstrap_first_user(
            "u1", "owner@x.example", "Owner", "h",
            membership_id="m1", role="owner", team_id="t1",
            team_slug="default", team_name="Default", now=100,
        )
        store.create_project("p1", "t1", "proj", "git@x/y.git", now=101)
        store.grant_project("g1", "p1", "u1", "reader", now=102)
        store.create_device("d1", "u1", "laptop", "windows", "fp", now=103)
        store.create_refresh_token("r1", "d1", "u1", "fam1", "th1", 103, 9999)
        store.create_enrollment(
            "e1", "eh1", team_id=None, role="developer",
            created_by="u1", now=103, expires_at=9999, max_uses=3,
        )
        store.consume_enrollment("e1", 104)
        store.revoke_access_jti("jti1", expires_at=8888, now=104)
        store.upsert_quota_policy(
            "q1", "team", "t1", daily_cloud_tokens=1000, daily_requests=10,
            requests_per_minute=5, updated_by="u1", now=105,
        )
        store.upsert_budget_policy(
            "b1", "global", "", budget=500, unit_price=2, window=30,
            updated_by="u1", now=105,
        )
        store.settle_usage("2030-01-01", "u1", "t1", estimate=0, actual_tokens=42,
                           is_cloud=True, counted_request=True, now=106)
        store.hit_rate_limit("rk1", 1_000, 10)
        store.insert_audit_events([
            {
                "device_id": "d1", "user_id": "u1", "team_id": "t1", "session_id": "s1",
                "seq": seq, "event_type": "ToolCall", "ts": 200 + seq,
                "payload": {"seq": seq}, "prev_hash": "0" * 64, "hash": f"h{seq}",
                "batch_id": "batch1", "received_at": 200 + seq,
            }
            for seq in (1, 2, 3)
        ])
        store.insert_audit_batch({
            "id": "batch1", "device_id": "d1", "user_id": "u1", "team_id": "t1",
            "first_seq": 1, "last_seq": 3, "event_count": 3,
            "client_chain_head": "c", "prev_chain_head": "0" * 64, "chain_head": "head1",
            "sequence_gap": 0, "received_at": 210,
        })
        store.record_auth_event("login", user_id="u1", detail="ok", remote="1.1.1.1", now=107)
        store.insert_policy("pol1", "global", "", 1, '{"x":1}', created_by="u1",
                            note="n", now=108)
        store.upsert_secret_meta("sk1", "env", "REF", "fp", note="n", now=109)
    finally:
        store.close()


class MigrationTests(TempDirTestCase):
    def _db(self) -> Path:
        # 只建一次：同一测试里多次取用不能再往同一个库里重复插（会撞唯一键）
        if not hasattr(self, "_db_path"):
            path = self.tmp / "governance.db"
            _seed(path)
            self._db_path = path
        return self._db_path

    # ------------------------------------------------------------ 计数对拍
    def test_dry_run_counts_source_only(self) -> None:
        target = _FakeTarget()
        report = migrate(self._db(), target, dry_run=True)
        self.assertTrue(report.dry_run)
        self.assertTrue(target.schema_ready)
        self.assertEqual(report.inserted, {})                       # dry-run 不写
        self.assertEqual(target.count("users"), 0)
        self.assertEqual(report.source["users"], 1)
        self.assertEqual(report.source["audit_events"], 3)
        self.assertEqual(report.source["audit_batches"], 1)
        self.assertEqual(report.source["usage_counters"], 1)

    def test_migrate_round_trip_counts_match(self) -> None:
        target = _FakeTarget()
        report = migrate(self._db(), target)
        self.assertFalse(report.dry_run)
        self.assertTrue(report.ok, report.mismatches())
        for spec in TABLES:
            self.assertEqual(report.source[spec.name], report.target[spec.name], spec.name)
            self.assertEqual(report.source[spec.name], report.inserted[spec.name], spec.name)

    def test_rerun_is_idempotent(self) -> None:
        target = _FakeTarget()
        migrate(self._db(), target)
        again = migrate(self._db(), target)
        self.assertTrue(again.ok)
        self.assertTrue(all(n == 0 for n in again.inserted.values()), again.inserted)

    def test_audit_events_migrated_in_insertion_order(self) -> None:
        target = _FakeTarget()
        migrate(self._db(), target)
        spec = _SPEC["audit_events"]
        seq_at = spec.columns.index("seq")
        seqs = [row[seq_at] for row in target.rows["audit_events"]]
        self.assertEqual(seqs, [1, 2, 3])                           # 顺序搬，不并发分批

    def test_source_counts_missing_table_is_zero(self) -> None:
        empty = self.tmp / "empty.db"
        sqlite3.connect(str(empty)).close()
        counts = source_counts(empty)
        self.assertTrue(all(n == 0 for n in counts.values()))
        self.assertEqual(set(counts), {spec.name for spec in TABLES})

    # ------------------------------------------------------------ CLI 薄壳
    def test_cli_requires_existing_source(self) -> None:
        cli = _load_cli()
        with patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                     if k != "VIBE_SRV_DB"}, clear=True):
            code = cli.main([str(self.tmp / "nope.db"), "--dsn", "postgres://x/y"])
        self.assertEqual(code, 1)

    def test_cli_requires_dsn(self) -> None:
        cli = _load_cli()
        with patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                     if k != "VIBE_SRV_DB"}, clear=True):
            code = cli.main([str(self._db())])
        self.assertEqual(code, 1)


def _load_cli():
    spec = importlib.util.spec_from_file_location("migrate_sqlite_to_pg", _CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


if __name__ == "__main__":
    unittest.main()