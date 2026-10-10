#!/usr/bin/env python3
"""把单机 SQLite 治理库搬到共享 Postgres（单向）。

用法（在 `apps/server` 目录下）：

    # 先看看要搬多少行，不写目标
    python scripts/migrate_sqlite_to_pg.py .vibe-governance/governance.db \\
        --dsn postgres://user:pass@pg.internal:5432/vibe --dry-run

    # 真搬（幂等，可重跑；半程失败直接再跑一次）
    python scripts/migrate_sqlite_to_pg.py .vibe-governance/governance.db \\
        --dsn postgres://user:pass@pg.internal:5432/vibe

`--dsn` 省略时读环境变量 `VIBE_SRV_DB`。退出码：0 成功且逐表计数对得上；
1 计数对不上或连接失败。

纪律见 `vibe_server/store/migrate.py`：单向、审计表整表顺序搬、可重跑幂等。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 让脚本能直接 import vibe_server：把 apps/server 放进 sys.path。
_SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVER_ROOT))

from vibe_server.errors import StoreError  # noqa: E402
from vibe_server.store.migrate import PostgresTarget, migrate  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_sqlite_to_pg",
        description="把 SQLite 治理库单向迁移到 Postgres（审计表整表顺序搬）",
    )
    parser.add_argument("sqlite", help="源 SQLite 数据库文件路径")
    parser.add_argument(
        "--dsn",
        default=os.environ.get("VIBE_SRV_DB", ""),
        help="目标 Postgres DSN（默认取环境变量 VIBE_SRV_DB）",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只统计源库行数，不写目标"
    )
    return parser


def _print_report(source: dict[str, int], target: dict[str, int], dry_run: bool) -> bool:
    print(f"{'表':<20}{'源':>10}{'目标':>10}")
    print("-" * 40)
    ok = True
    for name, count in source.items():
        tgt = target.get(name, 0)
        flag = "" if (dry_run or count == tgt) else "  ← 不一致"
        if not dry_run and count != tgt:
            ok = False
        print(f"{name:<20}{count:>10}{tgt:>10}{flag}")
    return ok


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv if argv is not None else sys.argv[1:])

    source_path = Path(args.sqlite)
    if not source_path.exists():
        print(f"源库不存在：{source_path}", file=sys.stderr)
        return 1
    dsn = (args.dsn or "").strip()
    if not dsn:
        print(
            "缺少目标 DSN。用 --dsn postgres://… 指定，或设置环境变量 VIBE_SRV_DB。",
            file=sys.stderr,
        )
        return 1

    target = PostgresTarget(dsn)
    try:
        report = migrate(source_path, target, dry_run=args.dry_run)
    except StoreError as exc:
        print(f"迁移失败：{exc}", file=sys.stderr)
        return 1
    finally:
        target.close()

    print("（dry-run：仅统计源库，未写目标）" if args.dry_run else "迁移完成。")
    ok = _print_report(report.source, report.target, report.dry_run)
    if args.dry_run:
        return 0
    if report.ok:
        print("\n逐表计数对拍一致。")
        return 0
    print("\n计数对拍不一致，请检查。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())