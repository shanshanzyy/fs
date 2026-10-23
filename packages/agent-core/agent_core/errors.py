"""统一异常类型。所有异常都继承 AgentError，便于 CLI 层统一兜底。"""

from __future__ import annotations


class AgentError(Exception):
    """所有 agent-core 异常的基类。"""


class SandboxViolation(AgentError):
    """违反沙箱约束：路径越狱、命中敏感文件、越权访问等。

    `kind` / `path` 是给**审计**用的结构化信息。为什么不从异常消息里解析：
    消息是给人看的，措辞会改；审计字段不能跟着文案一起漂。
    `kind` 取值：escape / sensitive / policy_deny / audit_dir / root_write /
    malformed / workspace / reserved_ns（路径落在保留命名空间，由未路由的
    本地工具报出，见 M12 的远端透明路由）。
    """

    def __init__(self, message: str, *, kind: str = "", path: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.path = path


class CommandDenied(AgentError):
    """命令被安全策略拒绝，不允许执行。

    注意这里**没有**可读的结构化字段：命令沙箱的裁决带有 executable / risk，
    但它在闸门 1 就被 `_assess_command` 拦下并就地记了审计
    （`sandbox.deny_command`），处理器根本不会执行到这条例外。
    给一个永远不会被读到的字段，比没有更糟 —— 它会让人以为那条路是活的。
    """


class PatchError(AgentError):
    """结构化补丁无法应用（search 未命中 / 命中多处 / 文件不可解码）。"""


class ApprovalDenied(AgentError):
    """人工审批环节被拒绝。"""


class ToolError(AgentError):
    """工具执行过程中的可预期错误（如文件不存在）。"""


class LLMError(AgentError):
    """模型调用失败（网络、鉴权、响应格式异常）。"""


class ConfigError(AgentError):
    """配置缺失或非法。"""


class UpdateError(AgentError):
    """客户端自动更新链路失败（M9）。

    `kind` 是给**审计**用的结构化分类（消息措辞会改，审计字段不能跟着漂）：
    shape / signature / sha256 / size / network / state / staging / no_release。
    更新是「下载一个可执行文件并运行它」的动作，因此这里的失败必须能分类 ——
    「验签不过」和「网断了」在排查时是完全不同的两件事。
    """

    def __init__(self, message: str, *, kind: str = "unknown") -> None:
        super().__init__(message)
        self.kind = kind
