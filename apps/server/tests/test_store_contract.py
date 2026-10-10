"""`Store` 契约测试：同一批用例在 SQLite 与 Postgres 上跑同一套断言。

**为什么值得单独抽一份**：换存储驱动这件事，唯一可信的证据是「同一份契约、
两个后端、逐条通过」。只测 SQLite 只能证明 SQLite 对；只测 PG 只能证明 PG 对；
两边跑同一套才证明「换库不改语义」。

本机默认没有真实 Postgres，所以 PG 那一支在 `VIBE_SRV_TEST_PG_DSN` 缺席时
`skip`（**环境相关 skip**，与 ConPTY / 符号链接同类）。SQLite 一支全量跑、
完全 hermetic。

PG 环境要求：`VIBE_SRV_TEST_PG_DSN` 指向一个**可清空的空测试库** —— 每个用例
前会 `TRUNCATE` 全部表，以便 `bootstrap_first_user` 这类「依赖空表」的断言成立。

钉住的语义（对应 M4+-PLAN §C2/C3/C4）：
  · 原子预留不超发 / 请求数上限 / 结算 / 释放
  · bootstrap 单语句原子 + 幂等
  · 注册令牌占用额度原子
  · 固定窗口限流
  · 审计事件幂等、链头/会话尾哈希、批次链顺序
"""

from __future__ import annotations

import os
import threading
import unittest
import uuid

from tests.support import TempDirTestCase  # noqa: F401 - 触发 sys.path 引导

from vibe_server.errors import StoreError
from vibe_server.hashing import GENESIS, link
from vibe_server.store.migrate import TABLES
from vibe_server.store.pgproto import PGPool
from vibe_server.store.pg import ensure_schema

_PG_DSN = os.environ.get("VIBE_SRV_TEST_PG_DSN", "").strip()
_DAY = "2030-01-01"


def _uid() -> str:
    return uuid.uuid4().hex


def _payload(seq: int) -> dict:
    return {"seq": seq, "note": "契约测试"}


def _audit_rows(
    device: str,
    session: str,
    count: int,
    *,
    team: str = "",
    user: str = "",
    batch: str = "",
    start_seq: int = 1,
    prev: str = GENESIS,
    base_ts: int = 1_000,
) -> list[dict]:
    """造一批链式审计事件（prev_hash/hash 按 hashing.link 逐条推进）。"""
    rows: list[dict] = []
    for i in range(count):
        seq = start_seq + i
        payload = _payload(seq)
        _, digest = link(prev, payload)
        rows.append(
            {
                "device_id": device,
                "user_id": user,
                "team_id": team,
                "session_id": session,
                "seq": seq,
                "event_type": "ToolCall",
                "ts": base_ts + seq,
                "payload": payload,
                "prev_hash": prev,
                "hash": digest,
                "batch_id": batch,
                "received_at": base_ts + seq,
            }
        )
        prev = digest
    return rows


