"""远程设备子系统的异常类型。

为什么不塞进全局 `errors.py`：远程链路是可选子系统，它的失败分类
（连不上 / 认证失败 / 主机指纹不符 / 远端路径越狱 / 写回字节数不符）
与本地执行面是**两套语义**。混在一起会让「本机」与「远端」的故障在排查时
无法区分 —— 而这两者的处置方式完全不同：前者去看代码与沙箱，后者去看网络、
凭据与主机指纹。审计字段更不能跟着文案漂，所以 `kind` 单独定义在这里。
"""

from __future__ import annotations

from ..errors import AgentError

# kind 取值（审计与用户提示共用同一套，不要在别处再造一套字符串）：
#   config      连接配置非法（字段缺失 / 格式不符 / 落盘文件损坏）
#   transport   本地 ssh 客户端起不来、超时、被信号杀死
#   auth        认证失败（公钥未被远端接受 / 密钥不可用）
#   hostkey     主机指纹校验不通过（**可能是中间人**，必须显式让用户看到）
#   unreachable 网络不可达 / 域名解析失败 / 端口被拒
#   notfound    远端路径或可执行文件不存在
#   remote      远端命令以非零退出码结束（这是正常失败路径，不是缺陷）
#   integrity   写回远端后字节数校验不符（传输被截断或并发改写）
REMOTE_KINDS = (
    "config",
    "transport",
    "auth",
    "hostkey",
    "unreachable",
    "notfound",
    "remote",
    "integrity",
)


class RemoteError(AgentError):
    """远程链路通用失败。"""

    def __init__(self, message: str, *, kind: str = "remote", detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        # 给排查用的补充信息（远端 stderr 摘要、退出码等）。**不进模型上下文**，
        # 只进审计与界面 —— 远端 stderr 是不可信文本，直接喂模型等于开了注入面。
        self.detail = detail


class RemoteConfigError(RemoteError):
    """连接配置非法。单独成型是为了让 CLI 能精确区分「配置错」与「连不上」。"""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message, kind="config", detail=detail)
