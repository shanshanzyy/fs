"""SSH 传输层：用**系统自带的 ssh 客户端**打通本机与远端设备。

## 为什么不自己实现 SSH 协议

项目红线是「零第三方依赖」（内网离线装不上包），所以 paramiko 不可用。
但**自己实现 SSH 协议是不可接受的**：那是一份需要长期维护的密码学与协议栈，
正确性风险远高于收益。系统 `ssh` 是内网环境的既成事实（设备必有），
把它当子进程驱动，是本项目唯一合理的选择 —— 与「手写 pgproto / 手写 PNG 解码」
是同一类取舍：**协议复杂度交给成熟实现，我们只负责把它约束住**。

## 我们负责约束什么

系统 ssh 默认是「给人交互用的」，直接拿来给 Agent 用会引入一堆问题。
本文件把它拧成一台**确定性的、无交互的、有界的**远程执行器：

1. **无交互** —— `BatchMode=yes` + `NumberOfPasswordPrompts=0`。
   任何需要输入的环节（密码、确认指纹）直接失败而不是挂住。
   Agent 不能等一个永远不会有人回答的提示。
2. **指纹策略显式化** —— 三态由连接配置给出（见 connection.py），
   `insecure` 会同时把 known_hosts 指向 /dev/null，**不留痕也不依赖它**。
3. **连接复用** —— POSIX 上开 ControlMaster，避免每条命令一次 TCP+TLS 式
   握手（远端往往是高延迟内网/VPN）。Windows 的 OpenSSH **不支持复用**，
   此时自动降级为「一命令一连接」，并在能力报告里如实标出来。
4. **有界** —— 连接超时、命令超时、输出截断都在这里兜住。
5. **argv 全程不进本地 shell** —— 本机侧永远传 argv 数组；
   远端侧因为 ssh 协议本身就是「把一个字符串交给远端登录 shell」，
   无法避免 shell，所以**逐项 `shlex.quote`**，并把多行脚本走 stdin 传递
   （`sh -s`），从根上消掉引号地狱。
"""

from __future__ import annotations

import json
import os
import posixpath
import shlex
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .connection import RemoteConnection
from .errors import RemoteError

# 输出截断上限。远端输出同样会进模型上下文，必须有界。
MAX_OUTPUT_CHARS = 40_000

# --------------------------------------------------------------------------
# 端口转发（P3 / ADR-040）
#
# `-L` 的规格是 `[bind:]port:host:hostport` —— **host 这一段是真正的风险点**：
# 若让它由用户填，一个 `-L 8080:10.0.0.5:6379` 就能把远端所在内网的任意服务
# 拽到本机，等于给界面开了一条没有审批的隧道。所以本文件把两端都钉死成环回
# 字面量，用户只能提供两个整数端口，规格字符串**完全由代码拼出**。
# --------------------------------------------------------------------------
FORWARD_HOST = "127.0.0.1"

# 远端那条「命令」不是拿来干活的，它是**生命周期的凭证**：
# 有会话时 ssh 会把本地 stdin 转给远端进程；父进程一死（含 SIGKILL），
# 内核关掉管道写端 → ssh 读到 EOF → 远端 `cat` 退出 → ssh 退出 → 本地监听消失。
# 若改用 `-N`（不建会话），ssh 根本不看 stdin，父进程崩溃就会留下孤儿监听 ——
# 这正是「孤儿兜底清理」的实现方式：不是事后去杀，而是让它不可能成为孤儿。
FORWARD_REMOTE_COMMAND = "cat >/dev/null"

# ssh 自身的失败退出码（OpenSSH 约定）。远端命令也可能返回 255，
# 因此不能只看退出码 —— 必须结合 stderr 特征判定（见 _classify_ssh_error）。
SSH_FAILURE_RETURNCODE = 255

