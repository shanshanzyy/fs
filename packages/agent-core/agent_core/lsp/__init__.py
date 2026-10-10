"""LSP 集成（阶段 E）：本机语言服务器，按需启停、缺失即降级。

## 三条边界（ADR-014）

1. **仅本地。** 只 spawn 本机的语言服务器二进制，通过标准输入输出通信，
   不连接任何远端 —— 没有「远端语言服务器」这个模式，也不打算有。
2. **不常驻。** 按 `(工作区, 语言)` 缓存实例，空闲 `lsp_idle_seconds`
   自动 shutdown，进程退出时 `atexit` 兜底。界面开着几小时不等于进程要一直活着。
3. **零强制依赖。** 语言服务器是机器上的外部程序，`shutil.which` 探不到就
   返回原因并降级到「正则符号 + grep」。`pyproject.toml` 的 `dependencies`
   保持 `[]`（与 tree-sitter 同一范式）。

这一层只产出数据（诊断、定义位置、重命名的逐文件编辑），**绝不落盘**；
写盘一律回 `host/session.py` 走补丁引擎 + 检查点 + 审计（ADR-004/011）。
"""

from .client import LspClient, LspError
from .manager import DEFAULT_IDLE_SECONDS, LspManager, get_manager, set_manager
from .servers import (
    LANGUAGE_IDS,
    ServerInfo,
    any_available,
    detect_all,
    language_for,
    resolve,
)

__all__ = [
    "DEFAULT_IDLE_SECONDS",
    "LANGUAGE_IDS",
    "LspClient",
    "LspError",
    "LspManager",
    "ServerInfo",
    "any_available",
    "detect_all",
    "get_manager",
    "language_for",
    "resolve",
    "set_manager",
]