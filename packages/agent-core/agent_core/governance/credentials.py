"""客户端凭证存储。

放在用户级目录（`~/.vibe/credentials.json`）而不是项目目录，理由有二：

1. **设备身份是「这台机器」的属性，不是「这个项目」的。** 放项目里会让
   同一个人在不同仓库里被当成不同设备，设备列表立刻变成噪音，
   而"撤销某台设备"也就失去意义。
2. **项目目录可能被提交、被打包、被共享。** 凭证放进去，
   迟早会有人 `git add -A` 把它带上。用户级目录天然不会被项目工具碰到。

**这个文件里只有短期访问令牌与可撤销的刷新令牌，没有任何模型厂商密钥。**
这是 ADR-007 在客户端侧的落地：客户端永不持有云端 API Key，
模型请求经网关代理，密钥在服务端注入。

权限收紧（0600）在 POSIX 上是硬约束 —— 权限过宽会**拒绝加载**而不是警告。
Windows 上 chmod 语义不同，只做尽力而为（README 已说明该靠用户目录 ACL）。
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_DIR = Path.home() / ".vibe"
CREDENTIALS_FILE = "credentials.json"

# 提前这么久就认为令牌该刷新了。网络往返 + 时钟偏差都要留余量，
# 否则会出现「刚好在边界上过期」的偶发 401，且极难复现。
EXPIRY_SKEW = 90

# 机器指纹：用于让服务端识别「同一台机器重复登录」，避免设备列表堆积。
# 只用 hostname + 用户名 + 一个本地随机盐 —— **不采集硬件序列号**，
# 那是过度采集，且在企业环境里可能触发合规问题。
_SALT_FILE = "device.salt"


@dataclass
class Credentials:
    """一份设备凭证。"""

    server: str = ""
    user_id: str = ""
    email: str = ""
    device_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    expires_at: int = 0
    fingerprint: str = ""
    obtained_at: int = 0
    # 上次上传审计后的链头。下次上传要用它做起点，否则链头对不上。
    audit_chain_head: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ 判定
    def is_expired(self, *, skew: int = EXPIRY_SKEW) -> bool:
        if not self.access_token:
            return True
        return self.expires_at <= int(time.time()) + skew

    def is_usable(self, server: str = "") -> bool:
        if not self.access_token and not self.refresh_token:
            return False
        if server and self.server and server.rstrip("/") != self.server.rstrip("/"):
            return False
        return True

    def describe(self) -> dict[str, Any]:
        """给 UI / `status` 命令看的视图 —— **不含任何令牌**。"""
        return {
            "server": self.server,
            "userId": self.user_id,
            "email": self.email,
            "deviceId": self.device_id,
            "expiresAt": self.expires_at,
            "expired": self.is_expired(),
            "hasRefreshToken": bool(self.refresh_token),
            "obtainedAt": self.obtained_at,
        }

    def __repr__(self) -> str:  # 防止令牌被 repr 进日志
        return (
            f"Credentials(server={self.server!r}, userId={self.user_id!r},"
            f" deviceId={self.device_id!r}, access=***, refresh=***)"
        )


class CredentialStore:
    def __init__(self, path: str | Path | None = None, *, strict_permissions: bool = True) -> None:
        if path is None:
            path = DEFAULT_DIR / CREDENTIALS_FILE
        self.path = Path(path).expanduser()
        self.strict_permissions = strict_permissions

    # ------------------------------------------------------------------ 读写
    def load(self) -> Credentials | None:
        if not self.path.exists():
            return None
        if self.strict_permissions:
            self._assert_safe()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 文件损坏时不抛异常，而是当作「没有凭证」。
            # 抛异常会让用户卡在一个无法登录的状态里 —— 实际上重新登录
            # 完全可以解决，而他会因为命令直接失败而不知道该干什么。
            return None
        if not isinstance(raw, dict):
            return None
        known = {f for f in Credentials.__dataclass_fields__}
        return Credentials(**{k: v for k, v in raw.items() if k in known})

    def save(self, credentials: Credentials) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(credentials)
        # 原子写：先写临时文件再 rename。直接覆写的话，进程在写一半时被杀
        # 会留下半个 JSON，下次 load 直接失败 —— 表现为「莫名其妙需要重新登录」。
        fd, tmp_name = tempfile.mkstemp(
            prefix=".credentials-", dir=str(self.path.parent), text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        self._restrict()

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------ 权限
    def _restrict(self) -> None:
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _assert_safe(self) -> None:
        if os.name == "nt":
            return  # Windows 上 st_mode 不可靠，改由用户目录 ACL 保证
        try:
            mode = self.path.stat().st_mode & 0o777
        except OSError:
            return
        if mode & 0o077:
            raise PermissionError(
                f"凭证文件 {self.path} 权限过宽（{oct(mode)}），其他用户可读。\n"
                f"  修复：chmod 600 {self.path}\n"
                "  凭证里有访问令牌，泄漏等于他人可以代你调用模型（并记在你账上）。"
            )

    # ------------------------------------------------------------------ 指纹
    def fingerprint(self) -> str:
        """本机指纹。取自 hostname + 用户名 + 本地盐，稳定且不含硬件标识。"""
        import hashlib
        import platform

        salt = self._load_salt()
        material = f"{platform.node()}|{_username()}|{salt}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()[:32]

    def _load_salt(self) -> str:
        path = self.path.parent / _SALT_FILE
        if path.exists():
            try:
                value = path.read_text(encoding="utf-8").strip()
                if value:
                    return value
            except OSError:
                pass
        value = secrets.token_hex(16)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")
            os.chmod(path, 0o600)
        except OSError:
            pass  # 写不进就用一次性盐，指纹不跨进程稳定但功能不受影响
        return value


def _username() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


__all__ = [
    "CREDENTIALS_FILE",
    "DEFAULT_DIR",
    "EXPIRY_SKEW",
    "CredentialStore",
    "Credentials",
]
