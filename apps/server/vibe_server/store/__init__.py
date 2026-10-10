"""存储工厂。

`open_store()` 按 URL 前缀分派。对未实现的驱动**明确报错**，
不静默退回 SQLite —— 静默降级会让「以为在用集群库、实际数据写在本机文件里」
这种事故悄无声息地发生。

`postgres://` 走自研的纯标准库驱动（`store/pg.py` + `store/pgproto.py`），
不引入任何第三方 DB 客户端 —— 这是服务端「零依赖、内网离线可跑」红线的延续。
"""

from __future__ import annotations

from pathlib import Path

from ..errors import ConfigError
from .base import Row, Store

# 共享存储的 scheme 别名（都指向同一个 Postgres 驱动）。
_POSTGRES_SCHEMES = ("postgres", "postgresql", "psql")
SUPPORTED_SCHEMES = ("sqlite", "file", *_POSTGRES_SCHEMES)


def open_store(target: str | Path, *, connect: bool = True) -> Store:
    """打开存储。target 可以是路径，也可以是 `scheme://...` 形式的 URL。"""
    raw = str(target)

    scheme = ""
    rest = raw
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
        scheme = scheme.lower()

    # 共享存储：多实例共用同一个 Postgres，配额/策略/审计才有一致真相。
    if scheme in _POSTGRES_SCHEMES:
        from .pg import PostgresStore

        store: Store = PostgresStore(raw)
        if connect:
            store.init_schema()
        return store

    if scheme in {"redis", "rediss"}:
        raise ConfigError(
            "Redis 在本项目里不是必需组件。限流与配额计数落在共享数据库的\n"
            "  rate_counters / usage_counters 表上，省掉一个需要单独运维的有状态服务。\n"
            "  要多实例部署时，把 VIBE_SRV_DB 指到同一个 Postgres 即可。"
        )
    if scheme and scheme not in SUPPORTED_SCHEMES:
        raise ConfigError(
            f"不支持的存储 scheme: {scheme!r}（支持: {', '.join(SUPPORTED_SCHEMES)}）"
        )

    # sqlite:///E:/x.db 这类写法会多出一个前导斜杠，Windows 下要还原
    if scheme == "sqlite":
        path = rest.lstrip("/")
    elif scheme:
        path = rest
    else:
        path = raw

    from .sqlite import SQLiteStore

    store = SQLiteStore(path)
    if connect:
        store.init_schema()
    return store


__all__ = ["Row", "Store", "SUPPORTED_SCHEMES", "open_store"]