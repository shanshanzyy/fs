"""密钥托管（SecretStore）。

ADR-007：云端密钥只在服务端，客户端零长期凭证。这一层就是那个「只在服务端」
的落点。三件事必须做到：

1. **明文只在内存里短暂出现。** 上层拿到的是「引用名 → 明文」的一次查询，
   用完即弃；返回值不写日志、不进审计、不参与序列化。
2. **可替换。** 内网起步用文件/环境变量，接到 Vault 就换实现，
   上层代码一行不改。这是 `SecretStore` 作为抽象基类的全部意义。
3. **有台账。** 每次读取都记「谁在什么时候取了哪个密钥」，且
   **台账里只有指纹**，没有值。密钥轮换提醒也靠台账的 `updated_at`。

`_redact()` 是这里的守门员：任何要把密钥放进日志/错误的路径都必须经过它。
宁可看到 `sk-1234…cdef` 这种半遮蔽，也不要为了「方便排查」打全量。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..errors import ConfigError

MAX_SECRET_BYTES = 16 << 10
_FETCH_TIMEOUT = 10


# --------------------------------------------------------------------------- 遮蔽
def fingerprint(value: str) -> str:
    """密钥指纹：sha256 前 16 位。台账与审计只存这个。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def redact(value: str) -> str:
    """把密钥变成可安全写进日志的样子。

    短串全遮蔽，长串留头尾各 4 位 —— 保留头尾是为了能在「是不是配错了密钥」
    这类排查中一眼对上，而不是为了让人还原它。
    """
    if not value:
        return "(空)"
    if len(value) <= 12:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}（len={len(value)}）"


# 错误信息里出现这些形态的串，一律替换掉再往外抛。
# 上游的报错原文经常带着 "invalid api key: sk-xxx"，直接透传就泄漏了。
_LEAK_PATTERNS = (
    re.compile(r"\b(?:sk|pk|ghp|glpat|xox[baprs]|AKIA|ASIA)[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{12,}", re.IGNORECASE),
    re.compile(r"\b[0-9a-fA-F]{40,}\b"),
)


def sanitize_error(text: str, *, limit: int = 400) -> str:
    """把上游返回的文本清洗成可安全记录的形式。

    上游的错误正文可能含密钥、可能含用户 prompt 片段，两者都不该进日志。
    截断也是必要的：上游偶尔会回一个巨大的 HTML 错误页。
    """
    cleaned = text or ""
    for pattern in _LEAK_PATTERNS:
        cleaned = pattern.sub("[REDACTED]", cleaned)
    cleaned = cleaned.replace("\r", " ").replace("\n", " ").strip()
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + f"…（共 {len(text)} 字符）"
    return cleaned


# --------------------------------------------------------------------------- 接口
@dataclass(frozen=True)
class SecretRecord:
    name: str
    value: str        # 明文，只应在网关内部存活
    source: str       # env | file | vault

    def __repr__(self) -> str:  # 防止明文被 repr 进日志
        return f"SecretRecord(name={self.name!r}, value={redact(self.value)!r}, source={self.source!r})"


class SecretStore(ABC):
    """密钥来源。实现必须保证：读取失败要么抛异常，要么返回 None，绝不返回空串当"成功"。"""

    provider = "base"

    @abstractmethod
    def get(self, name: str) -> SecretRecord | None:
        """按引用名取密钥。找不到返回 None（由调用方决定是报错还是降级）。"""

    @abstractmethod
    def names(self) -> list[str]:
        """已知的密钥名列表。用于后台轮换提醒，**不含任何值**。"""

    def describe(self) -> dict[str, object]:
        return {"provider": self.provider, "names": sorted(self.names())}

    def health(self) -> tuple[bool, str]:
        return True, f"{self.provider} 就绪"


# --------------------------------------------------------------------------- env
class EnvSecretStore(SecretStore):
    """从环境变量读。名形如 `VIBE_SRV_SECRET_<NAME>`。

    名字里带 `SECRET` 而不是直接 `VIBE_SRV_`：这样启动脚本里
    「哪些变量是密钥、哪些是普通配置」一眼可分，运维不容易漏配。
    """

    provider = "env"
    prefix = "VIBE_SRV_SECRET_"

    def __init__(self, *, env: dict[str, str] | None = None) -> None:
        self._env = env if env is not None else dict(os.environ)

    def _key(self, name: str) -> str:
        # 统一成大写、非字母数字变下划线，让 `cloud-main` 与 `cloud_main` 等价
        normalized = re.sub(r"[^A-Za-z0-9]", "_", name).upper()
        return self.prefix + normalized

    def get(self, name: str) -> SecretRecord | None:
        raw = self._env.get(self._key(name))
        if raw is None or raw == "":
            return None
        return SecretRecord(name=name, value=raw, source=self.provider)

    def names(self) -> list[str]:
        return [
            key[len(self.prefix) :].lower()
            for key in self._env
            if key.startswith(self.prefix) and self._env[key]
        ]


