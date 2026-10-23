"""命令行入口 —— M0 阶段的产品界面。

设计意图：先用 CLI 把「描述 → 规划 → 工具 → 补丁 → 人审 → 应用 → 验证」这条闭环跑通。
桌面端（M1）只是把同一个 Agent 换个前端，因此这里刻意不做任何花哨的东西，
只保证：输出可读、审批可控、状态可查。
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from typing import Any

from . import __version__
from .audit import record_session_end, record_session_start, verify_chain
from .config import Config
from .errors import AgentError, ConfigError, LLMError
from .governance.cli import handle as handle_governance
from .governance.cli import register_governance_parser
from .update.cli import handle as handle_update
from .update.cli import register_update_parser
from .governance.runtime import upload_audit_now, wire
from .llm import ToolCall, probe_endpoint
from .loop import Agent, AgentHooks, ApprovalRequest
from .sandbox import Risk
from .tools import ToolResult
from . import ui

BANNER = r"""
 __   __ _  _        _                    _
 \ \ / /(_)| |__  __| |  __ _   __ _  ___| |_
  \ V / | || '_ \/ _` | / _` | / _` |/ _ \ __|
   | |  | || |_) | (_| || (_| || (_| |  __/ |_
   |_|  |_||_.__/ \__,_| \__,_| \__, |\___|\__|
                                |___/  v{version}  本地编码智能体
"""

SLASH_HELP = """
可用命令（以 / 开头）：
  /help            显示本帮助
  /tools           列出当前可用工具
  /config          显示当前会话配置
  /diff            查看本次会话累计的改动 diff
  /audit           校验审计日志的哈希链
  /governance      查看治理面接入状态（策略、配额、上传水位）
  /skills          列出已加载的工作流（Skill）
  /skill <name>    展开某个工作流的完整步骤
  /reset           清空对话历史，开始新任务
  /iterations N    调整最大迭代次数
  /exit            退出
""".strip()


# --------------------------------------------------------------------------- UI
class ConsoleHooks(AgentHooks):
    """把 Agent 的内部动作渲染成终端输出。"""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self._streamed = False  # 本轮已通过增量打印过正文（A1）

    def on_iteration(self, index: int, total: int) -> None:
        if self._streamed:
            print()  # 结束上一段流式输出，另起一行
            self._streamed = False
        print(paint_dim(f"── 第 {index}/{total} 轮 ──"))

    def on_tool_call(self, call: ToolCall, risk: Risk, reason: str) -> None:
        color = {Risk.SAFE: ui.GREEN, Risk.REVIEW: ui.YELLOW, Risk.DENY: ui.RED}[risk]
        preview = _compact_arguments(call.arguments)
        print(
            f"{ui.paint('▶', color, ui.BOLD)} {ui.paint(call.name, ui.BOLD)}"
            f"{ui.paint('  ' + preview, ui.GRAY)}"
        )

    def on_tool_result(self, call: ToolCall, result: ToolResult) -> None:
        if result.diff:
            ui.print_diff(result.diff)
            return
        if not self.verbose and result.ok:
            first = result.content.splitlines()[0] if result.content else ""
            print(ui.paint(f"  ✓ {first[:120]}", ui.GRAY))
            return
        for line in result.content.splitlines()[:40]:
            print(ui.paint("  " + line, ui.GRAY if result.ok else ui.RED))
        if len(result.content.splitlines()) > 40:
            print(ui.paint("  ...", ui.GRAY))

    def on_assistant_delta(self, text: str) -> None:
        if text:
            self._streamed = True
            print(text, end="", flush=True)

    def on_assistant_text(self, text: str) -> None:
        if text:
            if self._streamed:
                # 增量已把正文打完，只补一个换行收尾，避免重复输出
                self._streamed = False
                print()
                return
            print()
            print(text)

    def on_notice(self, text: str) -> None:
        print()
        ui.warn(text)


def paint_dim(text: str) -> str:
    return ui.paint(text, ui.GRAY)


def _compact_arguments(arguments: dict) -> str:
    """把工具参数压缩成一行摘要，方便一眼看出 Agent 在干什么。"""
    if not arguments:
        return ""
    parts = []
    for key in ("path", "command", "pattern", "name"):
        if key in arguments:
            value = str(arguments[key]).replace("\n", "⏎")
            parts.append(value[:70])
    if not parts:
        for key, value in list(arguments.items())[:2]:
            parts.append(f"{key}={str(value)[:40]}")
    return " ".join(parts)


def make_approver(config: Config):
    """人工审批闸门。ADR-008：高危操作必须过这一关，不允许通过配置静默跳过。"""

    def approve(request: ApprovalRequest) -> str:
        print()
        ui.rule("需要你确认")
        print(f"  工具 : {ui.paint(request.tool, ui.BOLD)}")
        print(f"  风险 : {ui.paint(request.risk.value, ui.YELLOW)} — {request.reason}")

        preview = request.preview
        if preview.kind == "diff":
            print(f"  改动 : {ui.paint(preview.summary, ui.CYAN)}")
            print()
            ui.print_diff(preview.body)
        elif preview.kind == "text":
            print(f"  内容 : {ui.paint(preview.summary, ui.CYAN)}")
            print()
            for line in preview.body.splitlines()[:24]:
                print("    " + ui.paint(line, ui.GRAY))
        else:
            print("  参数 :")
            body = preview.body or preview.error
            for line in body.splitlines()[:24]:
                print("    " + ui.paint(line, ui.GRAY))
        print()
        print(
            ui.paint(
                "  y=执行一次   a=本次会话内该工具全部放行   n=拒绝   d=拒绝并禁用该工具",
                ui.GRAY,
            )
        )
        choice = ui.confirm("执行这个动作吗？", ["y", "a", "n", "d"], "n")
        return {"y": "approve", "a": "approve_all", "n": "deny", "d": "deny_all"}[choice]

    return approve


def auto_approver(_: ApprovalRequest) -> str:
    return "approve"


# --------------------------------------------------------------------------- 自检
def run_self_check(config: Config) -> int:
    ui.rule("环境自检")
    ok = True

    ui.info(f"Python      : {sys.version.split()[0]}")
    ui.info(f"agent-core  : v{__version__}")
    ui.info(f"工作区      : {config.workspace}")
    ui.info(f"模型        : {config.model}")
    ui.info(f"接入地址    : {config.base_url}")

    reachable, detail = probe_endpoint(config.base_url, config.api_key, timeout=8)
    if reachable:
        ui.ok(f"模型服务    : 可达（{detail}）")
    else:
        ok = False
        ui.error(f"模型服务    : 不可达 — {detail}")

    # 审计目录可写性
    try:
        probe = config.workspace / ".vibe"
        probe.mkdir(parents=True, exist_ok=True)
        (probe / ".write_test").write_text("ok", encoding="utf-8")
        (probe / ".write_test").unlink()
        ui.ok(f"审计目录    : 可写（{probe}）")
    except OSError as exc:
        ok = False
        ui.error(f"审计目录    : 不可写 — {exc}")

    git_dir = config.workspace / ".git"
    if git_dir.exists():
        ui.ok("版本控制    : 检测到 .git（改动可通过 git diff 复核）")
    else:
        ui.warn("版本控制    : 未检测到 .git，建议先 git init，便于回滚 Agent 的改动")

    ui.info("")
    ui.rule("工具与风险分级")
    from .tools import build_default_registry

    registry = build_default_registry()
    for name in registry.names():
        spec = registry.get(name)
        assert spec is not None
        color = {Risk.SAFE: ui.GREEN, Risk.REVIEW: ui.YELLOW, Risk.DENY: ui.RED}[spec.risk]
        print(f"  {ui.paint(spec.risk.value.upper().ljust(6), color)} {name}")

    print()
    if ok:
        ui.ok("自检通过，可以开始使用。")
        return 0
    ui.error("自检未通过，请先解决上面的问题。")
    return 1


# --------------------------------------------------------------------------- REPL
def repl(config: Config, *, auto_yes: bool, verbose: bool, runtime: Any = None) -> int:
    if runtime is None:
        runtime = wire(config)
        config = runtime.config
        for note in runtime.notices:
            ui.warn(note)
    agent = Agent(
        config=config,
        client=runtime.client,
        constraints=runtime.constraints,
        hooks=ConsoleHooks(verbose=verbose),
        approver=auto_approver if auto_yes else make_approver(config),
    )
    started_at = time.time()
    # 会话边界事件：审计里最有用的一类锚点 —— 「谁在哪个仓库、用哪个模型、干了多久」。
    # 少了它，一串 tool.* / agent.* 事件无法被归入某一段具体的工作。
    record_session_start(agent.audit, config)

    print(ui.paint(BANNER.format(version=__version__), ui.CYAN))
    if auto_yes:
        ui.warn(
            "已开启 --yes：包括 REVIEW 级（文件写入 / 命令执行）在内的确认将被自动放行。"
            "该开关仅供非交互 / CI 使用，请确保工作区已纳入 git 管理。"
        )
    ui.info(f"工作区 {config.workspace}")
    ui.info(f"模型   {config.model} @ {config.base_url}")
    # 项目规则（C1）：与桌面端徽标同一信息源
    rules = agent.project_instructions
    if rules.source:
        suffix = "（超过 32KB，已截断）" if rules.truncated else ""
        ui.info(f"规则   已加载 {rules.source}（{rules.chars} 字符）{suffix}")
    ui.info("输入 /help 查看命令，Ctrl+C 中断当前任务，/exit 退出。")
    print()

    while True:
        try:
            raw = input(ui.paint("你 › ", ui.CYAN, ui.BOLD)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue

        if raw.startswith("/"):
            if handle_slash(raw, agent, config, runtime=runtime):
                break
            continue

        try:
            answer = agent.run(raw)
        except LLMError as exc:
            ui.error(str(exc))
            continue
        except KeyboardInterrupt:
            print()
            ui.warn("已中断当前任务。已完成的部分改动保留在工作区，可用 /diff 查看。")
            continue
        except AgentError as exc:
            ui.error(f"{type(exc).__name__}: {exc}")
            continue

        print()
        ui.rule("完成")
        print(answer)
        _print_stats(agent)
        print()

    record_session_end(
        agent.audit,
        reason="exit",
        iterations=agent.stats.iterations,
        duration_ms=int((time.time() - started_at) * 1000),
    )
    _report_upload(upload_audit_now(runtime))
    ui.info("已退出。审计日志：" + str(config.audit_path))
    return 0


def _print_stats(agent: Agent) -> None:
    stats = agent.stats
    touched = list(dict.fromkeys(agent.ctx.written_files))
    parts = [
        f"轮次 {stats.iterations}",
        f"工具调用 {stats.tool_calls}",
    ]
    if stats.tool_failures:
        parts.append(f"工具失败 {stats.tool_failures}")
    if stats.denials:
        parts.append(f"被拒 {stats.denials}")
    if stats.trimmed:
        parts.append(f"裁剪上下文 {stats.trimmed} 条")
    if touched:
        parts.append(f"改动文件 {len(touched)}")
    if stats.prompt_tokens or stats.completion_tokens:
        parts.append(f"tokens ↑{stats.prompt_tokens} ↓{stats.completion_tokens}")
    print()
    print(ui.paint("  · " + " | ".join(parts), ui.GRAY))
    if touched:
        for name in touched:
            print(ui.paint("    - " + name, ui.GRAY))


def handle_slash(raw: str, agent: Agent, config: Config, *, runtime: Any = None) -> bool:
    """处理斜杠命令。返回 True 表示要退出。

    config 是 frozen dataclass，运行时调整参数走 dataclasses.replace 生成新实例，
    并同步回 agent，避免出现「命令改了但 Agent 还用旧值」的错位。
    """
    parts = raw.split()
    command = parts[0].lower()
    argument = parts[1] if len(parts) > 1 else ""

    if command in ("/exit", "/quit", "/q"):
        return True
    if command in ("/help", "/h", "/?"):
        print(SLASH_HELP)
    elif command == "/tools":
        print()
        for name in agent.registry.names():
            spec = agent.registry.get(name)
            assert spec is not None
            print(f"  {ui.paint(spec.risk.value.upper().ljust(6), ui.GRAY)} {name}")
            print(ui.paint(f"        {spec.description.splitlines()[0]}", ui.GRAY))
    elif command == "/config":
        print()
        current = agent.config
        for field_name in (
            "workspace", "model", "base_url", "temperature", "max_iterations",
            "max_context_chars", "tool_output_limit", "allow_write",
            "allow_shell", "allow_network_commands", "command_timeout",
            "auto_approve_review",
        ):
            print(f"  {field_name.ljust(22)} = {getattr(current, field_name)}")
    elif command == "/diff":
        diffs = agent.ctx.applied_diffs
        if not diffs:
            ui.info("本次会话还没有产生改动。")
        else:
            print()
            for diff in diffs:
                ui.print_diff(diff)
                print()
    elif command == "/audit":
        passed, detail = verify_chain(config.audit_path)
        (ui.ok if passed else ui.error)(f"审计链校验：{detail}")
        ui.info(f"文件位置：{config.audit_path}")
    elif command == "/governance":
        if runtime is None or runtime.status == "off":
            ui.info("未接入治理面（未配置 VIBE_SERVER_URL），纯本地模式。")
        else:
            view = runtime.describe()
            status_labels = {
                "governed": "已接入",
                "degraded": "已降级（本地默认策略）",
                "unauthenticated": "已配置但未登录",
                "off": "未接入",
            }
            ui.info(f"治理面：{status_labels.get(view['status'], view['status'])}")
            if view.get("credentials"):
                cred = view["credentials"]
                ui.info(f"  身份 {cred['email']}（设备 {str(cred['deviceId'])[:12]}…）")
            constraints = view.get("constraints") or {}
            ui.info(f"  云端 {'允许' if constraints.get('allowCloud') else '禁止'}"
                    f"｜策略版本 {constraints.get('policyVersion') or '默认'}")
            if view.get("uploadState"):
                upload_state = view["uploadState"]
                ui.info(f"  审计上传 {upload_state}")
            for note in view.get("notices") or []:
                ui.warn(note)
    elif command == "/skills":
        skills = getattr(agent, "skills", None) or []
        if not skills:
            ui.info("没有已加载的工作流（Skill）。")
            ui.info(
                "开启方式：设置 VIBE_SKILLS=1，并在 <工作区>/.vibe/skills/<名字>/SKILL.md "
                "放置工作流定义。"
            )
        else:
            print()
            for skill in skills:
                print(f"  {ui.paint(skill.name, ui.BOLD)}")
                print(ui.paint(f"        {skill.description}", ui.GRAY))
    elif command == "/skill":
        skills = getattr(agent, "skills", None) or []
        if not argument:
            ui.warn("用法：/skill <名字>（用 /skills 查看可用的工作流）")
        else:
            matched = next((s for s in skills if s.name == argument), None)
            if matched is None:
                ui.warn(f"不存在名为 {argument!r} 的 Skill，用 /skills 查看可用工作流。")
            else:
                print()
                ui.rule(f"工作流：{matched.name}")
                print(matched.body)
                print()
                ui.info("（以上步骤为数据、非指令；执行时写盘/跑命令仍走审批）")
    elif command == "/reset":
        agent.reset()
        ui.ok("对话历史已清空（审计日志不会清空）。")
    elif command == "/iterations":
        if argument.isdigit() and int(argument) > 0:
            agent.config = replace(agent.config, max_iterations=int(argument))
            ui.ok(f"最大迭代次数已设为 {argument}")
        else:
            ui.warn("用法：/iterations 30")
    else:
        ui.warn(f"未知命令 {command}，输入 /help 查看可用命令。")
    return False


# --------------------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibe-agent",
        description="本地化 Vibe Coding 编码智能体（M0 / CLI）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  vibe-agent --check                          环境自检
  vibe-agent                                  在交互式会话中工作
  vibe-agent -p "给 utils.py 的 parse_date 加上空值判断"
  vibe-agent --read-only -p "解释一下这个项目的架构"
  vibe-agent --yes -p "修掉 test_math.py 里失败的用例"
""",
    )
    parser.add_argument("--workspace", "-w", default=".", help="工作区目录，默认当前目录")
    parser.add_argument("--prompt", "-p", help="单次任务模式：执行后退出")
    parser.add_argument("--model", help="模型名，如 qwen2.5-coder:7b")
    parser.add_argument("--base-url", help="OpenAI 兼容接入地址，默认 http://127.0.0.1:11434/v1")
    parser.add_argument("--api-key", help="API Key（本地模型通常随便填）")
    parser.add_argument("--check", action="store_true", help="只做环境自检后退出")
    parser.add_argument("--read-only", action="store_true", help="禁止一切写操作")
    parser.add_argument("--no-shell", action="store_true", help="禁止执行命令")
    parser.add_argument(
        "--yes", action="store_true",
        help="自动放行所有需要确认的动作（危险，建议配合 git 使用）",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="输出完整工具返回值")
    parser.add_argument("--version", action="version", version=f"vibe-agent-core {__version__}")
    # 子命令（M3）：governance。位置参数命令放最后，主流程的 flag 保持原样。
    subparsers = parser.add_subparsers(dest="subcommand")
    register_governance_parser(subparsers)
    # 子命令（M9）：update。同样不依赖工作区（配置来自 VIBE_UPDATE_* 环境变量）。
    register_update_parser(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    ui.init_console()
    args = build_parser().parse_args(argv)

    # governance 子命令在工作区之外也要能用（login/status 不依赖任何项目），
    # 所以放在 Config.load 之前分发。
    if getattr(args, "subcommand", None) == "governance":
        return handle_governance(args)
    if getattr(args, "subcommand", None) == "update":
        return handle_update(args)

    overrides: dict[str, object] = {}
    if args.model:
        overrides["model"] = args.model
    if args.base_url:
        overrides["base_url"] = args.base_url.rstrip("/")
    if args.api_key:
        overrides["api_key"] = args.api_key
    if args.read_only:
        overrides["allow_write"] = False
    if args.no_shell:
        overrides["allow_shell"] = False

    try:
        config = Config.load(args.workspace, **overrides)
    except ConfigError as exc:
        ui.error(str(exc))
        return 2

    if args.check:
        return run_self_check(config)

    # 治理面装配（M3）：策略收紧配置、必要时换网关客户端、准备审计上传。
    # 未配置 server_url 时 wire() 原样返回，M0/M1 行为不变。
    runtime = wire(config)
    config = runtime.config
    for note in runtime.notices:
        ui.warn(note)

    if args.prompt:
        agent = Agent(
            config=config,
            client=runtime.client,
            constraints=runtime.constraints,
            hooks=ConsoleHooks(verbose=args.verbose),
            approver=auto_approver if args.yes else make_approver(config),
        )
        started_at = time.time()
        record_session_start(agent.audit, config)
        if args.yes:
            ui.warn(
                "已开启 --yes：包括 REVIEW 级（文件写入 / 命令执行）在内的确认将被自动放行。"
                "该开关仅供非交互 / CI 使用，请确保工作区已纳入 git 管理。"
            )
        try:
            answer = agent.run(args.prompt)
        except (LLMError, AgentError) as exc:
            record_session_end(agent.audit, reason="error", iterations=agent.stats.iterations)
            ui.error(str(exc))
            return 1
        except KeyboardInterrupt:
            record_session_end(agent.audit, reason="interrupted", iterations=agent.stats.iterations)
            print()
            ui.warn("已中断。")
            return 130
        record_session_end(
            agent.audit,
            reason="done",
            iterations=agent.stats.iterations,
            duration_ms=int((time.time() - started_at) * 1000),
        )
        _report_upload(upload_audit_now(runtime))
        print()
        ui.rule("完成")
        print(answer)
        _print_stats(agent)
        return 0

    return repl(config, auto_yes=args.yes, verbose=args.verbose, runtime=runtime)


def _report_upload(result: dict) -> None:
    """会话结束时的审计上传汇报。只在有信息量时出声，成功静默。"""
    if result.get("reason"):
        ui.warn(f"审计上传未完成：{result['reason']}")
    elif result.get("clientHeadMatch") is False:
        ui.error("审计上传的链头与服务端不一致，上传已暂停，请检查服务端日志。")
    elif result.get("uploaded"):
        ui.ok(f"已上传 {result['uploaded']} 条审计事件（接受 {result.get('accepted', 0)}）。")


if __name__ == "__main__":
    sys.exit(main())
