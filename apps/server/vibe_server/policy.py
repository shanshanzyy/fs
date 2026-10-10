"""策略下发与合并。

治理面最容易被绕过的地方不是认证，而是**策略合并**。典型错误：
团队的策略说「允许上云」，全局策略说「禁止上云」，实现里写
「团队策略覆盖全局」—— 于是团队一个设置就把公司红线绕过了。

所以这里定死一条纪律：

> **合并只收紧，不放松。** 任何一份策略都不可能放宽另一份。

具体到每种类型：

| 字段类型 | 合并算子 | 直觉 |
|---|---|---|
| 能力开关（allowCloud 等） | **AND** | 任一层说不许就是不许 |
| 数值上限（maxIterations 等） | **MIN** | 任一层收紧就按最紧的 |
| 拒绝清单（deniedPaths 等） | **并集** | 任一层禁的东西全都禁 |
| 强制审批清单 | **并集** | 同上 |
| 白名单（allowedModels） | **交集** | 任一层没列的就不可用 |

还有一条**不可协商**的：`routing.sensitiveForceLocal` 恒为 true。
策略文档里若显式写 false，**整份文档被拒绝**（不是被忽略）——
被忽略的话，写文档的人会以为生效了。

最后是**过期降级**：策略有 TTL，客户端太久没拉到新策略时
不能「拿着旧策略继续跑」，而是进受限模式（本地模型 + 强制审批）。
「裸奔」和「卡死」之间，受限模式是唯一可接受的那个。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import rbac
from .errors import BadRequest, Forbidden
from .features import (
    evaluate as evaluate_features,
)
from .features import (
    merge_flags,
    normalize_flags,
    to_document,
)
from .features import (
    summarize as summarize_flags,
)
from .hashing import canonical, fingerprint
from .ids import new_id

SCOPE_GLOBAL = "global"
SCOPE_TEAM = "team"

# --------------------------------------------------------------------------- 默认策略
# 默认值刻意是「最保守的一档」：不写策略时系统也该是安全的，
# 而不是先放开再靠管理员想起来去收。
DEFAULT_POLICY: dict[str, Any] = {
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
    # 功能灰度（M4）。形状：flag 名 -> {enabled, rolloutPercent, allowlist, denylist}。
    # 与能力开关一样「只收紧」：任一层说关就是关，上层没定义的 flag 不许下层凭空开。
    "features": {},
    "routing": {
        "allowCloud": False,
        # 恒真，不可配置（见文件头）
        "sensitiveForceLocal": True,
    },
}

# 能力开关：AND 合并
_BOOL_AND_FIELDS = ("allowCloud", "allowShell", "allowWrite", "autoApproveReview")
# 数值上限：MIN 合并
_INT_MIN_FIELDS = ("maxIterations", "maxContextChars", "toolOutputLimit", "commandTimeout")
# 拒绝清单：并集合并
_LIST_UNION_FIELDS = ("deniedPaths", "deniedCommands", "requireApproval")
# 白名单：交集合并（空列表 = 不限制）
_LIST_INTERSECT_FIELDS = ("allowedModels",)

_MAX_LIST_ITEMS = 500
_MAX_LIST_ITEM_LEN = 200
_PATH_RE_LIMIT = 400


# --------------------------------------------------------------------------- 结构
@dataclass
class PolicyBundle:
    """一份合并后的生效策略，连同它来自哪些版本。"""

    document: dict[str, Any]
    version: int
    etag: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    stale: bool = False
    emitted_at: int = 0
    #: 按**当前主体**算出的灰度开关（flag 名 -> bool）。
    #: 与 `document["features"]` 是两个东西：那里是定义（含百分比与名单），
    #: 这里是「这个人此刻是否在灰度内」。客户端决定走哪条分支只看后者，
    #: 界面要解释「你为什么在这个灰度里」才需要前者。
    features: dict[str, bool] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {
            "policy": self.document,
            "version": self.version,
            "etag": self.etag,
            "sources": self.sources,
            "stale": self.stale,
            "emittedAt": self.emitted_at,
            "features": dict(self.features),
        }


def _normalize(document: dict[str, Any]) -> dict[str, Any]:
    """规范成完整的策略文档（缺失字段填默认值）。"""
    raw = document if isinstance(document, dict) else {}
    result = {field_name: _clone(value) for field_name, value in DEFAULT_POLICY.items()}
    for key, value in raw.items():
        if key not in result:
            continue  # 未知字段忽略（校验阶段已经报过）
        result[key] = _clone(value)

    # `allowCloud` 有两处表达：顶层开关与 `routing.allowCloud`。
    # 二者必须**始终一致**，否则会出现最难查的一种失效：
    # 管理员发布 `{"allowCloud": true}`，接口回显也是 true、界面显示"云端已开"，
    # 但 `routing.allowCloud` 仍是默认的 false，合并时被 AND 成 false ——
    # 云端从来没被用过，而且不报任何错。
    #
    # 规则（与 merge 的「只收紧」一致，不会放宽任何东西）：
    #   · 只写了一处 → 另一处跟随它（这样写哪个都生效）；
    #   · 两处都写   → 取更严的（AND）；
    #   · 都没写     → 用默认（关）。
    raw_routing = raw.get("routing")
    raw_routing = raw_routing if isinstance(raw_routing, dict) else {}
    has_top = "allowCloud" in raw
    has_routing = "allowCloud" in raw_routing
    if has_top and has_routing:
        allow_cloud = bool(raw.get("allowCloud")) and bool(raw_routing.get("allowCloud"))
    elif has_routing:
        allow_cloud = bool(raw_routing.get("allowCloud"))
    elif has_top:
        allow_cloud = bool(raw.get("allowCloud"))
    else:
        allow_cloud = bool(result.get("allowCloud", False))

    result["allowCloud"] = allow_cloud
    result["routing"] = {
        "allowCloud": allow_cloud,
        # 无论文档怎么写，这里恒为 True
        "sensitiveForceLocal": True,
    }
    # features 必须重新规范一遍：文档里留的只能是纯 JSON 形态
    # （`{flag: {enabled, rolloutPercent, ...}}`），不能是内部 dataclass ——
    # 文档要被 canonical() 序列化算 etag，混入对象会让 etag 不可复现。
    result["features"] = to_document(normalize_flags(result.get("features"), strict=False))
    return result


def _clone(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.loads(json.dumps(value))
    return value


# --------------------------------------------------------------------------- 校验
def validate_document(raw: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化一份策略文档。不通过就抛 400，**不做静默修正**。"""
    if not isinstance(raw, dict):
        raise BadRequest("策略文档必须是对象")

    unknown = set(raw) - set(DEFAULT_POLICY)
    if unknown:
        raise BadRequest(
            f"策略文档含未知字段: {', '.join(sorted(unknown))}。"
            f"可用字段: {', '.join(sorted(DEFAULT_POLICY))}"
        )

    # --- 不可绕过项：显式关闭敏感路径强制本地 = 拒收整份文档 ---
    routing = raw.get("routing")
    if isinstance(routing, dict) and "sensitiveForceLocal" in routing:
        if routing["sensitiveForceLocal"] is not True:
            raise BadRequest(
                "routing.sensitiveForceLocal 不允许设为 false。"
                "这是合规红线（代码不出内网），不可配置绕过。"
            )

    for name in _BOOL_AND_FIELDS:
        if name in raw and not isinstance(raw[name], bool):
            raise BadRequest(f"{name} 必须是布尔值")

    for name in _INT_MIN_FIELDS:
        if name not in raw:
            continue
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise BadRequest(f"{name} 必须是整数")
        if value <= 0 or value > 10_000_000:
            raise BadRequest(f"{name} 必须在 1–10000000 之间，收到 {value}")

    for name in _LIST_UNION_FIELDS + _LIST_INTERSECT_FIELDS:
        if name not in raw:
            continue
        value = raw[name]
        if not isinstance(value, list):
            raise BadRequest(f"{name} 必须是数组")
        if len(value) > _MAX_LIST_ITEMS:
            raise BadRequest(f"{name} 最多 {_MAX_LIST_ITEMS} 项，收到 {len(value)}")
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise BadRequest(f"{name} 的每一项都必须是非空字符串")
            if len(item) > _MAX_LIST_ITEM_LEN:
                raise BadRequest(f"{name} 里有一项过长（>{_MAX_LIST_ITEM_LEN}）")
            # 拒绝清单里塞代码是常见的"顺手"行为，这里明确挡住
            if "\n" in item or "\r" in item:
                raise BadRequest(f"{name} 的项不能包含换行")

    if isinstance(routing, dict):
        allow = routing.get("allowCloud")
        if allow is not None and not isinstance(allow, bool):
            raise BadRequest("routing.allowCloud 必须是布尔值")

    # 灰度定义每个 flag 都是一张小结构，单独走它自己的校验。
    # 非法就整份拒收，与其它字段一致 —— 不做静默修正。
    normalize_flags(raw.get("features"))

    return _normalize(raw)


