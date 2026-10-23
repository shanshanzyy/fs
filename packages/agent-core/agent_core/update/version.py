"""版本号比较（M9）。

**这里刻意与服务端 `vibe_server/releases.py` 各存一份实现。**

治理面与内核是两个零第三方依赖的包，`agent_core` 不 import `vibe_server`
（反之亦然）。而「要不要更新」这个判断**两端都要做**：服务端决定
`update` 是否生效，客户端还要在本地再判一次（服务端不可达时靠镜像兜底）。
两端口径不一致的表现极其难查 —— 服务端说「有新版本」，客户端却说「已是最新」，
或者反过来装上了一个更旧的包。所以两份实现的口径**写死在同一套规则上**，
并由测试做交叉用例：

  · `[v]X[.Y[.Z[.W]]]`，1–4 段，每段纯 ASCII 数字且不超过 9 位（防超长整数）；
  · 前导零按**数值**比（`1.02` 等于 `1.2`，不是字典序）；
  · 缺位补 0（`1.2` 等于 `1.2.0`）；
  · 解析失败**不给更新**（fail-closed）—— 反向失败会让「版本号写错」变成
    「悄悄装了一个更旧的包」。
"""

from __future__ import annotations

__all__ = ["at_least", "compare", "is_newer", "parse_version"]


def parse_version(text: str) -> tuple[int, ...] | None:
    """解析 `[v]X[.Y[.Z[.W]]]`；不合法返回 None。"""
    raw = str(text or "").strip()
    if raw[:1] in ("v", "V"):
        raw = raw[1:]
    if not raw or len(raw) > 40:
        return None
    parts = raw.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    numbers: list[int] = []
    for part in parts:
        if not part.isascii() or not part.isdigit() or len(part) > 9:
            return None
        numbers.append(int(part))
    return tuple(numbers)


def compare(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    """逐段比较，缺位按 0 补。返回 -1 / 0 / 1。"""
    for index in range(max(len(left), len(right))):
        a = left[index] if index < len(left) else 0
        b = right[index] if index < len(right) else 0
        if a != b:
            return 1 if a > b else -1
    return 0


def is_newer(candidate: str, current: str) -> bool:
    """`candidate` 是否**严格**新于 `current`。任一侧解析失败 → False。"""
    new = parse_version(candidate)
    old = parse_version(current)
    if new is None or old is None:
        return False
    return compare(new, old) > 0


def at_least(current: str, floor: str) -> bool:
    """`current >= floor`。任一侧解析失败 → True（不触发强制升级）。"""
    a = parse_version(current)
    b = parse_version(floor)
    if a is None or b is None:
        return True
    return compare(a, b) >= 0