# 能力探测脚本：POSIX sh，输出 KEY=VALUE 行。**不依赖 bash。**
# 刻意不用 JSON：远端不一定有 python3，而 printf 是 POSIX 必备。
_PROBE_SCRIPT = r"""
set -u
p() { printf '%s=%s\n' "$1" "$2"; }
p OS "$(uname -s 2>/dev/null || printf '?')"
p ARCH "$(uname -m 2>/dev/null || printf '?')"
p KERNEL "$(uname -r 2>/dev/null || printf '?')"
p USER "$(id -un 2>/dev/null || printf '?')"
p PWD "$PWD"
p SHELL "${SHELL:-?}"
for c in git python3 grep find realpath sha256sum shasum base64 mktemp stat head ls cat mv wc test; do
  if command -v "$c" >/dev/null 2>&1; then p "HAS_$c" 1; else p "HAS_$c" 0; fi
done
if command -v git >/dev/null 2>&1; then
  p GIT_VERSION "$(git --version 2>/dev/null || printf '?')"
fi
if command -v python3 >/dev/null 2>&1; then
  p PY_VERSION "$(python3 -c 'import sys;print(sys.version.split()[0])' 2>/dev/null || printf '?')"
fi
if find . -maxdepth 0 -printf '' >/dev/null 2>&1; then p FIND_PRINTF 1; else p FIND_PRINTF 0; fi
if stat -c '%s' . >/dev/null 2>&1; then p STAT_GNU 1; else p STAT_GNU 0; fi
if stat -f '%z' . >/dev/null 2>&1; then p STAT_BSD 1; else p STAT_BSD 0; fi
if head -c 1 /dev/null >/dev/null 2>&1; then p HEAD_C 1; else p HEAD_C 0; fi
if ls -A -1 . >/dev/null 2>&1; then p LS_A1 1; else p LS_A1 0; fi
if [ -d "$PWD" ] && [ -w "$PWD" ]; then p ROOT_WRITABLE 1; else p ROOT_WRITABLE 0; fi
"""

# ssh 自身报错的 stderr 特征 → 结构化分类。
# 顺序有意义：先匹配更具体的（hostkey 变体），再匹配泛化的网络错误。
_ERROR_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("REMOTE HOST IDENTIFICATION HAS CHANGED", "hostkey"),
    ("Host key verification failed", "hostkey"),
    ("No ED25519 host key is known", "hostkey"),
    ("Permission denied", "auth"),
    ("Too many authentication failures", "auth"),
    ("Bad owner or permissions on", "auth"),
    ("Load key", "auth"),
    ("invalid format", "auth"),
    ("Could not resolve hostname", "unreachable"),
    ("Name or service not known", "unreachable"),
    ("Connection refused", "unreachable"),
    ("Connection timed out", "unreachable"),
    ("Connection closed by", "unreachable"),
    ("No route to host", "unreachable"),
    ("Network is unreachable", "unreachable"),
    ("Operation timed out", "unreachable"),
    ("Broken pipe", "unreachable"),
    ("Connection reset by peer", "unreachable"),
)

_AUTH_HELP = (
    "  排查：① 公钥是否已装到远端（ssh-copy-id）；② identity_file 是否正确；"
    "③ 远端 sshd 是否允许公钥认证。注意：本链路**不支持密码认证**。"
)
_HOSTKEY_HELP = (
    "  ⚠ 主机指纹校验未通过，**存在中间人的可能**，不要盲目清除 known_hosts。\n"
    "  排查：确认远端主机是否被重装/换过密钥；确认无误后再清理对应 known_hosts 条目。\n"
    "  若这是全新主机，把该连接的 host_key_policy 设为 accept-new（首次记录，变化仍拒绝）。"
)
_UNREACHABLE_HELP = (
    "  排查：主机名/端口是否正确；内网/VPN 是否连通；远端 sshd 是否在监听；"
    "如需跳板机请设置 proxy_jump。"
)


