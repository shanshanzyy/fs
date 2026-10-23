"""审计载荷脱敏（ADR-006 的代码化）。

架构文档的红线是「服务端只存元数据 + 审计事件，绝不存代码片段、
Prompt 原文、模型输出原文」。**写成文档的红线不是红线** ——
只要有一个字段能自由塞字符串，某天就会有人把 diff 塞进去，
而且没人会拦，因为「反正审计是给人看的」。

所以这里把它做成三道闸门，任何一道不通过就**拒收整条事件**：

1. **键白名单**：按事件类型枚举允许的字段。多一个字段就拒，
   不是「忽略掉多余的」——忽略会让客户端以为记下了，实际没记，
   排查时对不上账。
2. **值形状校验**：字符串不许含换行、长度有上限、
   且**不许长成代码的样子**（缩进、括号密度、关键字密度）。
   这条是最有价值的：它不依赖键名，直接看内容。
3. **危险键黑名单**：`content` / `code` / `prompt` / `diff` / `output`
   这类键名一律拒，即使某个事件类型的白名单不小心把它列进去了。

第 3 条看着冗余，但它挡的是**未来有人往白名单里加东西**这类错误 ——
是给「三个月后的自己」留的护栏。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------- 上限
MAX_STRING = 256          # 单个字符串字段上限：路径、模型名、工具名都在这个量级以下
MAX_SHORT_STRING = 64     # 更严的字段（id、事件类型）
MAX_EVENTS_PER_BATCH = 500

# --------------------------------------------------------------------------- 键黑名单
# 这些键名一出现就拒收。它们全是「正文类」字段的典型命名。
#
# 最后几个（command / pattern / error / detail / arguments ...）是**平台自造的**：
# agent-core 的本地审计确实用过这些字段名来装原文（`command=command`、
# `pattern=pattern`、`error=error[:300]`）。它们不在最初的黑名单里，
# 但一旦有人「照着客户端字段名补白名单」，原文就会顺着这条路进库。
# 所以把它们显式钉死：想传命令/正则，只能传 `*_digest` + `*_chars`。
DENIED_KEYS = frozenset(
    {
        "content", "contents", "code", "source", "source_code",
        "prompt", "prompts", "system_prompt", "user_prompt",
        "diff", "patch", "old_string", "new_string", "search", "replace",
        "output", "completion", "response", "answer", "message", "messages",
        "text", "body", "payload", "data", "blob", "raw",
        "stdout", "stderr", "output_text", "file_content", "filecontent",
        "search_content", "excerpt", "snippet", "context_text",
        # --- 平台自造：本地审计曾用来装原文的字段名 ---
        "command", "commands", "pattern", "patterns", "regex",
        "error", "errors", "detail", "details", "traceback", "stack", "repr",
        "arguments", "args", "script", "scripts", "query", "preview", "base64",
    }
)

# --------------------------------------------------------------------------- 事件类型
# 事件契约的**唯一事实来源**。客户端不硬编码这份名单，而是启动时拉
# `/api/v1/audit/schema`（见 `event_schema()`）—— 服务端加字段，客户端自动跟上；
# 服务端收紧，客户端自动少传。硬编码两份名单必然漂移，然后表现为
# 「某天开始一批上传全被拒」，且没人知道为什么。
#
# 字段名以**客户端实际产出的名字**为准（`agent_core.audit.record()` 的 kwargs），
# 而不是另起一套「更规范」的叫法。理由：客户端是数据的产出方，
# 每多一层改名就多一个对不上的地方；而且改名不会让数据变得更安全。
#
# 三条命名约定：
#   · 原文类值一律只收 `*_digest`（16–64 位 hex）或 `*_chars`/`*_bytes`（计数）；
#   · `reason` 这类短文本只留给**有界的、工具自己写的**理由
#     （`修改 src/main.py`、`命中高危模式：强制推送`、审批人写的备注）。
#     机器生成的报错文本、可能引述文件内容的文本，一律走 `*_digest` ——
#     审批那几个事件里的 reason 属于前者，`tool.preflight_failed` /
#     `sandbox.*` / `verify.run` 的报错属于后者，所以它们用 digest。
#   · 新增事件类型必须同时在这里登记，否则整批会被拒。这是刻意的摩擦：
#     让「加一个字段」变成一个需要想一下的动作。
#
# **每条登记的类型都必须有真实产出方。** 登记了却没人为它写事件，
# 那类审计在真机上永远是空的，而且事后分不清「没配」还是「没实现」——
# 所以反向检查（tests/test_chain_compat.py）会逐条核对。
# 下面这几种就是这道检查发现后删掉的：
#   · `tool.call`        —— 与 `tool.execute` 是同一个概念，两个名字迟早只改一边
#   · `agent.iteration`  —— 量最大、信息最小；`agent.run.finish.iterations` 已覆盖
#   · `client.heartbeat` —— 存活性信号不是审计事件，服务端也没有对应端点
_SCHEMA: dict[str, dict[str, str]] = {
    # --- 会话级 ---
    "session.start": {"workspace": "path", "model": "str", "provider": "str"},
    "session.end": {"reason": "str", "iterations": "int", "duration_ms": "int"},
    # --- Agent 循环 ---
    "agent.run.start": {"task_chars": "int", "task_digest": "digest"},
    "agent.run.finish": {
        "iterations": "int",
        "tool_calls": "int",
        "answer_chars": "int",
        "answer_digest": "digest",
    },
    "agent.run.interrupted": {"iteration": "int", "tool_calls": "int"},
    "agent.run.abort": {"reason": "str", "iterations": "int"},
    "agent.run.exhausted": {"iterations": "int"},
    "agent.run.failed": {"kind": "str"},
    # --- 工具调用 ---
    "tool.execute": {
        "tool": "str",
        "risk": "str",
        "elapsed_ms": "int",
        "output_chars": "int",
        "path": "path",
        "ok": "bool",
    },
    "tool.unknown": {"tool": "str"},
    "tool.preflight_failed": {"tool": "str", "reason_digest": "digest"},
    # 工具**内部异常**（不是「跑了没成功」）。kind 是异常类名，
    # 用来回答「哪个工具在崩」—— 这类聚合排查以前根本无从做起。
    "tool.error": {"tool": "str", "kind": "str", "detail_chars": "int"},
    "tool.read_file": {"path": "path", "chars": "int", "lines": "str"},
    "tool.list_dir": {"path": "path", "depth": "int", "entries": "int"},
    "tool.grep": {
        "path": "path",
        "hits": "int",
        "pattern_digest": "digest",
        "pattern_chars": "int",
    },
    "tool.find_files": {"hits": "int", "pattern_digest": "digest", "pattern_chars": "int"},
    "tool.repo_map": {"path": "path", "files": "int", "symbols": "int", "truncated": "bool"},
    # 语义检索：只收「查没查、命中几条、走的是词法还是混合」，
    # query 原文与 tool.grep 的正则同一处理（只留 digest + 长度）。
    "tool.semantic_search": {
        "path": "path",
        "hits": "int",
        "backend": "str",
        "query_digest": "digest",
        "query_chars": "int",
    },
    # 增量索引（J/P1-9）：字段**全是计数** —— 变了几个文件、索引现在多大、
    # 这次追平花了多久。不含任何路径，更不含文件内容（ADR-006）。
    "index.rebuild": {
        "added": "int",
        "changed": "int",
        "removed": "int",
        "files_count": "int",
        "ms": "int",
    },
    # Git 集成（P0-C）：只收「看过哪份改动、改了多少」，diff 原文一个字符都不收。
    "tool.git_diff": {
        "path": "path",
        "mode": "str",
        "ref": "str",
        "staged": "bool",
        "files": "int",
        "added": "int",
        "removed": "int",
        "diff_digest": "digest",
        "diff_chars": "int",
    },
    # 生成提交信息：只收长度与指纹。message 正文必然复述改动细节，
    # 且「生成」不等于「提交」，所以这类事件不构成提交证据。
    "git.commit_message": {
        "branch": "str",
        "files": "int",
        "added": "int",
        "removed": "int",
        "diff_digest": "digest",
        "diff_chars": "int",
        "message_chars": "int",
    },
    # Git 面板写操作（M7 / B4）：全部是「用户本人在面板上点」的动作（ADR-028）。
    # 批量路径只收条数 + 指纹；提交信息与远端报错文本只收指纹/长度（ADR-006）——
    # 后者可能引述带凭据的远端 URL，与 web 工具的 error_digest 同口径。
    "git.stage": {"count": "int", "paths_digest": "digest"},
    "git.unstage": {"count": "int", "paths_digest": "digest"},
    "git.discard": {
        "count": "int",
        "paths_digest": "digest",
        "checkpoint_id": "str",
    },
    # 逐条回退（P3）：只收「删了几条消息、首末 seq、对应检查点、文件动作计数」，
    # 消息正文与文件正文一个字符都不出机（ADR-006）。
    "rollback.truncate": {
        "count": "int",
        "first_seq": "int",
        "last_seq": "int",
        "checkpoint_id": "str",
        "restored": "int",
        "deleted": "int",
    },
    "git.commit": {
        "message_digest": "digest",
        "message_chars": "int",
        "oid": "str",
    },
    "git.push": {
        "remote": "str",
        "branch": "str",
        "ok": "bool",
        "reason_digest": "digest",
        "reason_chars": "int",
    },
    "git.pull": {
        "remote": "str",
        "branch": "str",
        "ok": "bool",
        "reason_digest": "digest",
        "reason_chars": "int",
    },
    "git.branch": {"name": "str", "action": "str"},
    # 联网检索（M7）：query 原文与抓取正文一个字符都不收（ADR-006）。
    # web_search 只收 provider/计数/指纹；web_fetch 只收 host+path（query string
    # 换 digest）、状态、字节数与内容指纹。报错文本走 error_digest+error_chars
    # —— 异常消息会引述 URL/查询词/响应片段，与 sandbox.* / verify.run 的
    # 机器报错同一口径（error 键在 DENIED_KEYS 里钉死，原文键名必被拒）。
    "tool.web_search": {
        "provider": "str",
        "query_digest": "digest",
        "query_chars": "int",
        "max_results": "int",
        "result_count": "int",
        "error_digest": "digest",
        "error_chars": "int",
    },
    "tool.web_fetch": {
        "host": "str",
        "path": "path",
        "query_digest": "digest",
        "status": "int",
        "bytes": "int",
        "content_digest": "digest",
        "truncated": "bool",
        "error_digest": "digest",
        "error_chars": "int",
    },
    "tool.edit_file": {
        "path": "path",
        "strategy": "str",
        "added": "int",
        "removed": "int",
        "search_digest": "digest",
        "replace_digest": "digest",
        "search_lines": "int",
    },
    "tool.write_file": {
        "path": "path",
        "mode": "str",
        "added": "int",
        "removed": "int",
        "bytes": "int",
        "content_digest": "digest",
    },
    # Repo Wiki（M3+）：落点目录与计数（写盘类工具），Wiki 正文不出机（ADR-006）。
    "tool.repo_wiki": {
        "path": "path",
        "files": "int",
        "created": "int",
        "updated": "int",
        "bytes": "int",
        "truncated": "bool",
    },
    # 本地编辑面（D）：用户在编辑器里直接写代码。与 Agent 发起的写盘分开成一类，
    # 因为「谁改的」是审计里最要紧的一栏 —— 混进 tool.write_file 就分不清
    # 这段代码是模型写的还是人手写的。所有字段都是指纹/长度/计数（ADR-006）。
    "edit.complete": {
        "path": "path",
        "chars": "int",
        "backend": "str",
        "prefix_digest": "digest",
        "suffix_digest": "digest",
    },
    "edit.rewrite.preview": {
        "path": "path",
        "start": "int",
        "end": "int",
        "hunks": "int",
        "selection_digest": "digest",
        "chars": "int",
    },
    "edit.rewrite.apply": {
        "path": "path",
        "strategy": "str",
        "applied": "int",
        "total": "int",
        "added": "int",
        "removed": "int",
    },
    "edit.save": {
        "path": "path",
        "strategy": "str",
        "added": "int",
        "removed": "int",
        "chars": "int",
    },
    # 多文件原子事务（P1-8）：一次把多个文件「同生共死」地落盘。
    # 只收计数与结果标志 —— 各文件的 diff 正文只进审批预览，不进审计（ADR-006）。
    "edit.transaction": {
        "files_count": "int",
        "changed_count": "int",
        "added": "int",
        "removed": "int",
        "strategy": "str",
        "rolled_back": "bool",
    },
    "tool.run_command": {
        "executable": "str",
        "risk": "str",
        "exit_code": "int",
        "status": "str",
        "timeout": "int",
        "stdout_bytes": "int",
        "stderr_bytes": "int",
        "command_digest": "digest",
        "command_chars": "int",
    },
    # 语言服务器（E）。诊断与定义跳转是纯只读；重命名会落盘，所以照旧记一条。
    # 新标识符名客户端已只存指纹与长度（`new_name_digest` / `new_name_chars`），
    # 服务端相应地不接受任何正文形态的字段。
    "tool.lsp_diagnostics": {
        "path": "path",
        "diagnostics": "int",
        "error_count": "int",
        "warning_count": "int",
        "server": "str",
        "truncated": "bool",
    },
    "lsp.rename.preview": {
        "path": "path",
        "line": "int",
        "char": "int",
        "files": "int",
        "edits": "int",
        "new_name_digest": "digest",
        "new_name_chars": "int",
    },
    "lsp.rename.apply": {
        "path": "path",
        "edits": "int",
        "added": "int",
        "removed": "int",
        "strategy": "str",
    },
    # 计划视图（P2-12）：人手动标注某一步的状态。计划**正文**（步骤文本）只留本机，
    # 这里只收「标注了哪一步、标成什么、计划共几步」—— 短标识与枚举不含源码。
    "plan.toggle": {
        "id": "str",
        "status": "str",
        "steps_count": "int",
        "revision": "int",
    },
    # 多 Agent 编排（M3）：一次只读子 Agent 委派。任务与摘要**正文**只留本机，
    # 这里只收计数、成败与任务指纹（ADR-006）。
    "subagent.run": {
        "ok": "bool",
        "iterations": "int",
        "tool_calls": "int",
        "answer_chars": "int",
        "task_digest": "digest",
    },
    # MCP 客户端（P1-6）：外部 MCP server 的接入与调用。**外部工具恒 REVIEW**
    # （ADR-015）。参数正文（args_text）与返回正文一个字符都不收 ——
    # 这里只收 server/tool 短标识、调用个数与结果标志；参数只留 digest。
    "mcp.server_start": {
        "server": "str",
        "transport": "str",
        "ok": "bool",
        "tools": "int",
        "ms": "int",
    },
    "mcp.call": {
        "server": "str",
        "tool": "str",
        "ok": "bool",
        "argc": "int",
        "args_digest": "digest",
        "ms": "int",
    },
    # MCP 管理面（L）：用户在界面新增 / 删除 MCP server 配置。只收元数据
    # （server 名 / 传输方式 / 参数个数），命令与参数正文一律不出机（ADR-006）。
    "mcp.server_added": {
        "server": "str",
        "transport": "str",
        "argc": "int",
    },
    "mcp.server_removed": {
        "server": "str",
        "transport": "str",
        "argc": "int",
    },
    # 测试树（P2-14）：用户本人点「跑测试」的计数汇总。失败**正文**
    # （断言信息、traceback）只留本机，这里只收计数与首个失败的位置元数据。
    "test.run": {
        "scope": "str",
        "requested": "int",
        "passed": "int",
        "failed": "int",
        "skipped": "int",
        "ms": "int",
        "file": "path",
        "line": "int",
    },
    # 交互式终端（M/P1-7）：用户本人开的 PTY。**只收 open/close**（逐次键入不记），
    # 且命令正文只留 digest —— 这里只收短标识 termId、计数、尺寸、后端模式与退出码。
    "term.open": {
        "termId": "str",
        "argc": "int",
        "argv_digest": "digest",
        "cols": "int",
        "rows": "int",
        "mode": "str",
        "remote": "str",
        "ms": "int",
    },
    "term.close": {
        "termId": "str",
        "argc": "int",
        "argv_digest": "digest",
        "cols": "int",
        "rows": "int",
        "mode": "str",
        "exit_code": "int",
        "ms": "int",
    },
    # 多模态图片上传（N/P1-10）：用户本人贴的图。只收元数据 ——
    # 字节数、扩展名、内容摘要；图片正文（base64/blob）在本地，绝不上传。
    "image.upload": {
        "bytes": "int",
        "ext": "str",
        "digest": "digest",
    },
    # 网页自检（O/P1-11）：截图与基线比对 / 设基线。只收摘要与比率 ——
    # URL 只收指纹（url_digest）、差异只收千分比整数；**截图内容一个字节都不收**。
    "visual.check": {
        "url_digest": "digest",
        "width": "int",
        "height": "int",
        "diff_ratio": "int",
        "baseline_missing": "bool",
        "ms": "int",
    },
    # 自动验证（改完代码后自己跑一遍的那条命令）。
    # 单独成一类是因为它的**退出码**是「这次改动到底成没成」的最高信号，
    # 混进 tool.run_command 会让这条信号淹没在模型自己发起的一堆命令里。
    "verify.run": {
        "executable": "str",
        "status": "str",
        "timeout": "int",
        "exit_code": "int",
        "output_chars": "int",
        "command_digest": "digest",
        "command_chars": "int",
        "reason_digest": "digest",
        "reason_chars": "int",
    },
    # 测试闭环（M9-C）：跑一轮的元数据。测试输出正文不出机（ADR-006），
    # 只收命令指纹、轮次、通过/失败计数与耗时。
    "test_loop.run": {
        "command_digest": "digest",
        "command_chars": "int",
        "rounds": "int",
        "passed": "int",
        "failed": "int",
        "ms": "int",
    },
    # 后台 / 并行 Agent（M9-D）。只收 task_id 归属、状态枚举、摘要长度与耗时；
    # 任务正文与结论不出机（ADR-006）。
    "task.spawn": {"task_id": "str", "state": "str", "chars": "int", "ms": "int"},
    # 定时任务调度（M10）。只收 job_id 归属、动作/状态枚举、prompt 长度与耗时；
    # job 的 prompt 正文不出机（ADR-006）。
    "schedule.job": {
        "job_id": "str",
        "action": "str",
        "state": "str",
        "chars": "int",
        "ms": "int",
    },
    # 上下文裁剪（C4）。只收「压掉了几条」与「摘要多长」——
    # 摘要**正文**必然引述对话里的约束、路径与命令原文，属于 ADR-006 的正文类，
    # 客户端也不会把它放进载荷。
    "context.compact": {"compacted_messages": "int", "summary_chars": "int"},
    # --- 规则与记忆（P2-15）---
    # 规则多层加载。只收层数、生效字符数与被截断的层数；
    # VIBE.md 的**正文**在本地，绝不上传（ADR-006）。
    "rules.load": {"layers": "int", "chars": "int", "truncated_count": "int"},
    # 写入一条记忆。只收来源 scope、字符数与内容指纹；
    # 记忆**正文**必然引述用户偏好与项目事实，属于正文类，只留本机。
    "memory.add": {"scope": "str", "chars": "int", "digest": "digest"},
    # 每轮任务注入记忆段。只收注入条数与总字符数，**不记正文**。
    "memory.inject": {"count": "int", "chars": "int"},
    # Skill / 斜杠命令（M9-A）：展开一个工作流的元数据。正文不出机（ADR-006），
    # 只收名字、字符数与来源路径。本地字段叫 source、上传前改名 origin
    #（`source` 在 DENIED_KEYS 里，是「源代码正文」同义词）。
    "skill.run": {"name": "str", "chars": "int", "origin": "path"},
    # 代码审阅（M9-H）：一次提交前自审的元数据。意见正文不出机（ADR-006），
    # 只收文件数、问题数、严重级数与耗时。
    "review.run": {"files": "int", "issues": "int", "severity_high": "int", "ms": "int"},
    # --- 远程设备 file 面（M11）---
    # 用户本人经界面做的一次远程连接管理动作。只收连接名归属、动作枚举、
    # 两个权限位、成功与否与耗时；host / 密钥路径等基础设施信息不出机（ADR-006）。
    "remote.connection": {
        "connection": "str",
        "action": "str",
        "allow_write": "bool",
        "allow_shell": "bool",
        "ok": "bool",
        "ms": "int",
    },
    # 用户本人经界面做的一次远端文件读/写动作。只收连接名、远端相对路径、
    # 动作枚举、字节数、内容 sha256 与增删行数；**文件正文与 diff 正文
    # 一个字符都不出机**（ADR-006）。
    "remote.file": {
        "connection": "str",
        "path": "path",
        "action": "str",
        "bytes": "int",
        "sha256": "digest",
        "added": "int",
        "removed": "int",
        "backed_up": "bool",
    },
    "remote.rollback": {
        "connection": "str",
        "path": "path",
        "action": "str",
        "bytes": "int",
        "backed_up": "bool",
    },
    # 客户端模型选择器（M9-I）：一次选型。任务正文不出机（ADR-006），
    # 只收意图、模型名与原因枚举。
    "model.route": {"intent": "str", "model": "str", "reason": "str"},
    # 治理面登录 / 注册 / 登出（M3+）：用户在桌面端接入治理面的动作。
    # 只收结果、装配状态与不透明标识（userId/deviceId）；邮箱、口令、邀请码、
    # 治理面地址一律只留本机（ADR-006）。
    "governance.login": {
        "ok": "bool",
        "status": "str",
        "user_id": "str",
        "device_id": "str",
    },
    "governance.register": {
        "ok": "bool",
        "status": "str",
        "user_id": "str",
        "device_id": "str",
    },
    "governance.logout": {"ok": "bool", "user_id": "str"},
    # --- 审批闸门 ---
    "approval.request": {"tool": "str", "risk": "str", "reason": "str"},
    "approval.decision": {
        "tool": "str",
        "decision": "str",
        "reason": "str",
        "arguments_digest": "digest",
    },
    "approval.denied_by_policy": {"tool": "str", "reason": "str"},
    "approval.no_ui": {"tool": "str", "decision": "str"},
    "approval.timeout": {"tool": "str", "timeout_s": "int"},
    # 草稿预览（P1-2）：审批期渲染「改动后效果」，正文不落盘、不出机（ADR-006）。
    # token 由客户端生成（一次性短标识），服务端只按 digest 形态收。
    "preview.draft": {
        "path": "path",
        "token": "digest",
        "files": "int",
        "chars": "int",
    },
    # 真实应用预览（P2 / ADR-039）：只登记本机 dev server 的端口与主机。
    # 远端预览转发（P3 / ADR-040）：只收状态跃迁与两个端口号，不收被转发的字节。
    "preview.external": {"port": "int", "host": "str"},
    "preview.tunnel": {
        "action": "str",
        "connection": "str",
        "remote_port": "int",
        "local_port": "int",
        "ok": "bool",
        "ms": "int",
        "reason": "str",
        "closed": "bool",
        "refs": "int",
    },
    # --- 沙箱拦截 ---
    # kind: escape / sensitive / policy_deny / audit_dir / root_write / malformed
    "sandbox.block": {
        "kind": "str",
        "tool": "str",
        "path": "path",
        "reason_digest": "digest",
        "reason_chars": "int",
    },
    "sandbox.deny_command": {
        "tool": "str",
        "executable": "str",
        "risk": "str",
        "reason_digest": "digest",
        "reason_chars": "int",
    },
    # --- 网关 ---
    "gateway.request": {
        "route": "str",
        "task": "str",
        "sensitive": "bool",
        "prompt_tokens": "int",
        "max_tokens": "int",
    },
    "gateway.response": {
        "route": "str",
        "status": "int",
        "latency_ms": "int",
        "total_tokens": "int",
        "usage_estimated": "bool",
        "degraded_from": "str",
    },
    "gateway.denied": {"kind": "str", "route": "str"},
    # 搜索中转（M7）：客户端经治理面转发的一次搜索。查询词对服务端也只记
    # 指纹 —— 服务端不做查询内容留存，与客户端 tool.web_search 同口径。
    "web.search_relay": {
        "query_digest": "digest",
        "query_chars": "int",
        "results": "int",
    },
    # --- 策略 ---
    "policy.fetch": {"version": "int", "scope": "str"},
    "policy.rejected": {"reason": "str"},
    # --- 客户端发布（M9）---
    # **服务端自产**：控制台/CLI 发布与撤销客户端版本。客户端没有调用点，
    # 因此这两条在 tests/test_chain_compat.py 的 server_side 白名单里。
    # sha256 只收前 16 位指纹（与 ADR-006 的「不落正文」同口径）——
    # 整串 sha256 已存在 release 表里，审计只回答「谁在什么时候发了哪个版本」。
    "release.publish": {
        "releaseId": "str",
        "version": "str",
        "channel": "str",
        "platform": "str",
        "arch": "str",
        "sha256Digest": "digest",
        "size": "int",
        "mandatory": "bool",
        "rolloutPercent": "int",
        "seq": "int",
    },
    "release.revoke": {"releaseId": "str", "revoked": "bool"},
    # --- 客户端自动更新（M9）---
    # 客户端自产（内核 update/service.py）。字段名以客户端词汇表的上传名为准：
    # 本机字段叫 `source`，上传前被 vocabulary 改名 `origin` —— `source` 在
    # DENIED_KEYS 里（它是「源代码正文」的同义词），照原样收会整条被拒。
    # 主题是「装了哪个版本」，不是「从哪台机器下的」，所以来源只留名字
    # （governance/local），地址一律不出本机。
    "update.check": {
        "result": "str",
        "version": "str",
        "origin": "str",
        "seq": "int",
    },
    "update.download": {
        "version": "str",
        "origin": "str",
        "size": "int",
        "result": "str",
    },
    "update.staged": {
        "version": "str",
        "origin": "str",
        "size": "int",
        "sha256_digest": "digest",
    },
    "update.verify_failed": {"origin": "str", "kind": "str"},
    "update.applied": {"version": "str"},
    "update.apply_rejected": {"reason": "str"},
}

# 允许的通用字段（任何事件都可以带）。只放「无内容风险」的定位信息。
_COMMON_FIELDS: dict[str, str] = {"session_id": "id", "seq": "int", "ts": "int"}

_TYPE_CAP = 64
# 十六进制摘要**大小写不敏感**。摘要本身是 hex，大小写等价；
# 而这里每拒一条事件，客户端那条事件就永久丢了（它已经算进本地上传链，
# 重传不会补上）。所以「格式等价却被拒」是纯粹的数据损失，不能容忍。
_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{16,64}$")
_PATH_RE = re.compile(r"^[\w.\- /\\:@+~]{0,200}$", re.UNICODE)

# 代码特征探测：用于「值形状」这道闸门
_CODE_MARKERS = (
    re.compile(r"^\s*(?:def|class|function|const|let|var|import|from|package|public|private)\b"),
    re.compile(r"[{};]\s*$"),
    re.compile(r"=>|::|->|&&|\|\|"),
    re.compile(r"^[\t ]{2,}\S"),          # 带缩进的行
    # HTML/XML 标签。**必须允许带属性**：`<div class="row">` 是最常见的一种，
    # 只认 `<div>` 这种无属性标签等于漏掉大半。
    re.compile(r"</?[a-zA-Z][\w-]*(?:\s[^<>]*)?/?>"),
)
_BRACKET_DENSITY = re.compile(r"[(){}\[\]]")


@dataclass(frozen=True)
class RedactionVerdict:
    ok: bool
    reason: str = ""
    cleaned: dict[str, Any] | None = None

    def describe(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason}


def looks_like_code(value: str) -> str:
    """判断字符串是否长得像代码。返回特征描述，空串表示不像。"""
    if "\n" in value or "\r" in value:
        return "含换行符"
    if len(value) > 200:
        return f"长度 {len(value)} 超过字段上限"
    if len(_BRACKET_DENSITY.findall(value)) >= 6:
        return "括号密度过高"
    for pattern in _CODE_MARKERS:
        if pattern.search(value):
            return "命中代码特征"
    return ""


def _check_value(key: str, kind: str, value: Any) -> tuple[bool, str, Any]:
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, f"字段 {key} 需要整数", None
        return True, "", int(value)

    if kind == "bool":
        if not isinstance(value, bool):
            return False, f"字段 {key} 需要布尔值", None
        return True, "", value

    if kind == "digest":
        if not isinstance(value, str) or not _DIGEST_RE.match(value):
            return False, f"字段 {key} 需要 16–64 位十六进制摘要（不要传原文）", None
        return True, "", value

    # str / path / id 都是字符串
    if not isinstance(value, str):
        return False, f"字段 {key} 需要字符串", None

    limit = MAX_SHORT_STRING if kind in ("id",) else MAX_STRING
    if len(value) > limit:
        return False, f"字段 {key} 长度 {len(value)} 超过上限 {limit}", None

    if kind == "path":
        if not _PATH_RE.match(value):
            return False, f"字段 {key} 不是合法路径形态", None
        code = looks_like_code(value)
        if code:
            return False, f"字段 {key} {code}", None
        return True, "", value

    # 普通字符串：一律不许含换行，且不许像代码
    code = looks_like_code(value)
    if code:
        return False, f"字段 {key} {code}", None
    return True, "", value


def validate_event(event: dict[str, Any]) -> RedactionVerdict:
    """校验一条审计事件的载荷。

    **调用方必须把 False 当成「整条拒收」**，而不是「过滤后收下」——
    见文件头第 1 条。
    """
    if not isinstance(event, dict):
        return RedactionVerdict(False, "事件不是对象")

    event_type = event.get("type")
    if not isinstance(event_type, str) or not event_type:
        return RedactionVerdict(False, "事件缺少 type")
    if len(event_type) > _TYPE_CAP:
        return RedactionVerdict(False, f"事件类型过长（>{_TYPE_CAP}）")

    schema = _SCHEMA.get(event_type)
    if schema is None:
        return RedactionVerdict(
            False,
            f"未知事件类型 {event_type!r}。新增类型需在服务端 redaction._SCHEMA 登记",
        )

    payload = event.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return RedactionVerdict(False, "payload 必须是对象")

    allowed = {**schema, **_COMMON_FIELDS}
    cleaned: dict[str, Any] = {}

    # --- 闸门 1 + 3：键必须白名单，且不在黑名单 ---
    for key in payload:
        if not isinstance(key, str):
            return RedactionVerdict(False, "payload 的键必须是字符串")
        lowered = key.lower()
        if lowered in DENIED_KEYS:
            return RedactionVerdict(
                False,
                f"字段 {key!r} 属于正文类字段（ADR-006 红线）。"
                "请改传长度、摘要或计数，不要传内容本身",
            )
        if key not in allowed:
            return RedactionVerdict(
                False,
                f"事件 {event_type!r} 不接受字段 {key!r}。"
                f"允许的字段：{', '.join(sorted(allowed))}",
            )

    # --- 闸门 2：值形状 ---
    for key, value in payload.items():
        if value is None:
            continue
        ok, reason, coerced = _check_value(key, allowed[key], value)
        if not ok:
            return RedactionVerdict(False, reason)
        cleaned[key] = coerced

    return RedactionVerdict(True, "", cleaned)


# --------------------------------------------------------------------------- 统计
def redaction_stats(events: list[dict[str, Any]]) -> dict[str, Any]:
    """批量校验，返回「收下哪些、拒绝哪些及原因」。

    拒绝原因会**回给客户端**（不回给最终用户），让开发者知道
    「我上报的这条为什么没收」——否则他们只会看到审计缺失，
    然后开始怀疑是服务端丢数据。
    """
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        verdict = validate_event(event)
        if verdict.ok:
            accepted.append({**event, "payload": verdict.cleaned})
        else:
            rejected.append({"index": index, "reason": verdict.reason})
    return {"accepted": accepted, "rejected": rejected}


def known_event_types() -> list[str]:
    return sorted(_SCHEMA)


def event_schema() -> dict[str, dict[str, str]]:
    """完整契约：事件类型 → {字段名: 类型}。

    `/audit/schema` 把它整份发给客户端。只发事件名是不够的 ——
    客户端还需要知道**每个类型接受哪些字段**，否则它会照着本地日志
    原样上传，然后被这里的键白名单逐条拒绝。而这类拒绝的后果比看起来重：
    客户端在把事件算进本地上传链之后才被拒，那条事件就永久丢了
    （本地链已经推进，重传不会补上）。
    """
    return {event_type: dict(fields) for event_type, fields in _SCHEMA.items()}


__all__ = [
    "DENIED_KEYS",
    "MAX_EVENTS_PER_BATCH",
    "MAX_STRING",
    "RedactionVerdict",
    "event_schema",
    "known_event_types",
    "looks_like_code",
    "redaction_stats",
    "validate_event",
]
