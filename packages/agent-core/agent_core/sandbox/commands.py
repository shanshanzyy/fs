"""命令沙箱：白名单 + 危险模式拒绝 + 风险分级。

对应 docs/ARCHITECTURE.md 的 ADR-008。三条硬规则：

1. **永不使用 shell=True**。命令用 shlex 切分为 argv 后直接交给
   subprocess.run(argv, shell=False)，从根上消除管道注入、命令串联、
   变量展开这类经典攻击面。
2. **禁止通过工具启动 shell**（bash / sh / cmd / powershell / python -c 等）。
   否则前面的白名单形同虚设 —— 这是最容易漏掉的一条。
3. **DENY 级命令不可配置绕过**；REVIEW 级必须人工确认。

风险分级：
    SAFE   —— 只读类（ls / cat / grep / git status ...），可自动放行
    REVIEW —— 有副作用（构建、安装、写文件、git 提交 ...），需人工确认
    DENY   —— 明确危险或绕过沙箱，直接拒绝
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..errors import CommandDenied


class Risk(str, Enum):
    SAFE = "safe"
    REVIEW = "review"
    DENY = "deny"


_SAFE_EXECUTABLES = {
    "ls", "dir", "pwd", "cd", "cat", "type", "head", "tail", "wc", "nl",
    "grep", "rg", "find", "fd", "tree", "file", "stat", "diff", "cmp",
    "echo", "sort", "uniq", "cut", "tr", "basename", "dirname", "realpath",
    "which", "where", "whoami", "hostname", "date", "du", "df", "test",
    "jq", "yq", "column", "less", "more",
}

_REVIEW_EXECUTABLES = {
    # 版本控制
    "git", "svn", "hg",
    # 包管理与运行时
    "npm", "npx", "pnpm", "yarn", "node", "deno", "bun",
    "python", "python3", "py", "pip", "pip3", "uv", "poetry", "conda",
    "go", "cargo", "rustc", "java", "javac", "mvn", "gradle", "gradlew",
    "dotnet", "php", "composer", "ruby", "gem", "bundle",
    # 构建与检查
    "make", "cmake", "ninja", "tsc", "eslint", "prettier", "vite", "webpack",
    "pytest", "tox", "ruff", "black", "mypy", "flake8", "isort", "coverage",
    # 有副作用的文件操作
    "rm", "mv", "cp", "mkdir", "rmdir", "touch", "ln", "tee", "sed", "awk",
    "chmod", "tar", "zip", "unzip", "gzip", "gunzip",
}

_NETWORK_EXECUTABLES = {
    "curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "ncat", "telnet",
    "ftp", "ping", "tracert", "nslookup", "dig",
}

# 绝对拒绝：提权、系统管理、磁盘操作、跨沙箱逃逸、进程控制
_DENIED_EXECUTABLES = {
    # 提权
    "sudo", "su", "doas", "runas", "pkexec",
    # 关机 / 重启 / 服务
    "shutdown", "reboot", "halt", "poweroff", "systemctl", "service",
    "launchctl", "sc", "netsh", "netstat",
    # 磁盘 / 分区 / 文件系统
    "mkfs", "fdisk", "parted", "diskpart", "format", "dd", "chkdsk",
    "mount", "umount", "fsck", "cipher", "takeown", "icacls", "cacls",
    # 用户 / 权限 / 计划任务
    "passwd", "useradd", "userdel", "usermod", "net", "net1", "crontab",
    "at", "schtasks", "wmic",
    # 注册表 / 系统配置
    "reg", "regedit", "setx", "bcdedit",
    # 进程控制
    "kill", "killall", "taskkill", "pkill", "killall5",
    # ★ 最关键的一条：禁止启动任何 shell 或解释器的内联执行入口
    "sh", "bash", "zsh", "fish", "csh", "ksh", "dash",
    "cmd", "powershell", "pwsh", "wsl",
    "eval", "exec", "source", "start", "nohup", "xargs",
}

# 危险参数组合（对原始命令串做正则匹配）
_DANGEROUS_RULES: tuple[tuple[str, str], ...] = (
    (r"\brm\s+(-[a-zA-Z]+\s+)*-[a-zA-Z]*[rf][a-zA-Z]*\b", "递归/强制删除"),
    (r"\brm\s+(-[a-zA-Z]+\s+)*--(recursive|force|no-preserve-root)", "递归/强制删除"),
    (r"\brmdir\s+/[sq]\b", "递归删除目录"),
    (r"\bgit\s+push\b[^\n]*(--force\b|--force-with-lease\b|\s-f\b)", "强制推送，会覆盖远端历史"),
    (r"\bgit\s+reset\s+--hard", "硬重置，未提交改动会永久丢失"),
    (r"\bgit\s+clean\s+-[a-zA-Z]*f", "清理未跟踪文件，可能删除未备份内容"),
    (r"\bgit\s+checkout\s+--\s", "丢弃工作区改动"),
    (r"\bgit\s+push\b[^\n]*\s--delete\b", "删除远端分支"),
    (r":\(\)\s*\{.*\};?\s*:", "fork 炸弹"),
    (r"\bchmod\s+(-R\s+)?0?777\b", "放开全部权限位"),
    (r"\bchown\s+-R\s+root", "递归改属主为 root"),
    (r"\b(curl|wget)\b[^\n]*\|\s*(ba|z)?sh\b", "下载内容直接管道给 shell 执行"),
    (r"\bnpm\s+(publish|unpublish)", "发布包到公共仓库"),
    (r"\bpip\s+install\b[^\n]*--break-system-packages", "破坏系统包管理"),
    (r"\b(uv|poetry)\s+publish", "发布包到公共仓库"),
    (r"\bdocker\s+(rm|rmi|system\s+prune|volume\s+rm)\b", "删除容器/镜像/卷"),
    (r"\bkubectl\s+delete\b", "删除集群资源"),
    (r"\bterraform\s+(destroy|apply\s+-auto-approve)", "基础设施破坏性变更"),
    (r"\b(aws|gcloud|az)\s+.*\b(delete|terminate|destroy)\b", "删除云资源"),
    (r">\s*/dev/(sd|nvme|hd)", "覆写块设备"),
    (r"\bsudo\b", "提权执行"),
    (r">\s*(/etc/|/usr/|/bin/|/boot/)", "写入系统目录"),
)

# 只读子命令白名单：让 Agent 能自由查看仓库/依赖状态，不必每次弹审批。
# 这些子命令不会改动工作区，是 Agent 干活时调用最频繁的一类命令。
_SAFE_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "git": frozenset({
        "status", "log", "diff", "show", "blame", "describe", "rev-parse",
        "rev-list", "ls-files", "ls-tree", "shortlog", "whatchanged", "reflog",
        "grep", "cat-file", "show-ref", "symbolic-ref", "name-rev",
        "count-objects", "verify-commit", "version", "diff-tree", "merge-base",
    }),
    "npm": frozenset({"ls", "list", "view", "outdated", "why", "explain", "ping"}),
    "pnpm": frozenset({"ls", "list", "why", "outdated"}),
    "yarn": frozenset({"list", "info", "why", "outdated"}),
    "pip": frozenset({"list", "show", "freeze", "download", "check"}),
    "pip3": frozenset({"list", "show", "freeze", "download", "check"}),
    "uv": frozenset({"tree", "pip", "version"}),
    "poetry": frozenset({"show", "check", "version"}),
    "cargo": frozenset({"tree", "metadata", "version", "check"}),
    "go": frozenset({"version", "env", "list", "doc", "vet"}),
    "mvn": frozenset({"version", "dependency:tree"}),
    "gradle": frozenset({"version", "dependencies"}),
}

# 这些全局开关后面跟一个值，识别子命令时要一起跳过
_VALUE_FLAGS = frozenset({
    "-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path",
    "--prefix", "--cwd", "--directory",
})

_SHELL_METACHARS: tuple[tuple[str, str], ...] = (
    ("|", "管道"),
    ("&&", "命令串联"),
    ("||", "命令串联"),
    (";", "命令串联"),
    ("`", "命令替换"),
    ("$(", "命令替换"),
    ("${", "变量展开"),
    (">", "输出重定向"),
    ("<", "输入重定向"),
    ("\n", "多行命令"),
    ("\r", "多行命令"),
)

# 这些解释器的 -c / -e / -Command 形式等于开后门，虽在白名单里也要单独拦
_INLINE_EXEC_FLAGS: dict[str, tuple[str, ...]] = {
    "python": ("-c",),
    "python3": ("-c",),
    "py": ("-c",),
    "node": ("-e", "--eval", "-p", "--print"),
    "deno": ("eval",),
    "bun": ("-e", "--eval"),
    "ruby": ("-e",),
    "perl": ("-e",),
    "php": ("-r",),
}


def _pick_subcommand(argv: list[str]) -> str:
    """取出子命令名：跳过全局开关及其取值。

    git -C /repo status   -> "status"
    npm --prefix x ls     -> "ls"
    """
    index = 1
    while index < len(argv):
        token = argv[index]
        if token in _VALUE_FLAGS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return ""


def _best_effort_executable(raw: str) -> str:
    """尽力猜出可执行文件名。**只用于审计展示，不参与任何安全判定。**

    为什么需要它：`analyze()` 有好几个分支在切分 argv **之前**就返回了
    （shell 元字符、危险参数组合、无法解析），那时 `Analysis.executable`
    还是空的。而「有人试图跑什么」恰恰是审计要回答的第一个问题 ——
    缺了它，一次提权尝试只会留下「deny、命令未知」，聚合分析无从做起。

    刻意写得很保守：拿不准就返回空串。一个猜错的可执行名比空值更坏，
    因为它会让「谁在尝试 sudo」这类统计得出错误结论。
    """
    first = (raw.strip().split() or [""])[0]
    first = first.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if first.endswith(".exe"):
        first = first[:-4]
    return first if re.match(r"^[a-z0-9_.+:-]+$", first) else ""


@dataclass
class CommandAnalysis:
    """一条命令的安全分析结论。"""

    command: str
    argv: list[str] = field(default_factory=list)
    executable: str = ""
    risk: Risk = Risk.DENY
    reason: str = ""
    outside_paths: list[str] = field(default_factory=list)

    @property
    def is_allowed(self) -> bool:
        return self.risk is not Risk.DENY

    @property
    def needs_review(self) -> bool:
        return self.risk is Risk.REVIEW


@dataclass
class CommandGuard:
    """命令安全闸门。"""

    workspace: Path
    allow_network: bool = False
    extra_allowed: frozenset[str] = frozenset()
    # 治理策略下发的额外拒绝清单（M3）。与硬拒绝清单分开而不是并进去：
    # 硬拒绝是 agent-core 的安全底线，策略清单是部署方业务约束，
    # 两者的「为什么被拒」要分开解释，排查时才不会互相污染。
    policy_denied: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).expanduser().resolve()

    # ------------------------------------------------------------------ 入口
    def analyze(self, command: str) -> CommandAnalysis:
        """分析但不抛异常，返回结论供调用方决定如何处置。"""
        raw = (command or "").strip()
        if not raw:
            return CommandAnalysis(command=raw, risk=Risk.DENY, reason="命令为空")

        # 下面几个分支在切分 argv 之前就返回了，那时还没有 executable。
        # 先尽力猜一个填上：审计要回答的第一个问题就是「试图跑的是什么」。
        best = _best_effort_executable(raw)

        # 1) shell 元字符：一律拒绝，避免"看起来没跑 shell 实际被串起来"
        for token, label in _SHELL_METACHARS:
            if token in raw:
                return CommandAnalysis(
                    command=raw,
                    executable=best,
                    risk=Risk.DENY,
                    reason=f"命令包含 {label} 字符 {token!r}。本沙箱不执行 shell，"
                    "请拆成单条命令分别调用。",
                )

        # 2) 危险参数组合
        lowered = raw.lower()
        for pattern, label in _DANGEROUS_RULES:
            if re.search(pattern, lowered):
                return CommandAnalysis(
                    command=raw,
                    executable=best,
                    risk=Risk.DENY,
                    reason=f"命中高危模式：{label}",
                )

        # 3) 切分 argv（不使用 shell，因此不存在展开与转义歧义）
        try:
            argv = shlex.split(raw, posix=True)
        except ValueError as exc:
            return CommandAnalysis(
                command=raw,
                executable=best,
                risk=Risk.DENY,
                reason=f"命令无法解析: {exc}",
            )
        if not argv:
            return CommandAnalysis(command=raw, risk=Risk.DENY, reason="命令为空")

        executable = os.path.basename(argv[0]).lower()
        if executable.endswith(".exe"):
            executable = executable[:-4]
        # 形如 mkfs.ext4 / format.com 的名字要连主名一起比对，否则能绕过黑名单
        stem = executable.split(".", 1)[0] if "." in executable else executable
        names = {executable, stem}

        # 4) 拒绝在命令里写绝对路径的可执行文件（绕过 basename 白名单）
        head = argv[0].replace("\\", "/")
        if head.startswith("/") or re.match(r"^[a-zA-Z]:/", head):
            return CommandAnalysis(
                command=raw,
                argv=argv,
                executable=executable,
                risk=Risk.DENY,
                reason="不支持以绝对路径调用可执行文件，请使用 PATH 中的命令名",
            )

        # 5) 黑名单
        denied_hit = names & _DENIED_EXECUTABLES
        if denied_hit:
            shell_like = denied_hit & {
                "sh", "bash", "zsh", "cmd", "powershell", "pwsh", "wsl",
                "eval", "exec", "xargs", "source",
            }
            hint = "（禁止启动 shell，否则命令白名单将形同虚设）" if shell_like else ""
            return CommandAnalysis(
                command=raw,
                argv=argv,
                executable=executable,
                risk=Risk.DENY,
                reason=f"命令 {sorted(denied_hit)[0]!r} 在硬拒绝清单中{hint}",
            )

        # 6) 策略下发的额外拒绝清单（M3）
        policy_hit = names & self.policy_denied
        if policy_hit:
            return CommandAnalysis(
                command=raw,
                argv=argv,
                executable=executable,
                risk=Risk.DENY,
                reason=f"命令 {sorted(policy_hit)[0]!r} 被治理策略拒绝",
            )

        # 7) 内联执行入口（python -c / node -e ...）等于任意代码执行
        inline_flags = _INLINE_EXEC_FLAGS.get(executable) or _INLINE_EXEC_FLAGS.get(stem)
        if inline_flags:
            for arg in argv[1:]:
                if arg in inline_flags:
                    return CommandAnalysis(
                        command=raw,
                        argv=argv,
                        executable=executable,
                        risk=Risk.DENY,
                        reason=f"{executable} {arg} 可在不落盘的情况下执行任意代码，"
                        "绕过文件沙箱，已拒绝。请改写成脚本文件后执行。",
                    )
                if not arg.startswith("-"):
                    break

        # 8) 白名单
        allowed = _SAFE_EXECUTABLES | _REVIEW_EXECUTABLES | _NETWORK_EXECUTABLES
        if not (names & allowed) and not (names & self.extra_allowed):
            return CommandAnalysis(
                command=raw,
                argv=argv,
                executable=executable,
                risk=Risk.DENY,
                reason=f"命令 {executable!r} 不在白名单中。"
                "如需放行，请通过 extra_allowed 显式加入（不要直接改源码常量）。",
            )

        # 9) 网络命令
        if executable in _NETWORK_EXECUTABLES and not self.allow_network:
            return CommandAnalysis(
                command=raw,
                argv=argv,
                executable=executable,
                risk=Risk.DENY,
                reason=f"网络命令 {executable!r} 默认禁止（数据外发风险），"
                "如需使用请开启 allow_network_commands。",
            )

        # 10) 扫描参数里的工作区外绝对路径
        outside = self._scan_outside_paths(argv[1:])

        # 11) 分级：只读子命令（git status / npm ls ...）直接放行
        subcommand = _pick_subcommand(argv)
        read_only = _SAFE_SUBCOMMANDS.get(executable) or _SAFE_SUBCOMMANDS.get(stem)
        if read_only and subcommand in read_only and not outside:
            return CommandAnalysis(
                command=raw,
                argv=argv,
                executable=executable,
                risk=Risk.SAFE,
                reason=f"只读子命令 {executable} {subcommand}",
            )

        if executable in _SAFE_EXECUTABLES and not outside:
            return CommandAnalysis(
                command=raw, argv=argv, executable=executable, risk=Risk.SAFE,
                reason="只读命令",
            )

        reasons = []
        if executable in _REVIEW_EXECUTABLES:
            reasons.append("可能产生副作用")
        if executable in _NETWORK_EXECUTABLES:
            reasons.append("涉及网络访问")
        if outside:
            reasons.append("引用了工作区外的路径: " + ", ".join(outside[:3]))
        return CommandAnalysis(
            command=raw,
            argv=argv,
            executable=executable,
            risk=Risk.REVIEW,
            reason="、".join(reasons) or "需要确认",
            outside_paths=outside,
        )

    def ensure_configured(self, command: str) -> CommandAnalysis:
        """用户**本人配置**的受控命令（verify / test）的准入检查。

        与 `ensure_allowed` 是同一条安全底线（DENY 即拒绝、REVIEW 放行），
        单独命名是为了让调用点明示一个事实：这条命令来自用户配置
        （环境变量 / VIBE.md 约定行），不是模型在运行时拼出来的 ——
        这正是它「免审批」的依据（ADR-011），但**不免检**：照样过白名单。
        """
        return self.ensure_allowed(command)

    def ensure_allowed(self, command: str) -> CommandAnalysis:
        """分析并在 DENY 时抛异常。REVIEW 交给审批环节处理。

        注意：这条路径**不会**产生 `sandbox.deny_command` 审计。
        在 Agent 循环里，闸门 1 会先按 `_assess_command` 的结论拦下调用，
        处理器压根不执行；那条审计记在 `_assess_command` 里（裁决产生的地方）。
        这个方法在这里保留，是给「不经过 assess 直接执行」的调用方兜底。
        """
        analysis = self.analyze(command)
        if analysis.risk is Risk.DENY:
            raise CommandDenied(
                f"命令被安全策略拒绝：\n  命令: {analysis.command}\n  原因: {analysis.reason}"
            )
        return analysis

    # ------------------------------------------------------------------ 内部
    def _scan_outside_paths(self, args: list[str]) -> list[str]:
        found: list[str] = []
        for arg in args:
            if len(arg) > 512:
                continue
            normalized = arg.replace("\\", "/")
            looks_absolute = normalized.startswith("/") or re.match(r"^[a-zA-Z]:/", normalized)
            if not looks_absolute:
                continue
            if normalized.startswith(("/dev/null", "/dev/stdout", "/dev/stderr")):
                continue
            try:
                candidate = Path(arg).resolve()
            except OSError:
                found.append(arg)
                continue
            if candidate != self.workspace and self.workspace not in candidate.parents:
                found.append(arg)
        return found
