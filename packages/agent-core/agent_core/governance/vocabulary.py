"""本地审计词汇 → 上传契约的翻译层。

## 为什么需要这一层

agent-core 的本地审计日志（M0/M1）和服务端接收契约（M2）是两张各自
演化的词汇表。它们长得很像，但**不是同一个东西**：

| 维度 | 本地日志 | 上传契约 |
|---|---|---|
| 目的 | 给本机的人看、能排障 | 给服务端做统计与合规留痕 |
| 时间戳 | `"2026-09-24T10:00:00"` | epoch 整数 |
| 正文 | 可以有（本地不出机） | **一个字符都不许有** |
| 事件类型 | 23 个，包括 `/audit` 自检这类纯本地动作 | 在 `redaction._SCHEMA` 登记的那些 |
| 字段名 | `prompt_chars` / `hits` / `command` | `task_chars` / `hits` / `command_digest` |

直接把本地日志原样上传会怎样？服务端的键白名单会**逐条拒收**。
而这个拒绝的后果比看起来重：客户端是在把事件算进**上传链**之后才发出去的，
一旦被拒，那条事件就永久丢了 —— 本地水位线已推进，重传并不会补上它。
所以「服务端会兜底拒绝」不是安全网，是数据丢失的通道。

这一层做的就是：**在进入上传链之前**，把每条本地记录翻译成契约允许的形状；
翻译不了的，明确丢弃并给出理由。

## 三条设计约定

1. **改名只在这里发生。** 别处（`protocol.normalize_event`）只负责形状，
   不负责词汇。改名逻辑散开之后，「哪个字段去哪了」就没人能一眼答上来。
2. **规则表必须穷举本地事件。** 没登记的事件名一律不上传，而且
   `unclassified()` 会把它报出来。新增一个 `audit.record("...")` 调用点时，
   测试会失败并要求显式分类 —— 这是防止「审计覆盖悄悄变少」的唯一手段。
3. **契约由服务端给，客户端不抄一份。** `translate()` 拿服务端的字段表
   做最后一道过滤：规则表里写了一个服务端不认识的字段，它会被丢掉
   而不是发出去。这样服务端收紧契约时，客户端自动少传，不会突然全被拒。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping

# --------------------------------------------------------------------------- 变换
# 每个变换负责把「本地字段的值」转成「契约字段的值」。
# 返回 None = 这个字段这次没有值（比如 path 是 None），直接不发。
# 返回 None 不会报错：本地日志里 `path=result.meta.get("path")` 本来就可能是 None。


def _t_keep(value: Any) -> Any:
    return value


def _t_str(value: Any) -> str | None:
    """短文本：压成一行并截断。

    压成一行是必须的 —— 服务端「值不许含换行」这条闸门拒的是**整条事件**，
    而 `reason` 这类字段在多行报错信息里很容易带上换行。
    截断到 200 而不是服务端的 256：留出余量，也让「本地截断」这件事
    在两端都发生一次时不会刚好卡在边界上。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    one_line = " ".join(value.split())
    return one_line if len(one_line) <= 200 else one_line[:199] + "…"


def _t_path(value: Any) -> str | None:
    return _t_str(value)


