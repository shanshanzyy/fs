"""治理面客户端接入层（M2）。

这个包是 agent-core 与「服务端治理面」之间的**唯一接缝**。
M0/M1 的 agent-core 是纯本地工具：自己存配置、自己调模型、自己写日志。
接入治理面之后它多了三重身份约束：

1. **模型不再直连** —— 走网关，由服务端决定这次推理用云还是用本地；
2. **策略不再自定** —— 由服务端下发，客户端只能收紧不能放宽；
3. **日志不再只留本地** —— 元数据上报，服务端靠哈希链校验有没有被改动。

设计上的三条硬约束（对应 ADR-004 / ADR-006 / ADR-007）：

- 上传的只有**元数据**：事件类型、时间、计数、结果状态。
  代码内容、prompt 正文、模型输出**永远不出本机** —— 这不是"尽量不传"，
  是 `protocol.normalize_event()` 里逐字段白名单卡死的。
- 长期凭证**只存在服务端**。本机 `credentials.json` 里放的是短期访问令牌
  与轮换用的 refresh token，且是 0600 + 原子写。
- **敏感路径强制本地**。这条不受任何策略影响 —— 服务端如果下发
  `sensitiveForceLocal: false` 会直接拒收整份文档。

模块分工：

    protocol.py      链式哈希的规范化 —— 必须与服务端**逐字节一致**
    vocabulary.py    本地审计词汇 → 上传契约的翻译（改名 / 摘要 / 明确丢弃）
    credentials.py   凭证落盘（0600 / 原子写 / 不把令牌写进日志）
    client.py        与服务端通信（零依赖 urllib + 401 自动刷新）
    policy.py        策略收敛到 Config（只能收紧）+ 过期降级
    audit_upload.py  审计增量上报（水位线 + 链头校验 + 契约缺失即停）
    gateway_llm.py   走网关的模型客户端（网关挂了本地照常可用）

这里要特别说一句 `vocabulary.py`：本地审计日志与服务端接收契约
**不是同一张词汇表**（本地要给人排障、可以有正文；上传要合规、一个字正文都不能有）。
翻译只允许在那一层发生，别处不改名。

对外通常只用这几个入口：:

    from agent_core.governance import GovernanceClient, CredentialStore
    from agent_core.governance import PolicyCache, apply as apply_policy
    from agent_core.governance import AuditUploader, open_governed_client

`open_governed_client()` 是给 Agent 装配模型时用的：**没接入治理面就
原样返回本地客户端**，M0/M1 的行为一行都不会变。
"""

from __future__ import annotations

from . import protocol, vocabulary
from .audit_upload import AuditUploader, UploadResult, UploadState
from .client import (
    ApiResponse,
    CredentialsRejected,
    GovernanceClient,
    GovernanceError,
    NotEnrolled,
    QuotaExhausted,
    Unreachable,
)
from .credentials import CredentialStore, Credentials
from .gateway_llm import (
    GatewayLLMClient,
    GatewayUnavailable,
    GovernedClient,
    QuotaUnavailable,
    RouteInfo,
    TokenRejected,
    open_governed_client,
)
from .policy import DEFAULTS, PolicyCache, RuntimeConstraints
from .policy import apply as apply_policy
from .policy import normalize as normalize_policy

__version__ = "0.2.0"

__all__ = [
    # 协议（跨端一致性的地基）
    "protocol",
    # 词汇翻译（本地日志 → 上传契约）
    "vocabulary",
    # 通信
    "GovernanceClient",
    "ApiResponse",
    "GovernanceError",
    "NotEnrolled",
    "CredentialsRejected",
    "QuotaExhausted",
    "Unreachable",
    # 凭证
    "CredentialStore",
    "Credentials",
    # 策略
    "PolicyCache",
    "RuntimeConstraints",
    "apply_policy",
    "normalize_policy",
    "DEFAULTS",
    # 审计上报
    "AuditUploader",
    "UploadResult",
    "UploadState",
    # 模型客户端
    "GatewayLLMClient",
    "GovernedClient",
    "RouteInfo",
    "open_governed_client",
    "GatewayUnavailable",
    "QuotaUnavailable",
    "TokenRejected",
]
