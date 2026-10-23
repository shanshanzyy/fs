"""后台 / 并行 Agent（M9-D）：用户本人发起、异步执行、结果异步回传。

与 M3 的 `task` 子 Agent 的区别：
  - `task` 是父 Agent **同步派生**的只读探索员（父等它交回结论）；
  - 本模块是**用户本人**经界面发起的**后台任务**（用户发起后去做别的，
    结果异步回传），两者并存、互不冲突。

设计边界（见 docs/M9-PLAN.md §D 与 ADR-028）：
  1. **用户本人发起** —— 不新增 Agent 发起的派生工具，规避「Agent 自己派
     Agent」的越权 / 递归风险（对齐 ADR-011「用户本人动作」）。
  2. **只读工具面** —— 子任务 Agent 用 `build_readonly_registry()`，结构上
     没有写盘 / 执行能力；写回仍回父会话走审批漏斗（ADR-008）。
  3. **审计链共用** —— 后台任务复用父会话的同一个 `AuditLog` 实例，
     `record` 内部有锁、线程安全（见 audit.py）。
  4. **纯标准库线程池** —— `concurrent.futures.ThreadPoolExecutor`，
     零第三方依赖。
"""

from .pool import AgentTask, AgentTaskPool, TaskState

__all__ = ["AgentTask", "AgentTaskPool", "TaskState"]