# --------------------------------------------------------------------------- file
class FileSecretStore(SecretStore):
    """从 JSON 文件读。文件权限必须在加载时校验。

    权限校验是刻意的「启动即失败」而不是「运行时警告」：
    一个 0644 的密钥文件在共享机器上等于半公开，而这种问题
    只有在你强制它失败时才会被修。
    """

    provider = "file"

    def __init__(self, path: str | Path, *, allow_insecure: bool = False) -> None:
        self.path = Path(path)
        self._cache: dict[str, str] | None = None
        if not self.path.exists():
            # doctor 会把这条原文打进「发现 N 个问题」，所以它必须自带修复动作，
            # 否则运维看到的就是「少了点东西」而不知道该敲哪条命令。
            raise ConfigError(
                f"密钥文件不存在: {self.path}\n"
                f"  修复：创建它（JSON 对象，键为引用名、值为非空字符串）并执行"
                f" chmod 600 {self.path}；或改用 SECRETS_PROVIDER=env。"
            )
        self._check_permissions(allow_insecure=allow_insecure)

    def _check_permissions(self, *, allow_insecure: bool) -> None:
        try:
            mode = self.path.stat().st_mode & 0o777
        except OSError as exc:
            raise ConfigError(f"无法读取密钥文件属性: {exc}") from exc
        # Windows 上 chmod 语义不同，mode 常常是 0o666；不能据此判失败
        if os.name == "nt":
            return
        if mode & 0o077 and not allow_insecure:
            raise ConfigError(
                f"密钥文件 {self.path} 权限过宽（{oct(mode)}），其他用户可读。\n"
                f"  修复：chmod 600 {self.path}\n"
                "  确认过风险想跳过检查，请设置 VIBE_SRV_SECRETS_INSECURE=1。"
            )

    def _load(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise ConfigError(f"读取密钥文件失败: {exc}") from exc
        if len(raw) > MAX_SECRET_BYTES:
            raise ConfigError(f"密钥文件过大（>{MAX_SECRET_BYTES} 字节），疑似配错")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ConfigError(f"密钥文件不是合法 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError("密钥文件顶层必须是对象（键为引用名，值为字符串）")

        parsed: dict[str, str] = {}
        for key, value in data.items():
            if not isinstance(value, str) or not value:
                raise ConfigError(f"密钥 {key!r} 的值必须是非空字符串")
            parsed[str(key)] = value
        self._cache = parsed
        return parsed

    def get(self, name: str) -> SecretRecord | None:
        value = self._load().get(name)
        if value is None:
            return None
        return SecretRecord(name=name, value=value, source=self.provider)

    def names(self) -> list[str]:
        return sorted(self._load())

    def reload(self) -> None:
        """轮换密钥后重载。缓存必须能主动失效，否则改了文件要重启服务。"""
        self._cache = None


# --------------------------------------------------------------------------- vault
class VaultSecretStore(SecretStore):
    """HashiCorp Vault KV v2。

    ⚠️ **未验证（M2 定档结论，见 `apps/server/README.md` §边界）**：
    内网运维确认没有 Vault，所以本分支**从未对着真实 Vault 跑过**，
    服务端侧只有「配置闸门」被测试钉住（缺 addr/token 要在启动前失败）。
    **切换条件**：内网接入 Vault 时 —— 届时补一个假 Vault HTTP 服务的
    协议层测试（对齐 `tests/test_oidc.py` 的做法），再解除本条标记。

    用 KV v2 而不是 v1：v2 带版本历史，误覆盖能回滚 ——
    密钥管理最怕的事故之一是「覆盖成错的值，而且没有旧值可退」。

    路径按 `mount/data/<name>` 拼，读 `data.value` 字段。
    这是 KV v2 的约定；写死它比「可配置字段名」更好，
    因为可配置意味着每个人配的都不一样，出问题时没人知道去哪找。
    """

    provider = "vault"

    def __init__(self, *, addr: str, token: str, mount: str = "secret", timeout: int = _FETCH_TIMEOUT) -> None:
        if not addr:
            raise ConfigError("SECRETS_PROVIDER=vault 时必须配置 VAULT_ADDR")
        if not token:
            raise ConfigError("SECRETS_PROVIDER=vault 时必须配置 VAULT_TOKEN")
        self.addr = addr.rstrip("/")
        self._token = token
        self.mount = mount.strip("/") or "secret"
        self.timeout = timeout

    def _request(self, path: str) -> dict | None:
        url = f"{self.addr}/v1/{self.mount}/data/{urllib.parse.quote(path, safe='')}"
        request = urllib.request.Request(
            url,
            headers={
                "X-Vault-Token": self._token,
                "Accept": "application/json",
                "User-Agent": "vibe-governance/0.1",
            },
        )
        # Vault 通常在内网，绕过系统代理
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=self.timeout) as response:
                payload = json.loads(response.read(MAX_SECRET_BYTES).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            # Vault 的错误正文里不会含密钥，但仍做清洗，防御性处理
            detail = sanitize_error(exc.read(1000).decode("utf-8", "replace"))
            raise ConfigError(f"Vault 读取 {path!r} 失败（HTTP {exc.code}）: {detail}") from exc
        except (urllib.error.URLError, ValueError, UnicodeDecodeError) as exc:
            raise ConfigError(f"Vault 不可达或响应异常: {exc}") from exc
        if not isinstance(payload, dict):
            return None
        return payload

    def get(self, name: str) -> SecretRecord | None:
        payload = self._request(name)
        if payload is None:
            return None
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            return None
        # KV v2 把自定义字段包在 data.data 里
        inner = data.get("data") if isinstance(data.get("data"), dict) else data
        value = inner.get("value") if isinstance(inner, dict) else None
        if not isinstance(value, str) or not value:
            raise ConfigError(
                f"Vault 里的 {name!r} 缺少非空字符串字段 value"
                "（约定：KV v2，路径 data/<name>，字段 value）"
            )
        return SecretRecord(name=name, value=value, source=self.provider)

    def names(self) -> list[str]:
        """列出该 mount 下的密钥名。

        需要 Vault 的 LIST 能力，有些策略只给读不给列 —— 这时返回空列表，
        而不是报错：台账缺失是可以接受的降级，把服务卡住不可以。
        """
        url = f"{self.addr}/v1/{self.mount}/metadata?list=true"
        request = urllib.request.Request(
            url,
            headers={"X-Vault-Token": self._token, "Accept": "application/json"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=self.timeout) as response:
                payload = json.loads(response.read(MAX_SECRET_BYTES).decode("utf-8"))
        except Exception:  # noqa: BLE001 — 列不出名字不算故障
            return []
        keys = (payload.get("data") or {}).get("keys") if isinstance(payload, dict) else None
        if not isinstance(keys, list):
            return []
        # 以 / 结尾的是目录，扁平化后仍返回，交给调用方判断
        return sorted(str(k).rstrip("/") for k in keys)


# --------------------------------------------------------------------------- 工厂
def open_secret_store(config: Any) -> SecretStore:
    provider = str(getattr(config, "secrets_provider", "env") or "env").lower()
    if provider == "env":
        return EnvSecretStore()
    if provider == "file":
        path = getattr(config, "secrets_file", None)
        if not path:
            raise ConfigError(
                "SECRETS_PROVIDER=file 时必须配置 SECRETS_FILE\n"
                "  修复：设置 SECRETS_FILE=/绝对路径/secrets.json，"
                "或删掉 SECRETS_PROVIDER 用默认的 env 后端。"
            )
        return FileSecretStore(
            path,
            allow_insecure=os.environ.get("VIBE_SRV_SECRETS_INSECURE", "") not in ("", "0"),
        )
    if provider == "vault":
        return VaultSecretStore(
            addr=str(getattr(config, "vault_addr", "")),
            token=str(getattr(config, "vault_token", "")),
            mount=str(getattr(config, "vault_mount", "secret")),
        )
    raise ConfigError(
        f"未知的密钥托管方式: {provider}（可选 env / file / vault）\n"
        "  修复：把 SECRETS_PROVIDER 设为 env / file / vault 之一。"
    )


# --------------------------------------------------------------------------- 台账
def rotation_report(rows: list[dict], *, max_age_days: int, now: int) -> list[dict]:
    """生成轮换提醒。

    架构文档要求 90 天轮换。这里不自动轮换 —— 自动轮换需要能写回
    Vault/env，而三种 provider 里只有 Vault 支持写；
    做成「提醒」可以在任何 provider 上都工作，且把决定权留给人。
    """
    result: list[dict] = []
    max_age = max(1, int(max_age_days)) * 86400
    for row in rows:
        updated = int(row.get("updated_at") or 0)
        age = now - updated if updated else 0
        if updated and age < max_age:
            continue
        result.append(
            {
                "name": str(row.get("name") or ""),
                "provider": str(row.get("provider") or ""),
                "fingerprint": str(row.get("fingerprint") or ""),
                "ageDays": (age // 86400) if updated else None,
                "overdue": True,
                "action": "轮到该换密钥了：更新后用同一名字写入，服务端重载即可",
            }
        )
    return result


__all__ = [
    "EnvSecretStore",
    "FileSecretStore",
    "SecretRecord",
    "SecretStore",
    "VaultSecretStore",
    "fingerprint",
    "open_secret_store",
    "redact",
    "rotation_report",
    "sanitize_error",
]
