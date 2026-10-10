"""update_plan（P2-12）：让 Agent 把「打算怎么做」写成结构化步骤，界面据此可视化。

## 为什么是一个工具，而不是解析自然语言

从回复里正则抽计划，失败率随措辞变化而剧烈波动，而失败是**静默**的
（抽不出来就是没有计划，没人会发现）。让模型显式调用本工具，计划就成了
一份结构化的契约：步骤、状态都可断言，也和既有的「工具即契约」架构一致。

## 三条边界

1. **纯内存**：计划是运行态，只存在 `Agent.plan` 里，不落盘、不进会话库。
   「跨会话记住上次的计划」不在本阶段范围内（见计划 §4「明确不做」）。
2. **不进审批**：`Risk.SAFE`，它不改工作区、不执行命令，只是把计划交给会话层。
3. **幂等全量替换**：每次调用给出**完整**步骤表，而不是增量打补丁 ——
   增量更新会让「模型漏说一步」直接变成界面上的幽灵步骤，而全量替换下
   模型只要照抄上一步的列表即可，代价只是多几十个 token。
"""

from __future__ import annotations

from typing import Any

from ..errors import ToolError
from ..sandbox import Risk
from .base import ToolContext, ToolResult, ToolSpec

# 步骤条数上限。计划是给人看的清单，超过这个数量就不再是「计划」而是待办清单，
# 界面也放不下；20 条足够覆盖「先探索再改再验证」这类任务。
MAX_PLAN_STEPS = 20
# 单步文本上限。界面一行放不下会折行，但上限必须有 —— 挡住「把整段代码当步骤」。
MAX_STEP_TEXT = 200
# 步骤 id 上限（模型给的短标识，用于人工勾选时定位）。
MAX_STEP_ID = 40

PLAN_STATUSES = ("pending", "in_progress", "done", "skipped")

_STATUS_LABEL = {
    "pending": "待办",
    "in_progress": "进行中",
    "done": "完成",
    "skipped": "跳过",
}
_STATUS_ICON = {
    "pending": "○",
    "in_progress": "◐",
    "done": "●",
    "skipped": "◌",
}


def normalize_steps(raw: Any) -> list[dict[str, str]]:
    """校验并归一化 `steps`。任何一条不合法都整批拒绝。

    整批拒绝而不是「跳过坏的那条」：计划是一份**完整**的叙述，悄悄丢掉一条
    会让界面上的计划与模型心里的计划不一致 —— 而这正是本工具要消灭的问题。
    """
    if raw is None:
        raise ToolError("缺少 steps 参数：请给出完整的步骤列表（可为空数组表示清空计划）")
    if not isinstance(raw, list):
        raise ToolError(f"steps 必须是数组，实际收到 {type(raw).__name__}")
    if len(raw) > MAX_PLAN_STEPS:
        raise ToolError(
            f"计划步骤过多（{len(raw)} 条，上限 {MAX_PLAN_STEPS} 条）。"
            "请把任务拆得更粗一些，或先只列出当前这一轮要做的事"
        )

    steps: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ToolError(f"第 {index} 步必须是对象，实际收到 {type(item).__name__}")
        text = str(item.get("text") or "").strip()
        if not text:
            raise ToolError(f"第 {index} 步缺少 text（这一步要做什么）")
        if len(text) > MAX_STEP_TEXT:
            raise ToolError(
                f"第 {index} 步的 text 过长（{len(text)} 字符，上限 {MAX_STEP_TEXT}）"
            )
        status = str(item.get("status") or "pending").strip().lower()
        if status not in PLAN_STATUSES:
            raise ToolError(
                f"第 {index} 步的 status 非法：{status!r}。"
                f"可选值：{'/'.join(PLAN_STATUSES)}"
            )
        step_id = str(item.get("id") or "").strip() or f"s{index}"
        if len(step_id) > MAX_STEP_ID:
            raise ToolError(f"第 {index} 步的 id 过长（上限 {MAX_STEP_ID} 字符）")
        if step_id in seen:
            raise ToolError(f"步骤 id 重复：{step_id!r}（id 用于人工勾选时定位，必须唯一）")
        seen.add(step_id)
        steps.append({"id": step_id, "text": text, "status": status})
    return steps


def _render(steps: list[dict[str, str]]) -> str:
    if not steps:
        return "[update_plan] 已清空计划（界面不再显示计划卡片）。"
    counts = {name: 0 for name in PLAN_STATUSES}
    for step in steps:
        counts[step["status"]] += 1
    tail = "，".join(
        f"{_STATUS_LABEL[name]} {counts[name]}" for name in PLAN_STATUSES if counts[name]
    )
    lines = [f"[update_plan] 已更新计划：共 {len(steps)} 步（{tail}）。"]
    for step in steps:
        lines.append(f"  {_STATUS_ICON[step['status']]} {step['text']}")
    lines.append("每完成一步就再调用一次本工具更新状态，让界面上的进度保持真实。")
    return "\n".join(lines)


def _update_plan(ctx: ToolContext, args: dict) -> ToolResult:
    steps = normalize_steps(args.get("steps"))
    revision = -1
    if ctx.plan_sink is not None:
        revision = ctx.plan_sink(steps)
    counts = {f"{name}_count": 0 for name in PLAN_STATUSES}
    for step in steps:
        counts[f"{step['status']}_count"] += 1
    return ToolResult(
        ok=True,
        content=_render(steps),
        meta={"steps": len(steps), "revision": revision, **counts},
    )


UPDATE_PLAN_SPEC = ToolSpec(
    name="update_plan",
    description=(
        "把「这次任务打算怎么做」写成结构化步骤表，界面会把它渲染成可勾选的计划卡片。\n"
        "参数 steps 是**完整**的步骤列表（全量替换，不是增量）："
        "每一项 {id, text, status}，status ∈ pending/in_progress/done/skipped。\n"
        "多步任务开工前**先列一次计划**，之后每完成一步**再调用一次**并更新对应状态 ——"
        "这样用户能随时看到进度，而不是等最后一句总结。\n"
        "传空数组表示清空计划。纯内存操作：不改文件、不执行命令、不需要审批。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "description": (
                    "完整的步骤列表；每项 {id, text, status}。"
                    f"最多 {MAX_PLAN_STEPS} 条，text 最多 {MAX_STEP_TEXT} 字符"
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "短标识（唯一），用于后续引用与人工勾选，如 \"1\"",
                        },
                        "text": {"type": "string", "description": "这一步要做什么（一句话）"},
                        "status": {
                            "type": "string",
                            "enum": list(PLAN_STATUSES),
                            "description": "pending 待办 / in_progress 进行中 / done 完成 / skipped 跳过",
                        },
                    },
                    "required": ["text"],
                },
            },
        },
        "required": ["steps"],
    },
    handler=_update_plan,
    risk=Risk.SAFE,
)


__all__ = [
    "MAX_PLAN_STEPS",
    "MAX_STEP_ID",
    "MAX_STEP_TEXT",
    "PLAN_STATUSES",
    "UPDATE_PLAN_SPEC",
    "normalize_steps",
]