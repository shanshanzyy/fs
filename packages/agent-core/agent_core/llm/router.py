"""ModelRouter（M9-I）：客户端模型选择器。

## 定位

服务端已有按治理面策略的**网关路由**（M2），本轮补的是**客户端**按任务难度
在「服务端已授权的模型集合」内选型的能力 —— 补全走小模型、复杂重构走大模型、
成本敏感走本地。两者分层、不冲突：客户端选型是服务端授权集合的**子集**，
只能收紧，不能选出治理面未允许的上游。

## 三条边界（对齐 M9-PLAN §2-I）

1. **只收紧、不外扩**：选型结果必须落在 `allowed_models`（治理面下发的授权集合）
   内；越界或未匹配时 **fail-closed 回退默认模型**，宁可退回也不硬选未授权上游。
2. **策略非法即回退 `none`**：`model_router_policy` 非法时按 `none` 处理（不选型、
   逐字节一致）并告警，不静默生效。
3. **默认关**：`enabled=False` 时 `resolve` 恒返回 `default_model`，请求体与未实现前
   **逐字节一致**（红线 1「代码不出本机」不变 —— 选型只影响「发往哪个已配置端点」）。

## 审计

只记 `intent / model / reason`（`none|static|size|fallback`）三元组（ADR-006）：
任务正文一个字符都不出机，模型名与原因枚举已够回答「谁在按什么策略选了哪个模型」。
"""

from __future__ import annotations

from dataclasses import dataclass

# 合法策略集合。
POLICIES = ("none", "static", "size")

# 任务意图（启发式判断的三档）。
INTENT_SIMPLE = "simple"
INTENT_DEFAULT = "default"
INTENT_COMPLEX = "complex"


@dataclass(frozen=True)
class ModelRouter:
    """按任务难度选型的静态路由器。

    所有字段一经构造即不可变（frozen），选型是纯函数 —— 不缓存、不发请求、
    不落盘。`allowed_models` 是治理面下发的授权集合（空 = 不限制）。
    """

    enabled: bool = False
    policy: str = "none"
    default_model: str = ""
    # static 策略的固定映射：{ "simple": 小模型, "complex": 大模型 }。
    static_map: dict[str, str] | None = None
    # 治理面授权集合（小写比较）。空 tuple = 不限制。
    allowed_models: tuple[str, ...] = ()

    def _effective_policy(self) -> str:
        """策略归一：非法的策略一律回退 `none`（不选型，逐字节一致）。"""
        p = (self.policy or "none").strip().lower()
        return p if p in POLICIES else "none"

    def resolve(self, intent: str, requested_model: str = "") -> tuple[str, str]:
        """按意图选型，返回 `(模型名, 原因)`。

        原因枚举：
          - `none`     —— 未启用或策略 none，未选型（返回 requested/default）；
          - `static`   —— static 映射命中；
          - `size`     —— size 启发式命中（由调用方把「大小档位」折算成模型）；
          - `fallback` —— 未匹配 / 越界 / 策略非法，fail-closed 回退默认。

        `requested_model` 是「未选型时本应发的模型」（即 config.model）。fail-closed
        回退用 `default_model`（非空时），否则退回 `requested_model`。
        """
        fallback = self.default_model or requested_model
        if not self.enabled:
            return requested_model or fallback, "none"

        policy = self._effective_policy()
        if policy == "none":
            return requested_model or fallback, "none"

        intent = (intent or INTENT_DEFAULT).strip().lower()
        if intent not in (INTENT_SIMPLE, INTENT_COMPLEX, INTENT_DEFAULT):
            intent = INTENT_DEFAULT

        chosen: str = ""
        if policy == "static":
            chosen = self._pick_static(intent)
        elif policy == "size":
            chosen = self._pick_size(intent, requested_model)

        # 授权闸：选出的模型必须落在授权集合内，否则 fail-closed 回退。
        if not chosen or not self._allowed(chosen):
            return fallback, "fallback"
        return chosen, policy

    def _pick_static(self, intent: str) -> str:
        if not self.static_map:
            return ""
        if intent == INTENT_SIMPLE:
            return self.static_map.get("simple", "")
        if intent == INTENT_COMPLEX:
            return self.static_map.get("complex", "")
        return ""

    def _pick_size(self, intent: str, requested_model: str) -> str:
        # size 策略：简单任务优先「更小」的模型，复杂任务优先「更大」的模型。
        # 启发式依据是模型名里的尺寸线索（tiny/mini/small 与 large/xxl/max 等），
        # 以及授权集合里的排序。无强线索时返回 requested_model（不臆造）。
        candidates = [m for m in self.allowed_models] if self.allowed_models else []
        if not candidates:
            return requested_model
        if intent == INTENT_SIMPLE:
            small = _smallest(candidates)
            return small or requested_model
        if intent == INTENT_COMPLEX:
            large = _largest(candidates)
            return large or requested_model
        return requested_model

    def _allowed(self, model: str) -> bool:
        if not self.allowed_models:
            return True  # 空 = 不限制
        return model.lower() in {m.lower() for m in self.allowed_models}


# ------------------------------------------------------------------ 尺寸启发式
_SIZE_SMALL = ("tiny", "mini", "small", "nano", "1b", "0.5b", "3b", "7b", "8b")
_SIZE_LARGE = ("large", "xxl", "max", "ultra", "70b", "72b", "110b", "405b", "671b")


def _size_score(model: str) -> int:
    """按名字里的尺寸线索打分：越小越负、越大越正。无线索 = 0。"""
    m = model.lower()
    score = 0
    for tok in _SIZE_SMALL:
        if tok in m:
            score -= 1
    for tok in _SIZE_LARGE:
        if tok in m:
            score += 1
    return score


def _smallest(candidates: list[str]) -> str:
    return min(candidates, key=lambda m: (_size_score(m), len(m)))


def _largest(candidates: list[str]) -> str:
    return max(candidates, key=lambda m: (_size_score(m), len(m)))


def infer_intent(messages: list[dict], max_chars: int = 4000) -> str:
    """从消息里启发式判断任务意图：`simple` / `default` / `complex`。

    纯词法启发式，不引向量、不发请求：
      - 首条用户消息里有「重构 / 架构 / 迁移 / 设计 / 审查」等强信号 → `complex`；
      - 有「改个变量 / 修个 typo / 加个注释 / 解释一下」等轻信号 → `simple`；
      - 消息总字符数超大 → 倾向 `complex`；其余 → `default`。
    """
    user_texts: list[str] = []
    total_chars = 0
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            total_chars += len(content)
            if msg.get("role") == "user":
                user_texts.append(content)
    first = " ".join(user_texts).lower()

    complex_hits = ("重构", "架构", "迁移", "设计", "审查", "review", "refactor",
                    "architect", "migrate", "design", "审计", "安全", "性能优化")
    simple_hits = ("改个", "修个", "typo", "注释", "解释", "重命名", "小改",
                   "加个", "删个", "explain", "rename", "comment")

    if any(h in first for h in complex_hits):
        return INTENT_COMPLEX
    if total_chars > max_chars:
        return INTENT_COMPLEX
    if any(h in first for h in simple_hits):
        return INTENT_SIMPLE
    return INTENT_DEFAULT


__all__ = [
    "INTENT_COMPLEX",
    "INTENT_DEFAULT",
    "INTENT_SIMPLE",
    "POLICIES",
    "ModelRouter",
    "infer_intent",
]
