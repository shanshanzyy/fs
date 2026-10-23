"""远端「工作区根」（M12）测试：命名空间写法 + 落盘往返 + 坏根丢弃。

验收要求（对应方案 A 阶段）：

1. `normalize_sub` 只能是**相对 conn.root 的相对路径**：拒绝绝对写法、`..`、空字节、
   `-` 开头；容忍正反斜杠与冗余 `./`；
2. `RemoteRoot.ns_path()` 是 `@remote/<连接>[/<子目录>]`，根自己不带尾斜杠；
3. 落盘往返：`roots` 与 `connections` 同一份文件（`.vibe/remote.json`），
   `FORMAT_VERSION` **不升**（老客户端忽略未知键）；
4. **一条坏根不毁整份清单**：连接不存在 / 子目录非法 → 丢弃进 `problems`，不抛错；
5. 重复登记拒绝、移出不存在拒绝（不静默成功）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.remote.connection import RemoteConnection  # noqa: E402
from agent_core.remote.errors import RemoteConfigError  # noqa: E402
from agent_core.remote.roots import (  # noqa: E402
    PREFIX,
    RemoteRoot,
    is_ns_path,
    normalize_sub,
    validate_root,
)
from agent_core.remote.store import (  # noqa: E402
    FORMAT_VERSION,
    RemoteStore,
    default_store_path,
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


class NormalizeSubTest(unittest.TestCase):
    def test_empty_means_root_itself(self) -> None:
        for raw in (None, "", ".", "./", "/"):
            if raw == "/":
                # 绝对写法在「加入工作区」里是歧义来源，单独拒绝（见下一条用例）
                continue
            self.assertEqual(normalize_sub(raw), "")

    def test_relative_path_is_normalized(self) -> None:
        self.assertEqual(normalize_sub("a/b"), "a/b")
        self.assertEqual(normalize_sub("a\\b\\c"), "a/b/c")
        self.assertEqual(normalize_sub("./a/./b/"), "a/b")
        self.assertEqual(normalize_sub("  项目/smart-writer  "), "项目/smart-writer")

    def test_absolute_and_escaping_rejected(self) -> None:
        with self.assertRaises(RemoteConfigError):
            normalize_sub("/home/gdhd/项目")
        with self.assertRaises(RemoteConfigError):
            normalize_sub("a/../../etc")
        with self.assertRaises(RemoteConfigError):
            normalize_sub("..")
        with self.assertRaises(RemoteConfigError):
            normalize_sub("-oProxyCommand=x")
        with self.assertRaises(RemoteConfigError):
            normalize_sub("a\x00b")


class RemoteRootTest(unittest.TestCase):
    def test_ns_path_has_no_trailing_slash(self) -> None:
        self.assertEqual(RemoteRoot("dev").ns_path(), "@remote/dev")
        self.assertEqual(RemoteRoot("dev", "项目/x").ns_path(), "@remote/dev/项目/x")
        self.assertTrue(RemoteRoot("dev").ns_path().startswith(PREFIX))

    def test_abs_root_uses_live_connection_root(self) -> None:
        conn = make_conn(root="/srv/app")
        self.assertEqual(RemoteRoot("devbox").abs_root(conn), "/srv/app")
        self.assertEqual(RemoteRoot("devbox", "a/b").abs_root(conn), "/srv/app/a/b")

    def test_is_ns_path(self) -> None:
        self.assertTrue(is_ns_path("@remote/dev/a"))
        self.assertFalse(is_ns_path("src/app.py"))
        self.assertFalse(is_ns_path(None))
        self.assertFalse(is_ns_path(123))

    def test_validate_root_reports_unknown_connection(self) -> None:
        store = RemoteStore(path=Path("unused"))
        reason = validate_root(RemoteRoot("nope"), store)
        self.assertIn("未找到名为", reason)


class StoreRootsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = default_store_path(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed(self, *conns: RemoteConnection) -> RemoteStore:
        store = RemoteStore.load(self.path)
        for conn in conns or (make_conn(),):
            store.upsert(conn)
        return store

    def test_roundtrip_and_version_not_bumped(self) -> None:
        store = self._seed()
        root = store.add_root("devbox", "项目/smart-writer")
        self.assertEqual(root.sub, "项目/smart-writer")

        reloaded = RemoteStore.load(self.path)
        self.assertEqual(len(reloaded.roots), 1)
        self.assertEqual(reloaded.roots[0].ns_path(), "@remote/devbox/项目/smart-writer")
        self.assertEqual(reloaded.problems, [])
        self.assertEqual(reloaded.default_name, "devbox")

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], FORMAT_VERSION)
        self.assertIn("roots", raw)
        self.assertEqual(raw["roots"][0]["connection"], "devbox")

    def test_duplicate_add_rejected(self) -> None:
        store = self._seed()
        store.add_root("devbox", "a")
        with self.assertRaises(RemoteConfigError):
            store.add_root("devbox", "./a/")

    def test_remove_missing_rejected(self) -> None:
        store = self._seed()
        with self.assertRaises(RemoteConfigError):
            store.remove_root("devbox", "not-there")

    def test_remove_persists(self) -> None:
        store = self._seed()
        store.add_root("devbox", "")
        store.add_root("devbox", "a")
        store.remove_root("devbox", "a")
        reloaded = RemoteStore.load(self.path)
        self.assertEqual([r.sub for r in reloaded.roots], [""])

    def test_bad_roots_dropped_into_problems(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "default": "devbox",
                    "connections": [make_conn().to_dict()],
                    "roots": [
                        {"connection": "ghost", "sub": "x"},
                        {"connection": "devbox", "sub": "/abs"},
                        {"connection": "devbox", "sub": "ok"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
            newline="",
        )
        store = RemoteStore.load(self.path)  # 不抛错
        self.assertEqual([r.sub for r in store.roots], ["ok"])
        self.assertEqual(len(store.problems), 2)

    def test_match_root_prefers_most_specific(self) -> None:
        store = self._seed()
        store.roots = [
            RemoteRoot("devbox", ""),
            RemoteRoot("devbox", "项目/smart-writer"),
        ]
        hit = store.match_root("devbox", "项目/smart-writer/src/app.py")
        assert hit is not None
        self.assertEqual(hit.sub, "项目/smart-writer")
        other = store.match_root("devbox", "别处/x.py")
        assert other is not None
        self.assertEqual(other.sub, "")  # 根本身兜底

    def test_match_root_none_when_connection_has_no_root(self) -> None:
        store = self._seed()
        store.roots = []
        self.assertIsNone(store.match_root("devbox", "a/b"))

    def test_best_root_prefers_shallowest(self) -> None:
        store = self._seed()
        store.roots = [RemoteRoot("devbox", "a/b"), RemoteRoot("devbox", "")]
        best = store.best_root("devbox")
        assert best is not None
        self.assertEqual(best.sub, "")


if __name__ == "__main__":
    unittest.main()
