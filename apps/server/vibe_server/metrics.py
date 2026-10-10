"""进程内指标（M4）。

`/metrics` 的意义是让「服务健康吗、慢在哪、错在哪」不必靠翻日志。
三条设计取舍，每条都是刻意的：

1. **进程内计数，不查库。** 监控系统是高频拉取的（通常 15 秒一次），
   每次 `/metrics` 都去聚合 SQLite 等于让监控本身成为数据库的负载来源。
   代价是**多实例部署时每个实例只报自己的量** —— 而「跨实例求和」本来就是
   Prometheus 的职责，不该由被监控对象来做。
2. **标签里没有用户维度。** `/metrics` 与 `/healthz` 一样无需认证（爬取方便），
   把 user_id / device_id 放进标签等于把「谁在用、用了多少」暴露给任何
   能访问到该端口的人。要按人看用量，走 `/api/v1/usage`，那里有 RBAC。
3. **标签值用「路由模板」而不是原始路径。** 路由是有限集合，而路径参数
   （`/devices/{deviceId}/revoke`）是无限的。直接拿原始路径当标签会造出
   无界基数，把监控系统自己打爆 —— 这是标签爆炸的经典翻车方式。
"""

from __future__ import annotations

import threading
from typing import Any

#: 状态码标签只按「响应码本身」聚合，不按接口 × 状态码的笛卡尔积。
_MAX_ROUTES = 200


class Metrics:
    """一次进程生命周期内的请求计数。线程安全（HTTP 层是多线程的）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[str, int] = {}          # status code -> count
        self._routes: dict[tuple[str, str], int] = {}  # (method, template) -> count
        self._latency_sum_ms: float = 0.0
        self._latency_count: int = 0
        self._route_latency: dict[str, float] = {}    # template -> 累计毫秒
        self._route_calls: dict[str, int] = {}
        self._overloaded = False

    def observe(self, *, method: str, template: str, status: int, elapsed_ms: float) -> None:
        """记录一次请求。`template` 是路由模板（如 `/api/v1/devices/{deviceId}/revoke`）。"""
        label = template or "(unmatched)"
        # 状态码先归一到整数：错误分支拿到的可能是 `HTTPStatus.NOT_FOUND`
        # 这个枚举（`errors.ApiError` 就是这么构造的），而 `str(枚举)` 在
        # Python 3.11 之前是 `HTTPStatus.NOT_FOUND` 而不是 `404`。
        # 不归一的话同一个 404 会分裂成两个标签值，告警规则按 `code="404"`
        # 写就会漏掉一半请求 —— 而且只在老版本 Python 上漏，很难发现。
        code = str(int(status))
        with self._lock:
            self._requests[code] = self._requests.get(code, 0) + 1
            if label not in self._route_calls and len(self._route_calls) >= _MAX_ROUTES:
                # 基数保护：宁可把新出现的路由归到 other，也不让标签集合无界增长。
                # 真触发到这一步说明路由表本身出了问题，/metrics 里能看出来。
                label = "(other)"
                self._overloaded = True
            key = (method, label)
            self._routes[key] = self._routes.get(key, 0) + 1
            self._latency_sum_ms += elapsed_ms
            self._latency_count += 1
            self._route_latency[label] = self._route_latency.get(label, 0.0) + elapsed_ms
            self._route_calls[label] = self._route_calls.get(label, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "requests": dict(self._requests),
                "routes": dict(self._routes),
                "latencySumMs": self._latency_sum_ms,
                "latencyCount": self._latency_count,
                "routeLatencyMs": dict(self._route_latency),
                "routeCalls": dict(self._route_calls),
                "labelOverflow": self._overloaded,
            }


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_prometheus(
    snapshot: dict[str, Any],
    *,
    version: str,
    uptime_seconds: int,
    store_ok: bool,
) -> str:
    """把快照渲染成 Prometheus 文本格式（version=0.0.4）。

    手工渲染而不是引一个客户端库：`dependencies` 必须保持为空
    （内网离线装不上依赖也要能起），而这个格式本身只有几行。
    """
    lines: list[str] = []

    def head(name: str, kind: str, help_text: str) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")

    head("vibe_build_info", "gauge", "构建信息，值恒为 1")
    lines.append(f'vibe_build_info{{version="{_escape(version)}"}} 1')

    head("vibe_uptime_seconds", "gauge", "进程已运行秒数")
    lines.append(f"vibe_uptime_seconds {int(uptime_seconds)}")

    head("vibe_store_up", "gauge", "存储可用性，1=可用，0=不可用")
    lines.append(f"vibe_store_up {1 if store_ok else 0}")

    head("vibe_requests_total", "counter", "HTTP 请求总数（按响应码）")
    for code, count in sorted(snapshot["requests"].items()):
        lines.append(f'vibe_requests_total{{code="{code}"}} {count}')

    head("vibe_route_requests_total", "counter", "HTTP 请求总数（按路由模板）")
    for (method, template), count in sorted(snapshot["routes"].items()):
        lines.append(
            f'vibe_route_requests_total{{method="{_escape(method)}",'
            f'route="{_escape(template)}"}} {count}'
        )

    head("vibe_request_duration_ms_total", "counter", "请求耗时累计（毫秒）")
    lines.append(f"vibe_request_duration_ms_total {snapshot['latencySumMs']:.3f}")

    head("vibe_request_duration_ms_count", "counter", "被计时的请求数")
    lines.append(f"vibe_request_duration_ms_count {snapshot['latencyCount']}")

    # 平均值单独给一条：告警规则里写 avg 比让看板做除法更不容易写错
    head("vibe_request_duration_ms_avg", "gauge", "平均请求耗时（毫秒）")
    count = snapshot["latencyCount"] or 0
    average = (snapshot["latencySumMs"] / count) if count else 0.0
    lines.append(f"vibe_request_duration_ms_avg {average:.3f}")

    # 429 = 配额/限流拒绝。它是「有人在被打回」的直接信号，单独给一条，
    # 否则只能从 status 维度里挑，告警规则容易漏。
    head("vibe_quota_rejections_total", "counter", "被配额/限流拒绝的请求数（429）")
    lines.append(f"vibe_quota_rejections_total {snapshot['requests'].get('429', 0)}")

    if snapshot["labelOverflow"]:
        head("vibe_metrics_label_overflow", "gauge", "路由标签集合是否已溢出（1=已溢出）")
        lines.append("vibe_metrics_label_overflow 1")

    lines.append("")
    return "\n".join(lines)


__all__ = ["Metrics", "render_prometheus"]