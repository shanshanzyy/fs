"""语义检索（阶段 B）测试。

验收口径（P0-FEATURES-PLAN §4-B8）：

1. **无 embedding 时仍返回结果** —— 回退生效，`backend=lexical`；
2. **依赖图能反向找到「谁 import 了 A」** —— 改动波及面；
3. **结果上限与越权路径防护**；
4. **索引缓存命中** —— 同一 workspace 二次调用不重扫。

另有一组纯单元测试钉住依赖图与词法的解析细节：它们是「精度」的来源，
而这部分逻辑没有外部依赖，完全可复现，不依赖本机是否装了 tree-sitter。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.audit import AuditLog  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.errors import SandboxViolation  # noqa: E402
from agent_core.index import (  # noqa: E402
    DepGraph,
    LexicalIndex,
    chunk_text,
    clear_cache,
    get_index,
    tokenize,
)
from agent_core.llm.embeddings import EmbeddingClient, cosine  # noqa: E402
from agent_core.sandbox import CommandGuard, PathGuard, Risk  # noqa: E402
from agent_core.tools import build_default_registry  # noqa: E402
from agent_core.tools.base import ToolContext  # noqa: E402
from agent_core.tools.semantic import _semantic_search  # noqa: E402
from agent_core.tools.symbols import ScanResult  # noqa: E402


def _graph(known: list[str], imports: dict[str, list[str]]) -> DepGraph:
    return DepGraph.build(known, imports)


class DepGraphTests(unittest.TestCase):
    """依赖图解析：只认能解析到**确实存在**的文件的边。"""

    def test_python_relative_import(self) -> None:
        graph = _graph(["pkg/main.py", "pkg/svc.py"], {"pkg/main.py": ["from .svc import run"]})
        self.assertEqual(graph.neighbors("pkg/main.py"), ["pkg/svc.py"])
        self.assertEqual(graph.dependents("pkg/svc.py"), ["pkg/main.py"])

    def test_python_from_dot_import_name(self) -> None:
        graph = _graph(["pkg/main.py", "pkg/helper.py"], {"pkg/main.py": ["from . import helper"]})
        self.assertEqual(graph.neighbors("pkg/main.py"), ["pkg/helper.py"])

    def test_python_absolute_import_under_src(self) -> None:
        # 仓库根布局不可知，因此 "" 与 "src" 两个根都要试
        graph = _graph(["src/app/svc.py", "src/app/main.py"], {"src/app/main.py": ["from app.svc import run"]})
        self.assertEqual(graph.neighbors("src/app/main.py"), ["src/app/svc.py"])

    def test_python_parent_import(self) -> None:
        graph = _graph(["pkg/a/x.py", "pkg/b.py"], {"pkg/a/x.py": ["from ..b import y"]})
        self.assertEqual(graph.neighbors("pkg/a/x.py"), ["pkg/b.py"])

    def test_ecma_relative_import_resolves_extension(self) -> None:
        graph = _graph(["src/app.ts", "src/util.ts"], {"src/app.ts": ['import { x } from "./util";']})
        self.assertEqual(graph.neighbors("src/app.ts"), ["src/util.ts"])

    def test_ecma_index_file(self) -> None:
        graph = _graph(["src/app.ts", "src/util/index.ts"], {"src/app.ts": ['import { x } from "./util";']})
        self.assertEqual(graph.neighbors("src/app.ts"), ["src/util/index.ts"])

    def test_go_directory_import(self) -> None:
        graph = _graph(
            ["cmd/main.go", "pkg/util/a.go", "pkg/util/b.go"],
            {"cmd/main.go": ['import "demo/pkg/util"']},
        )
        self.assertEqual(graph.neighbors("cmd/main.go"), ["pkg/util/a.go", "pkg/util/b.go"])

    def test_rust_crate_use(self) -> None:
        graph = _graph(["src/main.rs", "src/svc.rs"], {"src/main.rs": ["use crate::svc::run;"]})
        self.assertEqual(graph.neighbors("src/main.rs"), ["src/svc.rs"])

    def test_unresolvable_import_makes_no_edge(self) -> None:
        # 标准库、node_modules 依赖不该出现在图里
        graph = _graph(["a.py"], {"a.py": ["import os", 'import x from "react"']})
        self.assertTrue(graph.is_empty())

    def test_escape_never_becomes_edge(self) -> None:
        graph = _graph(["pkg/a.ts"], {"pkg/a.ts": ['import { x } from "../../outside";']})
        self.assertTrue(graph.is_empty())

    def test_self_import_is_skipped(self) -> None:
        graph = _graph(["a.py"], {"a.py": ["from a import x"]})
        self.assertTrue(graph.is_empty())

    def test_multi_name_import_resolves_each_target(self) -> None:
        # symbols.py 会把多行 import 压成一行，逗号分隔的多个目标都要能解析
        graph = _graph(["a.py", "pkg/b.py"], {"a.py": ["import os, pkg.b"]})
        self.assertEqual(graph.neighbors("a.py"), ["pkg/b.py"])


class LexicalTests(unittest.TestCase):
    def test_chunk_covers_all_lines_with_overlap(self) -> None:
        text = "\n".join(f"line {i}" for i in range(1, 121))
        chunks = chunk_text(text, window=40, overlap=1)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(chunks[0][0], 1)
        self.assertEqual(chunks[-1][1], 120)
        for (_, end, _), (start, _, _) in zip(chunks, chunks[1:]):
            self.assertLessEqual(start, end)  # 相邻块必须重叠 1 行
        covered = set()
        for start, end, _ in chunks:
            covered.update(range(start, end + 1))
        self.assertEqual(covered, set(range(1, 121)))

    def test_chunk_prefers_paragraph_boundary(self) -> None:
        lines = [f"line {i}" for i in range(1, 61)]
        lines[24] = ""  # 第 25 行是空行，落在窗口内 → 断点应收在这里
        chunks = chunk_text("\n".join(lines), window=40, overlap=1)
        self.assertEqual(chunks[0][1], 25)

    def test_chunk_skips_blank_lead(self) -> None:
        chunks = chunk_text("\n\n\ncode\n", window=40, overlap=1)
        self.assertEqual(chunks[0][0], 4)

    def test_tokenize_splits_cjk_bigrams(self) -> None:
        tokens = tokenize("用户登录 flow_manager")
        self.assertIn("登录", tokens)
        self.assertIn("用户", tokens)
        self.assertIn("flow_manager", tokens)

    def test_bm25_ranks_matching_chunk_first(self) -> None:
        index = LexicalIndex()
        index.add(0, "def handle_login(user):\n    # 用户登录\n    return session")
        index.add(1, "import os\n\ndef touch(path):\n    os.utime(path)")
        ranked = index.search("用户登录", k=5)
        self.assertEqual(ranked[0][0], 0)

    def test_empty_query_returns_nothing(self) -> None:
        index = LexicalIndex()
        index.add(0, "anything")
        self.assertEqual(index.search("", k=5), [])


class IndexStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        clear_cache()

    def tearDown(self) -> None:
        clear_cache()
        self._tmp.cleanup()

    def _write(self, rel: str, text: str) -> None:
        path = self.ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")

    def _rel_of(self):
        return PathGuard(self.ws).relative

    def test_search_finds_chunk_and_reports_lines(self) -> None:
        self._write("auth.py", "def handle_login(user):\n    '用户登录入口'\n    return True\n")
        self._write("util.py", "def touch(path):\n    return path\n")
        index = get_index(self.ws, self._rel_of())
        hits = index.search("用户登录", k=5)
        self.assertTrue(hits)
        paths = {hit.path for hit in hits}
        self.assertIn("auth.py", paths)
        self.assertGreaterEqual(hits[0].start, 1)
        self.assertLessEqual(hits[0].end, len((self.ws / hits[0].path).read_text(encoding="utf-8").split("\n")))

    def test_binary_and_ignored_dirs_are_skipped(self) -> None:
        self._write("auth.py", "用户登录\n")
        (self.ws / "blob.bin").write_bytes(b"\x00\x01\x02binary")
        self._write("node_modules/pkg/index.js", "用户登录\n")
        index = get_index(self.ws, self._rel_of())
        self.assertIn("auth.py", index.files)
        self.assertNotIn("blob.bin", index.files)
        self.assertFalse(any(f.startswith("node_modules/") for f in index.files))

    def test_cache_hit_returns_same_object(self) -> None:
        self._write("auth.py", "用户登录\n")
        first = get_index(self.ws, self._rel_of())
        second = get_index(self.ws, self._rel_of())
        self.assertIs(first, second)

    def test_cache_invalidated_when_workspace_changes(self) -> None:
        self._write("auth.py", "用户登录\n")
        first = get_index(self.ws, self._rel_of())
        self._write("new_file.py", "新增\n")
        second = get_index(self.ws, self._rel_of())
        self.assertIsNot(first, second)

    def test_graph_empty_without_tree_sitter_but_search_still_works(self) -> None:
        self._write("pkg/main.py", "from .svc import run\n\ndef handle_login():\n    return run\n")
        self._write("pkg/svc.py", "def run():\n    return 1\n")
        with mock.patch("agent_core.tools.symbols.treesitter_scan", return_value=None):
            index = get_index(self.ws, self._rel_of())
        self.assertTrue(index.graph.is_empty())
        self.assertTrue(index.search("handle_login", k=3))

    def test_imports_produce_edges_and_dependents(self) -> None:
        self._write("main.py", "from .helper import x\n")
        self._write("helper.py", "def x():\n    return 1\n")

        def fake_scan(path: Path, text: str) -> ScanResult:
            if path.name == "main.py":
                return ScanResult(imports=["from .helper import x"])
            return ScanResult()

        with mock.patch("agent_core.tools.symbols.scan_file", side_effect=fake_scan):
            index = get_index(self.ws, self._rel_of())
        self.assertEqual(index.graph.dependents("helper.py"), ["main.py"])
        self.assertEqual(index.related("helper.py"), (["main.py"], []))


class EmbeddingTests(unittest.TestCase):
    def test_cosine_bounds(self) -> None:
        self.assertAlmostEqual(cosine([1.0, 0.0], [1.0, 0.0]), 1.0, places=6)
        self.assertAlmostEqual(cosine([1.0, 0.0], [0.0, 1.0]), 0.0, places=6)
        self.assertEqual(cosine([1.0], [1.0, 2.0]), 0.0)  # 维度不一致 → 不可比

    def test_embed_returns_none_when_endpoint_unreachable(self) -> None:
        client = EmbeddingClient("http://127.0.0.1:1/v1", "any-model", timeout=2)
        self.assertIsNone(client.embed(["hello"]))
        ok, reason = client.probe()
        self.assertFalse(ok)
        self.assertTrue(reason)

    def test_probe_rejects_missing_model(self) -> None:
        ok, reason = EmbeddingClient("http://127.0.0.1:1/v1", "").probe()
        self.assertFalse(ok)
        self.assertIn("未配置", reason)


class SemanticToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        clear_cache()
        self._write("auth.py", "def handle_login(user):\n    '用户登录入口'\n    return True\n")
        self._write("report.py", "def monthly_report(rows):\n    return sum(rows)\n")
        self._write("billing.py", "def handle_login_billing():\n    '用户登录 计费'\n    return 0\n")

    def tearDown(self) -> None:
        clear_cache()
        self._tmp.cleanup()

    def _write(self, rel: str, text: str) -> None:
        path = self.ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")

    def _ctx(self, **overrides) -> ToolContext:
        config = Config.load(self.ws)
        if overrides:
            config = replace(config, **overrides)
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

    def test_falls_back_to_lexical_without_embedding_model(self) -> None:
        result = _semantic_search(self._ctx(), {"query": "用户登录"})
        self.assertTrue(result.ok)
        self.assertEqual(result.meta["backend"], "lexical")
        self.assertGreater(result.meta["hits"], 0)
        self.assertIn("auth.py", result.content)

    def test_unreachable_embedding_endpoint_still_returns_results(self) -> None:
        ctx = self._ctx(embed_model="any-model", base_url="http://127.0.0.1:1/v1")
        result = _semantic_search(ctx, {"query": "用户登录"})
        self.assertTrue(result.ok)
        self.assertEqual(result.meta["backend"], "lexical")
        self.assertGreater(result.meta["hits"], 0)
        self.assertIn("回退词法", result.content)

    def test_hybrid_backend_when_vectors_available(self) -> None:
        ctx = self._ctx(embed_model="fake-embed")

        class FakeClient:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def embed(self, texts):
                return [[1.0, 0.0] for _ in texts]

        with mock.patch("agent_core.tools.semantic.EmbeddingClient", FakeClient):
            result = _semantic_search(ctx, {"query": "用户登录", "k": 2})
        self.assertEqual(result.meta["backend"], "hybrid")
        self.assertLessEqual(result.meta["hits"], 2)
        self.assertIn("score=", result.content)

    def test_expand_deps_renders_dependents_section(self) -> None:
        """命中文件的「调用方」必须出现在结果里 —— 这是漏改的第一个信号。"""
        self._write("main.py", "from .auth import handle_login\n")
        clear_cache()
        with mock.patch("agent_core.tools.symbols.scan_file", side_effect=self._fake_scan):
            result = _semantic_search(self._ctx(), {"query": "用户登录", "k": 3})
        self.assertIn("auth.py", result.content)
        self.assertIn("← main.py", result.content)

    def test_expand_deps_false_omits_dependents_section(self) -> None:
        self._write("main.py", "from .auth import handle_login\n")
        clear_cache()
        with mock.patch("agent_core.tools.symbols.scan_file", side_effect=self._fake_scan):
            result = _semantic_search(
                self._ctx(), {"query": "用户登录", "k": 3, "expand_deps": False}
            )
        self.assertNotIn("← main.py", result.content)

    @staticmethod
    def _fake_scan(path: Path, text: str) -> ScanResult:
        if path.name == "main.py":
            return ScanResult(imports=["from .auth import handle_login"])
        return ScanResult()

    def test_k_is_capped_and_respected(self) -> None:
        one = _semantic_search(self._ctx(), {"query": "用户登录", "k": 1})
        self.assertEqual(one.meta["hits"], 1)
        many = _semantic_search(self._ctx(), {"query": "用户登录", "k": 9999})
        self.assertLessEqual(many.meta["hits"], 30)

    def test_blank_query_is_rejected(self) -> None:
        result = build_default_registry().execute(self._ctx(), "semantic_search", {"query": "   "})
        self.assertFalse(result.ok)

    def test_escape_path_is_denied_by_registry(self) -> None:
        registry = build_default_registry()
        result = registry.execute(self._ctx(), "semantic_search", {"query": "x", "path": "../secret"})
        self.assertFalse(result.ok)
        self.assertEqual(result.denied, "path")

    def test_escape_path_raises_directly(self) -> None:
        with self.assertRaises(SandboxViolation):
            _semantic_search(self._ctx(), {"query": "x", "path": "../secret"})

    def test_non_directory_path_is_rejected(self) -> None:
        result = build_default_registry().execute(
            self._ctx(), "semantic_search", {"query": "x", "path": "auth.py"}
        )
        self.assertFalse(result.ok)
        self.assertIn("不是目录", result.content)

    def test_tool_is_registered_as_safe(self) -> None:
        registry = build_default_registry()
        spec = registry.get("semantic_search")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.risk, Risk.SAFE)
        self.assertIn("semantic_search", registry.names())

    def test_audit_records_backend_and_hits(self) -> None:
        ctx = self._ctx()
        _semantic_search(ctx, {"query": "用户登录"})
        text = ctx.audit.path.read_text(encoding="utf-8")
        self.assertIn("tool.semantic_search", text)
        self.assertIn('"backend": "lexical"', text)


if __name__ == "__main__":
    unittest.main()