"""定时任务调度（M10）：用户本人创建、到点投只读后台任务、落盘 + 重启补跑。

与 M9-D「后台 / 并行 Agent」的关系：本模块**不重造任务池**，只在其上加一层
**定时 / 周期触发**。触发动作**只有一种** —— 把 job 的 `prompt` 投进
`async_agent.AgentTaskPool`（只读工具面、共用父审计链，ADR-032 沿用）。

设计边界（见 M10-PLAN §1.1 与 ADR-035）：
  1. **用户本人创建** —— 不向 Agent 暴露任何调度工具（对齐 ADR-011），堵死
     「Agent 自己给自己排期」的越权 / 递归路径。
  2. **只投只读后台任务** —— 不新增 shell 通道、不新增可写盘通道。
  3. **落盘 + 重启补跑** —— 定义落 `<workspace>/.vibe/scheduler.db`，进程重启后
     恢复定义、对逾期 job **各补跑一次**（catch-up once）。
  4. **审计只记元数据** —— `schedule.job` 只记 `job_id / action / state /
     chars / ms`，**prompt 正文一个字符都不出机**（ADR-006）。
  5. **纯标准库** —— cron 自写、落盘用 `sqlite3`、并发用 `threading`，零第三方依赖。
"""

from .cron import ScheduleError, parse_schedule
from .model import (
    CronSchedule,
    IntervalSchedule,
    JobSpec,
    OnceSchedule,
    Schedule,
)
from .runner import MIN_TICK_SECONDS, Scheduler
from .store import JobStore

__all__ = [
    "JobSpec",
    "Schedule",
    "CronSchedule",
    "IntervalSchedule",
    "OnceSchedule",
    "ScheduleError",
    "parse_schedule",
    "JobStore",
    "Scheduler",
    "MIN_TICK_SECONDS",
]
