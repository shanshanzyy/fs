"""服务端配置。

优先级：显式参数 > 环境变量 > 默认值。环境变量统一 `VIBE_SRV_` 前缀，
与 agent-core 的 `VIBE_`（客户端）分开，避免同一台机器上部署两边时串味。

一处刻意的设计：**JWT 签名密钥不允许有硬编码默认值**。
没有配置时会在数据目录生成一份随机密钥并落盘（0600），
而不是退回某个固定字符串 —— 否则「默认密钥没改」会变成全公司的通用通行证。
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, replace
from pathlib import Path

from .errors import ConfigError

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}
_PREFIX = "VIBE_SRV_"

# 共享存储 URL 的 scheme。命中即为「多实例意图」（见 ServerConfig.multi_instance_intent）。
_SHARED_DB_SCHEMES = {"postgres", "postgresql", "psql"}

_MULTI_INSTANCE_JWT_HINT = (
    "多实例模式必须显式配置 VIBE_SRV_JWT_SECRET（≥32 字符随机串），"
    "不允许自动生成 jwt.key：\n"
    "  各实例各生成一份密钥，会导致甲实例签发的令牌在乙实例上验不过，\n"
    "  表现为「随机地被要求重新登录」。所有实例必须用同一个密钥。\n"
    '  生成一个：python -c "import secrets; print(secrets.token_urlsafe(48))"'
)

# D2：多实例开了、存储却还是本机 SQLite —— 这是最危险的组合（静默灾难）
_MULTI_INSTANCE_SQLITE_HINT = (
    "多实例模式（VIBE_SRV_MULTI_INSTANCE=1）但存储仍是本机 SQLite。\n"
    "  每个实例各算一份配额、各推一条审计链 —— 表现为「配额莫名失效」「审计对不上」。\n"
    "  请把 VIBE_SRV_DB 指到所有实例共享的 Postgres，例如：\n"
    "    VIBE_SRV_DB=postgres://user:pass@pg.internal:5432/vibe?sslmode=require"
)

# D1：多实例几乎必然在 LB/反代之后，缺这两项则来源 IP 失真、链路可能明文
_MULTI_INSTANCE_TLS_HINT = (
    "多实例模式必须开启 REQUIRE_TLS=1。\n"
    "  多实例几乎必然部署在负载均衡/反向代理之后，否则策略与审计会在内网明文传输。\n"
    "  若已在前面做了 TLS 终止，也请显式打开该开关以表明「入口是加密的」。"
)

_MULTI_INSTANCE_FORWARDED_HINT = (
    "多实例模式必须设置 TRUST_FORWARDED_FOR=1。\n"
    "  不采信 X-Forwarded-For 时，限流与审计记录的来源 IP 全是反代的地址，\n"
    "  来源追溯与按 IP 限流都会失真。"
)


def _str(name: str, default: str) -> str:
    raw = os.environ.get(_PREFIX + name)
    return default if raw is None or raw.strip() == "" else raw.strip()


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(_PREFIX + name)
    if raw is None or raw.strip() == "":
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"环境变量 {_PREFIX}{name} 需要布尔值，实际收到 {raw!r}")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(_PREFIX + name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(
            f"环境变量 {_PREFIX}{name} 需要整数，实际收到 {raw!r}"
        ) from exc


def _load_or_create_secret(
    data_dir: Path, *, require_explicit: bool = False
) -> tuple[bytes, bool, bool]:
    """返回 (密钥, 是否本次新生成, 是否来自环境变量)。

    `require_explicit=True`（多实例意图）时**禁止落盘生成**：拿不到显式的
    `VIBE_SRV_JWT_SECRET` 就直接失败，避免各实例各生成一份互不认的密钥。
    """
    env = os.environ.get(_PREFIX + "JWT_SECRET", "").strip()
    if env:
        if len(env) < 32:
            raise ConfigError(
                f"{_PREFIX}JWT_SECRET 至少需要 32 个字符（当前 {len(env)}）。"
                "太短的签名密钥可以被暴力枚举。"
            )
        return env.encode("utf-8"), False, True

    if require_explicit:
        raise ConfigError(_MULTI_INSTANCE_JWT_HINT)

    key_file = data_dir / "jwt.key"
    if key_file.exists():
        raw = key_file.read_text(encoding="utf-8").strip()
        if raw:
            return raw.encode("utf-8"), False, False

    generated = secrets.token_urlsafe(48)
    data_dir.mkdir(parents=True, exist_ok=True)
    key_file.write_text(generated, encoding="utf-8")
    _restrict(key_file)
    return generated.encode("utf-8"), True, False


def _restrict(path: Path) -> None:
    """尽最大努力把文件权限收紧到仅属主可读写。

    Windows 上 `chmod` 基本无效（ACL 才是真的），所以这里只是「尽力」。
    真正可靠的隔离应该靠目录权限 + 服务账号，README 里已写明。
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


