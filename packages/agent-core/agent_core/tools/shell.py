"""命令执行工具：run_command。

安全要点（对应 ADR-008）：
1. 命令先过 CommandGuard 分析，DENY 直接拒绝，REVIEW 交给上层的审批闸门；
2. **永不使用 shell=True** —— 用 argv 数组直接调 subprocess，从根上消除管道注入；
3. **子进程环境变量做脱敏** —— 剥离 TOKEN/SECRET/PASSWORD/API_KEY 等变量，
   避免 Agent 跑一条 `env` 或 `printenv` 就把宿主机的凭据读到上下文里；
4. stdin 接 /dev/null，防止命令阻塞在交互式输入上。
"""

from __future__ import annotations

import os
import subprocess

from ..errors import SandboxViolation, ToolError
from ..sandbox import Risk
from .base import ToolContext, ToolResult, ToolSpec, wrap_untrusted

_SECRET_HINTS = (
    "TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "APIKEY", "API-KEY",
    "CREDENTIAL", "PRIVATE_KEY", "ACCESS_KEY", "SESSION_KEY", "AUTH",
    "OPENAI", "ANTHROPIC", "DEEPSEEK", "GITHUB_TOKEN", "GITLAB", "NPM_TOKEN",
)

_MAX_CAPTURE = 20_000


def scrub_env() -> dict[str, str]:
    """构造子进程环境变量：保留 PATH 等必需项，剥离疑似凭据。"""
    clean: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if any(hint in upper for hint in _SECRET_HINTS):
            continue
        clean[key] = value
    # 明确告知子进程当前处于非交互环境
    clean.setdefault("CI", "1")
    clean.setdefault("NO_COLOR", "1")
    return clean


def _run_command(ctx: ToolContext, args: dict) -> ToolResult:
    if not ctx.config.allow_shell:
        raise SandboxViolation("当前会话已关闭命令执行权限（allow_shell=false）")

    command = args["command"]
    analysis = ctx.commands.ensure_allowed(command)

    timeout = int(args.get("timeout") or ctx.config.command_timeout)
    timeout = max(1, min(timeout, 600))

    try:
        proc = subprocess.run(  # noqa: S603 — argv 数组 + shell=False，无注入面
            analysis.argv,
            cwd=str(ctx.config.workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=scrub_env(),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        ctx.audit.record(
            "tool.run_command", command=command, status="timeout", timeout=timeout
        )
        raise ToolError(
            f"命令执行超过 {timeout} 秒被终止：{command}\n"
            "  建议：把长任务拆小，或对该次调用显式提高 timeout（上限 600 秒）。"
        ) from None
    except FileNotFoundError:
        raise ToolError(
            f"找不到可执行文件 {analysis.executable!r}，请确认它已安装并在 PATH 中。"
        ) from None
    except OSError as exc:
        raise ToolError(f"命令启动失败: {exc}") from None

    stdout = (proc.stdout or "")[:_MAX_CAPTURE]
    stderr = (proc.stderr or "")[:_MAX_CAPTURE]

    ctx.audit.record(
        "tool.run_command",
        command=command,
        executable=analysis.executable,
        risk=analysis.risk.value,
        exit_code=proc.returncode,
        stdout_bytes=len(proc.stdout or ""),
        stderr_bytes=len(proc.stderr or ""),
    )

    parts = [f"[run_command] {command}", f"退出码: {proc.returncode}"]
    if stdout.strip():
        parts.append(wrap_untrusted("stdout", stdout.rstrip()))
    if stderr.strip():
        parts.append(wrap_untrusted("stderr", stderr.rstrip()))
    if not stdout.strip() and not stderr.strip():
        parts.append("（命令没有产生任何输出）")
    if proc.returncode != 0:
        parts.append(
            f"（命令以非零退出码 {proc.returncode} 结束。请先读懂报错再改代码，"
            "不要盲目重试同一条命令。）"
        )

    return ToolResult(
        ok=proc.returncode == 0,
        content="\n".join(parts),
        meta={"exit_code": proc.returncode, "executable": analysis.executable},
    )


def _assess_command(ctx: ToolContext, args: dict) -> tuple[Risk, str]:
    if not ctx.config.allow_shell:
        return Risk.DENY, "当前会话已关闭命令执行权限"
    analysis = ctx.commands.analyze(args.get("command") or "")
    if analysis.risk is Risk.DENY:
        # 命令沙箱的拒绝**在这里**就记一条审计。
        #
        # 为什么不放在工具处理器里（`ensure_allowed` 抛 CommandDenied 那条路）：
        # 循环的闸门 1 会先按这里返回的 DENY 拦下整个调用，
        # 处理器**根本不会被执行** —— 放在那里的话，这条审计永远不会产生。
        # 这正是本轮修掉的那类缺陷：服务端登记了、代码里也「写了」，
        # 但因为放在了不可达的位置，真机上永远是空的。
        ctx.audit.record(
            "sandbox.deny_command",
            tool="run_command",
            executable=analysis.executable or "unknown",
            risk=analysis.risk.value,
            reason=analysis.reason[:300],
        )
    return analysis.risk, analysis.reason


RUN_COMMAND_SPEC = ToolSpec(
    name="run_command",
    description=(
        "在工作区目录下执行一条命令，返回退出码与输出。\n"
        "约束（沙箱强制，不接受请求绕过）：\n"
        "  - 只能执行白名单内的命令，不经过 shell；\n"
        "  - 不支持管道、重定向、&&、$( ) 等 shell 语法，请拆成单条命令；\n"
        "  - 禁止启动 shell 本身，也禁止 python -c / node -e 这类内联执行；\n"
        "  - 网络类命令（curl/wget/ssh）默认禁止；\n"
        "  - 子进程不继承宿主机凭据类环境变量。\n"
        "典型用法：git status / git diff / pytest -q / npm test / make build。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "完整命令行，如 'pytest -q tests/'"},
            "timeout": {"type": "integer", "description": "超时秒数，默认取会话配置（60），上限 600"},
        },
        "required": ["command"],
    },
    handler=_run_command,
    risk=Risk.REVIEW,
    assessor=_assess_command,
)
