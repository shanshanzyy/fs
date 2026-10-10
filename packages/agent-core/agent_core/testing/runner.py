"""IDE 内测试树（K / P2-14）的后端：收集用例、跑用例、把结果解析成结构化数据。

## 三条边界

1. **命令来自用户配置，模型改不了它** —— `Config.test_command` 取自环境变量
   `VIBE_TEST_COMMAND` 或 VIBE.md 的 `test:` 约定行（K1）。执行前一律过
   `CommandGuard.ensure_configured`：DENY 即拒绝，REVIEW 放行（免审批的依据
   是「用户本人配置」，见 ADR-011；但**不免检**）。
2. **固定 argv + shell=False** —— 命令用 `shlex` 切分后直接交给 `subprocess`，
   `env=scrub_env()`、`stdin=DEVNULL`、带超时：口径与 `run_command` / 收尾
   `verify` 逐字一致，不新增一条绕过沙箱的执行路径。
3. **只解析、不落盘** —— collect/run 都不写工作区（junit xml 落在系统临时目录）。

## 为什么用 junit 而不是解析 stdout

pytest 的文本输出格式随版本漂移，而 junit XML 是稳定契约；用标准库
`xml.etree` 解析，零新增依赖。失败位置取 testcase 的 `line` 属性（用例**定义**
行）—— 这正是「点失败项跳到哪一行」想要的落点，而 traceback 里的行号随
断言位置漂移，不适合当导航目标。
"""

from __future__ import annotations

import re
import shlex
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..errors import AgentError
from ..sandbox import CommandGuard
from ..tools.shell import scrub_env

# 收集用例的上限。测试树是给人看的，几万条既画不动也看不完 ——
# 超出的部分截断并如实告知（truncated=true）。
MAX_CASES = 5000

# 收集要快（只解析，不执行），沿用命令沙箱的默认超时。
COLLECT_TIMEOUT = 60
# 跑测试要给足余量：一个真项目的用例集跑几分钟是常态，
# 按 60s 截断只会把「跑得慢」误报成「跑失败」。
RUN_TIMEOUT = 600

# 失败信息首行的长度上限。完整 traceback 是给终端看的，结构化结果里
# 只需要一行「哪里断言错了」；过长的正文也会把界面撑坏。
MESSAGE_HEAD_CHARS = 200


@dataclass
class CaseResult:
    """单个用例的执行结果。字段与计划 §8 的 K5 事件载荷对齐。"""

    nodeId: str
    status: str = "passed"  # passed / failed / error / skipped
    ms: int = 0
    failureFile: str = ""
    failureLine: int = 0
    messageHead: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CollectResult:
    configured: bool = False
    command: str = ""
    cases: list[dict[str, str]] = field(default_factory=list)
    truncated: bool = False
    error: str = ""
    exitCode: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["total"] = len(self.cases)
        return payload


def _split(command: str) -> list[str]:
    return shlex.split(command, posix=True)


def _compose(base: str, extra: str) -> str:
    """把用户配置的两段（命令 + 追加参数）拼成一条待分析/执行的命令串。"""
    base = (base or "").strip()
    extra = (extra or "").strip()
    return f"{base} {extra}".strip() if extra else base


# pytest 只有在**净 verbosity = -1**（即恰好一个 `-q`）时才逐行打印
# `path::test` 形式的 node id；低于 -1（`-qq`）退化成 `path: N` 的计数行。
# 而用户的 `test_command` 常自带 `-q`（如 `python -m pytest -q`），
# 再叠加收集用的 `-q` 就成了 `-qq`。这里按「净 -1」这个目标补齐差额，
# 而不是硬拼一个 `-q` —— 否则「用哪个 test_command」会决定测试树能不能用。
_COLLECT_VERBOSITY = -1