class StoreContractTests:
    """契约本体（普通 mixin，不是 TestCase —— 免得基类被当成可执行用例收集）。"""

    def make_store(self):
        raise NotImplementedError

    def setUp(self) -> None:
        super().setUp()          # 让 TempDirTestCase（若在 MRO 里）先建临时目录
        self.store = self.make_store()
        self.store.init_schema()
        self.addCleanup(self._close_store)

    def _close_store(self) -> None:
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 用户
    def test_user_lifecycle(self) -> None:
        uid = _uid()
        row = self.store.create_user(
            uid, "Owner@Corp.Example", "Owner", "hash-1", external_id="sub-1", now=10
        )
        self.assertEqual(row["id"], uid)
        self.assertEqual(row["email"], "owner@corp.example")     # 收敛为小写
        self.assertEqual(row["status"], "active")
        self.assertIsNotNone(self.store.get_user(uid))
        self.assertIsNotNone(self.store.find_user_by_email("OWNER@corp.example"))
        self.assertIsNotNone(self.store.find_user_by_external_id("sub-1"))
        self.assertIsNone(self.store.get_user("nope"))

        self.store.update_user(uid, now=20, display_name="Boss")
        self.assertEqual(self.store.get_user(uid)["display_name"], "Boss")
        self.assertEqual(self.store.get_user(uid)["updated_at"], 20)

        with self.assertRaises(StoreError):
            self.store.update_user(uid, now=20, evil_column="x")

        ids = [u["id"] for u in self.store.list_users(limit=1000)]
        self.assertIn(uid, ids)

    # ------------------------------------------------------------ 团队 / 成员
    def test_team_and_membership(self) -> None:
        uid, tid = _uid(), _uid()
        slug = "team-" + _uid()[:8]
        self.store.create_user(uid, f"{uid}@x.example", "U", "h", now=1)
        team = self.store.create_team(tid, slug.upper(), "Team A", now=1)
        self.assertEqual(team["slug"], slug)                     # 收敛为小写
        self.assertEqual(self.store.get_team(tid)["id"], tid)
        self.assertEqual(self.store.find_team_by_slug(slug)["id"], tid)
        self.assertIn(tid, [t["id"] for t in self.store.list_teams()])

        self.store.add_membership(_uid(), uid, tid, "developer", now=2)
        self.assertEqual(self.store.get_membership(uid, tid)["role"], "developer")
        memberships = self.store.list_memberships(uid)
        self.assertEqual(memberships[0]["team_slug"], slug)      # 带出 team_slug
        self.assertEqual(memberships[0]["team_name"], "Team A")
        members = self.store.list_team_members(tid)
        self.assertEqual(members[0]["email"], f"{uid}@x.example")

        self.store.set_membership_role(uid, tid, "team_admin")
        self.assertEqual(self.store.get_membership(uid, tid)["role"], "team_admin")

        with self.assertRaises(StoreError):
            self.store.set_membership_role(uid, tid, "wizard")

        self.assertTrue(self.store.remove_membership(uid, tid))
        self.assertFalse(self.store.remove_membership(uid, tid))

    # ------------------------------------------------------------ bootstrap
    def test_bootstrap_first_user_is_atomic_and_idempotent(self) -> None:
        uid, tid = _uid(), _uid()
        slug = "org-" + _uid()[:8]
        first = self.store.bootstrap_first_user(
            uid, "owner@x.example", "Owner", "h",
            membership_id=_uid(), role="owner", team_id=tid, team_slug=slug,
            team_name="Default", now=1,
        )
        self.assertTrue(first)
        second = self.store.bootstrap_first_user(
            _uid(), "second@x.example", "Second", "h",
            membership_id=_uid(), role="owner", team_id=_uid(), team_slug=slug,
            team_name="Default", now=2,
        )
        self.assertFalse(second)                                 # 已有用户 → 不写
        self.assertEqual(len(self.store.list_users(limit=1000)), 1)
        # 默认团队按 slug 复用，而不是造第二个
        self.assertEqual(self.store.get_membership(uid, tid)["team_id"], tid)
        self.assertEqual(self.store.find_team_by_slug(slug)["id"], tid)

    # ------------------------------------------------------------ 项目
    def test_project_and_grants(self) -> None:
        uid, tid, pid = _uid(), _uid(), _uid()
        self.store.create_user(uid, f"{uid}@x.example", "U", "h", now=1)
        self.store.create_team(tid, "t-" + _uid()[:8], "T", now=1)
        self.store.create_project(pid, tid, "proj", "git@x/y.git", now=1)
        self.assertEqual(self.store.get_project(pid)["name"], "proj")
        self.assertIn(pid, [p["id"] for p in self.store.list_projects(tid)])

        self.store.grant_project(_uid(), pid, uid, "reader", now=2)
        self.assertEqual(self.store.find_project_grant(pid, uid)["role"], "reader")
        grants = self.store.list_project_grants(pid)
        self.assertEqual(grants[0]["email"], f"{uid}@x.example")

    # ------------------------------------------------------------ 设备 / 刷新令牌
    def test_devices_and_refresh_tokens(self) -> None:
        uid, did = _uid(), _uid()
        self.store.create_user(uid, f"{uid}@x.example", "U", "h", now=1)
        self.store.create_device(did, uid, "laptop", "windows", "fp", now=1)
        self.assertEqual(self.store.get_device(did)["name"], "laptop")
        self.assertIn(did, [d["id"] for d in self.store.list_devices(uid)])

        self.store.touch_device(did, now=5)
        self.assertEqual(self.store.get_device(did)["last_seen_at"], 5)

        rid, fam = _uid(), _uid()
        self.store.create_refresh_token(rid, did, uid, fam, "th-" + rid, 1, 999, client_ip="1.2.3.4")
        found = self.store.find_refresh_by_hash("th-" + rid)
        self.assertEqual(found["id"], rid)
        self.store.mark_refresh_used(rid, 6, "next")
        self.assertEqual(self.store.find_refresh_by_hash("th-" + rid)["replaced_by"], "next")

        # 吊销设备要连带吊销它名下的刷新令牌
        self.assertTrue(self.store.revoke_device(did, now=7))
        self.assertFalse(self.store.revoke_device(did, now=8))
        self.assertIsNotNone(self.store.get_device(did)["revoked_at"])
        self.assertIsNotNone(self.store.find_refresh_by_hash("th-" + rid)["revoked_at"])

    # ------------------------------------------------------------ 注册令牌
    def test_consume_enrollment_is_atomic(self) -> None:
        eid = _uid()
        self.store.create_enrollment(
            eid, "eh-" + eid, team_id=None, role="developer",
            created_by="", now=1, expires_at=100, max_uses=2,
        )
        self.assertTrue(self.store.consume_enrollment(eid, 2))
        self.assertTrue(self.store.consume_enrollment(eid, 3))
        self.assertFalse(self.store.consume_enrollment(eid, 4))   # 用尽
        self.assertEqual(self.store.find_enrollment_by_hash("eh-" + eid)["uses"], 2)

        expired = _uid()
        self.store.create_enrollment(
            expired, "eh-" + expired, team_id=None, role="developer",
            created_by="", now=1, expires_at=10, max_uses=1,
        )
        self.assertFalse(self.store.consume_enrollment(expired, 11))  # 已过期

    # ------------------------------------------------------------ 配额
    def test_reserve_usage_never_overcommits(self) -> None:
        uid = _uid()
        self.assertTrue(self.store.reserve_usage(
            _DAY, uid, "", estimate=60, token_limit=100, request_limit=0, now=1))
        self.assertFalse(self.store.reserve_usage(              # 60+50 > 100
            _DAY, uid, "", estimate=50, token_limit=100, request_limit=0, now=1))
        self.assertTrue(self.store.reserve_usage(               # 60+40 == 100 恰好
            _DAY, uid, "", estimate=40, token_limit=100, request_limit=0, now=1))
        self.assertFalse(self.store.reserve_usage(
            _DAY, uid, "", estimate=1, token_limit=100, request_limit=0, now=1))
        self.assertEqual(self.store.get_usage(_DAY, uid)["reserved_tokens"], 100)

    def test_reserve_usage_is_atomic_under_concurrency(self) -> None:
        """并发抢占同一配额：判定与写入若拆成两条语句，就会超发。

        这是「判定 + 写入同一条 UPDATE」的**唯一可信证据** —— 上面的单线程用例
        无论怎么写都测不出原子性（先 SELECT 判、再 UPDATE 写的实现在单线程下
        表现完全一样）。反向改动会让成功数超过上限。
        """
        day, uid = "2032-03-01", _uid()
        limit, estimate, workers = 100, 10, 24
        start = threading.Barrier(workers)
        results: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            start.wait()                     # 尽量让所有线程同时冲进判定
            ok = self.store.reserve_usage(
                day, uid, "", estimate=estimate, token_limit=limit,
                request_limit=0, now=1)
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(results), limit // estimate)        # 恰好 10 个成功
        self.assertEqual(self.store.get_usage(day, uid)["reserved_tokens"], limit)

    def test_reserve_usage_respects_request_limit(self) -> None:
        uid = _uid()
        # 不限 token，只限请求数 1
        self.assertTrue(self.store.reserve_usage(
            _DAY, uid, "", estimate=10, token_limit=0, request_limit=1, now=1))
        self.store.settle_usage(
            _DAY, uid, "", estimate=10, actual_tokens=10, is_cloud=True,
            counted_request=True, now=2)
        self.assertFalse(self.store.reserve_usage(              # requests(1) 已达上限
            _DAY, uid, "", estimate=10, token_limit=0, request_limit=1, now=3))

    def test_settle_and_release(self) -> None:
        uid = _uid()
        self.store.reserve_usage(_DAY, uid, "", estimate=100, token_limit=1000,
                                 request_limit=0, now=1)
        self.store.settle_usage(_DAY, uid, "", estimate=100, actual_tokens=80,
                                is_cloud=True, counted_request=True, now=2)
        usage = self.store.get_usage(_DAY, uid)
        self.assertEqual(usage["reserved_tokens"], 0)
        self.assertEqual(usage["cloud_tokens"], 80)
        self.assertEqual(usage["requests"], 1)

        # 本地调用入 local_tokens；释放把预留退回
        self.store.reserve_usage(_DAY, uid, "", estimate=50, token_limit=1000,
                                 request_limit=0, now=3)
        self.store.settle_usage(_DAY, uid, "", estimate=50, actual_tokens=30,
                                is_cloud=False, counted_request=False, now=4)
        usage = self.store.get_usage(_DAY, uid)
        self.assertEqual(usage["local_tokens"], 30)
        self.assertEqual(usage["reserved_tokens"], 0)

        self.store.reserve_usage(_DAY, uid, "", estimate=40, token_limit=1000,
                                 request_limit=0, now=5)
        self.store.release_usage(_DAY, uid, 40, now=6)
        self.assertEqual(self.store.get_usage(_DAY, uid)["reserved_tokens"], 0)

    def test_get_usage_defaults_to_zero(self) -> None:
        usage = self.store.get_usage(_DAY, _uid())
        self.assertEqual(usage["cloud_tokens"], 0)
        self.assertEqual(usage["reserved_tokens"], 0)

    def test_list_usage_and_series(self) -> None:
        team, u1, u2 = _uid(), _uid(), _uid()
        day_a, day_b = "2031-02-01", "2031-02-02"
        for day, uid, tokens in ((day_a, u1, 10), (day_a, u2, 20), (day_b, u1, 30)):
            self.store.settle_usage(day, uid, team, estimate=0, actual_tokens=tokens,
                                    is_cloud=True, counted_request=True, now=1)
        same_team = self.store.list_usage(day_a, team_id=team)
        self.assertEqual(len(same_team), 2)
        series = self.store.usage_series(user_id=u1, since_day=day_a, limit=10)
        self.assertEqual([r["day"] for r in series], [day_b, day_a])   # day DESC

    # ------------------------------------------------------------ 限流
    def test_rate_limit_fixed_window(self) -> None:
        key = "k-" + _uid()[:8]
        allowed, count = self.store.hit_rate_limit(key, 1_000, 2)
        self.assertEqual((allowed, count), (True, 1))
        self.assertEqual(self.store.hit_rate_limit(key, 1_000, 2), (True, 2))
        self.assertEqual(self.store.hit_rate_limit(key, 1_000, 2), (False, 3))
        # 换窗口 → 重新计数
        self.assertEqual(self.store.hit_rate_limit(key, 2_000, 2), (True, 1))
        # limit<=0 表示不限
        self.assertEqual(self.store.hit_rate_limit(key, 3_000, 0), (True, 0))

    # ------------------------------------------------------------ 配额策略 / 预算
    def test_quota_policy_upsert(self) -> None:
        scope = _uid()[:8]
        self.store.upsert_quota_policy(
            _uid(), "team", scope, daily_cloud_tokens=100, daily_requests=5,
            requests_per_minute=2, updated_by="a", now=1)
        row = self.store.get_quota_policy("team", scope)
        self.assertEqual(row["daily_cloud_tokens"], 100)
        self.store.upsert_quota_policy(
            _uid(), "team", scope, daily_cloud_tokens=200, daily_requests=6,
            requests_per_minute=3, updated_by="b", now=2)
        row = self.store.get_quota_policy("team", scope)
        self.assertEqual(row["daily_cloud_tokens"], 200)
        self.assertEqual(row["updated_by"], "b")
        self.assertIn(scope, [r["scope_id"] for r in self.store.list_quota_policies()])

    def test_budget_policy_upsert(self) -> None:
        scope = _uid()[:8]
        self.store.upsert_budget_policy(
            _uid(), "global", scope, budget=1000, unit_price=2, window=30,
            updated_by="a", now=1)
        row = self.store.get_budget_policy("global", scope)
        self.assertEqual((row["budget"], row["unit_price"], row["window"]), (1000, 2, 30))
        self.store.upsert_budget_policy(
            _uid(), "global", scope, budget=500, unit_price=1, window=7,
            updated_by="b", now=2)
        row = self.store.get_budget_policy("global", scope)
        self.assertEqual((row["budget"], row["unit_price"], row["window"]), (500, 1, 7))

    # ------------------------------------------------------------ 审计
    def test_audit_insert_is_idempotent(self) -> None:
        device, session, team = _uid(), _uid(), _uid()
        rows = _audit_rows(device, session, 3, team=team)
        self.assertEqual(self.store.insert_audit_events(rows), 3)
        self.assertEqual(self.store.insert_audit_events(rows), 0)   # 重传天然幂等
        self.assertEqual(self.store.count_audit_events(team_id=team), 3)

        events = self.store.query_audit_events(team_id=team, limit=10)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["event_type"], "ToolCall")
        self.assertIsInstance(events[0]["payload"], dict)           # payload 还原成对象

    def test_audit_chain_helpers(self) -> None:
        device, session = _uid(), _uid()
        self.assertEqual(self.store.get_session_last_hash(device, session), GENESIS)
        self.assertEqual(self.store.get_device_last_seq(device, session), 0)
        self.assertEqual(self.store.get_batch_chain_head(device), GENESIS)

        rows = _audit_rows(device, session, 3)
        self.store.insert_audit_events(rows)
        self.assertEqual(self.store.get_session_last_hash(device, session), rows[-1]["hash"])
        self.assertEqual(self.store.get_device_last_seq(device, session), 3)

        # 第二批次必须从上一批的链头续起
        tail = self.store.get_session_last_hash(device, session)
        more = _audit_rows(device, session, 2, start_seq=4, prev=tail)
        self.store.insert_audit_events(more)
        self.assertEqual(self.store.get_session_last_hash(device, session), more[-1]["hash"])
        self.assertEqual(self.store.get_device_last_seq(device, session), 5)

    def test_audit_batch_chain_order(self) -> None:
        device = _uid()
        bid_early, bid_late = _uid(), _uid()
        # 先写 received_at 大的，再写小的 —— server_chain 必须按 received_at 升序返回
        self.store.insert_audit_batch({
            "id": bid_late, "device_id": device, "user_id": "", "team_id": "",
            "first_seq": 4, "last_seq": 5, "event_count": 2,
            "client_chain_head": "c1", "prev_chain_head": "p1", "chain_head": "head-late",
            "sequence_gap": 0, "received_at": 300,
        })
        self.store.insert_audit_batch({
            "id": bid_early, "device_id": device, "user_id": "", "team_id": "",
            "first_seq": 1, "last_seq": 3, "event_count": 3,
            "client_chain_head": "c0", "prev_chain_head": "p0", "chain_head": "head-early",
            "sequence_gap": 0, "received_at": 100,
        })
        # 批次幂等
        self.store.insert_audit_batch({
            "id": bid_late, "device_id": device, "user_id": "", "team_id": "",
            "first_seq": 4, "last_seq": 5, "event_count": 2,
            "client_chain_head": "c1", "prev_chain_head": "p1", "chain_head": "head-late",
            "sequence_gap": 0, "received_at": 300,
        })

        chain = self.store.server_chain(device)
        self.assertEqual([r["id"] for r in chain], [bid_early, bid_late])
        self.assertEqual(self.store.get_batch_chain_head(device), "head-late")  # 最新批

    def test_audit_summary(self) -> None:
        team = _uid()
        rows = _audit_rows(_uid(), _uid(), 2, team=team)
        rows[1]["event_type"] = "FileRead"
        self.store.insert_audit_events(rows)
        summary = {r["event_type"]: int(r["n"]) for r in self.store.audit_summary(team_id=team)}
        self.assertEqual(summary, {"ToolCall": 1, "FileRead": 1})

    # ------------------------------------------------------------ 吊销 / 清理
    def test_revoked_jti_and_purge(self) -> None:
        jti = _uid()
        self.assertFalse(self.store.is_access_jti_revoked(jti))
        self.store.revoke_access_jti(jti, expires_at=50, now=1)
        self.assertTrue(self.store.is_access_jti_revoked(jti))
        purged = self.store.purge_expired(100)
        self.assertEqual(purged["revoked_tokens"], 1)
        self.assertFalse(self.store.is_access_jti_revoked(jti))

    # ------------------------------------------------------------ 策略
    def test_policies(self) -> None:
        scope = _uid()[:8]
        self.assertEqual(self.store.next_policy_version("global", scope), 1)
        self.store.insert_policy(_uid(), "global", scope, 1, '{"a":1}',
                                 created_by="x", note="n1", now=1)
        self.assertEqual(self.store.next_policy_version("global", scope), 2)
        self.store.insert_policy(_uid(), "global", scope, 2, '{"a":2}',
                                 created_by="x", note="n2", now=2)
        self.assertEqual(self.store.get_latest_policy("global", scope)["version"], 2)
        versions = self.store.list_policy_versions("global", scope)
        self.assertEqual([v["version"] for v in versions], [2, 1])

    # ------------------------------------------------------------ 密钥台账
    def test_secret_meta(self) -> None:
        name = "key-" + _uid()[:8]
        self.store.upsert_secret_meta(name, "env", "REF", "fp1", note="n", now=1)
        row = self.store.get_secret_meta(name)
        self.assertEqual((row["provider"], row["fingerprint"]), ("env", "fp1"))
        self.store.upsert_secret_meta(name, "file", "REF2", "fp2", note="n2", now=2)
        row = self.store.get_secret_meta(name)
        self.assertEqual((row["provider"], row["fingerprint"]), ("file", "fp2"))
        self.assertIn(name, [r["name"] for r in self.store.list_secret_meta()])

    # ------------------------------------------------------------ 认证事件
    def test_auth_events(self) -> None:
        uid = _uid()
        self.store.record_auth_event("login", user_id=uid, detail="ok", remote="1.1.1.1", now=1)
        self.store.record_auth_event("logout", user_id=uid, now=2)
        mine = self.store.list_auth_events(user_id=uid)
        self.assertEqual({r["kind"] for r in mine}, {"login", "logout"})
        self.assertGreaterEqual(len(self.store.list_auth_events(limit=1000)), 2)


class SQLiteStoreContractTests(StoreContractTests, TempDirTestCase):
    """SQLite 一支：全量跑、hermetic（临时文件）。"""

    def make_store(self):
        from vibe_server.store.sqlite import SQLiteStore

        return SQLiteStore(self.tmp / "contract.db")


@unittest.skipUnless(_PG_DSN, "需要 VIBE_SRV_TEST_PG_DSN 指向可清空的测试库")
class PostgresStoreContractTests(StoreContractTests, unittest.TestCase):
    """Postgres 一支：同一批断言。缺席 DSN 时整类 skip（环境相关）。"""

    def make_store(self):
        from vibe_server.store.pg import PostgresStore

        # 每例前清空：bootstrap 这类断言依赖「空表」
        pool = PGPool(_PG_DSN)
        try:
            ensure_schema(pool)
            pool.run_simple(
                "TRUNCATE " + ", ".join(spec.name for spec in TABLES)
                + " RESTART IDENTITY CASCADE"
            )
        finally:
            pool.close()
        return PostgresStore(_PG_DSN)


if __name__ == "__main__":
    unittest.main()