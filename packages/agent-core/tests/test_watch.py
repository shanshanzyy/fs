"""增量索引 / 文件监听（阶段 J / P1-9）测试。

钉住三件事（计划 §7-J8）：

1. `SearchIndex.update()` 与「全量重建」**等价** —— 同一 query 的命中集合一致。
   这是增量索引唯一可能出错的地方：漏摘旧块、漏删空词桶、漏更新依赖边，
   任何一种都会让「改完代码搜到旧结果」，而那种错比搜不到更误导人。
2. watcher 停得干净 —— `stop()` 之后线程不再存活。本项目对「退出后不留残留」
   有硬要求（冒烟用例里有进程残留检查），线程级同理。
3. watch **关**时与今天完全一样 —— 不起线程、不加行为。
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.audit import AuditLog  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.host.session import Session  # noqa: E402
from agent_core.index import (  # noqa: E402
    IndexWatcher,
    SearchIndex,
    clear_cache,
    get_index,
    peek_index,
)
from agent_core.sandbox import PathGuard  # noqa: E402
from agent_core.tools.symbols import ScanResult  # noqa: E402

# 线程名（与 watch.py 里的一致）。用它判定「线程还在不在」。
_THREAD_NAME = "vibe-index-watch"


def _hits(index: SearchIndex, query: str) -> set[tuple[str, int, int]]:
    """命中集合。刻意用集合而不是有序列表：doc_id 的分配顺序在增量与重建
    之间必然不同，同分时的先后也就不同 —— 那是等价性允许的差异。"""
    return {(hit.path, hit.start, hit.end) for hit in index.search(query, k=100)}


class _Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        clear_cache()

    def tearDown(self) -> None:
        clear_cache()
        self._tmp.cleanup()

    def write(self, rel: str, text: str) -> None:
        path = self.ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")

    def rel_of(self):
        return PathGuard(self.ws).relative


class IncrementalUpdateTests(_Workspace):
    """`update()` 的效果必须与「重新 build 一遍」逐项一致。"""

    def setUp(self) -> None:
        super().setUp()
        self.write("auth.py", "def handle_login(user):\n    '用户登录入口'\n    return True\n")
        self.write("billing.py", "def pay():\n    '支付回调处理'\n    return 0\n")
        self.write("util.py", "def touch(path):\n    '工具函数'\n    return path\n")

    def test_added_file_becomes_searchable(self) -> None:
        index = SearchIndex.build(self.ws)
        self.assertFalse(_hits(index, "报表导出"))
        self.write("report.py", "def export_report():\n    '报表导出'\n")
        index.update(added=["report.py"])
        self.assertIn(("report.py", 1, 3), _hits(index, "报表导出"))

    def test_removed_file_disappears_from_results(self) -> None:
        """删除文件后必须检索不到 —— 反向验证就是让这条变红（计划 §7-J9）。"""
        index = SearchIndex.build(self.ws)
        self.assertTrue(_hits(index, "支付回调"))
        index.update(removed=["billing.py"])
        self.assertEqual(_hits(index, "支付回调"), set())
        self.assertNotIn("billing.py", index.files)

    def test_changed_file_replaces_old_chunks(self) -> None:
        index = SearchIndex.build(self.ws)
        self.assertTrue(_hits(index, "支付回调"))
        self.write("billing.py", "def refund():\n    '退款流程'\n    return 1\n")
        index.update(changed=["billing.py"])
        self.assertEqual(_hits(index, "支付回调"), set())
        self.assertTrue(_hits(index, "退款流程"))
        self.assertEqual(index.files.count("billing.py"), 1)

    def test_update_matches_full_rebuild(self) -> None:
        """增 / 改 / 删各来一个，结果必须与全量重建一致。"""
        index = SearchIndex.build(self.ws)
        for query in ("用户登录", "支付回调", "工具函数"):
            self.assertTrue(_hits(index, query), f"前置条件不成立: {query}")

        self.write("report.py", "def export_report():\n    '报表导出 与 支付回调 汇总'\n")
        self.write("auth.py", "def handle_login(user):\n    '用户登录入口 已改名'\n")
        (self.ws / "util.py").unlink()
        index.update(added=["report.py"], changed=["auth.py"], removed=["util.py"])

        rebuilt = SearchIndex.build(self.ws)
        self.assertEqual(len(index.chunks), len(rebuilt.chunks))
        self.assertEqual(sorted(index.files), sorted(rebuilt.files))
        self.assertEqual(index.lexical.doc_count, rebuilt.lexical.doc_count)
        self.assertEqual(index.lexical.term_count, rebuilt.lexical.term_count)
        for query in ("用户登录", "支付回调", "工具函数", "报表导出"):
            with self.subTest(query=query):
                self.assertEqual(_hits(index, query), _hits(rebuilt, query))

    def test_removed_term_leaves_no_stale_bucket(self) -> None:
        """删掉的词必须连桶一起消失，否则 df 偏大、idf 偏小，命中被稀释。"""
        self.write("rare.py", "def unicorn_guard():\n    '独角兽'\n")
        index = SearchIndex.build(self.ws)
        before = index.lexical.term_count
        self.assertTrue(_hits(index, "独角兽"))
        index.update(removed=["rare.py"])
        self.assertLess(index.lexical.term_count, before)
        self.assertEqual(index.lexical.search("独角兽", k=5), [])

    def test_stats_report_effective_counts(self) -> None:
        index = SearchIndex.build(self.ws)
        stats = index.update(
            added=["util.py"],  # 已经在索引里，再报一次 added 不该重复计入文件数
            changed=["auth.py"],
            removed=["ghost.py"],  # 从没索引过 → 不计入 removed
        )
        self.assertEqual(stats["added"], 1)
        self.assertEqual(stats["changed"], 1)
        self.assertEqual(stats["removed"], 0)
        self.assertEqual(stats["files"], 3)
        self.assertEqual(stats["chunks"], len(index.chunks))

    def test_update_tolerates_missing_and_binary_paths(self) -> None:
        index = SearchIndex.build(self.ws)
        (self.ws / "blob.bin").write_bytes(b"\x00\x01\x02binary")
        stats = index.update(added=["nope.py", "blob.bin"])
        self.assertEqual(stats["added"], 0)  # 两个都没能入索引
        self.assertNotIn("nope.py", index.files)
        self.assertNotIn("blob.bin", index.files)

    def test_graph_edges_follow_update(self) -> None:
        """改掉一个文件的 import，依赖边必须跟着换（不能只认首次扫描的结果）。"""
        self.write("main.py", "from .auth import handle_login\n")

        def fake_scan(path: Path, text: str) -> ScanResult:
            if path.name == "main.py":
                target = ".billing" if "billing" in text else ".auth"
                return ScanResult(imports=[f"from {target} import thing"])
            return ScanResult()

        with mock.patch("agent_core.tools.symbols.scan_file", side_effect=fake_scan):
            index = SearchIndex.build(self.ws)
            self.assertEqual(index.graph.dependents("auth.py"), ["main.py"])
            self.write("main.py", "from .billing import pay\nbilling\n")
            index.update(changed=["main.py"])
            self.assertEqual(index.graph.dependents("auth.py"), [])
            self.assertEqual(index.graph.dependents("billing.py"), ["main.py"])

    def test_remove_then_readd_keeps_single_entry(self) -> None:
        index = SearchIndex.build(self.ws)
        index.update(removed=["auth.py"])
        index.update(added=["auth.py"])
        self.assertEqual(index.files.count("auth.py"), 1)
        self.assertTrue(_hits(index, "用户登录"))


class WatcherTests(_Workspace):
    """watcher 本身：轮询、钉住、可停止。"""

    # 间隔取一个很大的值：线程的 Event.wait 一被 set 就立刻返回，
    # 所以 stop() 不会真的等这么久，但测试期间线程绝不会自己醒来抢跑。
    LONG = 3600

    def setUp(self) -> None:
        super().setUp()
        self.write("auth.py", "def handle_login(user):\n    '用户登录入口'\n")
        self.audit = AuditLog(self.ws / ".vibe" / "audit.jsonl", session_id="sess_watch")

    def test_poll_once_returns_none_without_changes(self) -> None:
        watcher = IndexWatcher(self.ws, self.rel_of(), interval=self.LONG, audit=self.audit)
        watcher.start()
        try:
            self.assertIsNone(watcher.poll_once())
        finally:
            watcher.stop()

    def test_poll_once_applies_changes_and_records_audit(self) -> None:
        watcher = IndexWatcher(self.ws, self.rel_of(), interval=self.LONG, audit=self.audit)
        watcher.start()
        try:
            self.write("fresh.py", "def pay():\n    '支付回调'\n")
            stats = watcher.poll_once()
            self.assertIsNotNone(stats)
            assert stats is not None
            self.assertEqual(stats["added"], 1)

            index = watcher.index
            assert index is not None
            self.assertIn(("fresh.py", 1, 3), _hits(index, "支付回调"))
            status = watcher.status()
            self.assertGreater(status["lastUpdate"], 0)
            self.assertEqual(status["pending"], 0)
            self.assertEqual(status["files"], len(index.files))

            body = self.audit.path.read_text(encoding="utf-8")
            self.assertIn("index.rebuild", body)
            # 只记计数：路径与文件内容一个字符都不许进审计（ADR-006）
            self.assertNotIn("fresh.py", body)
            self.assertNotIn("支付回调", body)
        finally:
            watcher.stop()

    def test_stop_reclaims_thread(self) -> None:
        watcher = IndexWatcher(self.ws, self.rel_of(), interval=self.LONG)
        watcher.start()
        self.assertTrue(watcher.status()["watching"])
        self.assertTrue(self._alive(), "watch 线程没有起来")
        watcher.stop()
        self.assertFalse(watcher.status()["watching"])
        self.assertFalse(self._alive(), "stop() 之后线程仍在运行")

    def test_stop_is_idempotent(self) -> None:
        watcher = IndexWatcher(self.ws, self.rel_of(), interval=self.LONG)
        watcher.start()
        watcher.stop()
        watcher.stop()  # atexit 会再调一次，必须无害
        self.assertFalse(self._alive())

    def test_watcher_shares_one_index_with_searchers(self) -> None:
        """J6：watcher 钉住的索引必须是检索拿到的**同一个**实例。

        否则 watcher 更新 A、检索读 B（旧），而且 B 永远不会被更新 ——
        表现就是「改了代码却搜到旧内容」。
        """
        watcher = IndexWatcher(self.ws, self.rel_of(), interval=self.LONG)
        watcher.start()
        try:
            self.assertIs(get_index(self.ws, self.rel_of()), watcher.index)
            self.assertIs(peek_index(self.ws), watcher.index)
        finally:
            watcher.stop()
        # 停掉之后解钉：缓存里不再有它，检索会重新建一份（而不是拿到死实例）
        self.assertIsNone(peek_index(self.ws))
        self.assertIsNot(get_index(self.ws, self.rel_of()), watcher.index)

    def test_pinned_index_survives_lru_pressure(self) -> None:
        """钉住的条目不被 LRU 淘汰：否则 watcher 会变成在更新一个没人读的实例。"""
        watcher = IndexWatcher(self.ws, self.rel_of(), interval=self.LONG)
        watcher.start()
        try:
            pinned = watcher.index
            for name in ("a", "b", "c", "d", "e", "f"):
                other = self.ws / name
                other.mkdir()
                self.write(f"{name}/mod.py", "def noop():\n    return 1\n")
                get_index(other, self.rel_of())
            self.assertIs(get_index(self.ws, self.rel_of()), pinned)
            self.assertTrue(pinned is not None and pinned.pinned)
        finally:
            watcher.stop()

    def _alive(self) -> bool:
        return any(
            thread.name == _THREAD_NAME and thread.is_alive()
            for thread in threading.enumerate()
        )


class SessionWatchTests(_Workspace):
    """会话层接线：watch 挂在 Session 上，随会话起停。"""

    def setUp(self) -> None:
        super().setUp()
        self.write("auth.py", "def handle_login(user):\n    '用户登录入口'\n")

    def test_watch_off_keeps_today_behaviour(self) -> None:
        session = Session(Config.load(self.ws))
        try:
            self.assertIsNone(session.watcher)
            status = session.index_status()
            self.assertFalse(status["watching"])
            # 没建过索引 → 如实回 0，而不是顺手把整个工作区读一遍
            self.assertEqual(status["files"], 0)
            self.assertFalse(
                any(t.name == _THREAD_NAME and t.is_alive() for t in threading.enumerate())
            )
        finally:
            session.close()

    def test_watch_on_reports_index_health(self) -> None:
        config = replace(
            Config.load(self.ws), index_watch=True, index_watch_interval=3600
        )
        session = Session(config)
        try:
            self.assertIsNotNone(session.watcher)
            status = session.index_status()
            self.assertTrue(status["watching"])
            self.assertEqual(status["files"], 1)
            self.assertGreaterEqual(status["chunks"], 1)
            self.assertEqual(status["interval"], 3600)
        finally:
            session.close()
        self.assertFalse(
            any(t.name == _THREAD_NAME and t.is_alive() for t in threading.enumerate())
        )

    def test_interval_has_a_floor(self) -> None:
        """间隔下限 2 秒：配成 0 会变成忙轮询。"""
        config = replace(
            Config.load(self.ws), index_watch=True, index_watch_interval=1
        )
        session = Session(config)
        try:
            assert session.watcher is not None
            self.assertEqual(session.watcher.interval, 2)
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()