def _verbosity_level(argv: list[str]) -> int:
    level = 0
    for token in argv[1:]:
        if token in ("-q", "--quiet"):
            level -= 1
        elif token in ("-v", "--verbose"):
            level += 1
        elif token.startswith("-") and not token.startswith("--") and len(token) > 1:
            body = token[1:]
            if set(body) <= {"q"}:
                level -= len(body)
            elif set(body) <= {"v"}:
                level += len(body)
    return level


def _with_collect_verbosity(argv: list[str]) -> list[str]:
    """把 argv 的 verbosity 补到 `_COLLECT_VERBOSITY`（多退少补）。"""
    delta = _COLLECT_VERBOSITY - _verbosity_level(argv)
    if delta > 0:  # 太安静 → 抬回来
        return [*argv, *(["-v"] * delta)]
    if delta < 0:  # 太吵 → 压下去
        return [*argv, *(["-q"] * (-delta))]
    return list(argv)


def _execute(
    argv: list[str], workspace: Path, timeout: int
) -> subprocess.CompletedProcess[str]:
    """按命令沙箱口径跑一次子进程。固定 argv、shell=False、脱敏环境、带超时。"""
    return subprocess.run(  # noqa: S603 — argv 数组 + shell=False，无注入面
        argv,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=scrub_env(),
        stdin=subprocess.DEVNULL,
        check=False,
    )


def _configured_result(command: str, message: str) -> CollectResult:
    return CollectResult(configured=False, command=command, error=message)


def collect(
    *,
    command: str,
    collect_args: str,
    workspace: Path,
    guard: CommandGuard,
    timeout: int = COLLECT_TIMEOUT,
) -> CollectResult:
    """跑 `test_command + collect_args`，解析出 node id 列表。

    未配置命令、命令被安全策略拒绝、命令启动失败都**不是服务端故障**：
    返回 `configured=false` + 中文 `error`，界面据此显示一行说明（K8 用例钉着
    「未配置时返回未配置而非 500」）。
    """
    base = (command or "").strip()
    if not base:
        return _configured_result("", "未配置测试命令（可通过 VIBE_TEST_COMMAND 或 VIBE.md 的 test: 行设置）")

    full = _compose(base, collect_args)
    try:
        analysis = guard.ensure_configured(full)
    except AgentError as exc:
        return _configured_result(full, f"测试命令被安全策略拒绝：{exc}")

    try:
        proc = _execute(_with_collect_verbosity(analysis.argv), workspace, timeout)
    except subprocess.TimeoutExpired:
        return _configured_result(full, f"收集用例超过 {timeout}s 未结束")
    except (FileNotFoundError, OSError) as exc:
        return _configured_result(full, f"测试命令启动失败：{exc}")

    output = ((proc.stdout or "") + "\n" + (proc.stderr or ""))
    cases, truncated = parse_node_ids(output)
    return CollectResult(
        configured=True,
        command=full,
        cases=cases,
        truncated=truncated,
        # 收集失败（如存在语法错误）时退出码非 0：把 stderr 尾部当作提示给出来，
        # 但仍是「已配置」—— 面板要能显示错误而不是退回「未配置」。
        error=("" if proc.returncode == 0 else output.strip()[-500:]),
        exitCode=proc.returncode,
    )


def parse_node_ids(output: str) -> tuple[list[dict[str, str]], bool]:
    """从 `pytest --collect-only -q` 的输出里抽出 node id。

    逐行形如 `tests/test_x.py::TestFoo::test_a`；末尾的
    `5 tests collected in 0.03s` 与告警段落都不是用例，按「含 `::`」过滤掉。
    """
    cases: list[dict[str, str]] = []
    truncated = False
    seen: set[str] = set()
    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line or "::" not in line:
            continue
        head, _, rest = line.partition("::")
        # 文件路径部分不该含空格；含空格的多半是告警/汇总行（如 "1 test collected"）。
        if not head or " " in head or not rest:
            continue
        if line in seen:
            continue
        seen.add(line)
        if len(cases) >= MAX_CASES:
            truncated = True
            break
        cases.append({"nodeId": line, "file": head, "name": rest})
    return cases, truncated


