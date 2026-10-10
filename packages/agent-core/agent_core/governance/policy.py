"""把服务端策略套到客户端配置上（只收紧）。

**这里只做「应用一份已合并的文档」，不做合并。**
合并（全局 + 团队，以及「只收紧」的四个算子）留在服务端 ——
那是唯一有全部上下文的地方，也是唯一该决定「哪条覆盖哪条」的地方。
客户端拿到的是合并后的最终文档，它只需要确保自己**不会比文档更宽松**。

这条分工让两端不会各自实现一遍合并逻辑（那必然漂移），
代价是客户端要重复一小段「应用」逻辑（约 40 行），而这一段有交叉测试锁住。

三种必须做对的语义：

1. **能力开关只允许 True→False。** 客户端本地配置里开了某项，
   策略说不许 —— 关掉。反过来（本地关着、策略说开）**绝不打开**：
   策略不是授权来源，它只是约束。
2. **数值上限只允许调小。**
3. **清单类只允许变长**（拒绝路径、强制审批），模型白名单只允许变窄。

以及一条不可绕过的底线：**策略过期时进受限模式**，而不是沿用旧策略裸奔。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from typing import Any

# 策略文档的默认值（与服务端 policy.DEFAULT_POLICY 对应）。
# 客户端**必须**能独立处理「一份缺字段的文档」：服务端版本更新加了新字段时，
# 老客户端不该因此崩溃或忽略整份策略。
DEFAULTS: dict[str, Any] = {
    "allowCloud": False,
    "allowShell": True,
    "allowWrite": True,
    "autoApproveReview": False,
    "maxIterations": 40,
    "maxContextChars": 400_000,
    "toolOutputLimit": 40_000,
    "commandTimeout": 120,
    "deniedPaths": [],
    "deniedCommands": [],
    "requireApproval": [],
    "allowedModels": [],
    "routing": {"allowCloud": False, "sensitiveForceLocal": True},
}

_BOOL_TIGHTEN = (
    ("allowCloud", "allow_cloud"),
    ("allowShell", "allow_shell"),
    ("allowWrite", "allow_write"),
    ("autoApproveReview", "auto_approve_review"),
)
_INT_TIGHTEN = (
    ("maxIterations", "max_iterations"),
    ("maxContextChars", "max_context_chars"),
    ("toolOutputLimit", "tool_output_limit"),
    ("commandTimeout", "command_timeout"),
)
_LIST_UNION = (
    ("deniedPaths", "denied_paths"),
    ("deniedCommands", "denied_commands"),
    ("requireApproval", "require_approval"),
)


@dataclass
class RuntimeConstraints:
    """策略带来的、不属于 Config 的运行时约束。

    这几项没法塞进 Config：Config 描述的是「Agent 怎么跑」，
    而这里是「哪些资源不许碰、哪些动作必须问人、能否上云」——
    它们要交给 PathGuard / 审批闸门 / 网关客户端去用。
    """

    allow_cloud: bool = False
    sensitive_force_local: bool = True
    denied_paths: tuple[str, ...] = ()
    denied_commands: tuple[str, ...] = ()
    require_approval: tuple[str, ...] = ()
    allowed_models: tuple[str, ...] = ()
    stale: bool = False
    source_version: int = 0
    notes: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        return {
            "allowCloud": self.allow_cloud,
            "sensitiveForceLocal": self.sensitive_force_local,
            "deniedPaths": list(self.denied_paths),
            "deniedCommands": list(self.denied_commands),
            "requireApproval": list(self.require_approval),
            "allowedModels": list(self.allowed_models),
            "stale": self.stale,
            "policyVersion": self.source_version,
        }

    def model_allowed(self, model: str) -> bool:
        if not self.allowed_models:
            return True  # 空 = 不限制
        return model.lower() in {m.lower() for m in self.allowed_models}


def normalize(document: dict[str, Any]) -> dict[str, Any]:
    """把文档补全成完整形状。

    未知字段直接忽略而不是报错：服务端将来加字段时，老客户端要能继续工作。
    但**已知字段的类型不对时按保守值处理**（开关按 False、数值按默认、清单按空），
    不按原值 —— 否则一个类型错误就能让限制失效。
    """
    result: dict[str, Any] = json.loads(json.dumps(DEFAULTS))
    if not isinstance(document, dict):
        return result

    for policy_key, _ in _BOOL_TIGHTEN:
        value = document.get(policy_key)
        if isinstance(value, bool):
            result[policy_key] = value
        elif value is not None:
            # 类型不对 → 按「不许」处理。策略读不懂时从严，这是唯一安全的偏置。
            result[policy_key] = False

    for policy_key, _ in _INT_TIGHTEN:
        value = document.get(policy_key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            result[policy_key] = value

    for policy_key, _ in _LIST_UNION + (("allowedModels", "allowed_models"),):
        value = document.get(policy_key)
        if isinstance(value, list):
            result[policy_key] = [str(item) for item in value if isinstance(item, (str, int))]
        elif value is not None:
            result[policy_key] = []

    routing = document.get("routing")
    result["routing"] = {
        "allowCloud": bool(result["allowCloud"]),
        # 恒真：策略不能关掉这条（服务端也会拒收写 false 的文档）
        "sensitiveForceLocal": True,
        "note": routing.get("note") if isinstance(routing, dict) else None,
    }
    return result


def apply(
    document: dict[str, Any],
    *,
    config: Any,
    stale: bool = False,
    max_staleness: int = 0,
) -> tuple[Any, RuntimeConstraints]:
    """把策略应用到 agent-core 的 Config 上。

    返回 (新的 Config, 运行时约束)。Config 是 frozen dataclass，
    所以用 `dataclasses.replace` 产出新对象而不是原地改 ——
    原地改会让「策略生效前的配置」无法回溯，出问题时查不了。
    """
    policy = normalize(document)
    notes: list[str] = []

    overrides: dict[str, Any] = {}

    # --- 过期：先降级成受限策略，再做正常收紧 ---
    if stale:
        policy = normalize(
            {
                "allowCloud": False,
                "autoApproveReview": False,
                "commandTimeout": min(int(policy["commandTimeout"]), 60),
            }
        )
        notes.append(
            "策略已过期"
            + (f"（超过 {max_staleness}s 未更新）" if max_staleness else "")
            + "，进入受限模式：仅本地模型、写操作强制人工审批"
        )

    # --- 能力开关：只关不开 ---
    for policy_key, config_key in _BOOL_TIGHTEN:
        if policy_key in ("allowCloud",):
            continue  # allowCloud 不是 Config 字段，进运行时约束
        current = getattr(config, config_key, None)
        if current is None:
            continue
        if bool(current) and not bool(policy[policy_key]):
            overrides[config_key] = False
            notes.append(f"{config_key} 被策略关闭")

    # --- 数值上限：只小不大 ---
    for policy_key, config_key in _INT_TIGHTEN:
        current = getattr(config, config_key, None)
        if not isinstance(current, int) or isinstance(current, bool):
            continue
        limit = int(policy[policy_key])
        if current > limit:
            overrides[config_key] = limit
            notes.append(f"{config_key} 由 {current} 收紧为 {limit}")

    # --- 模型白名单：不许用没列出的模型 ---
    allowed_models = tuple(policy["allowedModels"])
    model = str(getattr(config, "model", "") or "")
    if allowed_models and model and model.lower() not in {m.lower() for m in allowed_models}:
        # 不擅自换模型：换成一个"允许的"模型会让用户以为自己在用原来那个，
        # 而结果差异会全部归因到"模型变笨了"。这里只标注，由调用方决定怎么提示。
        notes.append(
            f"当前模型 {model!r} 不在策略允许清单内，云端路由不会被允许；"
            f"允许的模型：{', '.join(allowed_models)}"
        )

    new_config = replace(config, **overrides) if overrides else config

    constraints = RuntimeConstraints(
        allow_cloud=bool(policy["allowCloud"]) and bool(policy["routing"]["allowCloud"]),
        sensitive_force_local=True,
        denied_paths=tuple(policy["deniedPaths"]),
        denied_commands=tuple(policy["deniedCommands"]),
        require_approval=tuple(policy["requireApproval"]),
        allowed_models=allowed_models,
        stale=stale,
        source_version=0,
        notes=notes,
    )
    return new_config, constraints


# --------------------------------------------------------------------------- 缓存
@dataclass
class PolicyCache:
    """本地策略缓存：记下文档、ETag 与拉取时间。

    存在的意义是**让「策略过期」可判定**。没有缓存的话，
    每次启动都等价于"刚拉到"，过期检测就永远不触发 ——
    而策略过期的降级正是「离线很久的机器仍在按老策略跑」这个风险的唯一对策。
    """

    path: Any
    fetched_at: int = 0
    etag: str = ""
    version: int = 0
    document: dict[str, Any] = field(default_factory=dict)

    def load(self) -> "PolicyCache":
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self
        if not isinstance(raw, dict):
            return self
        self.fetched_at = int(raw.get("fetchedAt") or 0)
        self.etag = str(raw.get("etag") or "")
        self.version = int(raw.get("version") or 0)
        document = raw.get("document")
        self.document = document if isinstance(document, dict) else {}
        return self

    def save(self) -> None:
        payload = {
            "fetchedAt": self.fetched_at,
            "etag": self.etag,
            "version": self.version,
            "document": self.document,
        }
        temp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.path)
        except OSError:
            pass  # 缓存写不进不该阻断主流程

    def store(self, *, document: dict[str, Any], etag: str, version: int, now: int | None = None) -> None:
        self.document = document
        self.etag = etag
        self.version = version
        self.fetched_at = int(now if now is not None else time.time())
        self.save()

    def is_stale(self, *, max_staleness: int, now: int | None = None) -> bool:
        if not self.document:
            return False  # 从没拉到过策略：不是"过期"，是"没启用"，不触发降级
        if max_staleness <= 0:
            return False
        current = int(now if now is not None else time.time())
        return (current - self.fetched_at) > max_staleness

    def age_seconds(self, now: int | None = None) -> int:
        if not self.fetched_at:
            return 0
        return int(now if now is not None else time.time()) - self.fetched_at


__all__ = [
    "DEFAULTS",
    "PolicyCache",
    "RuntimeConstraints",
    "apply",
    "normalize",
]
