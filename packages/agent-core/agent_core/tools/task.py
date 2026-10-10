"""task（M3）：主 Agent 派生一个**只读**子 Agent 做探索 / 分析 / 检索。

## 为什么是一个工具

父 Agent 为了改一处代码，常常要先扫一大片仓库（找调用点、看依赖、读若干文件）。
这些内容大多**用完即弃**，却全部堆进父上下文，把真正该改的那几行挤到一边。
`task` 把这类只读子任务下沉给一个独立子 Agent，让它在自己的小上下文里探索、
只把结论交回。做成工具而不是内置行为，是因为「什么时候值得派子 Agent」
是一个需要模型判断的决策 —— 交给模型，而不是写死在循环里。

## 三条边界

1. **只读委派**：`Risk.SAFE`。子 Agent 的注册表由 `build_readonly_registry()`
   白名单给出，结构上没有写盘 / 执行能力，因此本工具**不进审批**。
   写盘一律回到父 Agent 走既有审批漏斗（ADR-008）。
2. **结果恒不可信**：子 Agent 的结论是对工作区 / 外部文本的再加工，天然是
   提示词注入面（它可能读到了恶意注释）。返回值恒经 `wrap_untrusted("subagent", ...)`
   包裹，父 Agent 得到的是一块**数据**，不是指令。
3. **无 sink 即明确失败**：直接调 registry 的场景（测试）没有 `subagent_sink`，
   工具如实报「未启用」，而不是假装成功返回空结论。

## 与 `update_plan` 的分工

`update_plan` 是父 Agent 对**自己**的会话状态的操作；`task` 是父 Agent 对
**子 Agent** 的委派。两者都不落盘、都不进审批，但前者改会话状态、后者只读探索。
"""

from __future__ import annotations

from ..errors import ToolError
from ..sandbox import Risk
from .base import ToolContext, ToolResult, ToolSpec, wrap_untrusted

# 子任务描述上限。超过这个长度就不再是「一个子任务的说明」而是任务本身，
# 那样就该由父 Agent 自己做，而不是绕一层子 Agent。
MAX_TASK_CHARS = 4000
# 简述（给人看的一行标签）上限。
MAX_DESCRIPTION_CHARS = 200


def normalize_task(args: dict) -> tuple[str, str]:
    """校验并归一化参数，返回 `(task, description)`。非法即 `ToolError`。

    整批拒绝而非「截断后照做」：`task` 是子 Agent 收到的**唯一**指令，
    悄悄截断会让它执行一个不完整的任务，而失败又是静默的。
    """
    task = str(args.get("task") or "").strip()
    if not task:
        raise ToolError(
            "缺少 task 参数：请说明要子 Agent 探索 / 分析什么"
            "（它只能读取与检索，不能改文件、不能执行命令）"
        )
    if len(task) > MAX_TASK_CHARS:
        raise ToolError(
            f"task 过长（{len(task)} 字符，上限 {MAX_TASK_CHARS}）。"
            "请把子任务收敛到一次探索能完成的范围"
        )
    description = str(args.get("description") or "").strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise ToolError(
            f"description 过长（{len(description)} 字符，上限 {MAX_DESCRIPTION_CHARS}）"
        )
    return task, description


def _task(ctx: ToolContext, args: dict) -> ToolResult:
    task, description = normalize_task(args)
    if ctx.subagent_sink is None:
        return ToolResult(
            ok=False,
            content=(
                "子 Agent 未启用（VIBE_SUBAGENT=0），本次无法委派探索任务。\n"
                "请自己用 repo_map / grep / read_file 完成探索。"
            ),
        )
    result = ctx.subagent_sink(task)
    if not result.ok:
        # 委派失败是父任务的一个**可恢复**挫折，不是父任务的失败：
        # 原样把失败回灌，父 Agent 会自行决定改做还是换法。
        return result
    meta = dict(result.meta)
    if description:
        meta["description"] = description
    # 只包正文：结论是工作区文本的再加工，按 ADR-006 当不可信数据对待。
    return ToolResult(
        ok=True,
        content=wrap_untrusted("subagent", result.content),
        meta=meta,
    )


TASK_SPEC = ToolSpec(
    name="task",
    description=(
        "派生一个**只读**子 Agent 去完成一次探索 / 分析 / 检索子任务，只把结论交回。\n"
        "适合「为了改这一处，得先扫一大片仓库」的场景：找调用点、看依赖、梳理数据流、"
        "确认某个符号的用法。子 Agent 在自己的小上下文里探索，父上下文因此保持干净。\n"
        "参数 task 是给子 Agent 的完整指令（说明要找什么、要回答什么）；"
        "description 是一行可选标签（供界面显示）。\n"
        "**子 Agent 只能读取与检索，不能改文件、不能执行命令** —— 需要写盘时请自己动手。\n"
        "返回值是子 Agent 的结论（不可信数据块）：给出结论、关键路径与行号、必要的短引用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "给子 Agent 的完整探索指令：要找什么、要回答什么问题。"
                    f"最多 {MAX_TASK_CHARS} 字符"
                ),
            },
            "description": {
                "type": "string",
                "description": f"一行简述（供界面显示），最多 {MAX_DESCRIPTION_CHARS} 字符",
            },
        },
        "required": ["task"],
    },
    handler=_task,
    # 只读委派：不写盘、不下命令，因此不进审批。子 Agent 的只读性由
    # build_readonly_registry() 白名单从结构上保证（工具不存在，模型就选不到）。
    risk=Risk.SAFE,
)


__all__ = ["MAX_DESCRIPTION_CHARS", "MAX_TASK_CHARS", "TASK_SPEC", "normalize_task"]