def run(
    *,
    command: str,
    node_ids: list[str],
    workspace: Path,
    guard: CommandGuard,
    timeout: int = RUN_TIMEOUT,
) -> dict[str, Any]:
    """跑指定用例（空列表 = 全部），用 junit XML 解析出结构化结果。"""
    base = (command or "").strip()
    if not base:
        return {
            "configured": False,
            "results": [],
            "total": 0, "passed": 0, "failed": 0, "skipped": 0, "ms": 0,
            "exitCode": 0,
            "error": "未配置测试命令（可通过 VIBE_TEST_COMMAND 或 VIBE.md 的 test: 行设置）",
        }

    try:
        analysis = guard.ensure_configured(base)
    except AgentError as exc:
        return {
            "configured": False,
            "results": [],
            "total": 0, "passed": 0, "failed": 0, "skipped": 0, "ms": 0,
            "exitCode": 0,
            "error": f"测试命令被安全策略拒绝：{exc}",
        }

    started = time.time()
    with tempfile.TemporaryDirectory(prefix="vibe-junit-") as tmp:
        xml_path = Path(tmp) / "report.xml"
        # node id 与 junit 路径**作为独立 argv 追加**，不再经过 shell 切分：
        # 参数化用例的 id 里可能带空格（test_a[hello world]），拼字符串再切会切碎它。
        argv = [*analysis.argv, *[str(n) for n in node_ids if str(n).strip()]]
        argv.append(f"--junitxml={xml_path}")
        try:
            proc = _execute(argv, workspace, timeout)
        except subprocess.TimeoutExpired:
            return _fail_payload(f"测试执行超过 {timeout}s 未结束", started)
        except (FileNotFoundError, OSError) as exc:
            return _fail_payload(f"测试命令启动失败：{exc}", started)

        xml_text = ""
        if xml_path.is_file():
            try:
                xml_text = xml_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return _fail_payload(f"读取测试报告失败：{exc}", started)

    ms = int((time.time() - started) * 1000)
    if not xml_text.strip():
        # 没有报告 = 用例压根没跑起来（导入错误 / 参数错误）。这不是「失败」，
        # 而是配置或环境问题，正文里给出输出尾部让人能自查。
        tail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[-500:]
        return _fail_payload(
            f"测试未产出 junit 报告（退出码 {proc.returncode}）" + (f"：{tail}" if tail else ""),
            started,
            exit_code=proc.returncode,
        )

    results = parse_junit(xml_text, workspace)
    passed = sum(1 for item in results if item.status == "passed")
    failed = sum(1 for item in results if item.status in ("failed", "error"))
    skipped = sum(1 for item in results if item.status == "skipped")
    return {
        "configured": True,
        "results": [item.to_dict() for item in results],
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        # ms 取「报告里的用例耗时之和」而不是墙钟时间：墙钟含进程启动与收集，
        # 用它做「测试变慢了吗」的对比会把环境抖动也算进去。
        "ms": sum(item.ms for item in results) or ms,
        "exitCode": proc.returncode,
        "error": "",
    }


def _fail_payload(message: str, started: float, exit_code: int = 0) -> dict[str, Any]:
    return {
        "configured": True,
        "results": [],
        "total": 0, "passed": 0, "failed": 0, "skipped": 0,
        "ms": int((time.time() - started) * 1000),
        "exitCode": exit_code,
        "error": message,
    }


# 失败 traceback 的末行形如 `tests\test_x.py:42: AssertionError`。
# 取**最后一个**匹配 = 最深的那一帧 = 真正断言的落点。
_LOCATION_RE = re.compile(r"([^\s:]+\.py):(\d+)")


