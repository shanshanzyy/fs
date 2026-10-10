"""路由决策。

架构文档的决策表在这里落地。但**照抄决策表是不够的**——决策表写的是
「命中敏感路径 → 强制本地」，而「有没有命中敏感路径」这个判断，
如果由客户端声明，那这条红线就形同虚设：客户端说没有就没有。

所以这里做**双重判定**：

1. **声明路径**：客户端在请求里带上本次触碰的文件路径（它本来就要带，
   因为审计要记「对哪个仓库做了什么」）。
2. **内容扫描**：网关**直接扫将要发往上游的 prompt 正文**，
   找路径样式的 token、密钥样式的高熵串，以及**带内嵌凭据的连接串
   与凭据赋值**（`postgres://user:pw@host`、`password = …`）——
   后两类拆成片段看人畜无害，只有整串才暴露问题。

任一条命中就强制本地。第 2 条是真正的兜底——它不依赖客户端诚实，
因为网关转发的就是正文本身，扫的是同一份字节。

第三条纪律：**这个函数只返回决策，不执行任何降级。** 降级链在 proxy 里，
因为「能不能降级」取决于上游实时状态，而不是静态规则。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- 目标
LOCAL = "local"
CLOUD = "cloud"

# 降级链：云端大模型 → 本地中模型 → 本地小模型 → 明确报错
DEGRADE_CHAIN = (CLOUD, LOCAL)


# --------------------------------------------------------------------------- 敏感判定
# 这些模式对应架构文档的 `*.key` / `*secret*` / `config/prod*`，
# 但写得更严：不只看后缀，还看路径片段与文件名中缀。
_SENSITIVE_SUFFIXES = (
    ".key", ".pem", ".p12", ".pfx", ".jks", ".keystore", ".kdbx",
    ".env", ".envrc", ".npmrc", ".pypirc", ".netrc", ".htpasswd",
)

_SENSITIVE_SEGMENTS = frozenset(
    {
        "secrets", "secret", "credentials", "credential", "private",
        "keys", ".ssh", ".gnupg", ".aws", ".azure", ".kube",
        "vault", "tokens",
    }
)

# 文件名里出现这些词，一律当敏感（`prod` 限定在 config 语境里，见 _looks_sensitive）
_SENSITIVE_WORDS = ("secret", "password", "passwd", "credential", "apikey", "api_key", "token")

# 直接命中这些名字的，无论路径如何都是敏感
_HARD_NAMES = frozenset(
    {
        "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
        ".env", ".env.local", ".env.production", "credentials.json",
        "service-account.json", "terraform.tfstate", "kubeconfig",
    }
)

_PROD_CONFIG_RE = re.compile(
    r"(^|[/\\])(config|conf|settings|env)[/\\][^/\\]*(prod|staging|live)[^/\\]*$",
    re.IGNORECASE,
)
# 上面那条只管「prod 出现在最后一段」（如 config/prod.yaml）。
# 但 `config/prod/db.yaml` 这种情况 prod 是**中间**一段，
# 只管末段会漏判 —— 而架构文档要求的是 `config/prod*` 整片强制本地。
# 所以补一条：只要「路径里有 config 类目录」且「后面某段含 prod」就命中。
_CONFIG_DIR_RE = re.compile(r"(^|[/\\])(config|conf|settings|env)([/\\]|$)", re.IGNORECASE)
_PROD_WORD_RE = re.compile(r"prod(uction)?", re.IGNORECASE)
_STAGING_WORD_RE = re.compile(r"(staging|stage|live|preprod|prerelease)", re.IGNORECASE)

# prompt 正文里出现的、长得像文件路径的 token。
# 刻意保守：只在明确带分隔符或明确后缀时才认为它是路径，
# 避免把正文里的普通单词（比如「secret 这个词」）也判成敏感，
# 导致云端路由永远用不上。
_PATH_TOKEN_RE = re.compile(
    # 带敏感后缀的文件名（允许 basename 直接以 `.env` 这种点开头）
    r"(?:[A-Za-z0-9_.\-]+[/\\])*[A-Za-z0-9_.\-]*\.(?:key|pem|p12|pfx|env|kdbx)\b"
    # 或任何带分隔符的路径
    r"|(?:[/\\]|\.\.?[/\\])[A-Za-z0-9_.\-/\\]{2,}"
)
_PROD_CONFIG_TOKEN_RE = re.compile(
    r"\b(?:config|conf|settings|env)[/\\][A-Za-z0-9_.\-/\\]*?"
    r"(?:prod|staging|live)[A-Za-z0-9_.\-/\\]*",
    re.IGNORECASE,
)
_SECRET_SEGMENT_TOKEN_RE = re.compile(
    r"(?:^|[/\\\s\"'])(?:secrets?|credentials?|\.ssh|\.aws|\.gnupg|\.kube)[/\\]",
    re.IGNORECASE,
)
# 密钥样式的串：够长、够杂、够像密钥
_KEYLIKE_RE = re.compile(
    r"\b(?:sk|pk|ghp|gho|glpat|xox[baprs]|AKIA|ASIA)[A-Za-z0-9_\-]{16,}\b"
)
_PEM_BLOCK_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_HEX64_RE = re.compile(r"\b[0-9a-fA-F]{64,}\b")

# 带内嵌凭据的连接串：`scheme://user:password@host`。
# 这是最容易漏的一类——它的每一段拆开都人畜无害（`postgres`、`user`、`prod`
# 都不是敏感词），可整串就是把数据库账号密码直接写在了正文里。
# 所以必须**整串识别**，而不是靠拆出来的片段。
_CREDENTIAL_URL_RE = re.compile(
    r"\b([a-zA-Z][a-zA-Z0-9+.\-]{1,15})://[^\s/:@\"']{1,64}:[^\s/@\"']{1,128}@"
)

# 凭据赋值：`password = xxx` / `api_key: "yyy"` / `token = ABC123`。
# 难点在**误报**：`password = password`（把参数赋回字段）、
# `token = os.environ["T"]`、`pwd = get_pwd()` 都是正常代码，不能拦。
# 判据是右边的「值」要像个**字面量**：带引号，或是含数字/符号的裸串。
# 纯标识符（全字母）与函数调用、下标访问一律放过。
_SECRET_ASSIGN_RE = re.compile(
    r"\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?key|"
    r"secret[_-]?key|private[_-]?key|auth[_-]?token|token|credential)s?\b"
    r"\s*[:=]\s*"
    r"(?:"
    r"""["'][^"'\s]{3,}["']"""
    r"|"
    r"(?![A-Za-z_]\w*\s*\()"                 # 不是函数调用
    r"(?![\w.]+\[)"                          # 不是下标/环境变量访问
    r"(?=[A-Za-z0-9!@#$%^&*_+.\-]{4,}\b)"
    r"(?=[^\s]*[0-9!@#$%^&*+\-])"            # 值里含数字或符号，不像纯标识符
    r"[A-Za-z0-9!@#$%^&*_+.\-]{4,}"
    r")",
    re.IGNORECASE,
)


def _looks_sensitive(path: str) -> str:
    """返回命中的理由；空串表示不敏感。

    返回理由而不是布尔，是为了让审计里能记下「因为哪条规则被拦」——
    「被拦了但不知道为什么」是运维最难受的状态。
    """
    raw = (path or "").strip()
    if not raw:
        return ""
    # 统一分隔符，兼容 Windows 路径
    normalized = raw.replace("\\", "/").strip("/")
    if not normalized:
        return ""
    parts = [p for p in normalized.split("/") if p]
    lowered_parts = [p.lower() for p in parts]
    name = lowered_parts[-1] if lowered_parts else ""

    if name in _HARD_NAMES:
        return f"文件名 {name} 属于硬敏感清单"
    for suffix in _SENSITIVE_SUFFIXES:
        if name.endswith(suffix):
            return f"文件名后缀 {suffix} 属于敏感类型"
    for segment in lowered_parts:
        if segment in _SENSITIVE_SEGMENTS:
            return f"路径片段 {segment!r} 属于敏感目录"
    if _PROD_CONFIG_RE.search(normalized):
        return "命中生产配置路径（config 目录 + prod 段）"
    # prod/staging 出现在 config 类目录之后的任意层级，都算生产配置
    if _CONFIG_DIR_RE.search(normalized) and (
        _PROD_WORD_RE.search(normalized) or _STAGING_WORD_RE.search(normalized)
    ):
        return "命中生产/预发配置路径（config 目录下出现 prod/staging 段）"
    for word in _SENSITIVE_WORDS:
        if word in name:
            return f"文件名包含 {word!r}"
    return ""


def scan_text(text: str) -> list[str]:
    """扫描正文，返回命中的理由列表（已去重）。

    这是「不依赖客户端诚实」的那一层。宁可偶尔误判把请求压回本地
    ——本地跑慢一点是可接受的，代码出内网是不可接受的。
    """
    if not text:
        return []
    reasons: list[str] = []

    if _PEM_BLOCK_RE.search(text):
        reasons.append("正文含 PEM 私钥块")

    for match in set(_KEYLIKE_RE.findall(text)):
        reasons.append(f"正文含密钥样式串（{match[:8]}…）")

    # 带内嵌凭据的连接串：只报协议名，**不回显凭据本身**
    for match in _CREDENTIAL_URL_RE.finditer(text):
        reasons.append(f"正文含带内嵌凭据的连接串（{match.group(1)}://…）")

    # 凭据赋值：只报关键词，**不回显值**
    for match in _SECRET_ASSIGN_RE.finditer(text):
        reasons.append(f"正文含疑似凭据赋值（{match.group(1)} = …）")

    for token in set(_PATH_TOKEN_RE.findall(text)):
        reason = _looks_sensitive(token)
        if reason:
            reasons.append(f"正文提及敏感路径 {token[:60]!r}：{reason}")
            if len(reasons) >= 8:
                break

    for token in set(_PROD_CONFIG_TOKEN_RE.findall(text)):
        reasons.append(f"正文提及生产配置 {token[:60]!r}")

    if _SECRET_SEGMENT_TOKEN_RE.search(text):
        reasons.append("正文提及密钥目录")

    # 兜底：64 位以上的十六进制串大概率是密钥/摘要，但代码里也常见哈希常量，
    # 所以只在同时出现「密钥」语义词时才判敏感，避免把正常代码全压回本地。
    if _HEX64_RE.search(text) and re.search(
        r"\b(?:secret|key|token|password|credential)\b", text, re.IGNORECASE
    ):
        reasons.append("正文同时含长十六进制串与密钥语义词")

    # 去重且保序
    seen: set[str] = set()
    unique: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            unique.append(reason)
    return unique


# --------------------------------------------------------------------------- 任务分级
TASK_COMPLETION = "completion"        # 补全 / 小范围改写
TASK_EDIT = "edit"                    # 单文件 / 多文件编辑
TASK_REASONING = "reasoning"          # 长上下文重构、架构级推理
TASK_UNKNOWN = "unknown"

# 客户端声明「这次要干什么」。这个声明用于**选档次**，不用于放宽限制：
# 声明成 completion 只会让它更可能留在本地，声明成 reasoning 也不会
# 让它绕过敏感路径检查（那是独立的一层）。
_LOW_BUDGET_TOKENS = 24_000      # 上下文不大且工具少 → 当成轻任务
_HIGH_BUDGET_TOKENS = 120_000    # 上下文很大 → 当成重任务


@dataclass
class RouteRequest:
    """路由决策的输入。"""

    task: str = TASK_UNKNOWN
    touched_paths: tuple[str, ...] = ()
    # 将要发往上游的完整正文（所有 message 拼起来），用于内容扫描
    outbound_text: str = ""
    # 客户端声明的估计上下文规模
    prompt_tokens: int = 0
    tool_count: int = 0
    # 客户端明确要求走本地（例如它自己知道在离线网络里）
    prefer_local: bool = False
    # 本次请求是否允许上云（由策略下发决定；False 是硬约束）
    cloud_allowed: bool = True


@dataclass
class RouteDecision:
    target: str
    task: str
    reasons: list[str] = field(default_factory=list)
    sensitive: bool = False
    # 敏感命中的原始理由，要进审计（**只记理由与路径，不记正文**）
    sensitive_reasons: list[str] = field(default_factory=list)
    # 是否因为「策略禁止上云」而非「敏感」被压回本地
    forced_by_policy: bool = False

    @property
    def downgraded(self) -> bool:
        return self.target == LOCAL and not self.forced_by_policy and not self.sensitive

    def describe(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "task": self.task,
            "sensitive": self.sensitive,
            "forcedByPolicy": self.forced_by_policy,
            "reasons": list(self.reasons),
            # 只回理由分类，不回正文片段——避免审计与日志里出现代码
            "sensitiveRuleCount": len(self.sensitive_reasons),
        }


def classify_task(request: RouteRequest) -> str:
    """推断任务档次。

    优先用客户端声明；没声明时按上下文规模与工具数量推断。
    宁可判低不判高：判高了会把请求推给云端，而判断错的代价是
    「本来能本地跑的被送出去了」，比反过来严重得多。
    """
    if request.task in (TASK_COMPLETION, TASK_EDIT, TASK_REASONING):
        return request.task
    tokens = max(0, int(request.prompt_tokens or 0))
    if tokens >= _HIGH_BUDGET_TOKENS:
        return TASK_REASONING
    if tokens <= _LOW_BUDGET_TOKENS and request.tool_count <= 8:
        return TASK_COMPLETION
    return TASK_EDIT


def decide(request: RouteRequest, *, policy: dict[str, Any] | None = None) -> RouteDecision:
    """做出路由决策。

    `policy` 是已合并的生效策略（服务端自己的策略 + 团队/用户覆盖），
    可提供 `allowCloud` / `routing` 开关。**策略只能收紧不能放松**：
    这里读到的 `allowCloud=False` 就是最终结论，不会再被后面的逻辑翻回来。
    """
    policy = policy or {}
    reasons: list[str] = []
    task = classify_task(request)

    # --- 第一层：敏感判定（红线，优先级最高）---
    sensitive_reasons: list[str] = []
    for path in request.touched_paths:
        reason = _looks_sensitive(path)
        if reason:
            sensitive_reasons.append(f"{path[:80]!r}：{reason}")
    sensitive_reasons.extend(scan_text(request.outbound_text))
    if sensitive_reasons:
        reasons.append(f"命中 {len(sensitive_reasons)} 条敏感规则，强制本地")
        return RouteDecision(
            target=LOCAL,
            task=task,
            reasons=reasons,
            sensitive=True,
            sensitive_reasons=sensitive_reasons,
        )

    # --- 第二层：策略硬约束 ---
    # 三层来源，任一说不行就是不行。顺序无关紧要，因为只有「否」被使用。
    cloud_allowed = (
        bool(policy.get("allowCloud", True))
        and bool(request.cloud_allowed)
    )
    if not cloud_allowed:
        reasons.append("策略未允许云端路由，留在本地")
        return RouteDecision(
            target=LOCAL, task=task, reasons=reasons, forced_by_policy=True
        )

    # --- 第三层：客户端偏好 ---
    if request.prefer_local:
        reasons.append("客户端要求本地执行")
        return RouteDecision(target=LOCAL, task=task, reasons=reasons, forced_by_policy=True)

    # --- 第四层：任务分级 ---
    # 只有「重任务」才值得上云。轻任务上云既慢又贵，且会白白把上下文送出去。
    if task == TASK_REASONING:
        reasons.append("长上下文/架构级任务，路由到云端增强")
        return RouteDecision(target=CLOUD, task=task, reasons=reasons)

    reasons.append(f"任务档次 {task} 由本地模型承担（默认本地优先）")
    return RouteDecision(target=LOCAL, task=task, reasons=reasons)


def upstream_for(target: str, config: Any) -> tuple[str, str]:
    """返回 (上游 URL, 密钥引用名)。密钥引用交给 SecretStore 解析。

    **这里不返回值本身**，只返回引用名 —— 让「谁持有密钥」这件事
    在类型上就只有一个答案：网关。任何调用方都拿不到明文。
    """
    if target == CLOUD:
        return str(config.cloud_upstream_url or ""), str(config.cloud_upstream_key_ref or "")
    return str(config.local_upstream_url or ""), str(config.local_upstream_key_ref or "")


__all__ = [
    "CLOUD",
    "DEGRADE_CHAIN",
    "LOCAL",
    "RouteDecision",
    "RouteRequest",
    "TASK_COMPLETION",
    "TASK_EDIT",
    "TASK_REASONING",
    "classify_task",
    "decide",
    "scan_text",
    "upstream_for",
]
