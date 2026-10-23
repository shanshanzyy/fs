"""Skill / 斜杠命令（M9-A）—— 把「怎么做某类任务」固化成可复用工作流。

Skill 与 `task` 子 Agent 的本质区别：`task` 是「临时派一个只读探索员」，
Skill 是「可沉淀、带描述、按需加载的高阶工作流」。Skill 内部仍走既有工具
（edit_file / run_command 等），自身**不新增任何底层工具、不新增写盘路径**。
"""

from __future__ import annotations

from .loader import SkillSpec, load_skills

__all__ = ["SkillSpec", "load_skills"]
