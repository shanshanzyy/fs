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
            "ms": ("ms", "int"),
        },
        note="起一个交互式终端。命令正文（用户 shell 的 argv）只留本机，"
        "上传的是大小/尺寸/后端模式（pty 或降级 pipe）与耗时",
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
