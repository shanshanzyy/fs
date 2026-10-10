"""vibe-agent-core —— 本地化 Vibe Coding Agent 的核心引擎。

M0 阶段目标：用 CLI 打通「描述 → 规划 → 工具调用 → 生成补丁 → 人审 → 应用 → 验证」完整闭环。

设计红线（详见 docs/ARCHITECTURE.md ADR）：
    - ADR-003 不引入重型 Agent 框架，自研循环
    - ADR-004 编辑必须走结构化 patch，禁止整文件重写
    - ADR-008 高危操作强制人工审批，不可配置关闭
    - M0 零第三方依赖，保证离线/内网可直接运行
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
