"""配额与限流。

**核心是「预留-结算」而不是「先查后加」**。原因很具体：

> 查一次剩余额度、判断够不够、然后扣减 —— 这三步之间只要有间隙，
> 并发请求就会同时看到「够」，然后一起扣。结果是超额。
> 这就是经典的 TOCTOU（检查时/使用时不一致）。

所以流程是：

```
reserve(estimate)  → 原子地「占用」一段额度（条件 UPDATE + rowcount）
    ↓ 上游调用
settle(actual)     → 释放预留，按实际用量入账
    or
release()          → 上游失败，撤销预留（不入账）
```

`estimate` 是预估上限（按 prompt 长度 + max_tokens 算），实际用量通常更小，
结算时按实际的来 —— 所以预留偏大会浪费一点额度但不会超发，
预留偏小则会超发。**估算必须偏保守（宁可估大）**，这是唯一安全的偏置方向。

限流单独一层：它是「每分钟多少次」，与「每天多少 token」正交，
两者都要过。限流用固定窗口计数，实现简单且在单机 SQLite 上够用；
真正的高并发场景要换滑动窗口/令牌桶，那时应该换到 Redis（见 README 的切换条件）。
"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass
from typing import Any

from ..errors import QuotaExceeded

SCOPE_GLOBAL = "global"
SCOPE_TEAM = "team"
SCOPE_USER = "user"

# 预留的偏置系数：估算值再乘一点余量。
# 上游返回的 token 数会略高于「输入长度 + max_tokens」的朴素估算
# （系统提示、工具 schema、多轮历史都会被计入），系数就是补这个差。
_ESTIMATE_SAFETY = 1.25
_MIN_ESTIMATE = 256

#: 用量汇总最多回溯多少天。上限存在的意义是别让一个 `?days=100000`
#: 把整张 usage_counters 拉进内存 —— 查询参数是客户端可控的。
MAX_SUMMARY_DAYS = 180
#: 汇总时「每天最多取多少行」（团队维度下一天每人一行）。
#: 超过就说明数据被截断，响应里会带 `truncated: true`。
_MAX_ROWS_PER_DAY = 500


def month_days(epoch: int) -> int:
    """epoch 所在自然月的天数（本地时区）。预算摊销窗口的默认取值。"""
    local = time.localtime(epoch)
    return calendar.monthrange(local.tm_year, local.tm_mon)[1]


def derive_daily_tokens(
    *, budget: int, unit_price: int, window: int, members: int = 1, month: int = 30
) -> int:
    """由预算推导「每人每日」云端 token 额度。

    - `unit_price > 0`：`budget` 是**成本预算**，先换算成 token 预算
      （`budget // unit_price`）；否则 `budget` 直接就是 **token 预算**。
    - `window > 0` 用给定窗口天数；否则用 `month`（当月天数）。
    - 团队预算按 `members`（成员数）**分摊到人**。
    - 全程**向下取整**：宁可少给，不可超发。
    - 预算 ≤ 0 → 返回 0（视为未设，调用方退回 config 默认）。
    """
    budget = int(budget)
    if budget <= 0:
        return 0
    unit_price = int(unit_price)
    tokens_budget = budget // unit_price if unit_price > 0 else budget
    span = int(window) if int(window) > 0 else max(1, int(month))
    people = max(1, int(members))
    return max(0, tokens_budget // span // people)


@dataclass(frozen=True)
class Limits:
    daily_cloud_tokens: int = 0
    daily_requests: int = 0
    requests_per_minute: int = 0

    def describe(self) -> dict[str, int]:
        return {
            "dailyCloudTokens": self.daily_cloud_tokens,
            "dailyRequests": self.daily_requests,
            "requestsPerMinute": self.requests_per_minute,
        }


@dataclass
class Reservation:
    """一次预留的凭据。结算时要用同一个 estimate 释放，不能少。"""

    day: str
    user_id: str
    team_id: str
    estimate: int
    counted_request: bool = True
    _settled: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "estimate": self.estimate,
            "countedRequest": self.counted_request,
            "settled": self._settled,
        }


class QuotaService:
    def __init__(self, *, store, config, clock) -> None:
        self.store = store
        self.config = config
        self.clock = clock

    # ============================================================== 限制解析
    def limits_for(self, *, user_id: str, team_id: str) -> Limits:
        """生效限制（见 `resolve_limits`）。"""
        return self.resolve_limits(user_id=user_id, team_id=team_id)[0]

    def resolve_limits(
        self, *, user_id: str, team_id: str
    ) -> tuple[Limits, str, dict[str, Any]]:
        """解析生效限制，并返回额度来源与推导明细。

        解析链：**用户显式 > 团队显式 > 全局显式 > 推导值 > config 默认**。

        策略叠加的方向是**越具体越优先**，而不是取最小值。
        原因：团队配额是「这个团队总共能用多少」的成本预算，
        不该被某一个成员的宽松设置顶掉，也不该反过来把整个团队卡死。
        真正的「只能收紧」纪律用在**路由开关**上（见 routing.decide），
        两件事的语义不同，用同一套规则会拧巴。

        **推导只补默认**：只要任一层存在显式策略行，就用它（哪怕值是 0，
        那是「不限」）；只有当三层都没有显式策略时，才用预算推导值。
        返回的 `source` 描述**云端 token 额度**的来源，形如
        `explicit:team` / `derived:global` / `default`。
        """
        found: dict[str, dict[str, Any]] = {}
        for scope_type, scope_id in (
            (SCOPE_USER, user_id),
            (SCOPE_TEAM, team_id),
            (SCOPE_GLOBAL, ""),
        ):
            row = self.store.get_quota_policy(scope_type, scope_id)
            if row is not None:
                found[scope_type] = row

        def pick(field: str, default: int) -> int:
            for scope_type in (SCOPE_USER, SCOPE_TEAM, SCOPE_GLOBAL):
                row = found.get(scope_type)
                if row is not None:
                    value = row.get(field)
                    if value is not None:
                        return int(value)
            return int(default)

        explicit_scope = next(
            (st for st in (SCOPE_USER, SCOPE_TEAM, SCOPE_GLOBAL) if st in found), ""
        )
        derived = self.derive_limits(user_id=user_id, team_id=team_id)

        if explicit_scope:
            tokens = pick("daily_cloud_tokens", self.config.default_daily_cloud_tokens)
            source = f"explicit:{explicit_scope}"
        elif int(derived.get("dailyCloudTokens") or 0) > 0:
            tokens = int(derived["dailyCloudTokens"])
            source = f"derived:{derived['scope']}"
        else:
            tokens = int(self.config.default_daily_cloud_tokens)
            source = "default"

        limits = Limits(
            daily_cloud_tokens=tokens,
            daily_requests=pick("daily_requests", self.config.default_daily_requests),
            requests_per_minute=pick("requests_per_minute", self.config.default_rpm),
        )
        return limits, source, derived

    def derive_limits(self, *, user_id: str, team_id: str) -> dict[str, Any]:
        """由预算推导云端 token 日额度。

        按「用户 > 团队 > 全局」找**第一条** `budget > 0` 的预算策略：
        团队预算按成员数分摊到人；窗口默认取当月天数；一律向下取整。
        预算为 0 / 未设 → 不推导。

        返回 `{"dailyCloudTokens": int, "scope": str|None, "budget": dict|None}`
        —— 额度、其来源作用域、命中的预算策略（供 `status()` 标注来源）。
        """
        month = month_days(self.clock.now())
        for scope_type, scope_id in (
            (SCOPE_USER, user_id),
            (SCOPE_TEAM, team_id),
            (SCOPE_GLOBAL, ""),
        ):
            if scope_type != SCOPE_GLOBAL and not scope_id:
                continue
            row = self.store.get_budget_policy(scope_type, scope_id)
            if row is None:
                continue
            budget = int(row.get("budget") or 0)
            if budget <= 0:
                continue
            members = (
                max(1, len(self.store.list_team_members(scope_id)))
                if scope_type == SCOPE_TEAM
                else 1
            )
            daily = derive_daily_tokens(
                budget=budget,
                unit_price=int(row.get("unit_price") or 0),
                window=int(row.get("window") or 0),
                members=members,
                month=month,
            )
            return {
                "dailyCloudTokens": daily,
                "scope": scope_type,
                "budget": {
                    "scopeType": scope_type,
                    "scopeId": scope_id,
                    "budget": budget,
                    "unitPrice": int(row.get("unit_price") or 0),
                    "window": int(row.get("window") or 0),
                    "members": members,
                },
            }
        return {"dailyCloudTokens": 0, "scope": None, "budget": None}

    # ============================================================== 估算
    @staticmethod
    def estimate_tokens(*, prompt_tokens: int, max_tokens: int) -> int:
        """预估本次上游调用可能消耗的 token 上限。

        偏保守（乘安全系数、有下限）是刻意的：见文件头。
        """
        base = max(0, int(prompt_tokens)) + max(0, int(max_tokens))
        return max(_MIN_ESTIMATE, int(base * _ESTIMATE_SAFETY))

    # ============================================================== 预留
    def reserve(
        self,
        *,
        user_id: str,
        team_id: str,
        is_cloud: bool,
        prompt_tokens: int,
        max_tokens: int,
        apply_rate_limit: bool = True,
    ) -> Reservation:
        """占用额度。不足则抛 QuotaExceeded（429）。

        只有云端请求查 token 配额：本地模型在用户自己的机器上跑，
        对它配额等于「不许你用自己的显卡」，既拦不住也不合理。
        但**请求数限流对两者都生效** —— 它防的是打爆服务端与上游。
        """
        now = self.clock.now()
        day = self.clock.today()
        limits = self.limits_for(user_id=user_id, team_id=team_id)

        # --- 1) 每分钟限流（先做，它最便宜且能挡住打爆场景）---
        if apply_rate_limit and limits.requests_per_minute > 0:
            window = now - (now % 60)
            allowed, count = self.store.hit_rate_limit(
                f"user:{user_id}", window, limits.requests_per_minute
            )
            if not allowed:
                retry_after = 60 - (now % 60)
                raise QuotaExceeded(
                    f"请求过于频繁（每分钟上限 {limits.requests_per_minute}），"
                    f"请 {retry_after} 秒后重试",
                    details={
                        "kind": "rate_limit",
                        "limit": limits.requests_per_minute,
                        "current": count,
                        "retryAfter": retry_after,
                        "scope": "user",
                    },
                )

        estimate = self.estimate_tokens(prompt_tokens=prompt_tokens, max_tokens=max_tokens)

        # --- 2) 每日 token / 请求数配额（原子预留）---
        token_limit = limits.daily_cloud_tokens if is_cloud else 0
        request_limit = limits.daily_requests
        if token_limit <= 0 and request_limit <= 0:
            return Reservation(day=day, user_id=user_id, team_id=team_id, estimate=0, counted_request=False)

        ok = self.store.reserve_usage(
            day,
            user_id,
            team_id,
            estimate=estimate if token_limit > 0 else 0,
            token_limit=token_limit,
            request_limit=request_limit,
            now=now,
        )
        if not ok:
            usage = self.store.get_usage(day, user_id)
            used = int(usage.get("cloud_tokens") or 0) + int(usage.get("reserved_tokens") or 0)
            # 分不清是 token 超了还是请求数超了，回明细让客户端能给出有用的提示
            raise QuotaExceeded(
                "今日配额已用尽。可以继续用本地模型，或明天再来。",
                details={
                    "kind": "quota",
                    "day": day,
                    "cloudTokensUsed": used,
                    "cloudTokensLimit": token_limit,
                    "requestsUsed": int(usage.get("requests") or 0),
                    "requestsLimit": request_limit,
                    "resetInSeconds": self.clock.next_midnight(),
                    "hint": "云端路由已不可用；把请求压回本地模型即可继续。",
                },
            )

        return Reservation(
            day=day,
            user_id=user_id,
            team_id=team_id,
            estimate=estimate if token_limit > 0 else 0,
            counted_request=request_limit > 0,
        )

    # ============================================================== 结算
    def settle(
        self,
        reservation: Reservation,
        *,
        actual_tokens: int,
        is_cloud: bool,
    ) -> dict[str, Any]:
        """按实际用量入账。即使 actual 超过 estimate 也要如实记 —— 少记就是漏账。"""
        if reservation._settled:
            return {"settled": False, "reason": "already"}

        now = self.clock.now()
        # 放宽上限保护：上游可能返回超过 max_tokens 的用量（比如它把思考过程
        # 也算进去了）。这里只做防溢出的截断，不做"不超过预留"的强制——
        # 强制成预留值等于系统性少记，配额就永远超不了，也就没意义了。
        actual = max(0, int(actual_tokens))
        hard_ceiling = reservation.estimate * 8 if reservation.estimate else actual
        if hard_ceiling and actual > hard_ceiling:
            actual = hard_ceiling

        self.store.settle_usage(
            reservation.day,
            reservation.user_id,
            reservation.team_id,
            estimate=reservation.estimate,
            actual_tokens=actual,
            is_cloud=is_cloud,
            counted_request=reservation.counted_request,
            now=now,
        )
        reservation._settled = True
        return {"settled": True, "actualTokens": actual, "isCloud": is_cloud}

    def release(self, reservation: Reservation) -> None:
        """上游失败：撤销预留，不入账。

        这条路径**必须有**。少了它，一次上游挂掉就会永久占掉一段额度，
        用户看到「今天额度没了」但审计里一条调用都没有 —— 最难查的一类问题。
        """
        if reservation._settled or reservation.estimate <= 0:
            reservation._settled = True
            return
        self.store.release_usage(
            reservation.day, reservation.user_id, reservation.estimate, now=self.clock.now()
        )
        reservation._settled = True

    # ============================================================== 查询
    def status(self, *, actor, user_id: str = "", team_id: str = "") -> dict[str, Any]:
        """配额视图。

        **作用域校验不可省**：没有它，任何登录用户都能查全公司的用量。
        """
        from .. import rbac
        from ..errors import Forbidden

        target = user_id or actor.user_id
        if target != actor.user_id:
            actor.scope_user(target, rbac.PERM_USAGE_READ)
        if team_id and team_id not in actor.team_ids and not actor.is_org_wide:
            raise Forbidden("无权查看该团队的配额")
        day = self.clock.today()
        usage = self.store.get_usage(day, target)
        limits, source, derived = self.resolve_limits(user_id=target, team_id=team_id or "")
        cloud_used = int(usage.get("cloud_tokens") or 0) + int(usage.get("reserved_tokens") or 0)
        return {
            "day": day,
            "userId": target,
            "teamId": team_id,
            "usage": {
                "cloudTokens": int(usage.get("cloud_tokens") or 0),
                "reservedTokens": int(usage.get("reserved_tokens") or 0),
                "localTokens": int(usage.get("local_tokens") or 0),
                "requests": int(usage.get("requests") or 0),
            },
            "limits": limits.describe(),
            # 让客户端能分辨「这个额度是推导来的还是显式设的」：
            # source 取 explicit:user/team/global、derived:...、default 之一。
            "source": source,
            "derived": {
                "dailyCloudTokens": int(derived.get("dailyCloudTokens") or 0),
                "scope": derived.get("scope"),
            },
            "budget": derived.get("budget"),
            "remaining": {
                "cloudTokens": (
                    max(0, limits.daily_cloud_tokens - cloud_used)
                    if limits.daily_cloud_tokens > 0
                    else None
                ),
                "requests": (
                    max(0, limits.daily_requests - int(usage.get("requests") or 0))
                    if limits.daily_requests > 0
                    else None
                ),
            },
            "resetInSeconds": self.clock.next_midnight(),
        }

    # ============================================================== 成本统计
    def usage_summary(
        self, *, actor, user_id: str = "", team_id: str = "", days: int = 30
    ) -> dict[str, Any]:
        """按天汇总用量 —— 「成本看板」的数据源。

        作用域校验比 `status()` 更要紧：`status()` 看的是「今天还剩多少」，
        这里看的是「过去 N 天花了多少」。能查全公司的用量等于能看全公司的开销，
        所以跨用户要 `usage:read`、跨团队要 `usage:read_team`。
        """
        from datetime import date, timedelta

        from .. import rbac
        from ..errors import Forbidden

        # 窗口一律夹到 [1, MAX_SUMMARY_DAYS]。
        # 刻意**不**把 0 当成「没传，用默认 30」：客户端显式写 `?days=0`
        # 是想看今天，静默放大成 30 天等于把「一天的量」换成一个月的量，
        # 而且没有任何提示。要更大的窗口就明说要几天。
        days = max(1, min(int(days), MAX_SUMMARY_DAYS))
        target_user = user_id or actor.user_id
        if not team_id and target_user != actor.user_id:
            actor.scope_user(target_user, rbac.PERM_USAGE_READ)
        if team_id and team_id not in actor.team_ids and not actor.is_org_wide:
            raise Forbidden("无权查看该团队的用量")

        today = self.clock.today()
        since = (date.fromisoformat(today) - timedelta(days=days - 1)).isoformat()
        # limit 给得比天数宽：团队维度下一天可能有多行（每人一行）。
        # 取到上限说明被截断了 —— 那会让合计偏低，所以如实标出来，
        # 而不是让调用方拿到一个「看起来正常但少了几天」的数。
        limit = days * _MAX_ROWS_PER_DAY
        rows = self.store.usage_series(
            user_id=target_user if not team_id else "",
            team_id=team_id,
            since_day=since,
            limit=limit,
        )

        by_day: dict[str, dict[str, int]] = {}
        for row in rows:
            day = str(row.get("day") or "")
            bucket = by_day.setdefault(
                day, {"cloudTokens": 0, "localTokens": 0, "requests": 0}
            )
            bucket["cloudTokens"] += int(row.get("cloud_tokens") or 0)
            bucket["localTokens"] += int(row.get("local_tokens") or 0)
            bucket["requests"] += int(row.get("requests") or 0)

        series = [{"day": day, **counts} for day, counts in sorted(by_day.items())]
        totals = {
            key: sum(item[key] for item in series)
            for key in ("cloudTokens", "localTokens", "requests")
        }
        return {
            "scope": {"userId": "" if team_id else target_user, "teamId": team_id},
            "sinceDay": since,
            "untilDay": today,
            "days": days,
            "truncated": len(rows) >= limit,
            "totals": totals,
            "series": series,
        }

    # ============================================================== 策略管理
    def set_policy(
        self,
        *,
        actor,
        scope_type: str,
        scope_id: str,
        daily_cloud_tokens: int,
        daily_requests: int,
        requests_per_minute: int,
    ) -> dict[str, Any]:
        from .. import rbac
        from ..errors import BadRequest
        from ..ids import new_id

        rbac.require_permission(actor, rbac.PERM_QUOTA_WRITE)
        if scope_type not in (SCOPE_GLOBAL, SCOPE_TEAM, SCOPE_USER):
            raise BadRequest(f"未知的作用域类型 {scope_type}")
        if scope_type == SCOPE_TEAM:
            actor.scope_team(scope_id, rbac.PERM_QUOTA_WRITE)
            if self.store.get_team(scope_id) is None:
                # 校验存在性是必要的：写进一条永远匹配不上的配额，
                # 界面显示"已设置"，实际一点没生效 —— 这种沉默的失效
                # 比明确报错难查得多（尤其当管理员以为"设好了"就不再看）。
                raise BadRequest(f"团队 {scope_id!r} 不存在")
        elif scope_type == SCOPE_USER:
            actor.scope_user(scope_id, rbac.PERM_QUOTA_WRITE)
            if self.store.get_user(scope_id) is None:
                raise BadRequest(
                    f"用户 {scope_id!r} 不存在。"
                    "如果想给「所有用户」设默认额度，请用 scopeType=global、scopeId 留空。"
                )
        elif not actor.is_org_wide:
            raise BadRequest("只有组织所有者可以设置全局配额")

        for name, value in (
            ("daily_cloud_tokens", daily_cloud_tokens),
            ("daily_requests", daily_requests),
            ("requests_per_minute", requests_per_minute),
        ):
            if int(value) < 0:
                raise BadRequest(f"{name} 不能为负")

        now = self.clock.now()
        row = self.store.upsert_quota_policy(
            new_id("qta"),
            scope_type,
            scope_id,
            daily_cloud_tokens=int(daily_cloud_tokens),
            daily_requests=int(daily_requests),
            requests_per_minute=int(requests_per_minute),
            updated_by=actor.user_id,
            now=now,
        )
        return {
            "scopeType": scope_type,
            "scopeId": scope_id,
            "limits": {
                "dailyCloudTokens": int(row.get("daily_cloud_tokens") or 0),
                "dailyRequests": int(row.get("daily_requests") or 0),
                "requestsPerMinute": int(row.get("requests_per_minute") or 0),
            },
            "updatedAt": now,
        }

    def list_policies(self, *, actor) -> list[dict[str, Any]]:
        from .. import rbac

        rbac.require_permission(actor, rbac.PERM_QUOTA_READ)
        rows = self.store.list_quota_policies()
        visible: list[dict[str, Any]] = []
        for row in rows:
            scope_type = str(row.get("scope_type") or "")
            scope_id = str(row.get("scope_id") or "")
            if scope_type == SCOPE_TEAM and not actor.is_org_wide and scope_id not in actor.team_ids:
                continue  # 别的团队的配额不该被看见
            if scope_type == SCOPE_USER and scope_id != actor.user_id and not actor.is_org_wide:
                if not actor.can(rbac.PERM_USAGE_READ_TEAM):
                    continue
            visible.append(
                {
                    "scopeType": scope_type,
                    "scopeId": scope_id,
                    "dailyCloudTokens": int(row.get("daily_cloud_tokens") or 0),
                    "dailyRequests": int(row.get("daily_requests") or 0),
                    "requestsPerMinute": int(row.get("requests_per_minute") or 0),
                    "updatedAt": int(row.get("updated_at") or 0),
                }
            )
        return visible

    # ============================================================== 预算管理
    def _validate_scope(
        self, *, actor, scope_type: str, scope_id: str, permission: str = ""
    ) -> None:
        """作用域校验（预算读写共用）。写法与 `set_policy` 保持一致。"""
        from ..errors import BadRequest

        if scope_type not in (SCOPE_GLOBAL, SCOPE_TEAM, SCOPE_USER):
            raise BadRequest(f"未知的作用域类型 {scope_type}")
        if scope_type == SCOPE_TEAM:
            actor.scope_team(scope_id, permission)
            if self.store.get_team(scope_id) is None:
                raise BadRequest(f"团队 {scope_id!r} 不存在")
        elif scope_type == SCOPE_USER:
            actor.scope_user(scope_id, permission)
            if self.store.get_user(scope_id) is None:
                raise BadRequest(f"用户 {scope_id!r} 不存在")
        elif not actor.is_org_wide:
            raise BadRequest("只有组织所有者可以操作全局配额")

    def _budget_view(self, row: dict[str, Any], *, scope_type: str, scope_id: str) -> dict[str, Any]:
        """把预算行渲染成响应体：原始值 + 推导出的每日额度（便于核对）。"""
        budget = int(row.get("budget") or 0)
        unit_price = int(row.get("unit_price") or 0)
        window = int(row.get("window") or 0)
        members = (
            max(1, len(self.store.list_team_members(scope_id)))
            if scope_type == SCOPE_TEAM and scope_id
            else 1
        )
        return {
            "scopeType": scope_type,
            "scopeId": scope_id,
            "budget": budget,
            "unitPrice": unit_price,
            "window": window,
            "members": members,
            "derivedDailyCloudTokens": derive_daily_tokens(
                budget=budget,
                unit_price=unit_price,
                window=window,
                members=members,
                month=month_days(self.clock.now()),
            ),
            "updatedAt": int(row.get("updated_at") or 0),
            "updatedBy": str(row.get("updated_by") or ""),
        }

    def set_budget(
        self,
        *,
        actor,
        scope_type: str,
        scope_id: str,
        budget: int,
        unit_price: int = 0,
        window: int = 0,
    ) -> dict[str, Any]:
        """写入预算策略（**推导用**，不是显式额度）。"""
        from .. import rbac
        from ..errors import BadRequest
        from ..ids import new_id

        rbac.require_permission(actor, rbac.PERM_QUOTA_WRITE)
        self._validate_scope(
            actor=actor, scope_type=scope_type, scope_id=scope_id, permission=rbac.PERM_QUOTA_WRITE
        )
        for name, value in (
            ("budget", budget),
            ("unit_price", unit_price),
            ("window", window),
        ):
            if int(value) < 0:
                raise BadRequest(f"{name} 不能为负")

        now = self.clock.now()
        row = self.store.upsert_budget_policy(
            new_id("bdg"),
            scope_type,
            scope_id,
            budget=int(budget),
            unit_price=int(unit_price),
            window=int(window),
            updated_by=actor.user_id,
            now=now,
        )
        view = self._budget_view(row, scope_type=scope_type, scope_id=scope_id)
        view["set"] = True
        return view

    def get_budget(self, *, actor, scope_type: str, scope_id: str) -> dict[str, Any]:
        """读取预算策略（作用域校验同 `status()`）。"""
        from .. import rbac

        rbac.require_permission(actor, rbac.PERM_QUOTA_READ)
        self._validate_scope(actor=actor, scope_type=scope_type, scope_id=scope_id)
        row = self.store.get_budget_policy(scope_type, scope_id)
        if row is None:
            return {
                "scopeType": scope_type,
                "scopeId": scope_id,
                "budget": 0,
                "unitPrice": 0,
                "window": 0,
                "members": (
                    max(1, len(self.store.list_team_members(scope_id)))
                    if scope_type == SCOPE_TEAM and scope_id
                    else 1
                ),
                "derivedDailyCloudTokens": 0,
                "updatedAt": 0,
                "updatedBy": "",
                "set": False,
            }
        view = self._budget_view(row, scope_type=scope_type, scope_id=scope_id)
        view["set"] = True
        return view


__all__ = [
    "MAX_SUMMARY_DAYS",
    "Limits",
    "QuotaService",
    "Reservation",
    "SCOPE_GLOBAL",
    "SCOPE_TEAM",
    "SCOPE_USER",
    "derive_daily_tokens",
    "month_days",
]
