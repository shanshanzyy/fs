"""远程设备子系统（M10）。

把本机的 Agent 接到**用户自己的远端设备**上：直接读写远端代码库、
在远端跑测试与构建。执行面的编排仍在本机（模型、审批、审计、补丁引擎都不动），
变的只是「文件与命令打在哪台机器上」。

## 与「代码不出本机」红线的关系（必须在读代码前先看这段）

ADR-006 的原文是：**代码与提示词正文不上传治理面**。它约束的方向是
「本机 → 治理面」，目的是让私有化部署里的源码不被集中收集。

本子系统引入的是另一条链路：**本机 → 用户自己的设备**，
走的是用户既有凭据与既有网络路径。这**不是**把源码交给第三方，
而是「用户本来就要在那台机器上改代码」这件事的自动化。因此：

  - 治理面**不会**收到任何远端代码内容 —— 审计里只有计数、路径、哈希与耗时
    （见 tools/remote.py 各处理器的 `audit.record` 调用，字段刻意不含正文）。
  - 远端读回的内容一律 `wrap_untrusted` 包裹（远端代码可能含注入指令）。
  - 远端写回恒 REVIEW，且改动前内容会在本机留底（见 tools/remote.py 模块头）。

## 门控

    VIBE_REMOTE=1        启用整套能力（默认关；关着时工具列表逐字节不变）
    VIBE_REMOTE_CONFIG   清单路径覆盖（默认 <workspace>/.vibe/remote.json）
    VIBE_REMOTE_DEFAULT  默认连接名（清单里也有一份，环境变量优先）

每条连接另有 `allow_write` / `allow_shell` 两个能力位，默认都是关的。
"""

from .connection import HOST_KEY_POLICIES, RemoteConnection
from .errors import REMOTE_KINDS, RemoteConfigError, RemoteError
from .pathguard import RemotePathGuard
from .roots import PREFIX, RemoteRoot, RouteHit, is_ns_path, normalize_sub, validate_root
from .routing import ROUTED_TOOLS, RemoteRouter, routed_specs
from .session import CHANGE_ACTIONS, ChangeEntry, RemoteSessions
from .store import RemoteStore, default_store_path, store_path_for
from .transport import Capabilities, RemoteResult, SSHTransport
from .workspace import (
    RemoteEditResult,
    RemoteReadResult,
    RemoteWorkspace,
    close_all_transports,
    close_transport,
    open_workspace,
    transport_for,
)

__all__ = [
    "CHANGE_ACTIONS",
    "HOST_KEY_POLICIES",
    "PREFIX",
    "REMOTE_KINDS",
    "ROUTED_TOOLS",
    "Capabilities",
    "ChangeEntry",
    "RemoteConfigError",
    "RemoteConnection",
    "RemoteEditResult",
    "RemoteError",
    "RemotePathGuard",
    "RemoteReadResult",
    "RemoteResult",
    "RemoteRoot",
    "RemoteRouter",
    "RemoteSessions",
    "RemoteStore",
    "RemoteWorkspace",
    "RouteHit",
    "SSHTransport",
    "close_all_transports",
    "close_transport",
    "default_store_path",
    "is_ns_path",
    "normalize_sub",
    "open_workspace",
    "routed_specs",
    "store_path_for",
    "transport_for",
    "validate_root",
]