@dataclass(frozen=True)
class ServerConfig:
    """服务端运行配置。"""

    # --- 存储 ---
    db_path: Path
    data_dir: Path
    # 共享存储 URL（如 `postgres://…`）。非空时优先于 db_path：多实例部署必须让
    # 所有实例指向同一个库，否则各写各的本机 SQLite —— 配额与审计会双双失效。
    db_url: str = ""
    # 显式声明「本部署是多实例」。默认 False（单机）；所有多实例专属校验只在
    # 该开关为真、或 db_url 非空时生效，以保证默认路径行为逐字节不变。
    multi_instance: bool = False

    # --- 监听 ---
    host: str = "127.0.0.1"
    port: int = 8787
    # 部署在内网反代后面时，信任 X-Forwarded-For 才拿得到真实来源 IP
    trust_forwarded_for: bool = False
    # 生产必须为 True：开启后会拒绝对非 TLS 连接提供除 /healthz 之外的服务
    require_tls: bool = False

    # --- 令牌 ---
    jwt_secret: bytes = b""
    jwt_issuer: str = "vibe-governance"
    jwt_audience: str = "vibe-agent"
    access_ttl: int = 1800          # 访问令牌 30 分钟（ADR：15–30 分钟）
    refresh_ttl: int = 30 * 86400   # 刷新令牌 30 天
    enrollment_ttl: int = 7 * 86400 # 一次性注册令牌 7 天

    # --- 密钥托管 ---
    secrets_provider: str = "env"   # env | file | vault
    secrets_file: Path | None = None
    vault_addr: str = ""
    vault_token: str = ""
    vault_mount: str = "secret"
    secret_max_age_days: int = 90   # 超过就提醒轮换

    # --- 模型上游 ---
    local_upstream_url: str = ""
    local_upstream_key_ref: str = ""
    cloud_upstream_url: str = ""
    cloud_upstream_key_ref: str = ""
    # 上游请求超时。推理请求天然慢，默认给足
    upstream_timeout: int = 300

    # --- 配额默认值（0 表示不限）---
    default_daily_cloud_tokens: int = 200_000
    default_daily_requests: int = 0
    default_rpm: int = 60

    # --- 策略 ---
    policy_required: bool = True
    policy_max_staleness: int = 12 * 3600

    # --- 审计 ---
    audit_max_batch_events: int = 500
    audit_max_payload_bytes: int = 4096   # 单条事件载荷上限，超了说明有人在塞正文

    # 本次启动是否新生成密钥（需要在启动日志里显式提醒）
    jwt_secret_generated: bool = False
    # 密钥是否来自显式环境变量（区别于从 jwt.key 读取）。多实例下必须为真。
    jwt_secret_from_env: bool = False

    # ------------------------------------------------------------------ 构造
    @classmethod
    def load(
        cls,
        *,
        data_dir: str | Path | None = None,
        db_path: str | Path | None = None,
        **overrides: object,
    ) -> "ServerConfig":
        base = Path(
            data_dir or _str("DATA_DIR", str(Path.cwd() / ".vibe-governance"))
        ).expanduser()

        raw_db = (
            str(db_path) if db_path is not None else _str("DB", str(base / "governance.db"))
        ).strip()

        # 共享存储 URL 不能过 Path()：Windows 上 `postgres://host/db` 会被解析成
        # `postgres:\host\db` 这种畸形路径。带 `://` 且 scheme 属共享库的一律原样保留。
        db_url = ""
        if "://" in raw_db and raw_db.split("://", 1)[0].lower() in _SHARED_DB_SCHEMES:
            db_url = raw_db
            db = base / "governance.db"      # 占位：URL 模式不读它
        else:
            db = Path(raw_db).expanduser()

        multi_flag = _bool("MULTI_INSTANCE", cls.multi_instance)
        multi_intent = multi_flag or bool(db_url)

        secret, generated, from_env = _load_or_create_secret(
            base, require_explicit=multi_intent
        )

        cfg = cls(
            db_path=db,
            data_dir=base,
            db_url=db_url,
            multi_instance=multi_flag,
            host=_str("HOST", cls.host),
            port=_int("PORT", cls.port),
            trust_forwarded_for=_bool("TRUST_FORWARDED_FOR", cls.trust_forwarded_for),
            require_tls=_bool("REQUIRE_TLS", cls.require_tls),
            jwt_secret=secret,
            jwt_issuer=_str("JWT_ISSUER", cls.jwt_issuer),
            jwt_audience=_str("JWT_AUDIENCE", cls.jwt_audience),
            access_ttl=_int("ACCESS_TTL", cls.access_ttl),
            refresh_ttl=_int("REFRESH_TTL", cls.refresh_ttl),
            enrollment_ttl=_int("ENROLLMENT_TTL", cls.enrollment_ttl),
            secrets_provider=_str("SECRETS_PROVIDER", cls.secrets_provider).lower(),
            secrets_file=(
                Path(_str("SECRETS_FILE", str(base / "secrets.json"))).expanduser()
                if _str("SECRETS_PROVIDER", "") else None
            ),
            vault_addr=_str("VAULT_ADDR", cls.vault_addr),
            vault_token=_str("VAULT_TOKEN", cls.vault_token),
            vault_mount=_str("VAULT_MOUNT", cls.vault_mount),
            secret_max_age_days=_int("SECRET_MAX_AGE_DAYS", cls.secret_max_age_days),
            local_upstream_url=_str("LOCAL_UPSTREAM_URL", cls.local_upstream_url),
            local_upstream_key_ref=_str("LOCAL_UPSTREAM_KEY_REF", cls.local_upstream_key_ref),
            cloud_upstream_url=_str("CLOUD_UPSTREAM_URL", cls.cloud_upstream_url),
            cloud_upstream_key_ref=_str("CLOUD_UPSTREAM_KEY_REF", cls.cloud_upstream_key_ref),
            upstream_timeout=_int("UPSTREAM_TIMEOUT", cls.upstream_timeout),
            default_daily_cloud_tokens=_int(
                "DEFAULT_DAILY_CLOUD_TOKENS", cls.default_daily_cloud_tokens
            ),
            default_daily_requests=_int("DEFAULT_DAILY_REQUESTS", cls.default_daily_requests),
            default_rpm=_int("DEFAULT_RPM", cls.default_rpm),
            policy_required=_bool("POLICY_REQUIRED", cls.policy_required),
            policy_max_staleness=_int("POLICY_MAX_STALENESS", cls.policy_max_staleness),
            audit_max_batch_events=_int("AUDIT_MAX_BATCH_EVENTS", cls.audit_max_batch_events),
            audit_max_payload_bytes=_int("AUDIT_MAX_PAYLOAD_BYTES", cls.audit_max_payload_bytes),
            jwt_secret_generated=generated,
            jwt_secret_from_env=from_env,
        )

        if overrides:
            unknown = set(overrides) - {f.name for f in cls.__dataclass_fields__.values()}
            if unknown:
                raise ConfigError(f"未知配置项: {', '.join(sorted(unknown))}")
            cfg = replace(cfg, **overrides)  # type: ignore[arg-type]

        cfg.validate()
        return cfg

    # ------------------------------------------------------------------ 校验
    def validate(self) -> None:
        if self.secrets_provider not in {"env", "file", "vault"}:
            raise ConfigError(
                f"未知的密钥托管方式 {self.secrets_provider!r}，"
                "可选 env / file / vault"
            )
        if self.secrets_provider == "vault" and not self.vault_addr:
            raise ConfigError("SECRETS_PROVIDER=vault 时必须配置 VAULT_ADDR")
        # 多实例意图下，签名密钥必须显式给出：自动生成/读取 jwt.key 都是「各实例
        # 一份」，令牌互不认。这条校验放在 validate() 里，才能覆盖通过 overrides
        # 事后改成多实例的场景（load() 里已先拦一道，避免真的写下 jwt.key）。
        if self.multi_instance_intent() and not self.jwt_secret_from_env:
            raise ConfigError(_MULTI_INSTANCE_JWT_HINT)
        # 以下三条只在多实例意图下生效，单机路径（默认）逐字节不变。
        if self.multi_instance_intent():
            # D2：多实例 + 本机 SQLite = 配额失效 + 审计不可信，宁可拒绝启动
            if not self.db_url:
                raise ConfigError(_MULTI_INSTANCE_SQLITE_HINT)
            # D1：多实例必然在 LB/反代之后，TLS 与真实来源 IP 是前置条件
            if not self.require_tls:
                raise ConfigError(_MULTI_INSTANCE_TLS_HINT)
            if not self.trust_forwarded_for:
                raise ConfigError(_MULTI_INSTANCE_FORWARDED_HINT)
        if self.cloud_upstream_url and not self.cloud_upstream_key_ref:
            # 云端上游没密钥引用 → 要么是内网免鉴权服务（少见），要么是忘了配
            # 这里只警告不拦，因为有些自建网关确实不校验密钥
            pass
        if self.access_ttl <= 0 or self.refresh_ttl <= 0:
            raise ConfigError("令牌有效期必须为正数")
        if self.access_ttl > self.refresh_ttl:
            raise ConfigError("访问令牌有效期不应超过刷新令牌")
        # 端口 0 是合法的：让操作系统分配一个空闲端口。
        # 测试与嵌入式用法都需要它，而"必须指定端口"会逼出
        # "随便挑一个端口然后偶发冲突"这种更糟的做法。
        if self.port < 0 or self.port > 65535:
            raise ConfigError(f"端口不合法: {self.port}（0 表示由系统分配）")
        if not self.require_tls and not self._is_loopback():
            raise ConfigError(
                f"监听 {self.host} 但未启用 TLS（REQUIRE_TLS=1）。\n"
                "  服务端会下发策略与接收审计，明文传输等于把这些暴露在同网段上。\n"
                "  若确实要明文对外提供服务，请显式确认后改为回环地址，"
                "或在前面架一个做 TLS 终止的反向代理并设置 TRUST_FORWARDED_FOR=1。"
            )

    def _is_loopback(self) -> bool:
        return self.host in {"127.0.0.1", "localhost", "::1"}

    # ------------------------------------------------------------------ 派生
    def multi_instance_intent(self) -> bool:
        """是否处于「多实例意图」：显式开关为真，或配了共享存储 URL。

        共享存储（`postgres://…`）本身就意味着「多个实例连同一个库」，
        因此单独配了 db_url 也按多实例对待。
        """
        return self.multi_instance or bool(self.db_url)

    def store_target(self) -> str:
        """交给 `open_store()` 的目标：共享 URL 优先，否则是 SQLite 文件路径。"""
        return self.db_url or str(self.db_path)

    # ------------------------------------------------------------------ 展示
    def describe(self) -> dict[str, object]:
        return {
            "db": self.store_target(),
            "host": self.host,
            "port": self.port,
            "requireTls": self.require_tls,
            "secretsProvider": self.secrets_provider,
            "policyRequired": self.policy_required,
            "accessTtl": self.access_ttl,
            "cloudUpstream": self.cloud_upstream_url or "(未配置)",
            "localUpstream": self.local_upstream_url or "(未配置，客户端直连本地模型)",
        }


__all__ = ["ServerConfig"]
