"""测试基线：把「跨会话记忆」默认关掉，让整套用例保持 hermetic。

服务端测试里有几组会**直接构造 Agent**（`test_local_audit_wiring.py`），
走的是 `agent_core.config.Config.load` —— 它默认 `memory_enabled=True`，
于是初始化时就去建用户级记忆库 `~/.vibe/memories.db`。在 CI / 沙箱里家目录
不可写，这会在跟被测逻辑毫无关系的地方抛 PermissionError。

agent-core 侧靠自己的 `tests/conftest.py` 关掉这个默认；服务端是独立测试树，
拿不到那份 conftest，所以这里再设一遍。单个用例若要验证记忆，显式传
`memory_enabled=True, memory_db=<tmp>` 打开即可。

`setdefault` 而非直接赋值：外部若显式设了 `VIBE_MEMORY`（比如手动开记忆跑一两例），
尊重外部设置。
"""

from __future__ import annotations

import os

os.environ.setdefault("VIBE_MEMORY", "0")