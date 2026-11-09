"""工作区多智能体讨论（M14）。

包入口只导出「档案库」：一份工作区级的智能体档案文件 + 三个 HTTP 端点。
讨论引擎（`agents.discussion`，B1）单独成模块，由需要它的调用方直接导入，
入口这里刻意不 import，避免所有使用档案库的场景被动拉起引擎依赖。
"""

from .profile import (
    AgentProfile,
    AgentProfileError,
    readonly_tool_names,
    validate_profile,
)
from .store import AgentStore, default_store_path, store_path_for

__all__ = [
    "AgentProfile",
    "AgentProfileError",
    "AgentStore",
    "default_store_path",
    "readonly_tool_names",
    "store_path_for",
    "validate_profile",
]
