"""测试基线：把「跨会话记忆」默认关掉，让整套用例保持 hermetic。

记忆库默认落在 `~/.vibe/memories.db`（用户级，跨工作区）。但在 CI / 沙箱里
是不能写家目录的 —— 于是任何「默认建 Agent」的用例都会在初始化时抛
PermissionError，跟被测逻辑毫无关系。这里用 `setdefault` 把默认值改成关：
单个用例若要验证记忆，显式用 `Config.load(ws, memory_enabled=True, memory_db=<tmp>)`
打开即可（见 tests/test_memory.py）。

`setdefault` 而非直接赋值：外部若显式设了 `VIBE_MEMORY`（比如手动开记忆跑一两例），
尊重外部设置。
"""

from __future__ import annotations

import os

os.environ.setdefault("VIBE_MEMORY", "0")