"""test_loop（M9-C）：测试驱动的自动修复闭环。

## 为什么是一个工具

`verify`（C2 验证闭环）是「改完被动回灌一次报错」，`test_loop` 是「**以测试为
主动目标**的自治循环」——Agent 显式发起「跑测试 → 失败 → 喂回失败摘要 → 修复
（恒走审批）→ 重跑」，直到绿或达到上限。做成工具而不是写死在循环里，是因为
「什么时候值得启动这个循环」是模型要判断的决策（用户说「改到测试全绿」时）。

## 三条边界

1. **只调度既有工具**：`Risk.SAFE`。本工具**不直接写盘、不直接执行命令**——测试
   命令的执行与 Agent 修复动作都交给上层（Agent 侧的目标循环）走既有 `run_command`
   口径与 `edit_file`/`edit_files` 审批漏斗（ADR-008，不可配置绕过）。
2. **恒有硬上限**：`max_rounds` + `timeout` 双重上限，防止「改不好→失败→再改」
   死循环（对齐 M0 的 MAX_AUTO_VERIFY 纪律）。到顶仍未绿时**如实报告并停止**。
3. **无 sink 即明确失败**：直接调 registry 的场景（测试）没有 `test_loop_sink`，
   工具如实报「未启用」，而不是假装成功返回空结论。
"""

from __future__ import annotations

from ..errors import ToolError
from ..sandbox import Risk
from .base import ToolContext, ToolResult, ToolSpec

# 测试命令长度上限。超长命令不该塞进一次循环 —— 那已经不是「跑测试」了。
MAX_TEST_COMMAND_CHARS = 2000


def _test_loop(ctx: ToolContext, args: dict) -> ToolResult:
    command = str(args.get("command") or "").strip()
    if not command:
        raise ToolError(
            "缺少 command 参数：请给出要跑的测试命令（如 'pytest -q tests/'）"
        )
    if len(command) > MAX_TEST_COMMAND_CHARS:
        raise ToolError(
            f"command 过长（{len(command)} 字符，上限 {MAX_TEST_COMMAND_CHARS}）"
        )
    if ctx.test_loop_sink is None:
        return ToolResult(
            ok=False,
            content=(
                "测试闭环未启用（VIBE_TEST_LOOP=0），本次无法启动自动修复循环。\n"
                "请自己用 run_command 跑测试、根据失败输出用 edit_file 修复。"
            ),
        )
    # 上限可由本次调用显式收窄（不能放宽到配置之上）。
    max_rounds = int(args.get("max_rounds") or ctx.config.test_loop_max_rounds)
    max_rounds = max(1, min(max_rounds, int(ctx.config.test_loop_max_rounds)))
    return ctx.test_loop_sink(command, max_rounds)


TEST_LOOP_SPEC = ToolSpec(
    name="test_loop",
    description=(
        "启动一个「以测试为目标」的自动修复循环：跑测试命令，失败则把失败摘要喂回，"
        "由你修复后重跑，直到测试通过或达到重试上限。\n"
        "典型用法：用户说「改到测试全绿」时，先用 edit_file 修一遍，再调 test_loop "
        "跑 `pytest -q` 验证，失败会带着报错继续修。\n"
        "约束：\n"
        "  - 测试命令执行走 run_command 的既有审批/审计口径；\n"
        "  - 你的修复动作**恒走人工审批**（不可配置关闭）；\n"
        "  - 有重试上限，到顶仍未通过会如实停止，不会死循环。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "要跑的测试命令，如 'pytest -q tests/'。"
                    f"最多 {MAX_TEST_COMMAND_CHARS} 字符"
                ),
            },
            "max_rounds": {
                "type": "integer",
                "description": "可选，重试上限（不能超过会话配置的默认值）",
            },
        },
        "required": ["command"],
    },
    handler=_test_loop,
    # 只调度既有工具，不直接写盘、不直接执行命令 —— 不进审批。
    risk=Risk.SAFE,
)


__all__ = ["MAX_TEST_COMMAND_CHARS", "TEST_LOOP_SPEC"]