# --------------------------------------------------------------------------- 合并
def _merge_allowlist(base: list[str], override: list[str]) -> list[str]:
    """白名单取交集。

    空列表的语义是「不限制」而不是「什么都不允许」，所以：
    - 一方不限 → 结果由**限制的那一方**决定（这里不是取交集为空，
      取交集为空会把"允许所有"变成"全部禁止"，那是收紧过头且没人预期）；
    - 两方都限制 → 取交集。
    """
    if not base or not override:
        return list(base or override)
    keep = {item.lower() for item in override}
    return [item for item in base if item.lower() in keep]


def merge_documents(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """按「只收紧」把多份策略合成一份。

    顺序无关 —— 这很重要：如果合并结果依赖顺序，那么
    「先加团队策略还是先加全局策略」会改变结果，运维迟早踩到。
    这里用的四个算子（AND / MIN / 并集 / 交集）全部满足交换律。
    """
    if not documents:
        return _normalize({})

    normalized_docs = [_normalize(document) for document in documents]
    merged = normalized_docs[0]
    for normalized in normalized_docs[1:]:
        for name in _BOOL_AND_FIELDS:
            merged[name] = bool(merged[name]) and bool(normalized[name])
        for name in _INT_MIN_FIELDS:
            merged[name] = min(int(merged[name]), int(normalized[name]))
        for name in _LIST_UNION_FIELDS:
            combined = list(merged[name]) + list(normalized[name])
            # 去重但保序，方便人读
            seen: set[str] = set()
            merged[name] = [
                item for item in combined if not (item in seen or seen.add(item))
            ]
        for name in _LIST_INTERSECT_FIELDS:
            merged[name] = _merge_allowlist(list(merged[name]), list(normalized[name]))

    # --- 功能灰度：per-flag 只收紧（开关 AND / 百分比 MIN / 名单并集与交集）---
    # 这里直接复用 features 模块的算子，而不是把四个字段摊进来手写：
    # 灰度的合并语义将来若调整（比如加"分批"），只需要改一处。
    merged["features"] = to_document(
        merge_flags(
            [normalize_flags(doc.get("features"), strict=False) for doc in normalized_docs]
        )
    )

    # routing.allowCloud 是 AND；sensitiveForceLocal 恒真
    allow_cloud = all(
        bool(doc.get("routing", {}).get("allowCloud", False)) for doc in normalized_docs
    )
    merged["routing"] = {"allowCloud": allow_cloud, "sensitiveForceLocal": True}
    # allowCloud 与 routing.allowCloud 必须一致，取更严的那个
    merged["allowCloud"] = bool(merged["allowCloud"]) and allow_cloud

    # --- 列表字段统一规范化排序 ---
    # 这些字段语义上是**集合**，但并集/交集的计算顺序会带出不同的排列。
    # 不排序的话，`merge([g, t])` 与 `merge([t, g])` 得到的内容等价、
    # 字节不同 —— 而 etag 是对字节算的，客户端会以为自己拿到了新策略，
    # 反复重拉。更糟的是「顺序无关」这个承诺就成了空话，
    # 运维在排查时会看到「同样的两份策略合并出不同结果」。
    for name in _LIST_UNION_FIELDS + _LIST_INTERSECT_FIELDS:
        merged[name] = sorted(set(merged[name]), key=lambda s: (s.lower(), s))
    return merged


def etag_for(document: dict[str, Any]) -> str:
    return fingerprint(canonical(document), length=24)


# --------------------------------------------------------------------------- 服务
class PolicyService:
    def __init__(self, *, store, config, clock) -> None:
        self.store = store
        self.config = config
        self.clock = clock

    # ------------------------------------------------------------------ 发布
    def publish(
        self,
        *,
        actor,
        scope_type: str,
        scope_id: str,
        document: dict[str, Any],
        note: str = "",
    ) -> dict[str, Any]:
        rbac.require_permission(actor, rbac.PERM_POLICY_WRITE)
        if scope_type not in (SCOPE_GLOBAL, SCOPE_TEAM):
            raise BadRequest(f"未知的策略作用域 {scope_type}")
        if scope_type == SCOPE_GLOBAL:
            if not actor.is_org_wide:
                raise Forbidden("只有组织所有者可以发布全局策略")
        else:
            actor.scope_team(scope_id, rbac.PERM_POLICY_WRITE)

        normalized = validate_document(document)
        now = self.clock.now()
        version = self.store.next_policy_version(scope_type, scope_id)
        row = self.store.insert_policy(
            new_id("pol"),
            scope_type,
            scope_id,
            version,
            canonical(normalized),
            created_by=actor.user_id,
            note=note[:200],
            now=now,
        )
        return {
            "scopeType": scope_type,
            "scopeId": scope_id,
            "version": int(row.get("version") or version),
            "etag": etag_for(normalized),
            "policy": normalized,
            "createdAt": now,
        }

    # ------------------------------------------------------------------ 下发
    @staticmethod
    def _features_for(document: dict[str, Any], actor: Any) -> dict[str, bool]:
        """按主体算出灰度开关。

        灰度在服务端评估而不是下发定义让客户端自己算：客户端计算意味着
        「谁在灰度内」依赖客户端实现，两台机器可能得出不同结果，
        而这个结论是要进审计的。
        """
        return evaluate_features(
            normalize_flags(document.get("features"), strict=False),
            user_id=getattr(actor, "user_id", "") or "",
            device_id=getattr(actor, "device_id", "") or "",
        )

    def resolve(self, *, actor, team_id: str = "") -> PolicyBundle:
        """算出该主体当前生效的策略。

        层级：全局 → 团队（该用户所属的、被显式指定的那个）。
        只取一层团队策略而不是把所有所属团队都合进来：
        「我在 A 团队当管理员、在 B 团队当开发」时，把两个团队的策略
        一起收紧会让 B 团队的宽松设置莫名失效，排查起来极其迷惑。
        调用方可以显式指定 team_id 来选择上下文。
        """
        if not self.config.policy_required:
            # 显式关掉策略下发时，回默认（保守）策略而不是"全放开"
            document = _normalize({})
            return PolicyBundle(
                document=document,
                version=0,
                etag=etag_for(document),
                sources=[{"scope": "builtin-default"}],
                emitted_at=self.clock.now(),
                features=self._features_for(document, actor),
            )

        documents: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []

        global_row = self.store.get_latest_policy(SCOPE_GLOBAL, "")
        if global_row is not None:
            documents.append(self._decode(global_row))
            sources.append(
                {"scope": SCOPE_GLOBAL, "version": int(global_row.get("version") or 0)}
            )

        chosen_team = team_id or (actor.memberships[0].team_id if actor.memberships else "")
        if chosen_team:
            if not actor.is_org_wide and chosen_team not in actor.team_ids:
                raise Forbidden("无权获取该团队的策略")
            team_row = self.store.get_latest_policy(SCOPE_TEAM, chosen_team)
            if team_row is not None:
                documents.append(self._decode(team_row))
                sources.append(
                    {
                        "scope": SCOPE_TEAM,
                        "scopeId": chosen_team,
                        "version": int(team_row.get("version") or 0),
                    }
                )

        if not documents:
            # 一份策略都没有：用默认（保守）策略，并标注出来，
            # 让客户端 UI 能提示「尚未配置策略」
            document = _normalize({})
            sources.append({"scope": "builtin-default"})
        else:
            document = merge_documents(documents)

        version = max((int(s.get("version") or 0) for s in sources), default=0)
        return PolicyBundle(
            document=document,
            version=version,
            etag=etag_for(document),
            sources=sources,
            emitted_at=self.clock.now(),
            features=self._features_for(document, actor),
        )

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        raw = row.get("document")
        if isinstance(raw, dict):
            return raw
        try:
            parsed = json.loads(str(raw or "{}"))
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def list_versions(self, *, actor, scope_type: str, scope_id: str = "") -> list[dict[str, Any]]:
        rbac.require_permission(actor, rbac.PERM_POLICY_READ)
        if scope_type == SCOPE_TEAM:
            actor.scope_team(scope_id, rbac.PERM_POLICY_READ)
        elif scope_type == SCOPE_GLOBAL and not actor.is_org_wide:
            raise Forbidden("只有组织所有者可以查看全局策略历史")

        rows = self.store.list_policy_versions(scope_type, scope_id)
        return [
            {
                "version": int(r.get("version") or 0),
                "etag": etag_for(self._decode(r)),
                "createdAt": int(r.get("created_at") or 0),
                "createdBy": str(r.get("created_by") or ""),
                "note": str(r.get("note") or ""),
                # 版本列表只回摘要，不回完整文档 —— 十几个版本全文加载很慢且没人看
                "summary": _summarize(self._decode(r)),
            }
            for r in rows
        ]


def _summarize(document: dict[str, Any]) -> str:
    normalized = _normalize(document)
    return (
        f"cloud={'on' if normalized['allowCloud'] else 'off'} "
        f"shell={'on' if normalized['allowShell'] else 'off'} "
        f"write={'on' if normalized['allowWrite'] else 'off'} "
        f"maxIter={normalized['maxIterations']} "
        f"deniedPaths={len(normalized['deniedPaths'])} "
        f"requireApproval={len(normalized['requireApproval'])} "
        f"{summarize_flags(normalize_flags(normalized['features'], strict=False))}"
    )


# --------------------------------------------------------------------------- 客户端侧
# 策略文档用 camelCase（对外协议风格），客户端配置用 snake_case。
# **必须显式映射**：不做映射而"按名字猜"，结果是两边键名对不上，
# 合并逻辑全程跑空，策略看着下发了但一点没生效 —— 这是最难发现的一类失效，
# 因为日志里一切正常，配置面板也显示"已应用策略"。
_CLIENT_KEY_MAP: dict[str, str] = {
    "allowCloud": "allow_cloud",
    "allowShell": "allow_shell",
    "allowWrite": "allow_write",
    "autoApproveReview": "auto_approve_review",
    "maxIterations": "max_iterations",
    "maxContextChars": "max_context_chars",
    "toolOutputLimit": "tool_output_limit",
    "commandTimeout": "command_timeout",
    "deniedPaths": "denied_paths",
    "deniedCommands": "denied_commands",
    "requireApproval": "require_approval",
    "allowedModels": "allowed_models",
}


def _client_key(policy_key: str) -> str:
    return _CLIENT_KEY_MAP.get(policy_key, policy_key)


def apply_to_client_config(
    document: dict[str, Any],
    *,
    client: dict[str, Any],
    stale: bool,
    max_staleness: int = 0,
) -> tuple[dict[str, Any], list[str]]:
    """把服务端策略套到客户端本地配置上。返回 (生效配置, 变更说明)。

    这是**客户端**要调用的函数（agent-core 的 governance 包会引它），
    放在服务端同一份代码里是为了保证两端用**同一个合并实现** ——
    两端各写一份「只收紧」的逻辑，迟早会出现一边紧一边松。

    客户端配置字典的键用 snake_case（与 agent-core 的 Config 一致）。
    """
    notes: list[str] = []
    effective = _clone(client)
    policy = _normalize(document)

    # --- 过期：降级到受限模式，而不是沿用旧策略继续跑 ---
    if stale:
        policy = _normalize(
            {
                "allowCloud": False,
                "autoApproveReview": False,
                "commandTimeout": min(int(policy["commandTimeout"]), 60),
            }
        )
        notes.append(
            f"策略已过期（>{max_staleness}s 未更新），进入受限模式："
            "仅本地模型、所有写操作强制人工审批"
        )

    # --- 能力开关：只允许 True→False ---
    for policy_key in ("allowCloud", "allowShell", "allowWrite", "autoApproveReview"):
        key = _client_key(policy_key)
        current = effective.get(key)
        if current is None:
            # 客户端配置里没这个字段：策略说不允许，必须显式写进去。
            # 不写的话客户端会用它的默认值（很可能是 True），策略等于没下发。
            if not bool(policy[policy_key]):
                effective[key] = False
                notes.append(f"{key} 按策略初始化为 false")
            continue
        if bool(current) and not bool(policy[policy_key]):
            effective[key] = False
            notes.append(f"{key} 被策略收紧为 false")

    # --- 数值上限：只允许调小 ---
    for policy_key in _INT_MIN_FIELDS:
        key = _client_key(policy_key)
        limit = int(policy[policy_key])
        current = effective.get(key)
        if current is None:
            effective[key] = limit
            notes.append(f"{key} 按策略初始化为 {limit}")
            continue
        if isinstance(current, bool) or not isinstance(current, int):
            continue
        if current > limit:
            effective[key] = limit
            notes.append(f"{key} 由 {current} 收紧为 {limit}")

    # --- 拒绝/强制审批清单：并集（客户端原有的不能被顶掉）---
    for policy_key in _LIST_UNION_FIELDS:
        key = _client_key(policy_key)
        existing = list(effective.get(key) or [])
        combined = existing + list(policy[policy_key])
        seen: set[str] = set()
        effective[key] = [x for x in combined if not (x in seen or seen.add(x))]
        added = len(effective[key]) - len(existing)
        if added:
            notes.append(f"{key} 新增 {added} 条来自策略的限制")

    # --- 模型白名单：交集（服务端列出的才可用）---
    key = _client_key("allowedModels")
    policy_models = list(policy["allowedModels"])
    existing_models = list(effective.get(key) or [])
    if policy_models:
        if existing_models:
            keep = {m.lower() for m in policy_models}
            effective[key] = [m for m in existing_models if m.lower() in keep]
            dropped = len(existing_models) - len(effective[key])
            if dropped:
                notes.append(f"{key} 被策略收窄，移除 {dropped} 个模型")
        else:
            effective[key] = policy_models
            notes.append(f"{key} 按策略限定为 {len(policy_models)} 个模型")

    # sensitiveForceLocal 恒真，这里是它的落地：客户端必须记下这一点
    effective["sensitive_force_local"] = True
    return effective, notes


__all__ = [
    "DEFAULT_POLICY",
    "PolicyBundle",
    "PolicyService",
    "SCOPE_GLOBAL",
    "SCOPE_TEAM",
    "apply_to_client_config",
    "etag_for",
    "merge_documents",
    "validate_document",
]
