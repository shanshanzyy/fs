"""检索增强（M3+ / 阶段 B）测试。

验收口径（docs/M3+-PLAN.md §2-B）：

1. **分词子词化**：`getUserName` → get/user/name，`find_files` → find/files，
   数字边界单独成词；原词与 CJK 二字组**保留**（不丢精确匹配面）。
2. **符号感知分块**：块边界落在符号定义处；无符号时退化得与 `chunk_text` 一致。
3. **RRF 融合**：按名次相加，量纲无关；默认路径仍走分数加权（`hybrid`）。
4. **去冗余**：同一文件块数受限。
5. **门控**：`retrieval_enhanced=False` 时增强分词**不生效** —— 这是
   「不配置时与改造前逐字节一致」的可执行证据。
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
from agent_core.index import (  # noqa: E402
    Chunk,
    LexicalIndex,
    SearchHit,
    chunk_text,
    chunk_text_by_symbols,
    clear_cache,
    split_identifier,
    tokenize_enhanced,
)
from agent_core.sandbox import CommandGuard, PathGuard  # noqa: E402
from agent_core.tools.base import ToolContext  # noqa: E402
from agent_core.tools.semantic import _dedup_by_file, _fuse_rrf, _semantic_search  # noqa: E402


# --------------------------------------------------------------------------- 分词
class IdentifierSplitTests(unittest.TestCase):
    def test_camel_case(self) -> None:
        self.assertEqual(split_identifier("getUserName"), ["get", "user", "name"])

    def test_snake_case(self) -> None:
        self.assertEqual(split_identifier("find_files"), ["find", "files"])

    def test_kebab_case(self) -> None:
        self.assertEqual(split_identifier("find-files"), ["find", "files"])

    def test_digit_boundary(self) -> None:
        self.assertEqual(split_identifier("HTTPClient2"), ["http", "client", "2"])
        self.assertEqual(split_identifier("version2Final"), ["version", "2", "final"])

    def test_consecutive_caps_stay_together(self) -> None:
        self.assertEqual(split_identifier("HTTPClient"), ["http", "client"])

    def test_plain_ascii_is_unchanged(self) -> None:
        self.assertEqual(split_identifier("user"), ["user"])


class EnhancedTokenizeTests(unittest.TestCase):
    def test_keeps_original_and_adds_subwords(self) -> None:
        tokens = tokenize_enhanced("getUserName")
        self.assertIn("getusername", tokens, "原词必须保留（精确匹配面不能丢）")
        for sub in ("get", "user", "name"):
            self.assertIn(sub, tokens)

    def test_keeps_cjk_bigrams(self) -> None:
        tokens = tokenize_enhanced("用户登录 flow_manager")
        self.assertIn("用户", tokens)
        self.assertIn("登录", tokens)
        self.assertIn("flow", tokens)
        self.assertIn("manager", tokens)


# --------------------------------------------------------------------------- 分块
class SymbolChunkTests(unittest.TestCase):
    def test_boundary_lands_on_symbol_definition(self) -> None:
        lines = [f"line {i}" for i in range(1, 61)]
        text = "\n".join(lines)
        # 符号定义在第 30 行；窗口 40 内它应当成为切点（第 30 行前切开）
        chunks = chunk_text_by_symbols(text, [30], window=40, overlap=1)
        self.assertEqual(chunks[0][1], 29)

    def test_ignores_symbol_at_file_head(self) -> None:
        """第 1 行是文件头，切在那里没有意义 —— 应当被忽略。"""
        lines = [f"line {i}" for i in range(1, 61)]
        chunks = chunk_text_by_symbols("\n".join(lines), [1], window=40, overlap=1)
        self.assertEqual(chunks[0][1], 40)

    def test_degenerates_to_plain_chunking_without_symbols(self) -> None:
        lines = [f"line {i}" for i in range(1, 121)]
        text = "\n".join(lines)
        self.assertEqual(
            chunk_text_by_symbols(text, [], window=40, overlap=1),
            chunk_text(text, window=40, overlap=1),
        )


# --------------------------------------------------------------------------- 词法索引
class EnhancedLexicalTests(unittest.TestCase):
    def test_enhanced_index_matches_subword(self) -> None:
        index = LexicalIndex(enhanced=True)
        index.add(0, "def getUserName(): return name")
        index.add(1, "def unrelated(): return 1")
        ranked = index.search("user", k=5)
        self.assertTrue(ranked)
        self.assertEqual(ranked[0][0], 0)

    def test_default_index_does_not_match_subword(self) -> None:
        """门控的可执行证据：默认索引里 `getUserName` 只是一个整词。"""
        index = LexicalIndex(enhanced=False)
        index.add(0, "def getUserName(): return name")
        self.assertEqual(index.search("user", k=5), [])

    def test_boost_weights_metadata(self) -> None:
        """把路径再算一遍 = 给命中文件名的块更高的 tf。"""
        index = LexicalIndex(enhanced=True)
        index.add(0, "return 1", boost="billing/report.py")
        index.add(1, "return 1", boost="misc/other.py")
        ranked = index.search("billing", k=5)
        self.assertEqual(ranked[0][0], 0)


# --------------------------------------------------------------------------- 融合与去冗余
def _hit(path: str, start: int, score: float) -> SearchHit:
    return SearchHit(chunk=Chunk(path=path, start=start, end=start, text=f"{path}:{start}"), score=score)


class FusionTests(unittest.TestCase):
    def test_rrf_promotes_a_hit_ranked_high_in_both_lists(self) -> None:
        # hits 已按词法分降序：A(词法第1) / B(第2) / C(第3)
        hits = [_hit("a.py", 1, 10.0), _hit("b.py", 1, 9.0), _hit("c.py", 1, 8.0)]
        # 向量分：B 最高 → B 在向量榜第 1
        fused = _fuse_rrf(hits, [0.1, 0.9, 0.5])
        self.assertEqual(fused[0].path, "b.py", "两榜都靠前的块应当融合到第一")

    def test_rrf_scores_are_symmetric_in_rank_not_magnitude(self) -> None:
        """RRF 只看名次：两条名次**对称**的命中（a 词法 1 / 向量 2，b 反之），
        即使词法分差 100 倍，也应得**完全相同**的分 —— 这正是「免疫量纲」的证据。"""
        fused = _fuse_rrf(
            [_hit("a.py", 1, 100.0), _hit("b.py", 1, 1.0)], [0.01, 0.99]
        )
        self.assertAlmostEqual(fused[0].score, fused[1].score, places=9)
        self.assertEqual({hit.path for hit in fused}, {"a.py", "b.py"})

    def test_dedup_caps_chunks_per_file(self) -> None:
        hits = [
            _hit("a.py", 1, 90.0),
            _hit("a.py", 2, 80.0),
            _hit("a.py", 3, 70.0),
            _hit("a.py", 4, 60.0),
            _hit("b.py", 1, 50.0),
        ]
        kept = _dedup_by_file(hits, per_file=3)
        self.assertEqual([h.path for h in kept], ["a.py", "a.py", "a.py", "b.py"])

    def test_dedup_zero_means_no_cap(self) -> None:
        hits = [_hit("a.py", i, 10.0 - i) for i in range(5)]
        self.assertEqual(len(_dedup_by_file(hits, per_file=0)), 5)


# --------------------------------------------------------------------------- 工具接线
class SemanticGateTests(unittest.TestCase):
    """`retrieval_enhanced` 开关必须真的改变行为，且默认不变。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        clear_cache()
        (self.ws / "auth.py").write_text(
            "def getUserName(uid):\n    return uid.name\n", encoding="utf-8", newline=""
        )

    def tearDown(self) -> None:
        clear_cache()
        self._tmp.cleanup()

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

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    def test_default_path_backend_is_hybrid_with_vectors(self) -> None:
        ctx = self._ctx(embed_model="fake-embed")
        with mock.patch("agent_core.tools.semantic.EmbeddingClient", self._FakeClient):
            result = _semantic_search(ctx, {"query": "getUserName", "k": 2})
        self.assertEqual(result.meta["backend"], "hybrid", "默认路径不能被改动")

    def test_enhanced_path_backend_is_rrf_with_vectors(self) -> None:
        ctx = self._ctx(embed_model="fake-embed", retrieval_enhanced=True)
        with mock.patch("agent_core.tools.semantic.EmbeddingClient", self._FakeClient):
            result = _semantic_search(ctx, {"query": "getUserName", "k": 2})
        self.assertEqual(result.meta["backend"], "rrf")

    def test_subword_query_hits_only_when_enhanced(self) -> None:
        """子词查询 `user` 只在增强开启时命中 `getUserName`。"""
        plain = _semantic_search(self._ctx(), {"query": "user"})
        self.assertEqual(plain.meta["hits"], 0)

        enhanced = _semantic_search(
            self._ctx(retrieval_enhanced=True), {"query": "user"}
        )
        self.assertGreater(enhanced.meta["hits"], 0)


if __name__ == "__main__":
    unittest.main()