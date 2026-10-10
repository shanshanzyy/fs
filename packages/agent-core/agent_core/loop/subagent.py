"""主从子 Agent 编排（M3）：父 Agent 派生一个**只读**子 Agent 做探索 / 分析 / 检索。

## 为什么这样做

父 Agent 为了改一处代码，常常要先扫一大片仓库（找调用点、看依赖、读若干文件）。
这些内容大多**用完即弃**，却全部堆进父上下文，把真正该改的那几行挤到一边。
把这类**只读子任务**下沉给一个独立的子 Agent，让它在自己的小上下文里探索、
只把结论交回，父上下文因此保持干净。

## 四条硬边界

1. **只读**：子 Agent 的注册表由 `build_readonly_registry()` 白名单给出，
   结构上没有任何写盘 / 执行工具；另设 `allow_write=False` / `allow_shell=False`
   作纵深防御。写盘一律回到父 Agent 走既有审批漏斗（ADR-008）。
2. **不递归**：子表**不含** `task`，子 Agent 无法再往下派生。
3. **共用审计**：子 Agent 复用父 Agent 的**同一个** `AuditLog` 实例 —— 链的正确性
   建立在「单实例持有 `_last_hash`」之上（见 audit.py），各建一个会让两条链交错，
   `verify_chain` 会把整条链判成篡改。
4. **不污染父视图**：子 Agent 的消息不落会话库、不产生计划、不发增量文本；
   只经 `emit`（即父的 `AgentHooks.on_subagent`）送**进度元数据**事件，正文只随
   `task` 工具返回值（经 `wrap_untrusted` 包裹）回到父上下文。

任何异常都被收敛成 `SubAgentResult(ok=False)`，**绝不**把父任务弄挂。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable

from ..config import Config
from ..sandbox import Risk
from ..tools import build_readonly_registry
from .agent import Agent, AgentHooks
from .prompts import build_subagent_prompt


@dataclass
class SubAgentResult:
    """一次委派的结果。`answer` 是子 Agent 的最终自然语言结论（未包裹）。"""

    ok: bool
    answer: str = ""
    iterations: int = 0
    tool_calls: int = 0
    error: str = ""


class _SubAgentHooks(AgentHooks):
    """把子 Agent 的内部动作压成 `on_subagent` 进度事件。

    **只**转发「进度类」回调（迭代 / 工具调用 / 工具结果 / 提醒）。刻意**不**转发：

      - `on_message` —— 子 Agent 的消息不落会话库
      - `on_context` / `on_plan` —— 不污染父的预算条与计划卡片
      - `on_assistant_delta` / `on_assistant_text` —— 子 Agent 的回答只经工具返回值回父，
        不往父对话流里灌增量文本

    事件里**不带**工具参数正文与工具结果正文（那些是工作区/外部文本，是注入面），
    只带工具名、风险等级与成败标志。
    """

    def __init__(self, emit: Callable[[dict[str, Any]], None], call_id: str) -> None:
        self._emit = emit
        self._call_id = call_id

    def on_iteration(self, index: int, total: int) -> None:
        self._emit(
            {"callId": self._call_id, "phase": "iteration", "index": index, "total": total}
        )

    def on_tool_call(self, call: Any, risk: Risk, reason: str) -> None:
        self._emit(
            {
                "callId": self._call_id,
                "phase": "tool",
                "name": str(getattr(call, "name", "") or ""),
                "risk": risk.value,
            }
        )

    def on_tool_result(self, call: Any, result: Any) -> None:
        self._emit(
            {
                "callId": self._call_id,
                "phase": "tool_result",
                "name": str(getattr(call, "name", "") or ""),
                "ok": bool(getattr(result, "ok", False)),
            }
        )

    def on_notice(self, text: str) -> None:
        # 提醒正文是 Agent 自己生成的短句（如「已达迭代上限」），不是工作区文本，
        # 可以直接送到界面；长度也一并带上，便于界面裁剪。
        self._emit(
            {"callId": self._call_id, "phase": "notice", "text": text, "chars": len(text)}
        )


def run_subagent(
    parent: Agent,
    task_text: str,
    *,
    call_id: str,
    emit: Callable[[dict[str, Any]], None],
) -> SubAgentResult:
    """派生一个只读子 Agent 完成 `task_text`，返回它的结论。

    `emit` 就是父的 `AgentHooks.on_subagent`：`started` / `finished` / `failed`
    三帧由调用方（`Agent._delegate`）负责发，本函数只发子 Agent 内部的进度帧。
    """
    config: Config = parent.config
    child_config = replace(
        config,
        # 同一 session：审计链共用父实例，session 标签保持一致即可。
        # 子 Agent 独立迭代预算 —— 委派是「探索子任务」，不该与父任务同预算。
        max_iterations=max(1, int(config.subagent_max_iterations)),
        # 纵深防御：即便只读白名单将来被改错，这两道闸门也挡住写盘与执行。
        allow_write=False,
        allow_shell=False,
        # 子 Agent 不做自动验证（那是父 Agent 改完文件后的事）。
        verify_command="",
        # 不往父视图推增量文本。
        stream_enabled=False,
        # 不注入跨会话记忆：省 token，也避免把记忆再外发给上游。
        memory_enabled=False,
    )
    registry = build_readonly_registry()
    prompt = build_subagent_prompt(
        workspace=str(child_config.workspace),
        tool_descriptions=registry.describe(),
        native_tools=bool(getattr(parent, "_native_tools", True)),
        project_instructions=parent.project_instructions.text,
    )
    child = Agent(
        config=child_config,
        registry=registry,
        client=parent.client,
        hooks=_SubAgentHooks(emit, call_id),
        # 只读面不会触发审批；万一将来误加了 REVIEW 工具，恒 deny 是失败安全的。
        approver=lambda request: "deny",
        # 中断透传：父被请求中断时，子 Agent 在轮次边界同样停下。
        should_stop=parent.should_stop,
        # 治理策略约束只作用于写/命令面；子 Agent 的只读面由白名单保证。
        constraints=None,
        memory=None,
        # **共用父的审计实例**（关键，见模块头第 3 条）。
        audit=parent.audit,
        system_prompt=prompt,
    )
    try:
        answer = child.run(task_text)
    except Exception as exc:  # noqa: BLE001 — 委派失败不该弄挂父任务
        return SubAgentResult(
            ok=False,
            iterations=child.stats.iterations,
            tool_calls=child.stats.tool_calls,
            error=f"{type(exc).__name__}: {exc}",
        )
    return SubAgentResult(
        ok=True,
        answer=answer,
        iterations=child.stats.iterations,
        tool_calls=child.stats.tool_calls,
    )


__all__ = ["SubAgentResult", "run_subagent"]