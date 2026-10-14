"""SearXNG 搜索中转（M7）。

客户端不直连搜索上游 —— 上游地址只活在服务端，客户端零密钥。
V1 仅 SearXNG JSON 形态：GET ``<search_url>?q=<query>&format=json``，
内网自部署、零密钥，egress 不出内网；Bing/博识等需要密钥的上游列为 V2
（届时密钥走服务端 file/Vault 托管，与 LLM key 同法）。

失败语义与 LLM 网关一致：不可达 / 非 JSON / 上游报错 = 明确 502，
不静默降级到假实现 —— 客户端宁可报错也让用户知道检索没生效。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from ..errors import ApiError

# 与客户端 tools/web.py 的 _MAX_QUERY_CHARS 同口径：查询词长到离谱
# 通常意味着把一段日志/代码当检索式塞进来了，直接拒。
MAX_QUERY_CHARS = 400
# 结果条数上限：防止单次请求拉回几 MB 的结果列表。
MAX_RESULTS_CAP = 20
# 搜索是快请求，不需要 LLM 网关那种 300 秒超时。
DEFAULT_TIMEOUT = 10


def searxng_search(
    search_url: str, query: str, max_results: int, timeout: int = DEFAULT_TIMEOUT
) -> list[dict]:
    """调 SearXNG JSON 端点，返回 ``[{title, url, content}]``。

    只在**这里**碰上游；上游返回的任何字段都先过一遍白名单清洗 ——
    SearXNG 的响应字段随部署而变，照单全收等于让上游决定服务端
    回给客户端的 JSON 形状。
    """
    endpoint = f"{search_url.rstrip('/')}?{urllib.parse.urlencode({'q': query, 'format': 'json'})}"
    request = urllib.request.Request(
        endpoint,
        headers={"Accept": "application/json", "User-Agent": "vibe-governance/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(4_000_000)
            status = response.status
    except urllib.error.HTTPError as exc:
        raise ApiError(
            502,
            "search_upstream_error",
            "搜索上游返回错误",
            details={"status": exc.code},
        ) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ApiError(
            502, "search_upstream_error", "搜索上游不可达"
        ) from exc

    if status != 200:
        raise ApiError(
            502, "search_upstream_error", "搜索上游返回错误", details={"status": status}
        )

    try:
        document = json.loads(raw.decode("utf-8", errors="replace"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(
            502, "search_upstream_error", "搜索上游返回的不是合法 JSON"
        ) from exc

    raw_results = document.get("results") if isinstance(document, dict) else None
    if not isinstance(raw_results, list):
        raise ApiError(
            502, "search_upstream_error", "搜索上游响应缺少 results 数组"
        )

    # 字段白名单 + 类型收敛：title/url/content 一律转字符串。
    # 只回客户端需要的三个字段，不透传上游的其余字段。
    cleaned: list[dict] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        cleaned.append(
            {
                "title": str(item.get("title") or "").strip(),
                "url": url,
                "content": str(item.get("content") or "").strip(),
            }
        )
        if len(cleaned) >= max_results:
            break
    return cleaned


__all__ = ["DEFAULT_TIMEOUT", "MAX_QUERY_CHARS", "MAX_RESULTS_CAP", "searxng_search"]
