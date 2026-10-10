"""IDE 内测试树（K / P2-14）：收集用例、跑用例、解析结构化结果。"""

from .runner import (
    COLLECT_TIMEOUT,
    MAX_CASES,
    RUN_TIMEOUT,
    collect,
    parse_junit,
    run,
)

__all__ = ["COLLECT_TIMEOUT", "MAX_CASES", "RUN_TIMEOUT", "collect", "parse_junit", "run"]