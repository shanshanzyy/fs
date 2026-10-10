"""Repo Wiki（M3+）：把仓库结构渲染成落盘的 markdown。

对外只需要 `generate_wiki()`：调用方（`tools/repo_wiki.py`）拿到 `WikiBundle`
后，逐文件走结构化补丁 + 审批落盘 —— 生成与落盘是分开的两步，便于审批预演
在不产生副作用的前提下算出完整产物。
"""

from .generate import WikiBundle, WikiPage, generate_wiki, module_of

__all__ = [
    "WikiBundle",
    "WikiPage",
    "generate_wiki",
    "module_of",
]