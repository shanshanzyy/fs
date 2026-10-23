"""远程设备连接模型。

一条连接回答两个问题：**连哪台机器**（host/user/port/密钥/跳板）与
**在那台机器的哪个目录下工作**（root）。除此之外的一切都在别处决定。

三个刻意的设计取舍，都是为了「不把 ssh 变成一个注入面」：

1. **不提供 `extra_ssh_args` 逃生口。** 让用户塞任意 ssh 参数等于让他塞
   `-o StrictHostKeyChecking=no`、`-F /dev/null`、甚至 `ProxyCommand=…` ——
   本文件辛苦拼出来的加固选项会被一行配置整体推翻。需要的能力
   （跳板机、指定密钥、端口）都做成**具名字段**，逐项校验。
2. **所有会进 argv 的字段都不允许以 `-` 开头。** ssh 的参数解析是
   「遇到不认识的 `-x` 就报错，但把后面的当参数」，而 `host` 恰恰在最后 ——
   一个名为 `-oProxyCommand=…` 的「主机名」会被 ssh 当成选项吃掉。
   正则强制首字符为字母数字，从根上堵死这条路（`_HOST_RE` / `_USER_RE`）。
3. **root 必须是绝对 POSIX 路径且不能含 `..` 残留。** 远端路径 jail 是
   **文本级**校验（本地 `Path.resolve()` 够不着远端文件系统），所以 root
   本身必须先干净，否则整条 jail 的地基就是歪的。

密码认证**刻意不支持**：ssh 被以 `BatchMode=yes` 调起，任何交互式提示都会
直接失败。在 Agent 上下文里没有安全的密码输入通道（把密码写进配置文件或
环境变量，风险远高于收益），因此只用公钥 / ssh-agent。
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .errors import RemoteConfigError

# 主机名 / IP：首尾必须是字母数字，中间允许点、下划线、连字符。
# 首字符非字母数字 → 不可能与 ssh 选项混淆（见模块头第 2 条）。
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,253}[A-Za-z0-9])?$")
# IPv6 字面量必须带方括号（`[::1]`）—— ssh 的 host 字段语法如此
_IPV6_RE = re.compile(r"^\[[0-9A-Fa-f:.]{1,45}\]$")
# 远端用户名：POSIX 允许的保守子集
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")
# 连接名：要能当文件名与 JSON 键用，因此只收 ASCII 保守子集
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")

# 主机指纹策略。三态而非布尔，因为「首次连接自动接受」与
# 「指纹变了也照连」是**性质完全不同**的两件事，不能压成一个开关：
#   strict       只接受 known_hosts 里已有的指纹（生产推荐）
#   accept-new   known_hosts 里没有就记录，**变了则拒绝**（默认；与 OpenSSH 8 对齐）
#   insecure     完全不校验 —— 存在中间人风险，必须由用户显式选择
HOST_KEY_POLICIES = ("strict", "accept-new", "insecure")


@dataclass(frozen=True)
class RemoteConnection:
    """一条远程设备连接。frozen：连接一旦登记就不该被就地处改。"""

    name: str
    host: str
    user: str = ""
    port: int = 22
    # 远端工作目录（绝对 POSIX 路径）。所有远端相对路径都相对它解析。
    root: str = "/"
    # 本地私钥路径（空 = 交给 ssh-agent / 默认密钥）。
    identity_file: str = ""
    # 跳板机：`[user@]host[:port]`。内网设备常需要它。
    proxy_jump: str = ""
    host_key_policy: str = "accept-new"
    connect_timeout: int = 10
    command_timeout: int = 120
    # 是否允许在此连接上写远端文件。**默认关**：加连接时默认只读，
    # 想改代码必须显式把它打开（纵深防御，见 tools/remote.py 的门控说明）。
    allow_write: bool = False
    # 是否允许在此连接上执行远端命令（跑测试 / 构建）。同样默认关。
    allow_shell: bool = False
    note: str = ""
    created_at: str = ""

    # ------------------------------------------------------------------ 校验
    def validate(self) -> None:
        """结构校验：字段格式。**不碰本地文件系统**（见 validate_local）。"""
        problems = self.problems()
        if problems:
            raise RemoteConfigError(
                f"连接 {self.name or '(未命名)'} 配置非法：\n  - "
                + "\n  - ".join(problems)
            )

    def problems(self) -> list[str]:
        """返回全部问题而不是「第一个问题」—— 一次让用户改完，而不是挤牙膏。"""
        out: list[str] = []

        if not _NAME_RE.match(self.name or ""):
            out.append(
                "name 需为 1-32 位字母/数字/下划线/点/连字符且以字母数字开头"
                f"（实际: {self.name!r}）"
            )
        if not (_HOST_RE.match(self.host or "") or _IPV6_RE.match(self.host or "")):
            out.append(
                f"host 格式非法（实际: {self.host!r}）；"
                "IPv6 请写成带方括号的形式，如 [fe80::1]"
            )
        if self.user and not _USER_RE.match(self.user):
            out.append(f"user 格式非法（实际: {self.user!r}）")
        if not isinstance(self.port, int) or isinstance(self.port, bool):
            out.append(f"port 需为整数（实际: {self.port!r}）")
        elif not 1 <= self.port <= 65535:
            out.append(f"port 超出范围 1-65535（实际: {self.port}）")

        root = self.root or ""
        if "\x00" in root:
            out.append("root 含空字节")
        elif not root.startswith("/"):
            out.append(f"root 必须是绝对路径（以 / 开头，实际: {root!r}）")
        else:
            # 必须看**原始**分段，不能看 normpath 的结果：
            # normpath 会把 `..` 消解掉，`/srv/../../etc` 归一化后是 `/etc`，
            # 那时再找 `..` 永远找不到 —— 校验就地形同虚设。
            segments = [seg for seg in root.split("/") if seg not in ("", ".")]
            if any(seg == ".." for seg in segments):
                out.append(f"root 含 .. 段（实际: {root!r}）")

        if self.proxy_jump:
            out.extend(self._proxy_jump_problems())

        if self.host_key_policy not in HOST_KEY_POLICIES:
            out.append(
                "host_key_policy 需为 "
                + "/".join(HOST_KEY_POLICIES)
                + f" 之一（实际: {self.host_key_policy!r}）"
            )
        for field_name, value in (
            ("connect_timeout", self.connect_timeout),
            ("command_timeout", self.command_timeout),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                out.append(f"{field_name} 需为整数（实际: {value!r}）")
            elif not 1 <= value <= 3600:
                out.append(f"{field_name} 超出范围 1-3600（实际: {value}）")
        if self.identity_file and not self.identity_file.strip():
            out.append("identity_file 为空字符串，请留空字段或给出有效路径")
        return out

    def _proxy_jump_problems(self) -> list[str]:
        """跳板机语法校验：`[user@]host[:port]`。

        跳板机串会原样进 `-J` 参数，因此和 host 一样必须挡住 `-` 开头的值。
        """
        text = self.proxy_jump.strip()
        user_part = ""
        if "@" in text:
            user_part, text = text.rsplit("@", 1)
        host_part, port_part = text, ""
        if host_part.startswith("["):  # IPv6
            end = host_part.find("]")
            if end < 0:
                return [f"proxy_jump 的 IPv6 字面量缺少 ]（实际: {self.proxy_jump!r}）"]
            rest = host_part[end + 1 :]
            host_part, port_part = host_part[: end + 1], rest.lstrip(":")
        elif ":" in host_part:
            host_part, port_part = host_part.rsplit(":", 1)

        out: list[str] = []
        if user_part and not _USER_RE.match(user_part):
            out.append(f"proxy_jump 的 user 非法（实际: {user_part!r}）")
        if not (_HOST_RE.match(host_part) or _IPV6_RE.match(host_part)):
            out.append(f"proxy_jump 的 host 非法（实际: {host_part!r}）")
        if port_part:
            if not port_part.isdigit() or not 1 <= int(port_part) <= 65535:
                out.append(f"proxy_jump 的 port 非法（实际: {port_part!r}）")
        return out

    def validate_local(self) -> None:
        """依赖本地文件系统的校验：私钥存在且是普通文件。

        与 validate() 分开的原因：纯结构校验要能在没有本地磁盘的场合跑
        （比如服务端下发一份连接清单做格式检查），而密钥存在性只在
        「我本机真要发起连接」时才有意义。
        """
        self.validate()
        if not self.identity_file:
            return
        path = self.identity_path()
        if path is None:
            raise RemoteConfigError(
                f"identity_file 无法解析：{self.identity_file!r}"
            )
        if not path.exists():
            raise RemoteConfigError(f"私钥文件不存在：{path}")
        if not path.is_file():
            raise RemoteConfigError(f"identity_file 不是普通文件：{path}")

    def identity_path(self) -> Path | None:
        """私钥的本地绝对路径。空值返回 None。"""
        if not self.identity_file:
            return None
        try:
            return Path(self.identity_file).expanduser()
        except (ValueError, OSError):  # Windows 上的非法路径字符等
            return None

    # ------------------------------------------------------------------ 派生
    @property
    def root_normalized(self) -> str:
        """归一化后的 root（去掉尾斜杠、消解 `.`）。"""
        normalized = posixpath.normpath(self.root or "/")
        return "/" if normalized == "." else normalized

    @property
    def target(self) -> str:
        """ssh 的 target 字段：`user@host` 或 `host`。"""
        return f"{self.user}@{self.host}" if self.user else self.host

    @property
    def key(self) -> str:
        """身份指纹：任一字段变了就是另一条链路，用于复用在位传输。

        刻意包含 root：同一个 host 上不同项目目录是两条独立工作区，
        共用一个传输会混淆 cwd 与能力探测结果。
        """
        return "|".join(
            (
                self.target,
                str(self.port),
                self.root_normalized,
                self.identity_file,
                self.proxy_jump,
                self.host_key_policy,
            )
        )

    @property
    def display(self) -> str:
        """展示名：`user@host:port → /root`。"""
        return f"{self.target}:{self.port} → {self.root_normalized}"

    @property
    def writable(self) -> bool:
        return self.allow_write

    @property
    def executable(self) -> bool:
        return self.allow_shell

    # ------------------------------------------------------------------ 序列化
    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "host": self.host,
            "user": self.user,
            "port": self.port,
            "root": self.root_normalized,
            "identity_file": self.identity_file,
            "proxy_jump": self.proxy_jump,
            "host_key_policy": self.host_key_policy,
            "connect_timeout": self.connect_timeout,
            "command_timeout": self.command_timeout,
            "allow_write": self.allow_write,
            "allow_shell": self.allow_shell,
            "note": self.note,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RemoteConnection":
        """从 JSON 反序列化。未知键**静默丢弃**，缺失键用默认值。

        为什么不在未知键上报错：连接清单可能被更新版本的客户端写过，
        旧客户端读到新字段就崩会让人没法降级。丢弃 + 结构校验兜底更稳。
        """
        if not isinstance(raw, dict):
            raise RemoteConfigError(f"连接条目需为对象，实际: {type(raw).__name__}")

        def _str(key: str, default: str = "") -> str:
            value = raw.get(key, default)
            return value if isinstance(value, str) else default

        def _int(key: str, default: int) -> int:
            value = raw.get(key, default)
            return value if isinstance(value, int) and not isinstance(value, bool) else default

        def _bool(key: str, default: bool = False) -> bool:
            value = raw.get(key, default)
            return value if isinstance(value, bool) else default

        conn = cls(
            name=_str("name"),
            host=_str("host"),
            user=_str("user"),
            port=_int("port", 22),
            root=_str("root", "/") or "/",
            identity_file=_str("identity_file"),
            proxy_jump=_str("proxy_jump"),
            host_key_policy=_str("host_key_policy", "accept-new"),
            connect_timeout=_int("connect_timeout", 10),
            command_timeout=_int("command_timeout", 120),
            allow_write=_bool("allow_write"),
            allow_shell=_bool("allow_shell"),
            note=_str("note"),
            created_at=_str("created_at"),
        )
        conn.validate()
        return conn

    def with_changes(self, **changes: object) -> "RemoteConnection":
        """派生一条改了若干字段的新连接（frozen 的写入口）。"""
        return replace(self, **changes)


def make_connection(
    name: str,
    host: str,
    *,
    user: str = "",
    port: int = 22,
    root: str = "/",
    **rest: object,
) -> RemoteConnection:
    """构造并校验一条连接。CLI 与新工具都走这里，保证校验不被绕过。"""
    conn = RemoteConnection(name=name, host=host, user=user, port=port, root=root, **rest)  # type: ignore[arg-type]
    conn.validate()
    return conn


__all__ = [
    "HOST_KEY_POLICIES",
    "RemoteConnection",
    "make_connection",
]
