"""服务端治理面的测试包。

`support.py` 负责把 `apps/server` 与 `packages/agent-core` 都挂到 sys.path，
让同一个进程里能同时 import `vibe_server` 与 `agent_core` ——
「两端链式哈希一致」这类跨端不变量只能这样验证。
"""
