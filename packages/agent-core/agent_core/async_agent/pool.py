"""后台任务池（M9-D）：线程池执行独立的只读 Agent，结果异步回传。

## 线程模型

`ThreadPoolExecutor(max_workers=async_agent_max)`。每个后台任务是一个**独立
`Agent` 实例**（`build_readonly_registry()` 只读工具面），与父会话**共用同一个
`AuditLog` 实例**（`record` 内部有锁，链的正确性由单实例的 `_lock` 保证）。

结果不落盘、不跨重启 —— 本轮只做**进程内**异步（跨重启持久化属后续，见
M9-PLAN「明确不做」）。任务字典由一把锁护住，`submit` / `status` / `result` /
`cancel` 都是线程安全的。

## 三条硬边界

1. **只读**：子任务 Agent 的注册表是 `build_readonly_registry()`，没有写盘 /
   执行工具；`allow_write=False` / `allow_shell=False` 作纵深防御。写回一律
   回父会话走审批（ADR-008）。
2. **用户本人发起**：本池不向 Agent 暴露任何派生工具，只有 host 层的
   `/api/tasks` 端点会 `submit`（对齐 ADR-011）。
3. **审计只记元数据**：`task.spawn` 只记 `task_id` / `state` / `chars` / `ms`
   （计数与摘要长度），任务正文与结论一个字符都不进审计（ADR-006）。
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from ..config import Config
from ..loop.agent import Agent
from ..tools import build_readonly_registry
from ..tools.base import wrap_untrusted

# 后台任务描述上限。与 tools/task.py 的 MAX_TASK_CHARS 同量级：后台任务是
# 「一整段要做的事」，不该无限长。
MAX_ASYNC_TASK_CHARS = 8000


class TaskState:
    """后台任务的生命周期状态（枚举值，与 protocol.py 的事件字段对齐）。"""

    PENDING = "pending"       # 已提交、排队中
    RUNNING = "running"       # 执行中
    SUCCEEDED = "succeeded"   # 正常完成
    FAILED = "failed"         # 异常
    CANCELLED = "cancelled"   # 已取消


@dataclass
class AgentTask:
    """一个后台任务的句柄。`result` 在完成前为 None。"""

    task_id: str
    prompt: str
    state: str = TaskState.PENDING
    # 摘要（给人看的一行）：完成时取结论首行，失败时取错误摘要。
    # **不含任务正文**（ADR-006）。
    summary: str = ""
    result: str | None = None
    error: str = ""
    chars: int = 0          # 结论字符数（成功时）
    iterations: int = 0
    tool_calls: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    # 取消标志：线程安全（set 由 cancel 触发，is_set 由工作线程轮询）。
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    def describe(self) -> dict[str, Any]:
        """回传给界面 / API 的摘要视图。**不含任务正文与结论**。"""
        return {
            "taskId": self.task_id,
            "state": self.state,
            "summary": self.summary,
            "chars": self.chars,
            "iterations": self.iterations,
            "toolCalls": self.tool_calls,
            "error": self.error[:200] if self.error else "",
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "elapsedMs": int((self.finished_at - self.started_at) * 1000)
            if self.finished_at
            else int((time.time() - self.started_at) * 1000),
        }


class AgentTaskPool:
    """后台任务池：submit / status / result / cancel，线程安全。"""

    def __init__(
        self,
        *,
        parent: Agent,
        max_workers: int = 4,
        timeout: int = 600,
        # 状态变化回调（host 层用它发 `task.state` 事件给界面）。
        on_state: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._parent = parent
        self._timeout = max(1, int(timeout))
        self._on_state = on_state
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)), thread_name_prefix="vibe-task"
        )
        self._lock = threading.Lock()
        self._tasks: dict[str, AgentTask] = {}
        self._futures: dict[str, Future] = {}

    # ------------------------------------------------------------------ 查询
    def status(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.describe() if task else None

    def result(self, task_id: str) -> dict[str, Any] | None:
        """返回任务结果（含正文），仅当已完成。未完成返回 None。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.state in (TaskState.PENDING, TaskState.RUNNING):
                return None
            payload = task.describe()
            payload["result"] = task.result
            return payload

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            # 按提交顺序返回（dict 保序）。
            return [t.describe() for t in self._tasks.values()]

    # ------------------------------------------------------------------ 提交
    def submit(self, prompt: str) -> str:
        """提交一个后台任务，返回 task_id。

        非法（空 / 超长）直接抛 ValueError —— 这是 host 层的客户端错误，
        不该进任务池。
        """
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("后台任务描述不能为空")
        if len(prompt) > MAX_ASYNC_TASK_CHARS:
            raise ValueError(
                f"后台任务描述过长（{len(prompt)} 字符，上限 {MAX_ASYNC_TASK_CHARS}）"
            )
        task_id = uuid.uuid4().hex[:12]
        task = AgentTask(task_id=task_id, prompt=prompt)
        with self._lock:
            self._tasks[task_id] = task
        self._emit(task, "pending")
        future = self._executor.submit(self._run, task)
        with self._lock:
            self._futures[task_id] = future
        return task_id

    # ------------------------------------------------------------------ 取消
    def cancel(self, task_id: str) -> bool:
        """请求取消一个任务。已完成 / 不存在返回 False。

        取消是**协作式**的：置取消标志，工作线程在轮次边界检查并停下
        （与主 Agent 的 `should_stop` 同范式）。不强行 kill 线程 ——
        那样会留下残缺的 Agent 内部状态。
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.state in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED):
                return False
            task._cancel_event.set()
        return True

    def shutdown(self) -> None:
        """关闭线程池（会话收尾时调用）。不等待未完成任务 —— 进程内任务
        随会话一起结束。"""
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ 内部
    def _run(self, task: AgentTask) -> None:
        started = time.time()
        self._set_state(task, TaskState.RUNNING)
        # 审计只记元数据（task_id 归属 + 计数），任务正文一个字符不进审计。
        self._parent.audit.record(
            "task.spawn",
            task_id=task.task_id,
            state="running",
            chars=len(task.prompt),
            ms=0,
        )
        try:
            answer = self._run_agent(task)
            task.chars = len(answer)
            task.iterations = 0
            task.tool_calls = 0
            task.summary = _summarize(answer)
            # 结论是工作区文本的再加工，按 ADR-006 当不可信数据交给界面。
            task.result = wrap_untrusted("async_task", answer)
            task.finished_at = time.time()
            self._set_state(task, TaskState.SUCCEEDED)
            self._parent.audit.record(
                "task.spawn",
                task_id=task.task_id,
                state="succeeded",
                chars=task.chars,
                ms=int((task.finished_at - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 — 后台任务失败不该弄挂主会话
            task.error = f"{type(exc).__name__}: {exc}"
            task.summary = task.error[:200]
            task.finished_at = time.time()
            self._set_state(task, TaskState.FAILED)
            self._parent.audit.record(
                "task.spawn",
                task_id=task.task_id,
                state="failed",
                chars=len(task.error),
                ms=int((task.finished_at - started) * 1000),
            )

    def _run_agent(self, task: AgentTask) -> str:
        """构造一个只读 Agent 跑一次后台任务，返回结论。

        只读性由 `build_readonly_registry()` 白名单从结构上保证（工具不存在，
        模型就选不到），`allow_write=False` / `allow_shell=False` 作纵深防御。
        """
        parent = self._parent
        config: Config = parent.config
        child_config = replace(
            config,
            # 独立迭代预算：后台任务是一次完整任务，不该与父会话同预算。
            max_iterations=max(1, int(config.max_iterations)),
            allow_write=False,
            allow_shell=False,
            # 后台任务不做自动验证 / 测试闭环（那是父会话改完文件后的事）。
            verify_command="",
            test_loop_enabled=False,
            # 不注入跨会话记忆：省 token，也避免把记忆再外发给上游。
            memory_enabled=False,
            # 子任务不派子 Agent、不加载 Skill 工作流 —— 保持只读、无递归。
            subagent_enabled=False,
            skills_enabled=False,
            stream_enabled=False,
        )
        registry = build_readonly_registry()
        child = Agent(
            config=child_config,
            registry=registry,
            client=parent.client,
            # 只读面不会触发审批；万一将来误加 REVIEW 工具，恒 deny 是失败安全。
            approver=lambda request: "deny",
            # 取消透传：任务被 cancel 时，后台 Agent 在轮次边界停下。
            should_stop=task._cancel_event.is_set,
            constraints=None,
            memory=None,
            # **共用父的审计实例**（关键，见模块头与 audit.py 的锁）。
            audit=parent.audit,
        )
        answer = child.run(task.prompt)
        task.iterations = child.stats.iterations
        task.tool_calls = child.stats.tool_calls
        return answer

    def _set_state(self, task: AgentTask, state: str) -> None:
        with self._lock:
            task.state = state
        self._emit(task, state)

    def _emit(self, task: AgentTask, state: str) -> None:
        if self._on_state is None:
            return
        self._on_state({"taskId": task.task_id, "state": state, "summary": task.summary})


def _summarize(text: str, limit: int = 160) -> str:
    """取结论首行做一行摘要（给人看，不进审计正文）。"""
    first = text.strip().splitlines()[0] if text.strip() else ""
    one_line = " ".join(first.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


__all__ = ["AgentTask", "AgentTaskPool", "TaskState", "MAX_ASYNC_TASK_CHARS"]