@dataclass(frozen=True)
class RemoteResult:
    """一次远端执行的原始结果。"""

    returncode: int
    stdout: str
    stderr: str
    duration: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class Capabilities:
    """远端环境探测结果。

    为什么要探：远端可能是 Alpine、老 CentOS、macOS、甚至 BSD。
    「GNU stat 还是 BSD stat」「有没有 python3」「find 支不支持 -printf」
    直接决定上层该用哪条命令 —— 猜错的表现是「莫名其妙报语法错」，
    而这类故障在远端最难排查。探一次、缓存住、如实报告。
    """

    os_name: str = "?"
    arch: str = "?"
    kernel: str = "?"
    user: str = "?"
    cwd: str = "/"
    shell: str = "?"
    git_version: str = ""
    python_version: str = ""
    find_printf: bool = False
    stat_gnu: bool = False
    stat_bsd: bool = False
    head_c: bool = False
    ls_a1: bool = False
    root_writable: bool = False
    sha256_tool: str = ""
    base64_decode: str = ""
    multiplexed: bool = False
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def has_git(self) -> bool:
        return bool(self.git_version)

    @property
    def has_python(self) -> bool:
        return bool(self.python_version)

    @property
    def can_stat(self) -> bool:
        return self.stat_gnu or self.stat_bsd

    @property
    def degraded(self) -> list[str]:
        """可读的降级项列表。空列表 = 全能力可用。

        **降级必须能被说出来**：悄悄退化成「只能用 ls 列目录」的实现，
        会让用户在遇到「拿不到文件大小」时以为功能本身就只有这样。
        """
        items: list[str] = []
        if not self.multiplexed:
            items.append("连接复用不可用（每条命令一次握手，Windows 客户端常见）")
        if not self.stat_gnu and not self.stat_bsd:
            items.append("无可用 stat：无法保留权限地覆盖已存在文件（新建不受影响）")
        if not self.find_printf:
            items.append("find 不支持 -printf：目录列举走 ls，拿不到条目类型与大小")
        if not self.head_c:
            items.append("head 不支持 -c：大文件读取会先整段传输再本地截断")
        if not self.has_python:
            items.append("远端无 python3：结构化操作退化为 POSIX 工具解析")
        if not self.root_writable:
            items.append("工作区目录不可写：写操作会失败，请检查远端权限")
        return items

    def describe(self) -> str:
        lines = [
            f"系统      : {self.os_name} {self.kernel} ({self.arch})",
            f"登录用户  : {self.user}    shell: {self.shell}",
            f"工作目录  : {self.cwd}",
            f"git       : {self.git_version or '未安装'}",
            f"python3   : {self.python_version or '未安装'}",
            f"连接复用  : {'是' if self.multiplexed else '否'}",
            f"目录可写  : {'是' if self.root_writable else '否'}",
        ]
        degraded = self.degraded
        if degraded:
            lines.append("能力降级  :")
            lines.extend(f"  - {item}" for item in degraded)
        return "\n".join(lines)