def _parse_failure_location(text: str) -> tuple[str, int]:
    """从失败正文里抽出「哪个文件的第几行」。抽不到就返回 `("", 0)`。"""
    matches = _LOCATION_RE.findall(text or "")
    if not matches:
        return "", 0
    path, line = matches[-1]
    return path.replace("\\", "/"), int(line)


def _resolve_module_file(classname: str, workspace: Path | None) -> tuple[str, list[str]]:
    """没有 `file` 属性时，靠 `classname` + 文件系统反推模块路径。

    junit 只在 `<testcase>` 上给 `classname`（`tests.test_x.TestFoo`），
    从中切出「模块到哪结束、类名从哪开始」是有歧义的 —— 用**工作区里真实存在
    的文件**来消歧：从最长的候选开始试，第一个存在的就是模块文件，余下即类名。
    """
    parts = [part for part in (classname or "").split(".") if part]
    if not parts or workspace is None:
        return "", []
    for cut in range(len(parts), 0, -1):
        candidate = "/".join(parts[:cut]) + ".py"
        try:
            if (Path(workspace) / candidate).is_file():
                return candidate, parts[cut:]
        except OSError:
            continue
    return "", parts[1:] if len(parts) > 1 else []


def _node_id(file_path: str, classes: list[str], name: str) -> str:
    return "::".join([file_path, *classes, name or ""])


def parse_junit(xml_text: str, workspace: Path | None = None) -> list[CaseResult]:
    """解析 junit XML → 用例结果列表（标准库 `xml.etree`）。

    对每个 `<testcase>`：有 `<failure>` → failed、有 `<error>` → error、
    有 `<skipped>` → skipped，否则 passed。

    位置信息按可靠性排序取：**失败正文里的 `path:line`**（抓的是真正断言那一行，
    也是导航最该跳的地方）> testcase 的 `line` 属性（用例定义行，部分 pytest
    版本才有）> testcase 的 `file` 属性；再不行用「classname + 文件系统」反推。
    node id 统一按 `file::Class::name` 组装，与 `--collect-only -q` 的输出对齐，
    树上的结果图标才能落到收集到的用例上。
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    cases = root.iter("testcase") if root.tag != "testcase" else iter([root])
    out: list[CaseResult] = []
    for case in cases:
        file_attr = (case.get("file") or "").replace("\\", "/")
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        seconds = _to_float(case.get("time"))
        attr_line = int(_to_float(case.get("line")))

        status = "passed"
        message_head = ""
        trace_file, trace_line = "", 0
        for node, label in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
            child = case.find(node)
            if child is None:
                continue
            status = label
            body = child.get("message") or (child.text or "")
            message_head = _head(body)
            if label in ("failed", "error"):
                trace_file, trace_line = _parse_failure_location(
                    (child.text or "") or (child.get("message") or "")
                )
            break

        module_file, classes = "", []
        if not file_attr:
            module_file, classes = _resolve_module_file(classname, workspace)
        else:
            dotted = file_attr
            module = dotted[:-3].replace("/", ".") if dotted.endswith(".py") else dotted.replace("/", ".")
            if classname.startswith(module + "."):
                classes = classname[len(module) + 1:].split(".")
            elif classname != module and classname:
                classes = [classname]
        file_path = file_attr or trace_file or module_file
        classes = [part for part in classes if part]

        out.append(
            CaseResult(
                nodeId=_node_id(file_path, classes, name) if file_path else f"{(classname or '').replace('.', '/')}::{name}",
                status=status,
                ms=int(round(seconds * 1000)),
                failureFile=file_path,
                failureLine=(trace_line or attr_line) if status in ("failed", "error") else 0,
                messageHead=message_head,
            )
        )
    return out


def _to_float(raw: Any) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _head(text: str) -> str:
    first = ""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            first = stripped
            break
    if len(first) > MESSAGE_HEAD_CHARS:
        first = first[:MESSAGE_HEAD_CHARS]
    return first