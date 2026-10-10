"""semantic_search（B6）：用自然语言回答「该改哪个文件」。

与 grep / find_files 的分工：
    grep          —— 你已经知道要找什么字符串；
    semantic_search —— 你只知道「要改的那个功能」叫什么，不知道该搜什么词。

实现是「词法召回 + 可选向量重排」的两段式（见 index/ 包）。这么做而不是
纯向量，是因为核心链路要求零强制依赖：没配 embedding 端点时它必须仍然可用，
只不过结果排序退化成词法 —— 功能在，精度降一档。
"""

from __future__ import annotations

from ..errors import ToolError
from ..index.store import SearchIndex, SearchHit, get_index
from ..llm.embeddings import EmbeddingClient, cosine
from ..sandbox import Risk
from .base import ToolContext, ToolResult, ToolSpec, wrap_untrusted

# 词法先多召回一些候选，再交给向量重排。重排只编码「候选块」而不是全库，
# 因此多召回几十个块的代价可以忽略，换来的是「词法没排进前 k 但语义最像」的块有机会翻上来。
_RECALL_FACTOR = 4
_MIN_RECALL = 24
# 送进 embedding 的单块字符上限：超长块会被多数模型的上下文截断，截断位置不可控，
# 不如自己先切，保证「送进去的一定是块的开头」。
_EMBED_CHUNK_CHARS = 1200
_SNIPPET_CHARS = 240
_BODY_LIMIT = 12_000

# 检索增强（M3+）：RRF 平滑常数（文献常用的 60）与「单文件最多保留几块」。
_RRF_K = 60
_PER_FILE_CAP = 3
# 融合后的 backend 标签：与默认路径的 "hybrid" 区分，便于审计里分辨走的是哪条路。
_BACKEND_RRF = "rrf"


def _embed_semantic(
    ctx: ToolContext, query: str, hits: list[SearchHit]
) -> list[float] | None:
    """给候选块算「与 query 的余弦相似度」。返回 None 表示端点不可用。"""
    client = EmbeddingClient(
        ctx.config.base_url,
        ctx.config.embed_model,
        ctx.config.api_key,
        timeout=min(max(5, ctx.config.request_timeout), 60),
    )
    vectors = client.embed([query] + [hit.chunk.text[:_EMBED_CHUNK_CHARS] for hit in hits])
    if not vectors or len(vectors) != len(hits) + 1:
        return None
    query_vector = vectors[0]
    return [cosine(query_vector, vector) for vector in vectors[1:]]


def _rerank(
    ctx: ToolContext, query: str, hits: list[SearchHit]
) -> tuple[list[SearchHit], str] | None:
    """用向量余弦对候选块重排（默认路径）。返回 None 表示端点不可用，调用方保持纯词法。"""
    semantic = _embed_semantic(ctx, query, hits)
    if semantic is None:
        return None

    lex_max = max((hit.score for hit in hits), default=0.0) or 1.0
    fused: list[SearchHit] = []
    for hit, cosine_score in zip(hits, semantic):
        lexical = hit.score / lex_max
        # 余弦落在 [-1, 1]，映射到 [0, 1] 才能和词法分同量纲相加
        mapped = (cosine_score + 1.0) / 2.0
        fused.append(SearchHit(chunk=hit.chunk, score=0.5 * lexical + 0.5 * mapped))
    fused.sort(key=lambda item: (-item.score, item.path, item.start))
    return fused, "hybrid"


def _fuse_rrf(hits: list[SearchHit], semantic: list[float]) -> list[SearchHit]:
    """RRF 融合（M3+ 检索增强）：只按**名次**相加，不按分数加权。

    为什么换成 RRF：词法分（BM25，无界）与余弦（[-1,1]）量纲根本不同，
    加权相加前必须各自归一化 —— 而归一化会把「第一名相对第二名的领先幅度」
    抹平（都压到 0~1 的同一段里）。RRF 用名次，天然免疫量纲差异，也不需要
    调权：一个块只要在任一路召回里排得靠前，就会得到可观的分。
    """
    order = sorted(range(len(hits)), key=lambda index: (-semantic[index], index))
    semantic_rank = {index: rank for rank, index in enumerate(order, start=1)}
    fused = [
        SearchHit(
            chunk=hit.chunk,
            score=1.0 / (_RRF_K + index + 1) + 1.0 / (_RRF_K + semantic_rank[index]),
        )
        for index, hit in enumerate(hits)  # hits 已按词法分降序 → 下标即词法名次
    ]
    fused.sort(key=lambda item: (-item.score, item.path, item.start))
    return fused


def _dedup_by_file(
    hits: list[SearchHit], per_file: int = _PER_FILE_CAP
) -> list[SearchHit]:
    """按文件去冗余（M3+ 检索增强）：同一个文件最多留 `per_file` 块（最高分的那些）。

    词法召回常把「同一文件里相邻的几个窗口」一起拉进来；直接取前 k 会让结果
    被一个文件占满 —— 用户要的是「去改哪几个文件」，不是「同一个文件的第 3 段」。
    只留每文件最高分的几块，其它文件才排得上来。
    """
    if per_file <= 0:
        return hits
    kept: list[SearchHit] = []
    seen: dict[str, int] = {}
    for hit in hits:  # hits 已按分数降序 → 先见到的就是该文件的最高分块
        count = seen.get(hit.path, 0)
        if count >= per_file:
            continue
        seen[hit.path] = count + 1
        kept.append(hit)
    return kept