class SSHTransport:
    """一条远程连接的执行通道。构造后需 `probe()` 才拿到能力。

    线程安全：`run*` 系列可并发调用（每条命令一个子进程）。
    能力探测加锁，避免并发探测重复执行。
    """

    def __init__(
        self,
        conn: RemoteConnection,
        *,
        multiplex: bool | None = None,
        control_dir: Path | None = None,
    ) -> None:
        self.conn = conn
        # Windows 的 OpenSSH 不支持 ControlMaster，显式探测并降级。
        self._multiplex = _supports_multiplexing() if multiplex is None else multiplex
        self._control_dir = control_dir
        self._control_lock = threading.Lock()
        self._caps: Capabilities | None = None
        self._caps_lock = threading.Lock()

    # ------------------------------------------------------------------ 装配
    def _ensure_control_dir(self) -> Path | None:
        """持久化套接字目录。懒创建 —— 不用复用时一个字节都不落盘。"""
        if not self._multiplex:
            return None
        with self._control_lock:
            if self._control_dir is None:
                base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
                self._control_dir = Path(base) / f"vibe-ssh-{os.getpid()}"
            try:
                self._control_dir.mkdir(parents=True, exist_ok=True)
                os.chmod(self._control_dir, 0o700)
            except OSError:
                # 目录建不出来就降级为不复用，而不是让整条链路挂掉。
                self._multiplex = False
                return None
            return self._control_dir

    def base_argv(self, *, extra_options: Sequence[str] = ()) -> list[str]:
        """拼出加固后的 sshargv。**所有会进 argv 的值都已在 connection 层校验过。**

        `extra_options` 只给本文件内部的专用入口用（目前只有端口转发），
        且必须是**代码拼出来的**常量或整数端口 —— 它不是一个用户可达的逃生口。
        """
        conn = self.conn
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "LogLevel=ERROR"]
        argv += ["-o", f"ConnectTimeout={conn.connect_timeout}"]
        argv += ["-o", "NumberOfPasswordPrompts=0"]
        argv += ["-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3"]

        if conn.host_key_policy == "strict":
            argv += ["-o", "StrictHostKeyChecking=yes"]
        elif conn.host_key_policy == "accept-new":
            argv += ["-o", "StrictHostKeyChecking=accept-new"]
        else:  # insecure —— 明确的用户选择，同时不留痕
            argv += [
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "GlobalKnownHostsFile=/dev/null",
            ]

        control_dir = self._ensure_control_dir()
        if control_dir is not None:
            argv += [
                "-o", "ControlMaster=auto",
                "-o", f"ControlPath={control_dir / '%C'}",
                "-o", "ControlPersist=120",
            ]

        identity = conn.identity_path()
        if identity is not None:
            argv += ["-i", str(identity)]
            # 限定只用指定密钥，避免 ssh-agent 里的一堆密钥逐个试到
            # MaxAuthTries 造成「太多认证失败」这种莫名其妙的报错。
            argv += ["-o", "IdentitiesOnly=yes"]
        if conn.proxy_jump:
            argv += ["-J", conn.proxy_jump]

        argv += list(extra_options)
        argv += ["-p", str(conn.port), conn.target]
        return argv

    def forward_argv(self, *, local_port: int, remote_port: int) -> list[str]:
        """拼一条**本地环回 → 远端环回**的转发命令。

        两个端口都必须是 `int` 且落在 1-65535：规格串不接受字符串拼接，
        免得 `"5173:127.0.0.1:22"` 这类输入被 ssh 原样解析成三段。

        `ExitOnForwardFailure=yes` 是必须的：没有它，端口占用时 ssh 会**照样
        建立起会话并保持运行**，我们看到的「进程活着」与「转发可用」就会脱钩，
        界面显示"已连接"而页面永远白屏。
        """
        for name, value in (("local_port", local_port), ("remote_port", remote_port)):
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
                raise RemoteError(
                    f"{name} 需为 1-65535 的整数（实际: {value!r}）", kind="config"
                )
        spec = f"{FORWARD_HOST}:{local_port}:{FORWARD_HOST}:{remote_port}"
        return self.base_argv(
            extra_options=["-T", "-o", "ExitOnForwardFailure=yes", "-L", spec]
        ) + [FORWARD_REMOTE_COMMAND]

    def spawn_forward(self, *, local_port: int, remote_port: int) -> subprocess.Popen[bytes]:
        """起一条转发进程。**stdin 必须留给我们持有**（见 `FORWARD_REMOTE_COMMAND`）。"""
        argv_full = self.forward_argv(local_port=local_port, remote_port=remote_port)
        try:
            return subprocess.Popen(  # noqa: S603 — argv 数组，不经本地 shell
                argv_full,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=_clean_env(),
            )
        except FileNotFoundError as exc:
            raise RemoteError(
                "本机找不到 ssh 客户端。请确认已安装 OpenSSH 客户端并在 PATH 中。",
                kind="transport",
                detail=str(exc),
            ) from None
        except OSError as exc:
            raise RemoteError(
                f"无法启动 ssh 转发进程: {exc}", kind="transport", detail=str(exc)
            ) from None

    # ------------------------------------------------------------------ 执行
    def _invoke(
        self,
        remote_command: str,
        *,
        timeout: int,
        stdin_bytes: bytes | None,
    ) -> RemoteResult:
        argv_full = self.base_argv() + [remote_command]
        started = time.monotonic()
        try:
            proc = subprocess.Popen(  # noqa: S603 — argv 数组，不经本地 shell
                argv_full,
                stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_clean_env(),
            )
        except FileNotFoundError as exc:
            raise RemoteError(
                "本机找不到 ssh 客户端。请确认已安装 OpenSSH 客户端并在 PATH 中。",
                kind="transport",
                detail=str(exc),
            ) from None
        except OSError as exc:
            raise RemoteError(
                f"无法启动 ssh 进程: {exc}", kind="transport", detail=str(exc)
            ) from None

        try:
            out, err = proc.communicate(input=stdin_bytes, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise RemoteError(
                f"远端执行超过 {timeout} 秒被终止（{self.conn.display}）。\n"
                "  建议：把长任务拆小，或提高该连接的 command_timeout（上限 3600）。",
                kind="transport",
            ) from None

        result = RemoteResult(
            returncode=proc.returncode or 0,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
            duration=time.monotonic() - started,
        )
        self._raise_if_transport_failure(result)
        return result

    def _invoke_bytes(
        self, remote_command: str, *, timeout: int, stdin_bytes: bytes | None
    ) -> tuple[int, bytes, str, float]:
        """二进制安全版本：stdout 保持 bytes（读文件用）。"""
        argv_full = self.base_argv() + [remote_command]
        started = time.monotonic()
        try:
            proc = subprocess.Popen(  # noqa: S603
                argv_full,
                stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_clean_env(),
            )
        except FileNotFoundError:
            raise RemoteError(
                "本机找不到 ssh 客户端。请确认已安装 OpenSSH 客户端并在 PATH 中。",
                kind="transport",
            ) from None
        except OSError as exc:
            raise RemoteError(f"无法启动 ssh 进程: {exc}", kind="transport") from None

        try:
            out, err = proc.communicate(input=stdin_bytes, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise RemoteError(
                f"远端读取超过 {timeout} 秒被终止（{self.conn.display}）。",
                kind="transport",
            ) from None

        duration = time.monotonic() - started
        err_text = err.decode("utf-8", errors="replace")
        self._raise_if_transport_failure(
            RemoteResult(proc.returncode or 0, "", err_text, duration)
        )
        return proc.returncode or 0, out, err_text, duration

    def _raise_if_transport_failure(self, result: RemoteResult) -> None:
        """区分「ssh 自己失败」与「远端命令失败」。

        只看退出码 255 是不够的：远端命令**也可能**返回 255。
        因此必须结合 stderr 特征 —— 有已知特征才判定为链路故障，
        否则如实当作远端命令的退出码向上报。
        """
        if result.returncode != SSH_FAILURE_RETURNCODE:
            return
        kind = _classify_ssh_error(result.stderr)
        if kind is None:
            return
        raise RemoteError(_transport_message(kind, result.stderr), kind=kind,
                          detail=result.stderr.strip()[:2000])

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout: int | None = None,
        stdin_bytes: bytes | None = None,
    ) -> RemoteResult:
        """在远端执行 argv（逐项引号包裹，不经过远端 shell 的解析语义）。

        注意：远端侧**无法完全避开 shell** —— ssh 协议本身就是把一个命令字符串
        交给远端登录 shell。我们能做的是把每个参数 `shlex.quote` 后拼装，
        使 shell 只做「按空白切分并去引号」，等价于 argv 传参。
        """
        return self._invoke(
            self._wrap_cwd(argv, cwd=cwd),
            timeout=timeout or self.conn.command_timeout,
            stdin_bytes=stdin_bytes,
        )

    def run_script(
        self,
        script: str,
        *,
        cwd: str | None = None,
        timeout: int | None = None,
        stdin_bytes: bytes | None = None,
        label: str = "script",
    ) -> RemoteResult:
        """执行一段 POSIX sh 脚本。

        脚本走「命令字符串」而不是 stdin 的 `sh -s`，是为了让 stdin 空出来
        传数据载荷（如写文件时的内容）。两者都要用 stdin 时只能二选一，
        而「脚本 + 载荷」是写回场景的刚需，因此选择脚本走参数。
        """
        wrapped = self._wrap_cwd_script(script, cwd=cwd, label=label)
        return self._invoke(
            wrapped,
            timeout=timeout or self.conn.command_timeout,
            stdin_bytes=stdin_bytes,
        )

    def _wrap_cwd(self, argv: list[str], *, cwd: str | None) -> str:
        if not argv:
            raise RemoteError("远端命令为空", kind="remote")
        directory = cwd or self.conn.root_normalized
        joined = " ".join(shlex.quote(str(part)) for part in argv)
        return f"cd {shlex.quote(directory)} && exec {joined}"

    def _wrap_cwd_script(self, script: str, *, cwd: str | None, label: str) -> str:
        directory = cwd or self.conn.root_normalized
        body = script.strip("\n")
        return (
            f"cd {shlex.quote(directory)} || exit 97\n"
            f"# {label}\n"
            f"{body}\n"
        )

    # ------------------------------------------------------------------ 探测
    def probe(self, *, force: bool = False) -> Capabilities:
        """探测远端能力并缓存。探测失败**抛出**，不静默给一份假能力表。"""
        with self._caps_lock:
            if self._caps is not None and not force:
                return self._caps
            result = self.run_script(_PROBE_SCRIPT, label="capability-probe")
            if result.returncode != 0:
                self._raise_remote_failure(result, what="能力探测")
            caps = self._parse_caps(result.stdout)
            self._caps = caps
            return caps

    def capabilities(self) -> Capabilities:
        """已探测则直接返回；未探测则探一次。"""
        return self.probe()

    def _parse_caps(self, stdout: str) -> Capabilities:
        raw: dict[str, str] = {}
        for line in stdout.splitlines():
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            raw[key.strip()] = value.strip()

        def flag(name: str) -> bool:
            return raw.get(name, "0") == "1"

        sha256_tool = ""
        if flag("HAS_sha256sum"):
            sha256_tool = "sha256sum"
        elif flag("HAS_shasum"):
            sha256_tool = "shasum -a 256"

        base64_decode = ""
        if flag("HAS_base64"):
            base64_decode = "base64 -d"

        return Capabilities(
            os_name=raw.get("OS", "?"),
            arch=raw.get("ARCH", "?"),
            kernel=raw.get("KERNEL", "?"),
            user=raw.get("USER", "?"),
            cwd=raw.get("PWD", "/"),
            shell=raw.get("SHELL", "?"),
            git_version=raw.get("GIT_VERSION", ""),
            python_version=raw.get("PY_VERSION", ""),
            find_printf=flag("FIND_PRINTF"),
            stat_gnu=flag("STAT_GNU"),
            stat_bsd=flag("STAT_BSD"),
            head_c=flag("HEAD_C"),
            ls_a1=flag("LS_A1"),
            root_writable=flag("ROOT_WRITABLE"),
            sha256_tool=sha256_tool,
            base64_decode=base64_decode,
            multiplexed=self._multiplex,
            raw=raw,
        )

    # ------------------------------------------------------------------ 文件
    def read_bytes(self, path: str, *, max_bytes: int, timeout: int | None = None) -> bytes:
        """读取远端文件（二进制安全）。超出 max_bytes 时抛错而不是静默截断。

        为什么**不静默截断**：截断后的内容喂给模型会被当成完整文件，
        进而「基于不存在的代码」做出改动 —— 这类错误极难排查。
        宁可明确报错让上层决定（截断展示 / 换手段）。
        """
        caps = self.capabilities()
        if caps.head_c:
            argv = ["head", "-c", str(max_bytes + 1), path]
            rc, out, err, _ = self._invoke_bytes(
                self._wrap_cwd(argv, cwd=None),
                timeout=timeout or self.conn.command_timeout,
                stdin_bytes=None,
            )
        else:
            # head -c 不支持：只能整段取回再本地截断（成本更高，如实降级）
            rc, out, err, _ = self._invoke_bytes(
                self._wrap_cwd(["cat", path], cwd=None),
                timeout=timeout or self.conn.command_timeout,
                stdin_bytes=None,
            )
        if rc != 0:
            raise RemoteError(
                f"读取远端文件失败：{path}\n  {err.strip()[:500]}",
                kind="notfound" if "No such file" in err else "remote",
                detail=err.strip()[:1000],
            )
        if len(out) > max_bytes:
            raise RemoteError(
                f"远端文件超过上限 {max_bytes} 字节（{path}）。\n"
                "  大文件请改用远端命令局部查看（如 sed -n / tail / grep）。",
                kind="remote",
            )
        return out

    def write_bytes(
        self,
        path: str,
        data: bytes,
        *,
        mode: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, object]:
        """原子写回远端文件，并**校验落盘字节数**。

        与本地 `commit_plan` 同一套路：同目录临时文件 → `mv` 顶替。
        `mv` 在同一文件系统内是 rename，具备原子性；跨文件系统的场景不成立，
        因此临时文件刻意放在**目标同目录**。

        写回后回读字节数比对：传输被截断（网络抖动、磁盘满）时报错，
        而不是留下一个「看起来写成功了」的半个文件。
        """
        caps = self.capabilities()
        directory = posixpath.dirname(path) or "/"
        quoted_path = shlex.quote(path)
        quoted_dir = shlex.quote(directory)

        existed = self.exists(path)
        existing_mode = mode
        if existed and existing_mode is None:
            existing_mode = self._mode_of(path)
            if existing_mode is None:
                raise RemoteError(
                    f"远端缺少可用的 stat，无法在保留原权限的前提下覆盖已存在文件：{path}\n"
                    "  处置：安装 coreutils（GNU stat）或改用远端 Agent；新建文件不受此限制。",
                    kind="transport",
                )
        target_mode = existing_mode or "644"

        if caps.raw.get("HAS_mktemp") == "1":
            make_tmp = f'mktemp {quoted_dir}/.vibe-remote.XXXXXX'
        else:
            make_tmp = f'printf %s {quoted_dir}/.vibe-remote.$$.tmp'

        script = (
            "set -eu\n"
            f'tmp=$({make_tmp})\n'
            'trap \'rm -f "$tmp"\' EXIT HUP INT TERM\n'
            'cat > "$tmp"\n'
            f'chmod {target_mode} "$tmp" 2>/dev/null || true\n'
            f'mv -f "$tmp" {quoted_path}\n'
            "trap - EXIT HUP INT TERM\n"
            f'wc -c < {quoted_path}\n'
        )
        result = self.run_script(
            script,
            timeout=timeout or self.conn.command_timeout,
            stdin_bytes=data,
            label=f"write {path}",
        )
        if result.returncode != 0:
            self._raise_remote_failure(result, what=f"写入 {path}")
        written = _last_int(result.stdout)
        if written is None or written != len(data):
            raise RemoteError(
                f"写回校验失败：期望 {len(data)} 字节，远端报告 {written!r} 字节（{path}）。\n"
                "  文件可能被截断，请重试或检查远端磁盘空间。",
                kind="integrity",
            )
        return {
            "bytes": len(data),
            "created": not existed,
            "mode": target_mode,
        }

    def delete_file(self, path: str, *, timeout: int | None = None) -> dict[str, object]:
        """删除远端文件（回退「本轮纯新建」的文件用）。

        与 `write_bytes` 同一套纪律：argv 逐项 `shlex.quote`（由 `run()` 统一做），
        只走 `rm -f`，不经过任何 shell 拼接。是否真的删掉用 `exists` 复核 ——
        `rm -f` 对不存在的文件也返回 0，不复核就无法区分「已删除」与「本来就不在」。
        """
        result = self.run(
            ["rm", "-f", "--", path], timeout=timeout or self.conn.command_timeout
        )
        if result.returncode != 0:
            self._raise_remote_failure(result, what=f"删除 {path}")
        return {"deleted": not self.exists(path), "path": path}

    def exists(self, path: str) -> bool:
        result = self.run(["test", "-e", path])
        return result.returncode == 0

    def is_dir(self, path: str) -> bool:
        return self.run(["test", "-d", path]).returncode == 0

    def _mode_of(self, path: str) -> str | None:
        caps = self.capabilities()
        if caps.stat_gnu:
            result = self.run(["stat", "-c", "%a", path])
        elif caps.stat_bsd:
            result = self.run(["stat", "-f", "%Lp", path])
        else:
            return None
        if result.returncode != 0:
            return None
        value = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
        # 只接受纯八进制，避免把远端异常输出当成权限写进 chmod（会变成命令注入面）
        if value and all(ch in "01234567" for ch in value) and len(value) <= 4:
            return value
        return None

    def list_dir(self, path: str) -> list[dict[str, object]]:
        """列目录。按远端能力选择实现，降级如实反映在结果来源里。"""
        caps = self.capabilities()
        if caps.has_python:
            script = (
                "import json,os,sys\n"
                "p=sys.argv[1] if len(sys.argv)>1 else '.'\n"
                "out=[]\n"
                "with os.scandir(p) as it:\n"
                "    for e in it:\n"
                "        try:\n"
                "            st=e.stat(follow_symlinks=False)\n"
                "            t='d' if e.is_dir(follow_symlinks=False) else "
                "('l' if e.is_symlink() else 'f')\n"
                "            out.append({'name':e.name,'type':t,'size':st.st_size})\n"
                "        except OSError:\n"
                "            out.append({'name':e.name,'type':'?','size':0})\n"
                "print(json.dumps(out))\n"
            )
            result = self.run(["python3", "-c", script, path])
            if result.returncode == 0:
                try:
                    data = json.loads(result.stdout.strip() or "[]")
                    if isinstance(data, list):
                        return [e for e in data if isinstance(e, dict)]
                except json.JSONDecodeError:
                    pass

        if caps.find_printf:
            result = self.run(
                ["find", path, "-mindepth", "1", "-maxdepth", "1", "-printf", "%y\t%s\t%f\n"]
            )
            if result.returncode == 0:
                entries: list[dict[str, object]] = []
                for line in result.stdout.splitlines():
                    parts = line.split("\t", 2)
                    if len(parts) != 3:
                        continue
                    kind, size_text, name = parts
                    entries.append(
                        {
                            "name": name,
                            "type": {"d": "d", "l": "l"}.get(kind, "f"),
                            "size": int(size_text) if size_text.isdigit() else 0,
                        }
                    )
                return entries

        # 最后一档：只有名字。**如实告知**，不让上层以为拿到了大小与类型。
        result = self.run(["ls", "-A", "-1", path])
        if result.returncode != 0:
            self._raise_remote_failure(result, what=f"列目录 {path}")
        return [
            {"name": name, "type": "?", "size": 0}
            for name in result.stdout.splitlines()
            if name.strip()
        ]

    def realpath(self, path: str) -> str | None:
        """解析远端真实路径（含符号链接）。能力不可用时返回 None。

        这是远端 jail 的**第二道防线**：文本级校验挡不住「工作区内的符号链接
        指向外部」。用 `realpath` / `python3 os.path.realpath` 拿真实路径，
        再由调用方确认它仍在工作区内。
        """
        caps = self.capabilities()
        if caps.has_python:
            script = "import os,sys;print(os.path.realpath(sys.argv[1]))"
            result = self.run(["python3", "-c", script, path])
        elif caps.raw.get("HAS_realpath") == "1":
            result = self.run(["realpath", path])
        else:
            return None
        if result.returncode != 0:
            return None
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return lines[-1] if lines else None

    def grep(
        self,
        pattern: str,
        *,
        path: str,
        ignore_case: bool = False,
        max_hits: int = 200,
        timeout: int | None = None,
    ) -> RemoteResult:
        """远端 grep。模式通过 argv 传入并逐项引号包裹，不经 shell 解释。"""
        argv = ["grep", "-rn", "-I", "--exclude-dir=.git"]
        if ignore_case:
            argv.append("-i")
        argv += ["-e", pattern, path]
        result = self.run(argv, timeout=timeout or self.conn.command_timeout)
        if result.returncode not in (0, 1):  # 1 = 无匹配，是正常结果
            self._raise_remote_failure(result, what=f"检索 {path}")
        return _truncate_lines(result, max_hits)

    # ------------------------------------------------------------------ 收尾
    def close(self) -> None:
        """关闭复用连接。失败无所谓（进程退出会带走套接字）。"""
        if not self._multiplex:
            return
        try:
            subprocess.run(  # noqa: S603
                self.base_argv()[: -1] + ["-O", "exit", self.conn.target],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                env=_clean_env(),
            )
        except (OSError, subprocess.SubprocessError):
            pass

    # ------------------------------------------------------------------ 内部
    def _raise_remote_failure(self, result: RemoteResult, *, what: str) -> None:
        stderr = result.stderr.strip()
        head = stderr[:800] if stderr else "（远端没有输出错误信息）"
        raise RemoteError(
            f"{what} 失败，远端退出码 {result.returncode}。\n  远端输出: {head}",
            kind="remote",
            detail=stderr[:2000],
        )


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _supports_multiplexing() -> bool:
    """本机 ssh 是否支持连接复用。

    Windows 的 OpenSSH **不支持** ControlMaster/ControlPath。相关选项虽然会被
    接受，但复用实际不生效，且可能因套接字路径语义差异给出误导性报错。
    因此直接在 Windows 上关掉它，并让能力报告如实说明「一条命令一次握手」。
    """
    return os.name != "nt"


def _clean_env() -> dict[str, str]:
    """子进程环境：剥离凭据类变量。

    复用 `tools/shell.scrub_env` 而不是另抄一份脱敏名单 —— 名单是安全红线，
    两处各写一份必然漂移。延迟导入是为了不让 remote ↔ tools 形成模块级耦合。
    """
    from ..tools.shell import scrub_env

    return scrub_env()


def _classify_ssh_error(stderr: str) -> str | None:
    """从 ssh 的 stderr 判断失败类型。认不出来返回 None（当作远端命令的退出码）。"""
    for signature, kind in _ERROR_SIGNATURES:
        if signature in stderr:
            return kind
    return None


def _transport_message(kind: str, stderr: str) -> str:
    head = stderr.strip().splitlines()
    first = head[0] if head else ""
    if kind == "auth":
        return f"远端认证失败：{first}\n{_AUTH_HELP}"
    if kind == "hostkey":
        return f"远端主机指纹校验未通过：{first}\n{_HOSTKEY_HELP}"
    if kind == "unreachable":
        return f"远端不可达：{first}\n{_UNREACHABLE_HELP}"
    return f"ssh 执行失败：{first or stderr.strip()[:300]}"


def _last_int(text: str) -> int | None:
    for line in reversed(text.strip().splitlines()):
        candidate = line.strip()
        if candidate.isdigit():
            return int(candidate)
    return None


def _truncate_lines(result: RemoteResult, max_lines: int) -> RemoteResult:
    lines = result.stdout.splitlines()
    if len(lines) <= max_lines:
        return result
    kept = lines[:max_lines]
    kept.append(f"... 另有 {len(lines) - max_lines} 行被省略 ...")
    return RemoteResult(
        returncode=result.returncode,
        stdout="\n".join(kept),
        stderr=result.stderr,
        duration=result.duration,
    )


__all__ = [
    "FORWARD_HOST",
    "FORWARD_REMOTE_COMMAND",
    "MAX_OUTPUT_CHARS",
    "Capabilities",
    "RemoteResult",
    "SSHTransport",
]
