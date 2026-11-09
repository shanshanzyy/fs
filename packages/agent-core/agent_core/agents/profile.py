"""工作区级智能体档案（M14-A）。

档案是**用户本人**维护的配置，不是模型可改的状态。落点是
`<workspace>/.vibe/agents.json`，恒随 `Config.workspace` —— 与
`remote-roots.json` 同一条教训：不能跟着可能被钉在别处的远程清单走，
否则「打开另一个文件夹」就会串进上一个工作区的档案。

三条落盘前校验（对齐 `docs/M14-PLAN.md` §3.1）：

1. `name` 工作区内唯一、1–24 字符、禁止控制字符；
2. `tools` 必须是**只读白名单**的子集 —— 写盘 / 执行类工具结构上进不来；
3. 整份文件 ≤ 256 KB，避免把工作区当提示词仓库。

v1 刻意**不支持**每个档案配独立模型 / 端点 / API Key：那会把密钥与出网点
从一个变成 N 个，与 ADR-006（正文不出机）冲突。所有 Agent 共用当前会话的
`client`，差异只体现在提示词与工具集上。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# 与 M14-PLAN §3.1 / §4.5 的口径保持一致。
MAX_NAME_CHARS = 24
MAX_ROLE_CHARS = 200
MAX_PROMPT_BYTES = 8 * 1024
MAX_FILE_BYTES = 256 * 1024

# 档案 id 由本机生成（uuid hex 前 12 位），也允许手写一个稳定标识。
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class AgentProfileError(ValueError):
    """档案不合法，或档案文件坏掉。"""


@dataclass(frozen=True)
class AgentProfile:
    """一条智能体档案。

    `id` 创建后不可改；`name` 是工作区内的人读标识，也要求唯一。
    `tools` 为空元组表示「不给工具」，与「未填写 = 默认全部只读工具」
    是两件事（后者在会话门面里按缺省值展开）。
    """

    id: str
    name: str
    role: str = ""
    system_prompt: str = ""
    tools: tuple[str, ...] = ()
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "systemPrompt": self.system_prompt,
            "tools": list(self.tools),
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AgentProfile":
        if not isinstance(raw, Mapping):
            raise AgentProfileError(
                f"档案需为对象，实际: {type(raw).__name__}"
            )
        name = raw.get("name")
        if not isinstance(name, str):
            raise AgentProfileError("档案缺少字符串字段 name")
        role = raw.get("role", "")
        if not isinstance(role, str):
            raise AgentProfileError("role 需为字符串")
        prompt = raw.get("systemPrompt", raw.get("system_prompt", ""))
        if not isinstance(prompt, str):
            raise AgentProfileError("systemPrompt 需为字符串")
        tools_raw = raw.get("tools", [])
        if not isinstance(tools_raw, list):
            raise AgentProfileError("tools 需为数组")
        tools = tuple(str(item) for item in tools_raw)
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise AgentProfileError("enabled 需为布尔值")
        agent_id = raw.get("id", "")
        if not isinstance(agent_id, str):
            raise AgentProfileError("id 需为字符串")
        return cls(
            id=agent_id.strip(),
            name=name.strip(),
            role=role.strip(),
            system_prompt=prompt,
            tools=_dedupe_tools(tools),
            enabled=enabled,
        )


def _dedupe_tools(tools: Iterable[str]) -> tuple[str, ...]:
    """去重且保留顺序 —— 同一工具写两遍没有语义，不值得报错。"""
    seen: set[str] = set()
    out: list[str] = []
    for item in tools:
        name = str(item).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return tuple(out)


def readonly_tool_names() -> list[str]:
    """当前环境下的只读工具名（即子 Agent 的那份白名单）。

    与 `build_readonly_registry()` 同源，因此永远不会出现写盘 / 执行类工具。
    `lsp` 走默认探测：没装语言服务器时清单里就没有 `lsp_diagnostics`。
    """
    from ..tools import build_readonly_registry

    return build_readonly_registry().names()


def validate_profile(
    profile: AgentProfile,
    *,
    existing: Mapping[str, AgentProfile],
    readonly_tools: Iterable[str],
) -> None:
    """落盘前校验。任何一条不过都抛 `AgentProfileError`，调用方据此 400。"""
    if not _ID_RE.match(profile.id or ""):
        raise AgentProfileError(
            "档案 id 非法：只允许字母、数字、下划线、连字符，长度 1–40"
        )

    name = profile.name
    if not name:
        raise AgentProfileError("档案名称不能为空")
    if len(name) > MAX_NAME_CHARS:
        raise AgentProfileError(f"档案名称最多 {MAX_NAME_CHARS} 个字符")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise AgentProfileError("档案名称不能包含控制字符")

    if len(profile.role) > MAX_ROLE_CHARS:
        raise AgentProfileError(f"角色描述最多 {MAX_ROLE_CHARS} 个字符")

    prompt_bytes = len(profile.system_prompt.encode("utf-8"))
    if prompt_bytes > MAX_PROMPT_BYTES:
        raise AgentProfileError(
            f"角色提示词最多 {MAX_PROMPT_BYTES // 1024} KB（当前 {prompt_bytes} 字节）"
        )

    allowed = set(readonly_tools)
    for tool in profile.tools:
        if tool not in allowed:
            raise AgentProfileError(
                f"工具 {tool!r} 不在只读白名单里。"
                "智能体只能读取工作区，不能写盘或执行命令。"
            )

    for other in existing.values():
        if other.id != profile.id and other.name == name:
            raise AgentProfileError(f"档案名称已存在：{name}")


__all__ = [
    "AgentProfile",
    "AgentProfileError",
    "MAX_FILE_BYTES",
    "MAX_NAME_CHARS",
    "MAX_PROMPT_BYTES",
    "MAX_ROLE_CHARS",
    "readonly_tool_names",
    "validate_profile",
]
