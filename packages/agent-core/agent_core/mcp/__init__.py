"""MCP 客户端（阶段 L / P1-6）：把外部 MCP server 当**工具来源**接进来。

## 三条边界（ADR-015）

1. **只当客户端** —— 不提供 MCP 服务端；只管连别人的 server（stdio 为主，
   可选 HTTP）。本轮只做 `tools` 能力，resources / prompts 属增量。
2. **外部工具恒 REVIEW** —— 它们的副作用在 server 进程里，我们既看不见也管不住，
   因此每次调用都需要人工确认（见 registry.py 的说明）。
3. **零强制依赖** —— 手写 JSON-RPC，不引官方 `mcp` SDK；`dependencies` 仍为 `[]`。

默认**关**（`mcp_enabled=false`）：没配 server 时这一层完全不存在，
会话行为与今天逐字节一致。
"""

from .client import McpClient, McpError, McpHttpClient
from .manager import DEFAULT_TIMEOUT, McpManager, get_manager, set_manager
from .registry import (
    NAME_PREFIX,
    McpServerConfig,
    build_server_specs,
    load_servers,
    merge_mcp,
    sanitize_schema,
)

__all__ = [
    "DEFAULT_TIMEOUT",
    "NAME_PREFIX",
    "McpClient",
    "McpError",
    "McpHttpClient",
    "McpManager",
    "McpServerConfig",
    "build_server_specs",
    "get_manager",
    "load_servers",
    "merge_mcp",
    "sanitize_schema",
    "set_manager",
]