def _t_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _t_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _t_digest(value: Any) -> str | None:
    """sha256 前 16 位十六进制 —— 与 `agent_core.audit.digest()` 同一算法。

    对齐到 16 位是有意的：服务端 `_DIGEST_RE` 要求 16–64 位 hex，
    而本地已经用 16 位算了一部分（`search_digest` 等）。
    两处算法必须一致，否则同一个值在本地与服务端算出不同指纹，
    「同一条命令在两台机器上都跑过」这类跨设备比对就失效了。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]


def _t_chars(value: Any) -> int | None:
    """字符数。注意是**字符**不是字节 —— 中文路径下两者差 3 倍。"""
    if value is None:
        return None
    if isinstance(value, str):
        return len(value)
    return None


def _t_basename(value: Any) -> str | None:
    """只取路径的最后一段。

    `session.start.workspace` 用它：审计要回答的是「在哪个仓库干活」，
    而完整本地路径会把用户名与目录结构一并送到服务端 ——
    那是「关于这台机器的信息」，不是「做了什么」，不属于 ADR-006 允许的范围。
    （注意：`tool.read_file.path` 那类是工作区**相对**路径，不受此影响。）
    """
    if value is None:
        return None
    text = str(value).strip().rstrip("/\\")
    if not text:
        return None
    return text.replace("\\", "/").rsplit("/", 1)[-1][:120] or None


TRANSFORMS = {
    "keep": _t_keep,
    "str": _t_str,
    "path": _t_path,
    "int": _t_int,
    "bool": _t_bool,
    "digest": _t_digest,
    "chars": _t_chars,
    "basename": _t_basename,
}


# --------------------------------------------------------------------------- 规则
@dataclass(frozen=True)
class Rule:
    """一条本地事件类型的上传规则。

    `upload` 为空表示**这条事件只留本机**。这不是「还没做」，
    是一个明确的决定：`agent.llm.error` 的全部内容就是一段报错原文，
    把它脱敏到只剩「发生过一次错误」之后，这条记录对服务端没有信息量，
    却要占一份存储与一条链位。不如留在本地。
    """

    upload: str
    fields: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    drop: Mapping[str, str] = field(default_factory=dict)
    note: str = ""


# 规则表。键 = 本地事件名（`audit.record("...")` 的第一个参数）。
# 顺序与 agent_core 的模块顺序一致，方便对照。
_RULES: dict[str, Rule] = {
    # ---------------------------------------------------------------- 会话级
    "session.start": Rule(
        upload="session.start",
        fields={
            # 只上传目录名，不上传完整本地路径 —— 见 _t_basename 的说明
            "workspace": ("workspace", "basename"),
            "model": ("model", "str"),
            "provider": ("provider", "str"),
        },
    ),
    "session.end": Rule(
        upload="session.end",
        fields={
            "reason": ("reason", "str"),
            "iterations": ("iterations", "int"),
            "duration_ms": ("duration_ms", "int"),
        },
    ),
    # ---------------------------------------------------------------- Agent 循环
    "agent.run.start": Rule(
        upload="agent.run.start",
        fields={
            # 本地叫 prompt_*，契约叫 task_*。改名而不是让服务端两套都收：
            # 一个概念两个名字，迟早会有人只改一边。
            "task_chars": ("prompt_chars", "int"),
            "task_digest": ("prompt_digest", "digest"),
        },
        note="任务正文只上传长度与指纹（ADR-006）",
    ),
    "agent.run.finish": Rule(
        upload="agent.run.finish",
        fields={
            "iterations": ("iterations", "int"),
            "tool_calls": ("tool_calls", "int"),
            "answer_chars": ("answer_chars", "int"),
            "answer_digest": ("answer_digest", "digest"),
        },
    ),
    "agent.run.interrupted": Rule(
        upload="agent.run.interrupted",
        fields={"iteration": ("iteration", "int"), "tool_calls": ("tool_calls", "int")},
    ),
    "agent.run.abort": Rule(
        upload="agent.run.abort",
        fields={"reason": ("reason", "str")},
    ),
    "agent.run.exhausted": Rule(
        upload="agent.run.exhausted",
        fields={"iterations": ("iterations", "int")},
    ),
    "agent.run.failed": Rule(
        upload="agent.run.failed",
        fields={"kind": ("kind", "str")},
        drop={
            "error": "报错原文（最长 300 字符）常引述文件内容或片段，"
            "按 ADR-006 只留本机；服务端只收 kind"
        },
    ),
    "agent.run.interrupt_requested": Rule(
        upload="",
        note="纯本地交互动作（用户按了中断），服务端无对应语义",
    ),
    "agent.llm.error": Rule(
        upload="",
        note="整条记录就是一段报错原文；脱敏到只剩「出过错」没有信息量",
    ),
    # ---------------------------------------------------------------- 审批闸门
    "approval.request": Rule(
        upload="approval.request",
        fields={
            "tool": ("tool", "str"),
            "risk": ("risk", "str"),
            "reason": ("reason", "str"),
        },
        note="reason 是工具自己写的有界评估理由（如「修改 src/main.py」、"
        "「命中高危模式：强制推送」），不是正文。只有决定没有请求的话，"
        "「多少请求压根没人处理」就统计不出来",
    ),
    "approval.decision": Rule(
        upload="approval.decision",
        fields={
            "tool": ("tool", "str"),
            "decision": ("decision", "str"),
            "reason": ("reason", "str"),
            "arguments_digest": ("arguments_digest", "digest"),
        },
        note="reason 是审批人写的理由（人写的短文本），可以上传；"
        "工具参数只上传指纹",
    ),
    "approval.denied_by_policy": Rule(
        upload="approval.denied_by_policy",
        fields={"tool": ("tool", "str"), "reason": ("reason", "str")},
    ),
    "approval.no_ui": Rule(
        upload="approval.no_ui",
        fields={"tool": ("tool", "str"), "decision": ("decision", "str")},
    ),
    "approval.timeout": Rule(
        upload="approval.timeout",
        fields={"tool": ("tool", "str"), "timeout_s": ("timeout_s", "int")},
    ),
    # 草稿预览（P1-2）：审批期内「看一眼改动后长什么样」，正文**不落盘**。
    # 要留痕的是「谁在什么时候打开过一份未落盘的草稿」，不是草稿里写了什么
    # —— 后者就在审批 diff 里（ADR-006），所以 token 与正文一律只留指纹。
    "preview.draft": Rule(
        upload="preview.draft",
        fields={
            "path": ("path", "path"),
            "token": ("token", "digest"),
            "files": ("files", "int"),
            "chars": ("chars", "int"),
        },
        note="草稿预览是**只读且不落盘**的渲染：记下渲染的主文档、涉及几个文件、"
        "共多少字符。token 是客户端生成的一次性短标识，只上传指纹",
    ),
    # 真实应用预览（P2 / ADR-039）：host 只**登记**用户自己起的本机 dev server，
    # 不起进程、也不转发字节。要留痕的是「连了哪个本机端口」，路径与内容不入库。
    "preview.external": Rule(
        upload="preview.external",
        fields={"port": ("port", "int"), "host": ("host", "str")},
        note="记「预览指向了本机哪个端口」。只登记不转发，"
        "URL 路径与页面内容都不上传（ADR-006）",
    ),
    # 远端预览转发（P3 / ADR-040）：状态机每次跃迁落一条，只记连接名与两个端口。
    # `reason` 是传输层的短类型标志（transport / timeout），不是报错正文。
    "preview.tunnel": Rule(
        upload="preview.tunnel",
        fields={
            "action": ("action", "str"),
            "connection": ("connection", "str"),
            "remote_port": ("remote_port", "int"),
            "local_port": ("local_port", "int"),
            "ok": ("ok", "bool"),
            "ms": ("ms", "int"),
            "reason": ("reason", "str"),
            "closed": ("closed", "bool"),
            "refs": ("refs", "int"),
        },
        note="远端环回端口转发的开 / 关 / 重连 / 恢复事件：只记连接名与两个端口号，"
        "**被转发的字节一个字都不上传**（ADR-006）",
    ),
    # ---------------------------------------------------------------- 工具调用
    "tool.execute": Rule(
        upload="tool.execute",
        fields={
            "tool": ("tool", "str"),
            "risk": ("risk", "str"),
            "elapsed_ms": ("elapsed_ms", "int"),
            "output_chars": ("output_chars", "int"),
            "path": ("path", "path"),
            "ok": ("ok", "bool"),
        },
        note="output_chars 是工具返回内容的长度，不是内容本身",
    ),
    "tool.unknown": Rule(upload="tool.unknown", fields={"tool": ("tool", "str")}),
    "tool.error": Rule(
        upload="tool.error",
        fields={
            "tool": ("tool", "str"),
            "kind": ("kind", "str"),
            "detail_chars": ("detail_chars", "int"),
        },
        note="工具**内部异常**（不是「跑了没成功」）。只上传异常类型名与消息长度；"
        "消息本身常含路径与参数，留在本机。没有这条的话，"
        "「哪个工具在崩」在审计里永远看不见",
    ),
    "tool.preflight_failed": Rule(
        upload="tool.preflight_failed",
        fields={"tool": ("tool", "str"), "reason_digest": ("reason", "digest")},
        drop={
            "reason": "改动预检的报错原文会引述目标片段（比如「未找到该 search 片段」"
            "后面跟着片段），只上传指纹"
        },
    ),
    "tool.read_file": Rule(
        upload="tool.read_file",
        fields={
            "path": ("path", "path"),
            "chars": ("chars", "int"),
            "lines": ("lines", "str"),  # 本地形状是 "1-50"，不是整数
        },
        note="只记读了哪个文件、多少字符、哪一段行号，不记内容",
    ),
    "tool.list_dir": Rule(
        upload="tool.list_dir",
        fields={
            "path": ("path", "path"),
            "depth": ("depth", "int"),
            "entries": ("entries", "int"),
        },
    ),
    "tool.grep": Rule(
        upload="tool.grep",
        fields={
            "path": ("path", "path"),
            "hits": ("hits", "int"),
            "pattern_digest": ("pattern", "digest"),
            "pattern_chars": ("pattern", "chars"),
        },
        drop={
            "pattern": "正则原文只留本机。上传指纹是为了「两台机器上跑的是不是同一条」"
            "这类跨设备比对；注意短正则会话的指纹可被字典猜出，所以它只能用来做"
            "相等性比对，不能当作「正则内容不可知」的保证"
        },
    ),
    "tool.find_files": Rule(
        upload="tool.find_files",
        fields={
            "hits": ("hits", "int"),
            "pattern_digest": ("pattern", "digest"),
            "pattern_chars": ("pattern", "chars"),
        },
        drop={"pattern": "同 tool.grep：只上传指纹与长度"},
    ),
    "tool.repo_map": Rule(
        upload="tool.repo_map",
        fields={
            "path": ("path", "path"),
            "files": ("files", "int"),
            "symbols": ("symbols", "int"),
            "truncated": ("truncated", "bool"),
        },
    ),
    "tool.semantic_search": Rule(
        upload="tool.semantic_search",
        fields={
            "path": ("path", "path"),
            "hits": ("hits", "int"),
            "backend": ("backend", "str"),
            "query_digest": ("query", "digest"),
            "query_chars": ("query", "chars"),
        },
        drop={
            "query": "与 tool.grep 同一约定：自然语言查询原文只留本机。"
            "query 里常包含业务名词（客户、订单表名），本身就是敏感信息"
        },
    ),
    # 增量索引（J/P1-9）：只报「这次追平动了几个文件、索引现在多大、花了多久」。
    # 全是计数，没有任何路径与内容 —— 上传它回答的是「索引是否在正常工作」。
    "index.rebuild": Rule(
        upload="index.rebuild",
        fields={
            "added": ("added", "int"),
            "changed": ("changed", "int"),
            "removed": ("removed", "int"),
            "files_count": ("files_count", "int"),
            "ms": ("ms", "int"),
        },
        note="watch 增量更新索引后的计数汇总；ms 用来发现「某个仓库让索引抖动」",
    ),
    "tool.git_diff": Rule(
        upload="tool.git_diff",
        fields={
            "path": ("path", "path"),
            "mode": ("mode", "str"),
            "ref": ("ref", "str"),
            "staged": ("staged", "bool"),
            "files": ("files", "int"),
            "added": ("added", "int"),
            "removed": ("removed", "int"),
            "diff_digest": ("diff", "digest"),
            "diff_chars": ("diff", "chars"),
        },
        drop={
            "diff": "diff 原文整段都是代码，按 ADR-006 只上传指纹与长度 —— "
            "上传它等于把工作区的源码送到服务端"
        },
        note="回答「有没有人在提交前先看一眼自己的改动」；ref/staged 用来区分看的是哪一份",
    ),
    "git.commit_message": Rule(
        upload="git.commit_message",
        fields={
            "branch": ("branch", "str"),
            "files": ("files", "int"),
            "added": ("added", "int"),
            "removed": ("removed", "int"),
            "diff_digest": ("diff", "digest"),
            "diff_chars": ("diff", "chars"),
            "message_chars": ("message_chars", "int"),
        },
        drop={
            "diff": "同 tool.git_diff：只上传指纹与长度",
            "message": "模型生成的提交信息正文会复述改动细节，只上传长度",
        },
        note="只记「生成过一条提交信息、覆盖多少改动」。**提交本身不在这里发生**，"
        "所以这类事件不构成「谁提交了代码」的证据",
    ),
    # Git 面板动作（M7 / B4）：全部是「用户本人在面板上点」的写操作（ADR-028），
    # 与 Agent 通道隔离。批量路径只上传「条数 + 指纹」（路径可能含业务名词）；
    # 提交信息与远端报错文本在本地存原文、上传只留指纹/长度（ADR-006）。
    "git.stage": Rule(
        upload="git.stage",
        fields={"count": ("count", "int"), "paths_digest": ("paths_digest", "digest")},
        note="暂存了几个文件；路径只留条数与指纹",
    ),
    "git.unstage": Rule(
        upload="git.unstage",
        fields={"count": ("count", "int"), "paths_digest": ("paths_digest", "digest")},
        note="取消暂存了几个文件；口径同 git.stage",
    ),
    "git.discard": Rule(
        upload="git.discard",
        fields={
            "count": ("count", "int"),
            "paths_digest": ("paths_digest", "digest"),
            "checkpoint_id": ("checkpoint_id", "str"),
        },
        note="放弃了几个文件的改动；checkpoint_id 指向可回滚找回的检查点",
    ),
    "rollback.truncate": Rule(
        upload="rollback.truncate",
        fields={
            "count": ("count", "int"),
            "first_seq": ("first_seq", "int"),
            "last_seq": ("last_seq", "int"),
            "checkpoint_id": ("checkpoint_id", "str"),
            "restored": ("restored", "int"),
            "deleted": ("deleted", "int"),
        },
        note="逐条回退删掉了几条消息（首末 seq）、对应哪个检查点、恢复了/删除了几个文件 "
        "—— 消息正文与文件正文都不出机（ADR-006）",
    ),
    "git.commit": Rule(
        upload="git.commit",
        fields={
            "message_digest": ("message", "digest"),
            "message_chars": ("message", "chars"),
            "oid": ("oid", "str"),
        },
        drop={"message": "提交信息由用户手打，可能复述改动细节，只上传指纹与长度"},
        note="**这才是真正的提交动作**（git.commit_message 只是生成文本）；"
        "oid 是提交后的短哈希",
    ),
    "git.push": Rule(
        upload="git.push",
        fields={
            "remote": ("remote", "str"),
            "branch": ("branch", "str"),
            "ok": ("ok", "bool"),
            "reason_digest": ("reason", "digest"),
            "reason_chars": ("reason", "chars"),
        },
        drop={"reason": "远端报错文本可能引述带凭据的远端 URL，只上传指纹与长度"},
        note="推送当前分支；结果与报错长度用来发现「推不上去」的环境问题",
    ),
    "git.pull": Rule(
        upload="git.pull",
        fields={
            "remote": ("remote", "str"),
            "branch": ("branch", "str"),
            "ok": ("ok", "bool"),
            "reason_digest": ("reason", "digest"),
            "reason_chars": ("reason", "chars"),
        },
        drop={"reason": "同 git.push：只上传指纹与长度"},
        note="快进拉取当前分支；非快进不会产生任何改动",
    ),
    "git.branch": Rule(
        upload="git.branch",
        fields={"name": ("name", "str"), "action": ("action", "str")},
        note="新建 / 切换分支；name 经 refname 白名单校验，不含内容敏感信息",
    ),
    "tool.edit_file": Rule(
        upload="tool.edit_file",
        fields={
            "path": ("path", "path"),
            "strategy": ("strategy", "str"),
            "added": ("added", "int"),
            "removed": ("removed", "int"),
            "search_digest": ("search_digest", "digest"),
            "replace_digest": ("replace_digest", "digest"),
            "search_lines": ("search_lines", "int"),
        },
        note="改动前后的片段在本地已算成 digest（见 tools/editing.py），直接沿用",
    ),
    "tool.write_file": Rule(
        upload="tool.write_file",
        fields={
            "path": ("path", "path"),
            "mode": ("mode", "str"),
            "added": ("added", "int"),
            "removed": ("removed", "int"),
            "bytes": ("bytes", "int"),
            "content_digest": ("content_digest", "digest"),
        },
    ),
    # ---------------------------------------------------------------- 远端设备 tool 面（M10/M11）
    # 远端 Agent 工具的动作事件**只判本地**（upload=""）。
    # 理由有三条，任一条都足以让它们不出机：
    #   1. 记录里带 `connection`（用户内网主机名/别名）与**远端相对路径** ——
    #      那是「关于用户基础设施的信息」，不属于 ADR-006 允许的「做了什么」；
    #   2. `tool.remote_edit` 记录 `backup`（本机留底的**绝对路径**），
    #      正是 ADR-006 明确禁止外发的那类本机路径信息；
    #   3. 面向用户/界面的远端审计面由 M11 的 `remote.connection` / `remote.file`
    #      承载（见下文），这两条才是设计过的上传口径。
    # 这里显式登记而不是"忘了登记"：未登记会让 `unclassified()` 把真缺陷信号淹掉。
    "tool.remote_probe": Rule(
        upload="",
        note="远端连通性探测。记录含连接名与远端主机信息，只留本机（ADR-006）",
    ),
    "tool.remote_read_file": Rule(
        upload="",
        note="远端读文件。记录含连接名与远端路径，只留本机（ADR-006）",
    ),
    "tool.remote_list_dir": Rule(
        upload="",
        note="远端列目录。记录含连接名与远端路径，只留本机（ADR-006）",
    ),
    "tool.remote_grep": Rule(
        upload="",
        note="远端检索。命中数与模式长度已足够本机排查，连接名/远端路径不出机（ADR-006）",
    ),
    "tool.remote_edit": Rule(
        upload="",
        note="远端结构化编辑。记录含连接名、远端路径与本机留底**绝对路径**，只留本机（ADR-006）",
    ),
    "tool.remote_write_file": Rule(
        upload="",
        note="远端整文件写。记录含连接名与远端路径，只留本机（ADR-006）",
    ),
    "tool.remote_run_command": Rule(
        upload="",
        note="远端命令执行。记录含连接名与可执行名，只留本机（ADR-006）",
    ),
    "tool.remote_review_changes": Rule(
        upload="",
        note="远端改动自审。记录含连接名与本机留底目录是否存在，只留本机（ADR-006）",
    ),
    # ---------------------------------------------------------------- 远端设备 file 面（M11）
    # 与上面 8 条 tool.* 不同，这两条是**设计过的上传口径**：
    # 它们面向的是「用户本人经界面做的一次远端操作」——回答「在哪台设备上、
    # 对哪个文件、做了什么、多少字节、增删多少行」，**一个字符的正文都没有**。
    # 连接名在这里是可上传的（它标识归属，与 `task_id` / `job_id` 同性质），
    # 远端相对路径也可上传（路径本身不含内容，ADR-006 允许路径与计数）。
    # 注意这两条与 `tool.remote_*` 是**两套面**：前者面向用户审计，后者是 Agent 动作。
    "remote.connection": Rule(
        upload="remote.connection",
        fields={
            "connection": ("connection", "str"),
            "action": ("action", "str"),
            "allow_write": ("allow_write", "bool"),
            "allow_shell": ("allow_shell", "bool"),
            "ok": ("ok", "bool"),
            "ms": ("ms", "int"),
        },
        note="用户本人对远程连接做的一次管理动作。只记归属、动作枚举"
        "（added/updated/removed/default/permissions/open/test、"
        "以及 M12 的「加入工作区」root_add / 「移出工作区」root_remove）、"
        "两个权限位、成功与否与耗时 —— host / 密钥路径等基础设施信息不出机（ADR-006）"
        "。M12 的根动作**复用本条**（`action` 两处均为 `str`、不做枚举），"
        "不新增事件、不动 `fields`。",
    ),
    "remote.file": Rule(
        upload="remote.file",
        fields={
            "connection": ("connection", "str"),
            "path": ("path", "path"),
            "action": ("action", "str"),
            "bytes": ("bytes", "int"),
            "sha256": ("sha256", "digest"),
            "added": ("added", "int"),
            "removed": ("removed", "int"),
            "backed_up": ("backed_up", "bool"),
        },
        note="用户本人对远端文件做的一次读/写动作。只记归属、相对路径、"
        "动作枚举（list/read/save/create）、字节数、内容 sha256 与增删行数 —— "
        "**文件正文与 diff 正文只走端点响应与界面，一个字符都不出机**（ADR-006）",
    ),
    "remote.rollback": Rule(
        upload="remote.rollback",
        fields={
            "connection": ("connection", "str"),
            "path": ("path", "path"),
            "action": ("action", "str"),
            "bytes": ("bytes", "int"),
            "backed_up": ("backed_up", "bool"),
        },
        note="按轮次回退远端文件：哪个连接、哪个相对路径、动作枚举"
        "（restore/delete/skip）、多少字节、是否留下回退前副本 —— "
        "**文件正文与 diff 正文都不出机**（ADR-006）",
    ),
    # Repo Wiki（M3+）：只报「往哪写、写了几个文件、多少字节、有没有被截断」。
    # Wiki 正文由本机代码结构生成，按 ADR-006 一律不出机 —— 上传它回答的是
    # 「Wiki 生成是否正常工作」，不是「仓库里写了什么」。
    "tool.repo_wiki": Rule(
        upload="tool.repo_wiki",
        fields={
            "path": ("path", "path"),
            "files": ("files", "int"),
            "created": ("created", "int"),
            "updated": ("updated", "int"),
            "bytes": ("bytes", "int"),
            "truncated": ("truncated", "bool"),
        },
        note="落点是工作区相对目录；正文（markdown）只留本机，不上传",
    ),
    # 联网检索（M7）：查询词与抓取正文都是**外发/外部内容**。
    # query 只上传 digest+长度（可能含代码片段，ADR-006）；fetch 的 URL 只上传
    # host+path，query string 换 digest（URL 参数里可能有 token —— 与 host 静态壳
    # 的 Referer 泄露防线同一考虑）；抓取的正文只上传内容指纹。
    # 报错文本一律 error_digest+error_chars —— 异常消息会引述 URL/查询词/响应片段，
    # 与 sandbox.* / verify.run 的机器报错同一处理（服务端 DENIED_KEYS 钉死了
    # error 键，白名单列了也过不了闸门 3，原文键名上传必被整条拒收）。
    "tool.web_search": Rule(
        upload="tool.web_search",
        fields={
            "provider": ("provider", "str"),
            "query_chars": ("query_chars", "int"),
            "query_digest": ("query_digest", "digest"),
            "max_results": ("max_results", "int"),
            "result_count": ("result_count", "int"),
            "error_digest": ("error_digest", "digest"),
            "error_chars": ("error_chars", "int"),
        },
        drop={
            "query": "查询词会外发给搜索上游且常含业务名词/代码片段，"
            "按 ADR-006 只留本机，上传指纹与长度",
            "error": "异常消息会引述 URL 与响应片段，按 ADR-006 只留本机，"
            "上传指纹与长度",
        },
        note="一次联网搜索的元数据",
    ),
    "tool.web_fetch": Rule(
        upload="tool.web_fetch",
        fields={
            "host": ("host", "str"),
            "path": ("path", "path"),
            "query_digest": ("query_digest", "digest"),
            "status": ("status", "int"),
            "bytes": ("bytes", "int"),
            "content_digest": ("content_digest", "digest"),
            "truncated": ("truncated", "bool"),
            "error_digest": ("error_digest", "digest"),
            "error_chars": ("error_chars", "int"),
        },
        drop={
            "url": "URL 的 query string 里可能有 token/敏感参数，"
            "只上传 host+path，query string 换 digest",
            "error": "异常消息会引述 URL 与响应片段，按 ADR-006 只留本机，"
            "上传指纹与长度",
        },
        note="一次网页抓取的元数据。抓取正文恒 wrap_untrusted 进模型上下文，"
        "不出本机，这里只上传指纹与计数",
    ),
    # ---------------------------------------------------------------- 本地编辑面（D）
    # 用户在编辑器里直接写代码（补全 / 行内改写 / 保存）也算「代码是怎么进仓库的」，
    # 所以照旧上传 —— 但只上传指纹与长度：补全建议、改写后的选区正文一律不出机（ADR-006）。
    "edit.complete": Rule(
        upload="edit.complete",
        fields={
            "path": ("path", "path"),
            "chars": ("chars", "int"),
            "backend": ("backend", "str"),
            "prefix_digest": ("prefix_digest", "digest"),
            "suffix_digest": ("suffix_digest", "digest"),
        },
        note="backend 区分 fim（模型原生补全）与 chat（回退），用来回答"
        "「这台机器上的 FIM 到底可不可用」；prefix/suffix 是光标两侧的上下文，只留指纹",
    ),
    "edit.rewrite.preview": Rule(
        upload="edit.rewrite.preview",
        fields={
            "path": ("path", "path"),
            "start": ("start", "int"),
            "end": ("end", "int"),
            "hunks": ("hunks", "int"),
            "selection_digest": ("selection_digest", "digest"),
            "chars": ("chars", "int"),
        },
        note="**只算不落盘**的预览：记下改写了哪几行、拆出几个 hunk。"
        "改写正文与选区正文都只留指纹与长度",
    ),
    "edit.rewrite.apply": Rule(
        upload="edit.rewrite.apply",
        fields={
            "path": ("path", "path"),
            "strategy": ("strategy", "str"),
            "applied": ("applied", "int"),
            "total": ("total", "int"),
            "added": ("added", "int"),
            "removed": ("removed", "int"),
        },
        note="逐 hunk 接受的结果：applied/total 直接回答「模型提了几块、人接受了几块」。"
        "strategy 恒为 hunk-subset（ADR-004 的受控例外，边界见 ADR-011）",
    ),
    "edit.save": Rule(
        upload="edit.save",
        fields={
            "path": ("path", "path"),
            "strategy": ("strategy", "str"),
            "added": ("added", "int"),
            "removed": ("removed", "int"),
            "chars": ("chars", "int"),
        },
        drop={"content": "保存的正文就是源码，按 ADR-006 只上传长度与增删行数"},
    ),
    # 多文件原子事务（P1-8）：一次把多个文件「同生共死」地落盘。
    # 只上传计数与结果标志 —— 各文件的 diff 正文只进审批预览，不进审计（ADR-006）。
    "edit.transaction": Rule(
        upload="edit.transaction",
        fields={
            "files_count": ("files_count", "int"),
            "changed_count": ("changed_count", "int"),
            "added": ("added", "int"),
            "removed": ("removed", "int"),
            "strategy": ("strategy", "str"),
            "rolled_back": ("rolled_back", "bool"),
        },
        note="一次事务涉及几个文件、实际改了几个、增删多少行、是否整批回滚。"
        "rolled_back=true 是「这次改动一个字都没留下」的信号，"
        "与 changed_count=0 一起用来发现「多文件改动频繁失败」",
    ),
    # ---------------------------------------------------------------- 语言服务器（E）
    # 语言服务器是「第三只眼」：诊断、定义跳转纯只读；**重命名会落盘**，
    # 所以照旧记一条 —— 但新名称正文一律不出机（ADR-006），只留指纹与长度。
    "tool.lsp_diagnostics": Rule(
        upload="tool.lsp_diagnostics",
        fields={
            "path": ("path", "path"),
            "diagnostics": ("diagnostics", "int"),
            "error_count": ("error_count", "int"),
            "warning_count": ("warning_count", "int"),
            "server": ("server", "str"),
            "truncated": ("truncated", "bool"),
        },
        note="记「看过一次静态诊断、报了几处」。诊断**正文**（引述源码与符号名）"
        "只进模型上下文，不在这里",
    ),
    "lsp.rename.preview": Rule(
        upload="lsp.rename.preview",
        fields={
            "path": ("path", "path"),
            "line": ("line", "int"),
            "char": ("char", "int"),
            "files": ("files", "int"),
            "edits": ("edits", "int"),
            "new_name_digest": ("new_name_digest", "digest"),
            "new_name_chars": ("new_name_chars", "int"),
        },
        note="**只算不落盘**的预览：人打算改哪个符号、跨几个文件。"
        "新标识符名本地已只存指纹与长度（见 host/session.py）",
    ),
    "lsp.rename.apply": Rule(
        upload="lsp.rename.apply",
        fields={
            "path": ("path", "path"),
            "edits": ("edits", "int"),
            "added": ("added", "int"),
            "removed": ("removed", "int"),
            "strategy": ("strategy", "str"),
        },
        note="逐文件落盘的结果。重命名是**用户本人的动作**，不走审批浮层，"
        "但落盘前收检查点、记审计；strategy 恒为 lsp-rename",
    ),
    # ---------------------------------------------------------------- MCP 客户端（L）
    # 外部 MCP server 是别人的进程（ADR-015）。这里回答两件事：
    # 「接了哪些 server、它们各自声明了几个工具」，以及
    # 「哪个 server 的哪个工具被调了多少次、成没成」。
    # **参数正文与返回正文一律不出机**（ADR-006）：参数只上传指纹与个数。
    "mcp.server_start": Rule(
        upload="mcp.server_start",
        fields={
            "server": ("server", "str"),
            "transport": ("transport", "str"),
            "ok": ("ok", "bool"),
            "tools": ("tools", "int"),
            "ms": ("ms", "int"),
        },
        note="外部 MCP server 每次（含重启）启动的结果。tools 是它声明的工具数；"
        "只有 server 名与计数，不含任何参数或返回正文",
    ),
    "mcp.call": Rule(
        upload="mcp.call",
        fields={
            "server": ("server", "str"),
            "tool": ("tool", "str"),
            "ok": ("ok", "bool"),
            "argc": ("argc", "int"),
            "args_digest": ("args_text", "digest"),
            "ms": ("ms", "int"),
        },
        drop={
            "args_text": "外部工具的调用参数正文只留本机，只上传指纹（ADR-006）"
        },
        note="一次外部 MCP 工具调用。**外部工具恒 REVIEW**（ADR-015），"
        "这里回答「哪个 server 的哪个工具被调了多少次、成没成」",
    ),
    "mcp.server_added": Rule(
        upload="mcp.server_added",
        fields={
            "server": ("server", "str"),
            "transport": ("transport", "str"),
            "argc": ("argc", "int"),
        },
        note="用户在界面里新增 / 替换了一个外部 MCP server 的配置。"
        "只上传 server 名、传输方式与参数个数（ADR-006）；**命令与参数正文不出机**，"
        "写入只落本地配置文件",
    ),
    "mcp.server_removed": Rule(
        upload="mcp.server_removed",
        fields={
            "server": ("server", "str"),
            "transport": ("transport", "str"),
            "argc": ("argc", "int"),
        },
        note="用户在界面里删除了一个外部 MCP server 的配置。字段口径同 mcp.server_added",
    ),
    "plan.toggle": Rule(
        upload="plan.toggle",
        fields={
            "id": ("id", "str"),
            "status": ("status", "str"),
            "steps_count": ("steps_count", "int"),
            "revision": ("revision", "int"),
        },
        note="人手动标注计划里某一步的状态（「这步我自己做掉了」）。"
        "计划**正文**是模型对任务的自然语言描述、可能整段引述代码，只留本机；"
        "这里只上传短标识、状态枚举与条数（ADR-006）",
    ),
    # 多 Agent 编排（M3）：父 Agent 派生一次**只读**子 Agent 做探索/分析/检索。
    # 子 Agent 的**任务正文与摘要正文一律不出机**（ADR-006）—— 摘要可能整段
    # 引述代码；这里只回答「委派了几轮、调了几个只读工具、有没有成、摘要多长」。
    "subagent.run": Rule(
        upload="subagent.run",
        fields={
            "ok": ("ok", "bool"),
            "iterations": ("iterations", "int"),
            "tool_calls": ("tool_calls", "int"),
            "answer_chars": ("answer_chars", "int"),
            "task_digest": ("task_digest", "digest"),
        },
        note="一次「父 Agent 派生只读子 Agent」。子 Agent 只读、不写盘、不递归；"
        "任务正文与摘要正文只留本机，这里只上传计数、成败与任务指纹",
    ),
    # 测试树（K/P2-14）：用户本人点「跑测试」后的计数汇总。**不记失败正文** ——
    # 断言信息、traceback 会引述被测代码与数据，只留本机（ADR-006）；
    # 首个失败的 path/line 属位置元数据，用来回答「哪一块最容易挂」。
    "test.run": Rule(
        upload="test.run",
        fields={
            "scope": ("scope", "str"),
            "requested": ("requested", "int"),
            "passed": ("passed", "int"),
            "failed": ("failed", "int"),
            "skipped": ("skipped", "int"),
            "ms": ("ms", "int"),
            "file": ("file", "path"),
            "line": ("line", "int"),
        },
        note="IDE 内测试树的运行汇总。scope=all 表示跑全部、subset 表示只跑勾选的用例；"
        "file/line 是**首个**失败的位置（没有失败则空/0）",
    ),
    # 交互式终端（M/P1-7）：用户本人开的 PTY。**只记 open/close**（逐次键入会爆量，
    # 不在本词汇表里）；命令正文在本地算成 argv_digest，只上传指纹（ADR-006）。
    "term.open": Rule(
        upload="term.open",
        fields={
            "termId": ("termId", "str"),
            "argc": ("argc", "int"),
            "argv_digest": ("argv_digest", "digest"),
            "cols": ("cols", "int"),
            "rows": ("rows", "int"),
            "mode": ("mode", "str"),
            "remote": ("remote", "str"),
            "ms": ("ms", "int"),
        },
        note="起一个交互式终端。命令正文（用户 shell 的 argv）只留本机，"
        "上传的是大小/尺寸/后端模式（pty 或降级 pipe）、远程连接名与耗时",
    ),
    "term.close": Rule(
        upload="term.close",
        fields={
            "termId": ("termId", "str"),
            "argc": ("argc", "int"),
            "argv_digest": ("argv_digest", "digest"),
            "cols": ("cols", "int"),
            "rows": ("rows", "int"),
            "mode": ("mode", "str"),
            "exit_code": ("exit_code", "int"),
            "ms": ("ms", "int"),
        },
        note="关闭一个交互式终端。exit_code 回答「用户是正常退出还是被收掉」；"
        "**逐次键入内容不记**（会爆量且属正文）",
    ),
    # 多模态图片上传（N/P1-10）：用户本人贴的图。**只记元数据**（ADR-006）：
    # 字节数、扩展名、内容摘要；图片正文只落本机 .vibe/images/，不出本机。
    "image.upload": Rule(
        upload="image.upload",
        fields={
            "bytes": ("bytes", "int"),
            "ext": ("ext", "str"),
            "digest": ("digest", "digest"),
        },
        note="上传一张图片作为多模态输入。只记字节数 / 扩展名 / 内容指纹，"
        "**图片正文不出本机**（仅随请求发往用户配置的模型端点）",
    ),
    # 网页自检（O/P1-11）：把一张屏幕截图与基线比对（或设为基线）。**只记摘要与比率**
    # （ADR-006）：URL 只上传指纹、差异只上传千分比整数；**截图内容一个字节都不上传**。
    "visual.check": Rule(
        upload="visual.check",
        fields={
            "url_digest": ("url_digest", "digest"),
            "width": ("width", "int"),
            "height": ("height", "int"),
            "diff_ratio": ("diff_ratio", "int"),
            "baseline_missing": ("baseline_missing", "bool"),
            "ms": ("ms", "int"),
        },
        note="网页预览的截图自检。diff_ratio 是千分比整数（0–1000）；"
        "baseline_missing 表示这次只落了基线、没做比对。截图正文只留本机",
    ),
    "tool.run_command": Rule(
        upload="tool.run_command",
        fields={
            "executable": ("executable", "str"),
            "risk": ("risk", "str"),
            "exit_code": ("exit_code", "int"),
            "status": ("status", "str"),
            "timeout": ("timeout", "int"),
            "stdout_bytes": ("stdout_bytes", "int"),
            "stderr_bytes": ("stderr_bytes", "int"),
            "command_digest": ("command", "digest"),
            "command_chars": ("command", "chars"),
        },
        drop={
            "command": "命令行原文只留本机 —— 它经常内嵌 token、内网地址、"
            "甚至一整个 -c 脚本。上传指纹与长度足以回答"
            "「两台机器跑的是不是同一条命令」"
        },
    ),
    # ---------------------------------------------------------------- 自动验证
    "verify.run": Rule(
        upload="verify.run",
        fields={
            "executable": ("executable", "str"),
            "status": ("status", "str"),
            "timeout": ("timeout", "int"),
            "exit_code": ("exit_code", "int"),
            "output_chars": ("output_chars", "int"),
            "command_digest": ("command", "digest"),
            "command_chars": ("command", "chars"),
            "reason_digest": ("reason", "digest"),
            "reason_chars": ("reason", "chars"),
        },
        drop={
            "command": "验证命令行原文只留本机（同 tool.run_command）",
            "reason": "跳过原因里会引述整条命令（CommandDenied 的消息体带「命令: ...」），"
            "所以只上传指纹与长度",
        },
        note="退出码是「这次改动到底成没成」的最高信号，单独成一类",
    ),
    # ---------------------------------------------------------------- 测试闭环（M9-C）
    "test_loop.run": Rule(
        upload="test_loop.run",
        fields={
            "command_digest": ("command", "digest"),
            "command_chars": ("command", "chars"),
            "rounds": ("rounds", "int"),
            "passed": ("passed", "int"),
            "failed": ("failed", "int"),
            "ms": ("ms", "int"),
        },
        drop={
            "command": "测试命令行原文只留本机（同 tool.run_command）",
        },
        note="测试闭环跑一轮的元数据：命令指纹、轮次、通过/失败计数与耗时。"
        "测试输出正文**只留本机**（ADR-006），这三个计数已够回答「闭环跑了几轮、成没成」",
    ),
    # 后台 / 并行 Agent（M9-D）：`task.spawn` 记一次后台任务的生命周期节点。
    # 只记 task_id 归属 + 状态 + 摘要长度 + 耗时；任务**正文与结论**一个字符
    # 都不出机（ADR-006）。状态是枚举（running/succeeded/failed），不是自由文本。
    "task.spawn": Rule(
        upload="task.spawn",
        fields={
            "task_id": ("task_id", "str"),
            "state": ("state", "str"),
            "chars": ("chars", "int"),
            "ms": ("ms", "int"),
        },
        note="后台任务一次生命周期节点的元数据。正文与结论只留本机，"
        "这里只记 task_id 归属、状态枚举、摘要长度与耗时",
    ),
    # 定时任务调度（M10）：`schedule.job` 记一次 job 的生命周期动作。
    # 只记 job_id 归属 + 动作枚举 + 启用状态 + prompt 长度 + 耗时；job 的
    # prompt **正文**一个字符都不出机（ADR-006）。动作是枚举
    #（created/updated/deleted/toggled/triggered/skipped/failed），不是自由文本。
    "schedule.job": Rule(
        upload="schedule.job",
        fields={
            "job_id": ("job_id", "str"),
            "action": ("action", "str"),
            "state": ("state", "str"),
            "chars": ("chars", "int"),
            "ms": ("ms", "int"),
        },
        note="定时任务一次生命周期动作的元数据。prompt 正文只留本机，"
        "这里只记 job_id 归属、动作与状态枚举、prompt 长度与耗时",
    ),
    # ---------------------------------------------------------------- 上下文裁剪
    "context.compact": Rule(
        upload="context.compact",
        fields={
            "compacted_messages": ("compacted_messages", "int"),
            "summary_chars": ("summary_chars", "int"),
        },
        note="裁剪时压掉了几条历史、摘要有多长。**摘要正文不上传**（ADR-006）—— "
        "它必然引述对话里的约束、路径与命令原文，只留本机；"
        "这两条计数已经够回答「谁的会话在被压缩」",
    ),
    # ---------------------------------------------------------------- 沙箱拦截
    "sandbox.block": Rule(
        upload="sandbox.block",
        fields={
            "kind": ("kind", "str"),
            "tool": ("tool", "str"),
            "path": ("path", "path"),
            "reason_digest": ("reason", "digest"),
            "reason_chars": ("reason", "chars"),
        },
        drop={
            "reason": "拦截消息里有完整请求路径与**工作区绝对路径**，"
            "只上传指纹与长度"
        },
        note="安全审计里最该被聚合的一类：谁在撞路径 jail、撞的哪一类",
    ),
    "sandbox.deny_command": Rule(
        upload="sandbox.deny_command",
        fields={
            "tool": ("tool", "str"),
            "executable": ("executable", "str"),
            "risk": ("risk", "str"),
            "reason_digest": ("reason", "digest"),
            "reason_chars": ("reason", "chars"),
        },
        drop={
            "reason": "拒绝原因里会引述工作区外路径与命令片段，"
            "只上传指纹与长度"
        },
        note="记在 tools/shell.py 的 _assess_command 里 —— 闸门 1 会先拦下 DENY，"
        "放处理器里的话这条审计永远不会产生",
    ),
    # ---------------------------------------------------------------- 规则与记忆（P2-15）
    "rules.load": Rule(
        upload="rules.load",
        fields={
            "layers": ("layers", "int"),
            "chars": ("chars", "int"),
            "truncated_count": ("truncated_count", "int"),
        },
        note="规则多层加载的层数 / 生效字符数 / 被截断或丢弃的层数。"
        "VIBE.md 的**正文只留本机**（ADR-006）—— 它常含内网地址与私有约定，"
        "这三个计数已够回答「谁在靠多层规则、谁撞了预算」",
    ),
    "memory.add": Rule(
        upload="memory.add",
        fields={
            "scope": ("scope", "str"),
            "chars": ("chars", "int"),
            "digest": ("digest", "digest"),
        },
        note="写入一条记忆的**动作**。只传来源、长度与指纹 —— "
        "记忆正文必然引述用户偏好与项目事实，属于正文类，只留本机",
    ),
    "memory.inject": Rule(
        upload="memory.inject",
        fields={
            "count": ("count", "int"),
            "chars": ("chars", "int"),
        },
        note="每轮任务注入记忆段的条数与总字符数。**注入的正文不上传**——"
        "它会被拼进系统提示，本质与用户消息同级的自由文本",
    ),
    # Skill / 斜杠命令（M9-A）：`run_skill` 展开一个工作流的动作。
    # 只记 name / 字符数 / 来源路径（相对工作区）；Skill **正文**是用户放置的
    # 自由文本（常含私有约定与步骤），按 ADR-006 一律不出机。
    # 本地字段叫 source，上传前改名 origin：`source` 在服务端 DENIED_KEYS 里
    #（它是「源代码正文」的同义词），照原样传会被整条拒掉。
    "skill.run": Rule(
        upload="skill.run",
        fields={
            "name": ("name", "str"),
            "chars": ("chars", "int"),
            "origin": ("source", "path"),
        },
        note="展开一个 Skill 工作流的元数据。正文只留本机，这里只记名字、长度与来源",
    ),
    # 代码审阅（M9-H）：`review.run` 记一次提交前自审的发起动作。
    # 只记「审了多少文件、提了几条、几条是严重级、耗时」——审阅**意见正文**是
    # 模型输出、属 ADR-006 的正文类，一个字符都不出机。计数已够回答
    # 「自审到底做了没、有没有抓到大问题」。
    "review.run": Rule(
        upload="review.run",
        fields={
            "files": ("files", "int"),
            "issues": ("issues", "int"),
            "severity_high": ("severity_high", "int"),
            "ms": ("ms", "int"),
        },
        note="一次提交前自审的元数据。意见正文只留本机，这里只记文件数、"
        "问题数、严重级数与耗时",
    ),
    # 客户端模型选择器（M9-I）：`model.route` 记一次选型结果。
    # 只记「任务意图 + 选出的模型名 + 原因枚举（none/static/size/fallback）」——
    # 任务**正文**是 ADR-006 的正文类，一个字符都不出机。模型名与原因已够回答
    # 「谁在按什么策略选了哪个模型」。
    "model.route": Rule(
        upload="model.route",
        fields={
            "intent": ("intent", "str"),
            "model": ("model", "str"),
            "reason": ("reason", "str"),
        },
        note="一次模型选型的元数据。任务正文只留本机，这里只记意图、模型名与原因枚举",
    ),
    "governance.login": Rule(
        upload="governance.login",
        fields={
            "ok": ("ok", "bool"),
            "status": ("status", "str"),
            "user_id": ("user_id", "str"),
            "device_id": ("device_id", "str"),
        },
        note="用户在桌面端登录治理面的动作。只记结果、装配状态与不透明标识 —— "
        "邮箱、口令、治理面地址一律只留本机（ADR-006）",
    ),
    "governance.register": Rule(
        upload="governance.register",
        fields={
            "ok": ("ok", "bool"),
            "status": ("status", "str"),
            "user_id": ("user_id", "str"),
            "device_id": ("device_id", "str"),
        },
        note="用户用邀请码注册并登录的动作。字段口径同 governance.login，"
        "邀请码/邮箱/口令均不上传",
    ),
    "governance.logout": Rule(
        upload="governance.logout",
        fields={
            "ok": ("ok", "bool"),
            "user_id": ("user_id", "str"),
        },
        note="用户退出治理面登录的动作。只记结果与不透明用户标识",
    ),
    # ---------------------------------------------------------------- 客户端自动更新（M9）
    "update.check": Rule(
        upload="update.check",
        fields={
            "result": ("result", "str"),
            "version": ("version", "str"),
            "origin": ("source", "str"),
            "seq": ("seq", "int"),
        },
        note="一次更新检查的结论：available/idle/failed/declined。只记版本号、"
        "来源名（governance/local）与发布序号 —— 来源地址是本机内网拓扑，不上传。"
        "本地字段叫 source，上传前改名 origin：`source` 在服务端 DENIED_KEYS 里"
        "（它是「源代码正文」的同义词），照原样传会被整条拒掉",
    ),
    "update.download": Rule(
        upload="update.download",
        fields={
            "version": ("version", "str"),
            "origin": ("source", "str"),
            "size": ("size", "int"),
            "result": ("result", "str"),
        },
        note="安装包下载的起止。只记字节数（清单声明值）与结果，不记下载地址",
    ),
    "update.staged": Rule(
        upload="update.staged",
        fields={
            "version": ("version", "str"),
            "origin": ("source", "str"),
            "size": ("size", "int"),
            "sha256_digest": ("sha256Digest", "digest"),
        },
        note="安装包已通过 sha256 校验并原子落盘，等待用户确认安装。"
        "sha256 只上传前 16 位 —— 完整摘要配合已知包体可做确认性比对，"
        "而这里只需要一个「同一份包」的短标识",
    ),
    "update.verify_failed": Rule(
        upload="update.verify_failed",
        fields={
            "origin": ("source", "str"),
            "kind": ("kind", "str"),
        },
        note="清单验签或包体校验失败。这是**被篡改/被损坏**信号，必须留痕；"
        "kind 取值 shape/signature/sha256/size",
    ),
    "update.applied": Rule(
        upload="update.applied",
        fields={"version": ("version", "str")},
        note="壳侧确认本机已更新到该版本（启动时读到 marker 后补记）",
    ),
    "update.apply_rejected": Rule(
        upload="update.apply_rejected",
        fields={"reason": ("reason", "str")},
        note="壳侧拒绝执行 staging 路径（形态校验失败等）。记录被拒原因，"
        "这是「本机拒绝了安装」的证据",
    ),
}


# --------------------------------------------------------------------------- 结果
@dataclass(frozen=True)
class Translated:
    """一条本地记录翻译后的结果。"""

    event: dict[str, Any]
    local_event: str
    upload_event: str
    dropped: tuple[tuple[str, str], ...] = ()
    unknown_fields: tuple[str, ...] = ()
    note: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.event)


# --------------------------------------------------------------------------- 翻译
def rule_for(local_event: str) -> Rule | None:
    """查本地事件的规则。返回 None 表示这个事件名**从未被分类**。"""
    return _RULES.get(local_event)


def classified_events() -> set[str]:
    return set(_RULES)


def unclassified(candidates: Any) -> list[str]:
    """在 `candidates`（本地出现过的事件名）里挑出没有规则的。

    给测试用：它把「源码里所有 audit.record("...") 调用点」和规则表对一遍。
    新增调用点而不登记规则时，这个函数会报出来 ——
    否则新事件会静默地不上传，审计覆盖悄悄变窄，而没人会发现。
    """
    return sorted(name for name in candidates if name not in _RULES)


def digest_value(value: str) -> str:
    """对外暴露同一个摘要算法，供本地日志写入方复用。"""
    return _t_digest(value) or ""


def translate(
    entry: Mapping[str, Any],
    *,
    session_id: str,
    sequence: int,
    fallback_ts: int,
    schema: Mapping[str, Mapping[str, str]] | None = None,
) -> Translated | None:
    """把一条本地审计记录翻译成上传事件。

    返回 None 表示这条记录**不参与上传**（未知事件类型，或规则标记为纯本地）。
    调用方必须把它当成「跳过」，而不是「出错了」—— 本地日志里有相当一部分
    事件（`/audit` 自检、中断请求）本来就不该出现在服务端。

    `schema` 是服务端给的字段表（`/api/v1/audit/schema` 的 `fields`）。
    传了就做最后一道过滤：规则表里多写的字段会被丢掉并计入 `unknown_fields`。
    不传则只用规则表 —— 但调用方应当总是传，见模块头第 3 条。
    """
    from .protocol import _to_epoch  # 复用同一套时间戳解析

    local_event = str(entry.get("event") or entry.get("type") or "").strip()
    if not local_event:
        return None
    rule = _RULES.get(local_event)
    if rule is None or not rule.upload:
        return None

    allowed = schema.get(rule.upload) if schema else None
    payload: dict[str, Any] = {}
    unknown: list[str] = []

    for target, (source, transform) in rule.fields.items():
        if allowed is not None and target not in allowed:
            # 服务端不收这个字段。丢掉而不是发出去 —— 发出去会让**整条**被拒。
            unknown.append(target)
            continue
        fn = TRANSFORMS.get(transform, _t_keep)
        value = fn(entry.get(source))
        if value is None:
            continue
        payload[target] = value

    ts = _to_epoch(entry.get("ts_epoch") or entry.get("ts"), fallback_ts)

    event = {
        "type": rule.upload,
        "seq": int(sequence),
        "ts": int(ts),
        "payload": payload,
    }
    return Translated(
        event=event,
        local_event=local_event,
        upload_event=rule.upload,
        dropped=tuple(rule.drop.items()),
        unknown_fields=tuple(unknown),
        note=rule.note,
    )


__all__ = [
    "Rule",
    "TRANSFORMS",
    "Translated",
    "classified_events",
    "digest_value",
    "rule_for",
    "translate",
    "unclassified",
]
