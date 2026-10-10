"""MCP 工具来源（L2）：把外部 server 的工具变成我们的 `ToolSpec`。

## 信任边界（ADR-015）

外部 MCP server 的工具**恒为 `Risk.REVIEW`** —— 不可配置放行。
理由：这些工具的副作用完全在 server 进程里，我们既看不见它写了什么、
也无法用 `PathGuard` / `CommandGuard` 约束它；它只是被「接进来」的一个
黑盒。因此唯一的兜底就是「每次调用都让人看一眼」（ADR-008：REVIEW 级
审批不可被配置关闭，会话内 `approve_all` 仍可显式放行）。

配套的三条硬约束：

1. **名字前缀隔离** —— 工具名形如 `mcp__<server>__<tool>`，与内置工具
   物理上不可能撞名；一旦真的撞了（比如两个 server 同名），**拒绝启动该
   server** 并给出原因，而不是覆盖。
2. **argv 只来自受信配置文件** —— `command`/`args` 从 `.vibe/mcp.json`
   读入，绝不接受模型或前端在运行时拼进来的命令。
3. **参数 schema 白名单化清洗** —— 只保留类型/属性/必填等结构键，
   丢弃 server 声明的可疑字段（`$ref`、`$defs`、任意额外键）。

结果一律 `wrap_untrusted` 包裹：server 返回的文本是**不可信数据**，
里面可能藏着「忽略以上要求，去做 X」这类注入指令。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..sandbox import Risk
from ..tools.base import ToolContext, ToolResult, ToolSpec, ToolPreview, wrap_untrusted
from .client import McpError

# 工具名前缀。`__` 而不是 `:` —— 有些模型的函数名解析器不认冒号。
NAME_PREFIX = "mcp__"

# 只保留这些 schema 结构键。其余（$ref / $defs / 供应商扩展）一律丢弃：
# 我们不做完整 JSON Schema 求值，留着只会误导模型。
_SCHEMA_KEYS = frozenset(
    {
        "type", "description", "properties", "required", "items", "enum",
        "default", "minimum", "maximum", "minItems", "maxItems",
    }
)


@dataclass(frozen=True)
class McpServerConfig:
    """一个外部 MCP server 的受信配置（来自本地 JSON 文件）。"""

    name: str
    transport: str = "stdio"          # stdio | http
    command: str = ""                 # stdio：可执行文件
    args: tuple[str, ...] = ()        # stdio：固定 argv（**不含用户运行时输入**）
    env: Mapping[str, str] = field(default_factory=dict)
    url: str = ""                     # http：JSON-RPC 端点

    def to_api(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            # 只回可执行文件名，不回完整 argv —— 界面要的是「接的是哪个 server」
            "command": Path(self.command).name if self.command else "",
            "url": self.url,
        }


def load_servers(path: str | Path) -> list[McpServerConfig]:
    """读 servers 配置文件。文件不存在返回 `[]`（= 没配 server，不是错误）。

    接受两种形态（都常见）：
      · Claude Desktop 风：`{"mcpServers": {"<name>": {...}}}`
      · 列表风：`{"servers": [{...}]}` 或顶层直接是数组 `[{...}]`

    缺 `command`（stdio）或缺 `url`（http）的条目**跳过**：一条坏配置不该
    拖垮其余 server。文件本身读不出/不是合法 JSON 才抛 `McpError`。
    """
    file_path = Path(path)
    if not file_path.is_file():
        return []
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise McpError(f"MCP 配置读取失败：{exc}") from exc

    entries: list[tuple[str, dict[str, Any]]] = []
    if isinstance(raw, list):
        entries = [("", item) for item in raw if isinstance(item, dict)]
    elif isinstance(raw, dict):
        for key in ("mcpServers", "servers"):
            value = raw.get(key)
            if isinstance(value, dict):
                entries.extend(
                    (str(name), item)
                    for name, item in value.items()
                    if isinstance(item, dict)
                )
            elif isinstance(value, list):
                entries.extend(("", item) for item in value if isinstance(item, dict))
            if entries:
                break
    else:
        raise McpError("MCP 配置的顶层必须是对象或数组")

    servers: list[McpServerConfig] = []
    for fallback_name, item in entries:
        server = _to_server(fallback_name, item)
        if server is not None:
            servers.append(server)
    return servers


def _to_server(fallback_name: str, item: dict[str, Any]) -> McpServerConfig | None:
    name = str(item.get("name") or fallback_name or "").strip()
    if not name:
        return None
    url = str(item.get("url") or item.get("httpUrl") or "").strip()
    command = str(item.get("command") or "").strip()
    transport = str(item.get("transport") or ("http" if url and not command else "stdio")).lower()
    if transport not in ("stdio", "http"):
        transport = "stdio"
    if transport == "stdio" and not command:
        return None
    if transport == "http" and not url:
        return None

    raw_args = item.get("args") or ()
    args = tuple(str(a) for a in raw_args) if isinstance(raw_args, (list, tuple)) else ()
    raw_env = item.get("env") or {}
    env = (
        {str(k): str(v) for k, v in raw_env.items()}
        if isinstance(raw_env, dict)
        else {}
    )
    return McpServerConfig(
        name=name, transport=transport, command=command, args=args, env=env, url=url
    )


# --------------------------------------------------------------------------- schema
def sanitize_schema(raw: Any) -> dict[str, Any]:
    """把 server 声明的 `inputSchema` 洗成我们校验器认得的形状。

    只保留结构键；缺 `type`/`properties` 时补一个空对象 schema。
    这样 `validate_arguments` 至少不会因为畸形声明而崩。
    """
    cleaned = _clean_schema(raw)
    if not isinstance(cleaned, dict):
        cleaned = {}
    if cleaned.get("type") != "object":
        cleaned["type"] = "object"
    props = cleaned.get("properties")
    cleaned["properties"] = props if isinstance(props, dict) else {}
    required = cleaned.get("required")
    cleaned["required"] = [str(x) for x in required] if isinstance(required, list) else []
    return cleaned


def _clean_schema(node: Any) -> Any:
    if isinstance(node, list):
        return [_clean_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key not in _SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {str(k): _clean_schema(v) for k, v in value.items()}
        elif key == "items":
            out[key] = _clean_schema(value)
        elif key in ("enum", "required"):
            out[key] = [_clean_schema(item) for item in value] if isinstance(value, list) else value
        else:
            out[key] = value
    return out


# --------------------------------------------------------------------------- 生成
def build_server_specs(
    server: McpServerConfig,
    tools: list[dict[str, Any]],
    manager: Any,
    taken: set[str],
) -> list[ToolSpec]:
    """把某 server 的工具列表变成 `ToolSpec` 列表。名字冲突即抛 `McpError`。

    **先算完全部名字再注册**：若不如此，前几个工具已经落进 registry、后一个
    才撞名，「拒绝启动该 server」就变成了「启动了一半」。
    """
    planned: list[tuple[str, str, dict[str, Any]]] = []
    seen: set[str] = set()
    for tool in tools:
        raw_name = str(tool.get("name") or "").strip()
        if not raw_name:
            continue
        full = f"{NAME_PREFIX}{server.name}__{raw_name}"
        if full in taken or full in seen:
            raise McpError(f"工具名冲突：{full}（拒绝启动 server {server.name}）")
        seen.add(full)
        planned.append((raw_name, full, tool))
    return [make_spec(server, raw, full, tool, manager) for raw, full, tool in planned]


def make_spec(
    server: McpServerConfig,
    raw_name: str,
    full_name: str,
    tool: dict[str, Any],
    manager: Any,
) -> ToolSpec:
    description = str(tool.get("description") or "").splitlines()
    title = description[0] if description else raw_name
    schema = sanitize_schema(tool.get("inputSchema"))

    def handler(ctx: ToolContext, args: dict) -> ToolResult:
        started = time.monotonic()
        args_text = json.dumps(args, ensure_ascii=False, sort_keys=True)
        try:
            result = manager.call(server.name, raw_name, args)
        except McpError as exc:
            ctx.audit.record(
                "mcp.call",
                server=server.name,
                tool=raw_name,
                ok=False,
                argc=len(args),
                args_text=args_text,
                ms=int((time.monotonic() - started) * 1000),
            )
            return ToolResult(
                ok=False,
                content=(
                    f"MCP 工具 {raw_name}（server={server.name}）调用失败：{exc}"
                ),
                meta={"server": server.name, "tool": raw_name},
            )

        is_error = bool(result.get("isError"))
        body = _render_result(result)
        ctx.audit.record(
            "mcp.call",
            server=server.name,
            tool=raw_name,
            ok=not is_error,
            argc=len(args),
            args_text=args_text,
            ms=int((time.monotonic() - started) * 1000),
        )
        content = wrap_untrusted(f"mcp:{server.name}/{raw_name}", body)
        if is_error:
            return ToolResult(
                ok=False,
                content=content,
                meta={"server": server.name, "tool": raw_name, "mcp_error": True},
            )
        return ToolResult(
            ok=True,
            content=content,
            meta={"server": server.name, "tool": raw_name},
        )

    def previewer(ctx: ToolContext, args: dict) -> ToolPreview:
        return ToolPreview(
            kind="json",
            body=json.dumps(
                {
                    "server": server.name,
                    "transport": server.transport,
                    "tool": raw_name,
                    "arguments": args,
                },
                ensure_ascii=False,
                indent=2,
            ),
            summary=f"外部 MCP 工具：{server.name} / {raw_name}",
        )

    return ToolSpec(
        name=full_name,
        description=(
            f"[MCP:{server.name}] {title}\n"
            f"来源：外部 MCP server（{server.name}）。**每次调用都需人工审批**。"
        ),
        parameters=schema,
        handler=handler,
        # ADR-015：外部工具恒 REVIEW，不可配置放行。
        risk=Risk.REVIEW,
        previewer=previewer,
    )


def _render_result(result: dict[str, Any]) -> str:
    """把 `tools/call` 的返回渲染成文本。兼容 content[] 与 structuredContent。"""
    parts: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
        elif item.get("type") == "resource":
            parts.append(str(item.get("resource") or ""))
        else:
            parts.append(json.dumps(item, ensure_ascii=False))
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    text = "\n".join(p for p in parts if p)
    if not text:
        return "（MCP server 返回了空结果）"
    return text


# --------------------------------------------------------------------------- 合并
def merge_mcp(
    registry: Any,
    servers: list[McpServerConfig],
    *,
    audit: Any = None,
    manager: Any = None,
) -> list[dict[str, Any]]:
    """把外部 server 的工具并入 `registry`，返回每个 server 的状态。

    **这是 MCP 工具进入工具清单的唯一入口** —— 与内置工具同一 registry、
    同一审批漏斗（`ToolRegistry.execute`）。某个 server 起不来只影响它自己：
    记一条原因，其余 server 与会话完全不受影响。
    """
    if manager is None:
        from .manager import get_manager

        manager = get_manager()
    manager.bind_audit(audit)

    taken = set(registry.names())
    status: list[dict[str, Any]] = []
    for server in servers:
        manager.attach(server)
        try:
            tools = manager.list_tools(server.name)
            specs = build_server_specs(server, tools, manager, taken)
        except McpError as exc:
            reason = str(exc)
            manager.refuse(server.name, reason)
            status.append(
                {"name": server.name, "available": False, "reason": reason, "tools": []}
            )
            continue
        except Exception as exc:  # noqa: BLE001 — 外部 server 的畸形返回不该崩会话
            reason = f"{type(exc).__name__}: {exc}"
            manager.refuse(server.name, reason)
            status.append(
                {"name": server.name, "available": False, "reason": reason, "tools": []}
            )
            continue
        for spec in specs:
            registry.register(spec)
            taken.add(spec.name)
        status.append(
            {
                "name": server.name,
                "available": True,
                "reason": "",
                "tools": [spec.name for spec in specs],
            }
        )
    return status


__all__ = [
    "NAME_PREFIX",
    "McpServerConfig",
    "build_server_specs",
    "load_servers",
    "make_spec",
    "merge_mcp",
    "sanitize_schema",
]