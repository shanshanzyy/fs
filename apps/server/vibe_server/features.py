"""功能灰度（M4）。

灰度与策略的区别一句话讲清：**策略是「能不能」，灰度是「现在先给谁」。**
策略收紧的是安全边界，灰度调整的是新功能铺开的范围。

三条设计取舍：

1. **分桶必须稳定。** 同一个人今天在灰度内，明天还在、换台机器也还在 ——
   否则用户看到功能忽有忽无，报障时无法复现，最后只能「全量开」了事。
   所以用 `hash(flag 名 + 稳定标识)` 取模，不用随机数、不用时间。
2. **关优先于开。** 与策略合并同一条纪律：`enabled=false` 就是 0%，
   排除名单命中就是不给，百分比再高也不行。任一层说关就是关。
3. **每个 flag 独立分桶。** 哈希里带 flag 名，否则「A 功能放 10%」
   放行的恰好也是 B 功能那 10%，两个小流量实验互相污染样本。

灰度定义随策略文档下发（`policy.features`），不单开一张表：
它天然是「按作用域分层叠加」的东西，复用策略的全局→团队合并既省一套
存储，也保证了两者用同一套「只收紧」语义。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .errors import BadRequest
from .hashing import sha256_hex

#: flag 名约束。上限与字符集都是刻意的：这个名字会出现在策略文档、
#: 下发响应和客户端配置里，放开字符集等于让一个「键名」变成任意载荷。
#: 允许大写是为了与文档其它键名（`allowCloud` / `rolloutPercent`）的
#: camelCase 风格一致 —— 在这里强制小写，运维只会写错一次然后困惑。
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_MAX_LIST_ITEMS = 200
_MAX_LIST_ITEM_LEN = 128
MAX_FLAGS = 200

#: 百分比的分母。固定成 100 而不是可配：可配的分母会让
#: 「已经放给 10% 的人」在分母改变时突然变成另一个集合。
_PERCENT_SCALE = 100


@dataclass(frozen=True)
class FeatureFlag:
    """一个功能的灰度定义。默认值 = **关**，与策略默认值同样保守。"""

    enabled: bool = False
    rollout_percent: int = 0
    #: 强制放行名单（用于「先给某个团队试」）。受 `enabled` 约束。
    allowlist: tuple[str, ...] = ()
    #: 强制排除名单。**优先于一切**，包括 allowlist 与 100%。
    denylist: tuple[str, ...] = ()

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "rolloutPercent": self.rollout_percent,
            "allowlist": list(self.allowlist),
            "denylist": list(self.denylist),
        }


# --------------------------------------------------------------------------- 规范化
def _string_list(value: Any, label: str, *, strict: bool) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        if strict:
            raise BadRequest(f"{label} 必须是数组")
        return ()
    if len(value) > _MAX_LIST_ITEMS:
        if strict:
            raise BadRequest(f"{label} 最多 {_MAX_LIST_ITEMS} 项，收到 {len(value)}")
        value = value[:_MAX_LIST_ITEMS]
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            if strict:
                raise BadRequest(f"{label} 的每一项都必须是非空字符串")
            continue
        if len(item) > _MAX_LIST_ITEM_LEN:
            if strict:
                raise BadRequest(f"{label} 里有一项过长（>{_MAX_LIST_ITEM_LEN}）")
            continue
        if "\n" in item or "\r" in item:
            if strict:
                raise BadRequest(f"{label} 的项不能包含换行")
            continue
        if item not in out:  # 去重保序，让 etag 稳定
            out.append(item)
    return tuple(out)


def normalize_flags(raw: Any, *, strict: bool = True) -> dict[str, FeatureFlag]:
    """把策略文档里的 `features` 段规范成 flag 表。

    `strict=False` 时非法项被跳过而不是抛错 —— 用在合并路径上：
    那里的文档应该已经过 `validate_document`，不该因为一个坏值让
    「算出生效策略」这件事整个失败（那会让所有客户端都拿不到策略）。
    """
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        if strict:
            raise BadRequest("features 必须是对象（flag 名 -> 配置）")
        return {}
    if len(raw) > MAX_FLAGS:
        if strict:
            raise BadRequest(f"features 最多 {MAX_FLAGS} 个，收到 {len(raw)}")
        return {}

    result: dict[str, FeatureFlag] = {}
    for name, spec in raw.items():
        if not isinstance(name, str) or not _NAME_RE.match(name):
            if strict:
                raise BadRequest(
                    f"非法的功能名 {name!r}：需字母开头，"
                    "仅含字母数字与 _.- ，长度不超过 64"
                )
            continue
        if spec is None:
            spec = {}
        if not isinstance(spec, dict):
            if strict:
                raise BadRequest(f"features.{name} 必须是对象")
            continue
        unknown = set(spec) - {"enabled", "rolloutPercent", "allowlist", "denylist"}
        if unknown:
            if strict:
                raise BadRequest(
                    f"features.{name} 含未知字段: {', '.join(sorted(unknown))}。"
                    "可用字段: enabled, rolloutPercent, allowlist, denylist"
                )
            continue
        enabled = spec.get("enabled", False)
        if not isinstance(enabled, bool):
            if strict:
                raise BadRequest(f"features.{name}.enabled 必须是布尔值")
            continue
        percent = spec.get("rolloutPercent", 0)
        if isinstance(percent, bool) or not isinstance(percent, int):
            if strict:
                raise BadRequest(f"features.{name}.rolloutPercent 必须是整数")
            continue
        if not 0 <= percent <= _PERCENT_SCALE:
            if strict:
                raise BadRequest(
                    f"features.{name}.rolloutPercent 必须在 0–{_PERCENT_SCALE} 之间，"
                    f"收到 {percent}"
                )
            continue
        result[name] = FeatureFlag(
            enabled=enabled,
            rollout_percent=percent,
            allowlist=_string_list(
                spec.get("allowlist"), f"features.{name}.allowlist", strict=strict
            ),
            denylist=_string_list(
                spec.get("denylist"), f"features.{name}.denylist", strict=strict
            ),
        )
    return result


# --------------------------------------------------------------------------- 合并
def _dedup(items: Iterable[str]) -> tuple[str, ...]:
    """去重**并排序**。

    排序不是洁癖：合并结果要参与 etag 计算，而 etag 是对字节算的。
    不排序的话 `merge([g, t])` 与 `merge([t, g])` 内容等价、字节不同，
    客户端会以为自己拿到了新策略，反复重拉 —— 更糟的是「顺序无关」
    这个承诺变成空话，运维排查时会看到「同样两份策略合并出不同结果」。
    与 policy.py 里列表字段的处理方式保持一致。
    """
    return tuple(sorted(set(items), key=lambda s: (s.lower(), s)))


def _intersect_allowlists(lists: list[tuple[str, ...]]) -> tuple[str, ...]:
    """白名单取交集；空表示「不限制」而不是「什么都不允许」。

    与策略文档里 `allowedModels` 同一套语义：一方不限 → 由限制的那方决定。
    """
    limited = [item for item in lists if item]
    if not limited:
        return ()
    keep = set(limited[0])
    for other in limited[1:]:
        keep &= set(other)
    return _dedup(item for item in limited[0] if item in keep)


def merge_flags(layers: list[dict[str, FeatureFlag]]) -> dict[str, FeatureFlag]:
    """按「只收紧」合并多层灰度定义。

    算子与策略文档一一对应：`enabled` 取 AND、百分比取 MIN、
    `denylist` 取并集、`allowlist` 取交集。

    **只在某一层出现的 flag，在其它层按默认（关 / 0%）参与合并** ——
    于是「团队想开一个全局没开的开关」不会生效。这是刻意的：
    若允许下层凭空开新功能，任何团队管理员都能绕过全公司的灰度节奏，
    而灰度本来就是为了控制「谁先拿到」。
    """
    if not layers:
        return {}
    names: list[str] = []
    for layer in layers:
        for name in layer:
            if name not in names:
                names.append(name)

    merged: dict[str, FeatureFlag] = {}
    for name in names:
        specs = [layer.get(name, FeatureFlag()) for layer in layers]
        merged[name] = FeatureFlag(
            enabled=all(spec.enabled for spec in specs),
            rollout_percent=min(spec.rollout_percent for spec in specs),
            allowlist=_intersect_allowlists([spec.allowlist for spec in specs]),
            denylist=_dedup(item for spec in specs for item in spec.denylist),
        )
    return merged


# --------------------------------------------------------------------------- 评估
def bucket_of(name: str, key: str) -> int:
    """把稳定标识稳定地映射到 [0, 100)。

    哈希里带上 flag 名：不同功能用同一批 key 也必须落在不同的桶上，
    否则几个灰度实验的样本会完全重合。
    """
    digest = sha256_hex(f"{name}\x1f{key}")
    return int(digest[:8], 16) % _PERCENT_SCALE


def is_on(
    name: str, flag: FeatureFlag, *, user_id: str, device_id: str = ""
) -> bool:
    """判断某个稳定标识此刻是否在灰度内。"""
    if not flag.enabled:
        return False
    # 名单用 user_id 与 device_id 两侧匹配：「排除某个人的所有设备」
    # 与「只排除这台机器」都应该能表达。
    members = [key for key in (user_id, device_id) if key]
    if any(key in flag.denylist for key in members):
        # 排除优先于一切，包括 allowlist 与 100%：
        # 出事要能立刻把某个人摘出去，而不是先改百分比再等下一次评估
        return False
    if any(key in flag.allowlist for key in members):
        return True
    if flag.rollout_percent >= _PERCENT_SCALE:
        return True
    if flag.rollout_percent <= 0:
        return False
    if not user_id:
        # 没有稳定标识就分不了桶。宁可不开 —— 反过来是「身份缺失即全开」。
        return False
    # 分桶只看 user_id：按设备分会让同一个人在不同机器上看到不同结果，
    # 那不是灰度，那是抖动。device_id 只用于名单。
    return bucket_of(name, user_id) < flag.rollout_percent


def evaluate(
    flags: dict[str, FeatureFlag], *, user_id: str, device_id: str = ""
) -> dict[str, bool]:
    """算出生效的开关集合。键排序，保证同样的输入产出同样的字节。"""
    return {
        name: is_on(name, flag, user_id=user_id, device_id=device_id)
        for name, flag in sorted(flags.items())
    }


def to_document(flags: dict[str, FeatureFlag]) -> dict[str, dict[str, Any]]:
    """转回策略文档形态（纯 JSON 结构）。

    策略文档会被 `canonical()` 序列化后算 etag，所以里面**不能**留
    dataclass 对象 —— 那会让 etag 变成 `default=str` 的产物，
    莫名其妙且不可复现。所有进出策略文档的路径都必须经过这里或
    `normalize_flags`，保持「文档里只有 JSON」这条不变量。
    """
    return {name: flag.describe() for name, flag in sorted(flags.items())}


def summarize(flags: dict[str, FeatureFlag]) -> str:
    """一行摘要，给策略版本列表用。"""
    if not flags:
        return "features=0"
    on = [name for name, flag in sorted(flags.items()) if flag.enabled]
    return f"features={len(flags)} on={','.join(on) if on else '-'}"


__all__ = [
    "MAX_FLAGS",
    "FeatureFlag",
    "bucket_of",
    "evaluate",
    "is_on",
    "merge_flags",
    "normalize_flags",
    "summarize",
    "to_document",
]