def _render(
    index: SearchIndex,
    query: str,
    hits: list[SearchHit],
    backend: str,
    expand_deps: bool,
) -> str:
    stats = index.stats()
    lines = [
        f"[semantic_search] 查询 {query!r} —— 后端 {backend}"
        + ("（向量端点不可用，已回退词法）" if backend == "lexical" else ""),
        f"索引：{stats['files']} 个文件 / {stats['chunks']} 个块 / {stats['edges']} 条依赖边"
        + ("（未装 tree-sitter，依赖边为空）" if stats["edges"] == 0 else ""),
    ]

    if not hits:
        lines.append(
            "（无命中。可换个说法、少用专有名词，或先用 repo_map 看目录结构）"
        )
        return "\n".join(lines)

    lines.append(f"命中 {len(hits)} 条（按相关度排序）：")
    for rank, hit in enumerate(hits, 1):
        lines.append(
            f"{rank}. {hit.path}:L{hit.start}-L{hit.end}  score={hit.score:.3f}\n"
            f"   {hit.snippet(_SNIPPET_CHARS)}"
        )

    if expand_deps:
        incoming: list[str] = []
        outgoing: list[str] = []
        for hit in hits:
            dependents, neighbors = index.related(hit.path, limit=3)
            for item in dependents:
                if item not in incoming:
                    incoming.append(item)
            for item in neighbors:
                if item not in outgoing:
                    outgoing.append(item)
        if incoming:
            lines.append(
                "以下文件 import 了命中文件 —— 改接口时它们是首当其冲的调用方（漏改即断）："
            )
            lines.extend(f"  ← {item}" for item in incoming[:8])
        if outgoing:
            lines.append("命中文件自身依赖的模块（改行为时可能需要一起看）：")
            lines.extend(f"  → {item}" for item in outgoing[:8])

    body = "\n".join(lines)
    if len(body) > _BODY_LIMIT:
        body = body[:_BODY_LIMIT] + "\n…（结果过长已截断，请调小 k 或缩小 path）"
    return body


def _semantic_search(ctx: ToolContext, args: dict) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("query 不能为空：请用一句话描述你要找的功能，如「哪里处理用户登录」")

    k = max(1, min(int(args.get("k") or 8), 30))
    expand_deps = bool(args.get("expand_deps", True))

    root = ctx.paths.check_list(args.get("path") or ".")
    if not root.is_dir():
        raise ToolError(f"路径不是目录: {ctx.paths.relative(root)}")

    index = get_index(
        root,
        ctx.paths.relative,
        ctx.config.semantic_max_files,
        enhanced=ctx.config.retrieval_enhanced,
    )
    if not index.chunks:
        return ToolResult(
            ok=True,
            content=wrap_untrusted(
                "semantic_search",
                "[semantic_search] 该路径下没有可检索的文本文件"
                "（二进制与忽略目录不计），请换一个 path 或先用 list_dir 确认结构。",
            ),
            meta={"hits": 0, "files": 0, "backend": "lexical"},
        )

    hits = index.search(query, k=max(k * _RECALL_FACTOR, _MIN_RECALL))
    backend = "lexical"
    if ctx.config.embed_model and hits:
        # 增强路径：RRF 按名次融合（量纲无关）；默认路径：分数加权相加。
        if ctx.config.retrieval_enhanced:
            semantic = _embed_semantic(ctx, query, hits)
            if semantic is not None:
                hits, backend = _fuse_rrf(hits, semantic), _BACKEND_RRF
        else:
            reranked = _rerank(ctx, query, hits)
            if reranked is not None:
                hits, backend = reranked
    if ctx.config.retrieval_enhanced:
        hits = _dedup_by_file(hits)
    hits = hits[:k]

    body = _render(index, query, hits, backend, expand_deps)
    ctx.audit.record(
        "tool.semantic_search",
        path=ctx.paths.relative(root),
        # 与 tool.grep 同一约定：原文只留本机审计文件，上传时由
        # governance/vocabulary.py 换成 digest + 长度（ADR-006）。
        query=query,
        hits=len(hits),
        backend=backend,
    )
    return ToolResult(
        ok=True,
        content=wrap_untrusted("semantic_search", body),
        meta={
            "hits": len(hits),
            "files": len(index.files),
            "backend": backend,
            "edges": index.graph.edge_count(),
        },
    )


SEMANTIC_SEARCH_SPEC = ToolSpec(
    name="semantic_search",
    description=(
        "用自然语言描述你要找的功能（如「哪里处理用户登录」「补丁应用失败时的回滚逻辑」），"
        "返回最相关的文件、行号与代码片段，并列出依赖这些文件的调用方。\n"
        "用途：**在不知道该搜什么关键字时**先定位到文件，再用 read_file 精读。\n"
        "若你已经知道要搜的字符串，用 grep 更准更快。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "一句话描述要找的功能或逻辑"},
            "k": {"type": "integer", "description": "返回多少条结果，默认 8，上限 30"},
            "path": {"type": "string", "description": "检索范围，默认整个工作区"},
            "expand_deps": {
                "type": "boolean",
                "description": "是否附带「谁 import 了命中文件」等依赖关系，默认 true",
            },
        },
        "required": ["query"],
    },
    handler=_semantic_search,
    risk=Risk.SAFE,
)