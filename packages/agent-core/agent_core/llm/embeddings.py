"""可选的向量后端（B4）：OpenAI 兼容 `POST /embeddings`。

「可选」在这里有两层含义，都要保证：
1. 没配 `VIBE_EMBED_MODEL` → 调用方压根不构造它，走纯词法；
2. 配了但端点不可达 → `embed()` 返回 None，调用方**原地回退词法**，不抛错。

因此这里刻意不抛 LLMError 给上层：语义检索是「锦上添花」的一步，
向量端点抖动导致整个 `semantic_search` 失败，是本末倒置。

复用 `openai_compat._build_opener`：本地地址绕过系统代理这条经验，
在 embedding 上同样成立，而且踩坑表现一模一样（连不上 127.0.0.1）。
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request

from .openai_compat import _build_opener


def _endpoint(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    if not base.endswith("/v1") and "/v1" not in base:
        return f"{base}/v1{path}"
    return f"{base}{path}"


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度。维度不一致视为不可比，返回 0.0（当次不参与重排）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


class EmbeddingClient:
    """极简 embedding 客户端。失败一律返回 None / (False, 原因)。"""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: int = 30,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout = max(1, timeout)
        self._opener = _build_opener(base_url)

    # ------------------------------------------------------------------ 接口
    def probe(self) -> tuple[bool, str]:
        """用一次极小请求判定可用性。不吞掉「为什么不可用」。"""
        if not self.model:
            return False, "未配置 embedding 模型（VIBE_EMBED_MODEL）"
        vectors = self.embed(["probe"])
        if vectors is None:
            return False, f"向量端点不可用或模型 {self.model!r} 不存在"
        return True, f"向量端点可用，维度 {len(vectors[0])}"

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """返回与 texts 等长的向量列表；任何异常/形状不符都返回 None。"""
        if not texts:
            return []
        payload = {"model": self.model, "input": list(texts)}
        url = _endpoint(self.base_url, "/embeddings")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, headers=self._headers(), method="POST"
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw.strip() else {}
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
            return None

        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or len(items) != len(texts):
            return None

        vectors: list[list[float]] = []
        for item in items:
            vector = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(vector, list) or not vector:
                return None
            try:
                vectors.append([float(value) for value in vector])
            except (TypeError, ValueError):
                return None
        return vectors

    # ------------------------------------------------------------------ HTTP
    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "vibe-agent-core/0.1",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers