"""会话层：把 Agent 包装成一个可被界面远程驱动、且随时能中断的会话。

## 这里真正解决的问题

Agent 的两处接口天生是「同步、面向单机」的：

- `AgentHooks` 是回调，M0 里它直接 `print` 到终端；
- `Approver` 是个**阻塞**函数，M0 里它 `input()` 等用户敲键盘。

搬到界面上之后，审批变成了「Agent 线程暂停 → 事件推给浏览器 → 用户点按钮 →
HTTP 请求到达 → 唤醒 Agent 线程」。本模块用「事件总线 + 一次性的
`threading.Event` 会合点」把这条链路接起来，**不改动 Agent 循环一行逻辑**——
循环仍然以为自己在等一个同步函数返回。这是让 Core 保持与界面无关的关键。

## 失效安全（fail-safe）

审批闸门在以下任一情况一律**拒绝**，绝不默认放行：

- 超时未响应（默认 30 分钟）
- 当前没有任何界面连着（没人能按按钮）
- 任务已被请求中断

理由很直接：这道闸门是 ADR-008 的最后一道防线，它的默认值必须是「不放行」。
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import os
import posixpath
import shlex
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from .. import gitinfo
from ..audit import digest, record_session_end, record_session_start, verify_chain
from ..config import Config
from ..context import build_refs_prompt
from ..context.images import identify_image, to_message_parts
from ..edit import (
    Hunk,
    TextDoc,
    EditTransaction,
    TransactionError,
    apply_hunks,
    commit_plan,
    plan_overwrite,
    split_hunks,
)
from ..edit.patcher import _diff
from ..errors import AgentError, LLMError, SandboxViolation, ToolError
from ..index import IndexWatcher, peek_index
from ..index.lexical import OVERLAP_LINES, WINDOW_LINES
from ..llm import (
    LLMClient,
    build_completion_prompt,
    build_rewrite_prompt,
    clean_completion_text,
    window_completion_context,
)
from ..loop import Agent, AgentHooks, ApprovalRequest, RunStats
from ..memory import MemoryStore
from ..lsp.manager import _uri_to_path, get_manager
from ..mcp.client import McpError as McpClientError
from ..mcp.manager import get_manager as get_mcp_manager
from ..mcp.registry import (
    NAME_PREFIX as MCP_NAME_PREFIX,
    load_servers as load_mcp_servers,
    merge_mcp,
    save_servers as save_mcp_servers,
    _to_server as _parse_mcp_server,
)
from ..preview_media import draft_rank
from ..sandbox import Risk
from ..remote.connection import RemoteConnection, make_connection
from ..remote.degrade import is_remote_workspace, remote_degraded
from ..remote.errors import RemoteConfigError, RemoteError
from ..remote.roots import PREFIX, RemoteRoot, parse_ns_root, validate_root
from ..remote.session import ChangeEntry, RemoteSessions
from ..remote.store import RemoteStore, store_path_for
from ..remote.tunnel import MAX_TUNNELS, TunnelManager
from ..remote.workspace import open_workspace, transport_for
from ..terminal.manager import (
    TerminalError,
    default_shell_argv,
    get_manager as get_terminal_manager,
    shell_label,
)
from ..testing import collect as collect_tests, run as run_tests
from ..tools import (
    WEB_FETCH_SPEC,
    WEB_SEARCH_SPEC,
    ToolRegistry,
    ToolResult,
    ToolPreview,
    build_default_registry,
)
from ..tools.base import DEFAULT_IGNORED_DIRS, MAX_SCAN_BYTES
from ..tools.fs import _FILE_FOOTER, _FILE_HEADER
from ..tools.remote import _backup_before_write, locate_remote_turn, restore_turn
from ..tools.plan import PLAN_STATUSES
from ..update.service import UpdateOptions, UpdateService
from ..governance.client import GovernanceClient, GovernanceError
from ..governance.credentials import CredentialStore
from ..governance.runtime import is_config_locked, wire
from . import protocol as P
from . import visual
from .bus import EventBus
from .files import file_digest
from .store import SessionStore, validate_message_group

# 审批等待上限。超时即拒 —— 见模块头的失效安全说明。
DEFAULT_APPROVAL_TIMEOUT = 30 * 60

# 单次预演内容的上限。整文件写入可能有好几 MB，直接塞进事件流会拖垮界面。
MAX_PREVIEW_BYTES = 200_000

# 行内改写的选区上限。选区过大时模型改写质量骤降，也会把提示词撑爆；
# 宁可让用户缩小选区，也不要产出一份没法审阅的巨型 diff。
MAX_REWRITE_CHARS = 60_000
REWRITE_CONTEXT_LINES = 20

# 补全去重缓存（D3+）。只吃「打字停顿后重复触发」这一小段窗口；条目数封顶防无界增长。
# 只在内存、随会话消亡，绝不落盘 —— 补全正文不长期留存（ADR-006）。
COMPLETION_CACHE_TTL = 30.0  # 秒
COMPLETION_CACHE_MAX = 32

# 远端 @folder 引用一次最多列多少条（与本地 @folder 的渲染上限同量级）。
REMOTE_REF_LISTING_MAX = 400

# 远程终端的伪终端自检宽限期。本地 2.5s 足够，但 ssh 要握手 + 远端登录 +
# 加载 rc 文件，慢链路上很容易超过 —— 提前判「哑终端」会把一条好 PTY 降级成管道。
REMOTE_TERM_PROBE_SECONDS = 20.0


def remote_terminal_argv(sessions: Any, remote_workspace: str) -> tuple[list[str], str]:
    """拼「远程终端」的 argv：`ssh -tt <加固参数> <host> [cd <root> && exec $SHELL -l]`。

    返回 `(argv, 连接名)`。为什么走本地 ssh 子进程而不是另开协议：终端通路本来就是
    「本机起一个子进程、由伪终端接管按键与屏幕」，远程只是把子进程从 `powershell.exe`
    换成 `ssh` —— 按键、回显、resize、断开收尾全部复用现有链路（ADR-016 的边界一条不破）。
    `-tt` 是**强制分配远端 TTY**，没有它远端不认「交互式」、`vim`/`top` 直接退化。

    起始目录取连接的 root（外加 `@remote/<连接>/<子目录>` 里的子目录），用
    `cd <quoted> && exec "$SHELL" -l` 交回登录 shell —— 这条串要同时兼容
    bash/zsh/sh/fish，所以只用「单引号路径 + $SHELL + exec」这三样都成立的写法。
    """
    conn = sessions.active()
    if conn is None:
        raise ValueError("远程工作区尚未连上，无法启动远程终端")
    transport = sessions.require_workspace().transport
    # `-tt` 与端口转发的 `-T` 一样，是**代码拼出的常量**，不是用户可达的逃生口。
    argv = transport.base_argv(extra_options=["-tt"])
    _connection, sub = parse_ns_root(remote_workspace)
    start = str(getattr(conn, "root_normalized", "") or "")
    if sub:
        start = posixpath.join(start or "/", sub)
    if start and start != "/":
        argv.append(f"cd {shlex.quote(start)} && exec \"$SHELL\" -l")
    return argv, str(getattr(conn, "name", "") or "")


def _workspace_is_remote_doorplate(workspace: Any) -> bool:
    """工作区是不是 M11 §7 的「远端项目门牌」：除 `.vibe/` 外没有任何本地内容。

    M11 §7 的专用工作区就是「只放 `.vibe/remote.json` 的空本地目录」。用它把
    「远端项目工作区」与「本地项目顺手加了远端连接」区分开 —— 后者仍然应该起
    本机终端，不能因为 `.vibe/remote.json` 存在就把用户的本地 shell 换成 ssh。
    """
    try:
        entries = [
            entry for entry in Path(str(workspace)).iterdir() if entry.name != ".vibe"
        ]
    except OSError:
        return False
    return not entries


def manifest_terminal_argv(config: Any) -> tuple[list[str], str] | None:
    """「清单驱动的远端项目工作区」里的终端：同样落在远端设备上（M11 §7）。

    有些工作区本身就是**远端项目的门牌号** —— 目录里只有 `.vibe/remote.json`，
    代码全在设备上。这种工作区里起本地 shell 只会得到一个看着像项目、实际一行
    代码都没有的本机路径（用户以为在操作远端，命令却全跑在本机），所以这里把
    终端也接到清单里解析出来的那台设备上。

    返回 `None` 表示「本工作区不是这种门牌工作区」→ 调用方按本机终端处理：
    没有清单，或者本地还有项目文件（那是一个本地工作区，只是碰巧配了远端连接）。
    连接解析沿用 `RemoteStore.resolve` 的三级兜底（默认 > 唯一那条）；多条又没设
    默认时**不猜**，让错误原样上抛（猜错就是把命令打到错的机器上）。
    """
    if not _workspace_is_remote_doorplate(getattr(config, "workspace", "")):
        return None
    store = RemoteStore.load(store_path_for(config))
    if not store.names():
        return None
    conn = store.resolve(None)
    argv = transport_for(conn).base_argv(extra_options=["-tt"])
    start = str(getattr(conn, "root_normalized", "") or "")
    if start and start != "/":
        argv.append(f"cd {shlex.quote(start)} && exec \"$SHELL\" -l")
    return argv, str(getattr(conn, "name", "") or "")


def _remote_listing_text(ns_path: str, entries: list[dict[str, Any]]) -> str:
    """把远端 `list_dir` 的结果渲染成与本地 `@folder` 同构的一层清单。

    远端不返回 `Path`，因此不能复用 `render_dir_listing`；但口径必须一致：
    目录带 `/`、文件带字节数、跳过忽略目录与点开头项、空目录明说「空目录」。
    """
    lines = [f"{ns_path}/"]
    ordered = sorted(
        entries,
        key=lambda e: (
            str(e.get("type") or "") != "d",
            str(e.get("name") or "").lower(),
        ),
    )
    shown = 0
    for entry in ordered:
        if shown >= REMOTE_REF_LISTING_MAX:
            lines.append("  ... （条目过多，已截断）")
            break
        name = str(entry.get("name") or "")
        if not name or name in DEFAULT_IGNORED_DIRS or name.startswith("."):
            continue
        if str(entry.get("type") or "") == "d":
            lines.append(f"  {name}/")
        else:
            size = int(entry.get("size") or 0)
            lines.append(f"  {name}  ({size} B)")
        shown += 1
    if shown == 0:
        lines.append("  （空目录）")
    return "\n".join(lines)


def _completion_cache_key(
    rel: str, prefix: str, suffix: str, limit: int, fim: bool
) -> str:
    """补全缓存键：把「影响结果的全部输入」并进指纹。

    开关 FIM / 改 token 上限 / 换上下文都改变结果，因此必须一起参与哈希 ——
    否则改了配置却命中旧缓存，用户会看到「设置没生效」。换模型由调用方
    `invalidate_completion_cache()` 覆盖。
    """
    return digest(f"{rel}\x00{prefix}\x00{suffix}\x00{limit}\x00{int(fim)}")


# 重命名新名称的长度上限。标识符没这么长，设上限是为了挡住「整段代码当名字」。
MAX_RENAME_NAME = 64

# 增量事件的节流参数（A1）。本地模型每秒吐几十 token，逐 token 发 SSE 会让
# 事件总线的回放历史被 delta 淹没；攒够一小段再发，实时感几乎无损。
DELTA_FLUSH_INTERVAL = 0.12  # 秒
DELTA_FLUSH_CHARS = 96


class BusyError(RuntimeError):
    """已有任务在执行中。"""


class StaleFileError(RuntimeError):
    """保存时磁盘内容与打开时的指纹不符 —— 乐观锁拒绝覆盖（D11）。"""


def _clip(text: str, limit: int = MAX_PREVIEW_BYTES) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def serialize_preview(preview: ToolPreview) -> dict[str, Any]:
    """把预演结果转成可 JSON 化的字典（供 SSE 推送）。"""
    body, clipped = _clip(preview.body)
    return {
        "kind": preview.kind,
        "body": body,
        "summary": preview.summary,
        "error": preview.error,
        "clipped": clipped,
    }


def _lsp_registration(config: Config) -> bool | None:
    """`lsp_diagnostics` 是否注册：`off` 明确不注册，其余交给 PATH 自动探测。

    返回 None = 「你自己探」（`build_default_registry` 会跑 `shutil.which`）——
    本机装了什么语言服务器只有运行时才知道，不该在配置层硬编码。

    远程工作区（M13）：本机语言服务器够不到远端的文件，**注册了只会给出假阴性**
    ——「诊断说没问题」比「明确说这一项不可用」难排查十倍，所以这里直接不注册。
    """
    if str(getattr(config, "remote_workspace", "") or "") and getattr(
        config, "remote_enabled", False
    ):
        return False
    mode = str(getattr(config, "lsp_mode", "auto") or "auto")
    return False if mode == "off" else None


def _mcp_registration(config: Config) -> list | None:
    """MCP 是否接入、接入哪些 server。

    返回 None = **未启用**（`mcp_enabled=false`，即默认）—— 与今天逐字节一致，
    不 spawn 任何子进程。启用时读 servers 配置文件；文件读不出来只记原因，
    返回空列表，绝不让会话启动失败。
    """
    if not getattr(config, "mcp_enabled", False):
        return None
    manager = get_mcp_manager()
    manager.bind_workspace(config.workspace)
    manager.set_timeout(int(getattr(config, "mcp_timeout", 30) or 30))
    try:
        return load_mcp_servers(config.mcp_config_path)
    except McpClientError as exc:
        manager.set_config_error(str(exc))
        return []


def _ssh_probe() -> dict[str, Any]:
    """本机是否有系统 ssh 客户端（远程链路的前提）。

    只回「有没有、路径是什么」—— 这是本机基础设施信息，给界面用来在
    「远程」面板上直说「本机没装 ssh，连不了」，而不是让用户点了「打开」才报错。
    """
    path = shutil.which("ssh")
    return {"available": bool(path), "path": path or ""}


def _spec_text(spec: Mapping[str, Any], key: str, base: Any, default: str = "") -> str:
    """取一个字符串字段（缺省沿用 `base` 的值）。类型不对就明确报错，不静默转型。"""
    if key in spec:
        value = spec[key]
    elif base is not None and hasattr(base, key):
        value = getattr(base, key)
    else:
        value = default
    if value is None:
        return default
    if not isinstance(value, str):
        raise AgentError(f"{key} 需为字符串（实际: {type(value).__name__}）")
    return value


def _spec_int(spec: Mapping[str, Any], key: str, base: Any, default: int) -> int:
    if key in spec:
        value = spec[key]
    elif base is not None and hasattr(base, key):
        value = getattr(base, key)
    else:
        value = default
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentError(f"{key} 需为整数（实际: {value!r}）")
    return value


def _int_or_zero(value: Any) -> int:
    """审计用：把「可能不是整数」的入参收敛成整数，**绝不因为记审计而失败**。

    审计写不进去比少记一条严重得多（链断了之后所有后续条目都要重算），
    所以这里宁可记 0，也不在异常路径上再抛一次。
    """
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _spec_flag(spec: Any, key: str, default: bool = False) -> bool:
    """取一个布尔字段（`spec` 不是对象时直接回默认值，供 add 的 `default` 用）。"""
    if not isinstance(spec, dict):
        return default
    value = spec.get(key, default)
    if not isinstance(value, bool):
        raise AgentError(f"{key} 需为布尔值（实际: {value!r}）")
    return value


def _connection_from_spec(spec: object, *, base: RemoteConnection | None) -> RemoteConnection:
    """把界面提交的 spec 变成一条**已校验**的连接。

    `base` 给出时是「更新」：缺省字段沿用原值（部分更新也成立，但不支持改名 ——
    名字是清单主键，改名等于「删一条 + 加一条」，界面该显式做这两步）。
    没给 `base` 时是「新增」，`name` / `host` 必须有。
    """
    if not isinstance(spec, dict):
        raise AgentError("连接配置需为 JSON 对象（spec）")
    spec = dict(spec)
    # 允许界面用一个 `target`（`user@host`）字段，与 CLI 的写法对齐。
    if "target" in spec:
        target = _spec_text(spec, "target", None)
        user_part, _, host_part = target.rpartition("@")
        spec.setdefault("user", user_part)
        spec.setdefault("host", host_part or target)
    name = _spec_text(spec, "name", base, "")
    if base is not None:
        name = base.name  # 改名不走 update（见 docstring）
    if not name:
        raise AgentError("连接缺少 name")
    # 逐项校验前先看字段类型 —— 类型错的报错要比「host 格式非法」更早、更准。
    _spec_text(spec, "host", base, "")
    _spec_int(spec, "port", base, 22)
    _spec_int(spec, "connect_timeout", base, 10)
    _spec_int(spec, "command_timeout", base, 120)
    _spec_flag(spec, "allow_write", base.allow_write if base is not None else False)
    _spec_flag(spec, "allow_shell", base.allow_shell if base is not None else False)
    conn = make_connection(
        name,
        _spec_text(spec, "host", base, ""),
        user=_spec_text(spec, "user", base, ""),
        port=_spec_int(spec, "port", base, 22),
        root=_spec_text(spec, "root", base, "/"),
        identity_file=_spec_text(spec, "identity_file", base, ""),
        proxy_jump=_spec_text(spec, "proxy_jump", base, ""),
        host_key_policy=_spec_text(spec, "host_key_policy", base, "accept-new"),
        connect_timeout=_spec_int(spec, "connect_timeout", base, 10),
        command_timeout=_spec_int(spec, "command_timeout", base, 120),
        allow_write=_spec_flag(spec, "allow_write", base.allow_write if base is not None else False),
        allow_shell=_spec_flag(spec, "allow_shell", base.allow_shell if base is not None else False),
        note=_spec_text(spec, "note", base, ""),
    )
    # 最后收一次缺口：新增时 host 不能为空（`make_connection` 的校验会拦格式，
    # 但空串在 `_HOST_RE` 下同样非法 —— 这条只为把报错说成人话）。
    if not conn.host:
        raise AgentError("连接缺少 host（形如 `user@host` 或 `host`）")
    return conn


def _connection_view(conn: RemoteConnection, store: RemoteStore) -> dict[str, Any]:
    """单条连接的对外视图（与清单端点同形，供增 / 改 / 权限位端点复用）。"""
    return {
        **conn.to_dict(),
        "display": conn.display,
        "default": conn.name == store.default_name,
        "problems": conn.problems(),
    }


# 远端条目类型 → 界面的可读分类。`?` 是「远端没有 python3/find，拿不到类型」
# 那一档（见 transport.list_dir），如实标成 unknown，不假装是文件或目录。
_REMOTE_KINDS = {"d": "dir", "f": "file", "l": "link"}


def _diagnostic_api(item: dict[str, Any]) -> dict[str, Any]:
    """把 LSP 的 0 基位置转成对前端友好的形态（行号 1 基，列 0 基）。"""
    return {
        "line": int(item.get("line") or 0) + 1,
        "char": int(item.get("char") or 0),
        "endLine": int(item.get("endLine") or 0) + 1,
        "endChar": int(item.get("endChar") or 0),
        "severity": str(item.get("severity") or "info"),
        "message": str(item.get("message") or ""),
        "source": str(item.get("source") or ""),
        "code": str(item.get("code") or ""),
    }


def _apply_lsp_edits(text: str, edits: list[dict[str, Any]]) -> str | None:
    """按 LSP 的 `TextEdit` 列表改一份正文。

    位置是 0 基 (line, character)。**从后往前应用**，否则前面的编辑会挪动
    后面编辑的坐标。偏移量在**同一份原文**上一次性算好，因此天然免疫这个问题 ——
    实际上是「先算全部区间，再按倒序一次性拼接」。

    区间重叠（服务器给了互相冲突的编辑）返回 None，宁可整批不应用，
    也不要产出一份半对半错的代码。
    """
    lines = text.split("\n")
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)

    def pos_of(line: int, char: int) -> int:
        index = max(0, min(int(line), len(lines) - 1))
        base = offsets[index]
        return base + max(0, min(int(char), len(lines[index])))

    spans: list[tuple[int, int, str]] = []
    for edit in edits or []:
        if not isinstance(edit, dict):
            continue
        start = pos_of(edit.get("startLine", 0), edit.get("startChar", 0))
        end = pos_of(edit.get("endLine", 0), edit.get("endChar", 0))
        if end < start:
            start, end = end, start
        spans.append((start, end, str(edit.get("newText") or "")))

    spans.sort(key=lambda span: span[0], reverse=True)
    for index in range(len(spans) - 1):
        # 倒序排列下，前者起点更靠后；若它起点落在后者的区间内即重叠
        if spans[index][0] < spans[index + 1][1]:
            return None

    out = text
    for start, end, new_text in spans:
        out = out[:start] + new_text + out[end:]
    return out


@dataclass
class _PendingApproval:
    """一次等待用户决定的审批。Agent 线程挂在这个 gate 上。"""

    id: str
    request: ApprovalRequest
    gate: threading.Event
    decision: str = "deny"
    # 该审批对应的草稿 token（P1-2）。同一个审批重复请求预览时复用同一个 token ——
    # 每点一次就发一个新 token，等于让「已失效」这件事永远追不上新发的凭证。
    draft_token: str = ""


class Session:
    """一个工作区对应一个会话。线程安全。"""

    def __init__(
        self,
        config: Config,
        *,
        bus: EventBus | None = None,
        registry: ToolRegistry | None = None,
        client: LLMClient | None = None,
        approval_timeout: int = DEFAULT_APPROVAL_TIMEOUT,
        store: SessionStore | None = None,
    ) -> None:
        self.config = config
        self.bus = bus or EventBus()
        self.registry = registry or build_default_registry(
            lsp=_lsp_registration(config),
            # 子 Agent（M3）：默认关 —— 不配置时工具清单逐字节不变。
            # 显式传是因为 host 侧自建 registry，绕过 Agent 的默认注册调用。
            subagent=config.subagent_enabled,
            # 联网检索（M7）：默认关；恒 REVIEW + 结果 wrap_untrusted（tools/web.py）。
            web=config.web_enabled,
            # 远程设备（M10）：默认关 —— 不配置时工具清单逐字节不变。
            # 写/执行类恒 REVIEW，只读类恒 SAFE（remote_grep 除外）。
            remote=config.remote_enabled,
        )
        self.approval_timeout = approval_timeout
        self._client = client  # resume 重建 Agent 时要用同一个客户端
        # 会话持久化（A2）：store 由上层（HostServer）按配置注入。
        # None = 关闭落盘；store 自身的损坏容忍见 store.py。
        self.store = store

        self._lock = threading.RLock()
        self._state: str = P.STATE_IDLE
        self._run_id: str = ""
        self._run_seq: int = 0
        # 本轮的「用户消息 seq」锚点（P3 逐条回退）：提交时按库里的 max_seq 预测，
        # run 结束时用它给本轮检查点打标。0 = 未知（会话未持久化）。
        self._run_user_seq: int = 0
        self._interrupt = threading.Event()
        self._pending: dict[str, _PendingApproval] = {}
        self._thread: threading.Thread | None = None
        self._notices: list[str] = []
        self._answer: str = ""
        self._error: str = ""
        # 行内改写草稿缓存（D8）：token -> 服务端缓存的改写结果。
        # 前端回传 token 而非 new_text —— 否则伪造整段内容即可绕过 hunk 语义。
        self._rewrites: dict[str, dict[str, Any]] = {}
        # 重命名草稿缓存（E5）：token -> 服务端缓存的逐文件编辑结果。
        # 与 `_rewrites` 同一理由：前端回传正文一律不被信任，token 才是唯一凭据。
        self._renames: dict[str, dict[str, Any]] = {}
        # 审批期草稿缓存（P1-2）：token -> {approvalId, files{rel: 正文}, primary, ...}。
        # **只存内存、只活一次审批**（审批解决即整体丢弃）。它是全项目唯一一个
        # 「内容由模型产生、却尚未落盘」的对外读取面，因此边界必须窄：
        #   · 没有这个 token 就读不到（不可枚举，token 为随机 hex）；
        #   · 审批一结束 token 立即失效（不留「批完还能翻旧稿」的窗口）；
        #   · 正文只在内存里，任何时候都不写盘（写盘只能走结构化补丁）。
        self._drafts: dict[str, dict[str, Any]] = {}
        # 补全去重缓存（D3+）：digest -> (monotonic_ts, payload)。仅内存、不落盘、
        # 不跨会话；命中直接复用，省掉一次模型调用（详见 complete()）。
        self._completions: dict[str, tuple[float, dict[str, Any]]] = {}
        # 会话标题（A2）取用户原文，而不是带引用块的前置提示 ——
        # 否则每条会话的标题都会变成「[引用上下文] 用户引用了 N 项内容」。
        self._title_hint: str = ""

        # 测试树（K/P2-14）：收集结果只缓存不落盘，最近一次结果同理。
        # 两把锁分工明确 —— `_tests_lock` 护缓存，`_tests_run_lock` 保证
        # 「同一时刻只跑一批」（见 tests_run 的说明）。
        self._tests_lock = threading.Lock()
        self._tests_cache: Any = None
        self._tests_run_lock = threading.Lock()
        self._tests_seq = 0
        self._tests_last: dict[str, Any] | None = None
        self._tests_thread: threading.Thread | None = None

        # 交互式终端（M/P1-7）：只记「本会话开过哪些终端」，会话收尾时按 id 关掉。
        # 终端进程由进程内单例管理（见 terminal/manager.py）；这里不持管理器实例，
        # 避免切工作区时把别的会话的终端一起误关。
        self._term_ids: set[str] = set()

        # 治理面接入（M3）：桌面端后端与 CLI 走同一装配入口。
        # 用户在环境变量里配置 VIBE_SERVER_URL 并用 CLI 登录过，
        # 桌面端启动时就自动获得策略收紧、网关模型与审计上报 ——
        # 不需要在桌面端再做一套登录 UI（那是 M3 之后的增强）。
        self.governance = None
        # 客户端自动更新（M9）：懒装配、按 options 变化重建（见 _update_service）。
        self._update: UpdateService | None = None
        constraints = None
        if client is None and config.server_url:
            self.governance = wire(config)
            for note in self.governance.notices:
                self._record_notice(note)
                self.bus.publish(P.event(P.RUN_NOTICE, runId="", text=note))
            config = self.governance.config
            client = self.governance.client
            constraints = self.governance.constraints

        self.agent = Agent(
            config=config,
            registry=self.registry,
            client=client,
            constraints=constraints,
            hooks=_SessionHooks(self),
            approver=self._approve,
            should_stop=self._interrupt.is_set,
        )
        # 跨会话记忆（P2-15）：与 Agent 共用同一个库实例 —— `/api/memories`
        # 与 Agent 每轮注入必须看到同一份数据，各建一个连接迟早会读写不一致。
        # 关掉 VIBE_MEMORY 时 Agent.memory 为 None，会话层同样不碰。
        self.memory: MemoryStore | None = self.agent.memory
        self._started_at = time.time()
        record_session_start(self.agent.audit, config)

        # MCP 客户端（L/P1-6）：在 Agent 建好之后并入 —— 这样 `mcp.server_start`
        # 有审计落点（Agent 的 audit）。未启用时 `_mcp_registration` 返回 None，
        # 一行都不做。某个 server 起不来只影响它自己（见 merge_mcp）。
        mcp_servers = _mcp_registration(config)
        if mcp_servers:
            merge_mcp(
                self.registry, mcp_servers, audit=self.agent.audit, manager=get_mcp_manager()
            )

        # 增量索引 / 文件监听（J）：watch 挂在**会话**上而不是进程上 ——
        # 会话与工作区是一对一的（切工作区会换掉整个 Session），生命周期天然吻合。
        # 参数取自 agent.config 而不是入参 config：治理面可能收紧过配置，
        # 而真正读它的是 Agent 及其工具（semantic_search）。
        self.watcher: IndexWatcher | None = None
        # 远程工作区（M13）：watch 监听的是本机目录，而此刻代码在远端 ——
        # 起一个永远扫不到东西的线程毫无意义，还会让 preview.changed 显得像是好的。
        if self.agent.config.index_watch and not self._is_remote_workspace():
            self.watcher = IndexWatcher(
                self.agent.config.workspace,
                # 用 PathGuard.relative 而不是默认实现：必须与 semantic_search
                # 建索引时的相对路径口径**逐字符一致**，否则增量会找不到旧块。
                self.agent.ctx.paths.relative,
                interval=self.agent.config.index_watch_interval,
                max_files=self.agent.config.semantic_max_files,
                # 检索增强（M3+）同样要传：watcher 与 semantic_search 必须 pin
                # 同一个索引形状的实例，否则增量对检索不可见。
                enhanced=self.agent.config.retrieval_enhanced,
                audit=self.agent.audit,
                # 预览自动刷新（P0）：只广播「变化路径」，不带内容。关掉索引监听
                # （VIBE_INDEX_WATCH=0）时自然没有这个能力，界面如实提示而不假装能用。
                on_change=self._on_files_changed,
            )
            self.watcher.start()

        # 后台 / 并行 Agent（M9-D）：默认关 —— `VIBE_ASYNC_AGENT=0` 时任务池为
        # None，`/api/tasks` 端点不暴露、行为与未实现前**逐字节一致**。启用后
        # 用户本人可经界面发起后台任务（只读工具面、审计链共用），结果异步回传。
        # 任务池挂在会话上（与 watcher 同生命周期）—— 切工作区换掉整个 Session
        # 时，旧任务池随 close() 一起收掉。
        self.tasks: AgentTaskPool | None = None
        if self.agent.config.async_agent_enabled:
            from ..async_agent import AgentTaskPool

            self.tasks = AgentTaskPool(
                parent=self.agent,
                max_workers=self.agent.config.async_agent_max,
                timeout=self.agent.config.async_agent_timeout,
                on_state=self._on_task_state,
            )

        # 定时任务调度（M10）：默认关 —— `VIBE_SCHEDULER=0` 时调度器为 None，
        # `/api/schedules` 端点不暴露、无线程、无库文件，行为与未实现前
        # **逐字节一致**。**依赖 M9-D 池**：池未启用时不建调度器并明说原因
        #（fail-closed，不静默降级）。调度器挂在会话上（与 watcher / 任务池同
        # 生命周期）—— 切工作区换掉整个 Session 时，旧调度器随 close() 一起收掉。
        self.scheduler: Scheduler | None = None
        if self.agent.config.scheduler_enabled:
            if self.tasks is None:
                self._notices.append(
                    "定时任务未启动：需要先开启 VIBE_ASYNC_AGENT=1"
                    "（定时任务只投只读后台任务）。"
                )
            else:
                from ..scheduler import JobStore, Scheduler

                self.scheduler = Scheduler(
                    pool=self.tasks,
                    # 落 <workspace>/.vibe/scheduler.db（宿主资产，同 checkpoints）
                    store=JobStore(self.agent.config.scheduler_db_path),
                    audit=self.agent.audit,
                    tick=self.agent.config.scheduler_tick,
                    max_per_tick=self.agent.config.scheduler_max_per_tick,
                    max_jobs=self.agent.config.scheduler_max_jobs,
                    catch_up=self.agent.config.scheduler_catch_up,
                    on_state=self._on_job_state,
                )
                # start() 内做「装载定义 + 重启补跑」，再起常驻线程。
                self.scheduler.start()

        # 远程设备工作面（M11）：默认关 —— `VIBE_REMOTE=0` 时不暴露任何远程
        # 端点、界面不渲染「远程」面板，行为与未实现前**逐字节一致**。状态层
        # **惰性建**（首次用到才分配），与 watcher / 任务池同生命周期 —— 切工作区
        # 换掉整个 Session 时，活动连接的传输随 close() 一起收掉。
        self._remote: RemoteSessions | None = None

        # 远端预览的转发通道（P3）：与 `_remote` 同生命周期、同样惰性建。
        # 单独一层而不是塞进 RemoteSessions —— 前端连接状态（在哪个目录浏览）
        # 与「开了几条转发」是两件事：断开连接不该顺手掐掉预览，反之亦然。
        self._tunnels: TunnelManager | None = None
        # 转发状态机上一次的取值。只用于「跃迁才落审计」——
        # 每次状态回调都记一条会把审计刷成噪声，反而看不出什么时候真断过。
        self._tunnel_states: dict[str, str] = {}

        # 远程工作区（M13 / ADR-042）：这是在这次不对一成不变的本机模式下才做的事 ——
        # `remote_workspace` 非空即表示「本次会话的工作区在一台远端机器上」，
        # 于是进来就把那条连接打开：一次只活动一条连接（M11/M12 纪律）下，
        # 「进得了这个工作区」与「那条连接可用」本来就是同一件事。
        #
        # 失败**刻意不致命**（`Config.remote_workspace` 的连通性在
        # `switch_workspace(remote=…)` 里已经验证过一次；这里再失败多半是
        # ControlMaster 过期之类的偶发）—— 让界面能显示「远端连不上：<原因>」
        # 比让整个会话起不来有用得多。
        self._remote_error: str = ""
        if self.agent.config.remote_enabled and str(
            getattr(self.agent.config, "remote_workspace", "") or ""
        ):
            self._open_remote_workspace()

    def _open_remote_workspace(self) -> None:
        """打开远程工作区指向的那条连接，并把浏览位置落到子目录上。

        任何异常都收敛进 `_remote_error` —— Session 构造期的一次网络抖动不该
        演变成「界面打不开」。真正的冰冻点是此刻的状态既不谎称可用、
        也不假装无事：`remote_error()` 会把原因带出去。
        """
        try:
            connection, sub = parse_ns_root(self.agent.config.remote_workspace)
            if not connection:
                return
            sessions = self.remote_sessions()
            sessions.open(connection)
            if sub:
                sessions.chdir(sub)
        except Exception as exc:  # noqa: BLE001 — 见上：这里失败是**预期内的一种状态**
            self._remote_error = str(exc)

    def _is_remote_workspace(self) -> bool:
        """是不是远程工作区会话（ADR-042）。本机模式恒 False。

        这个判定要在 **Agent 建好之后**才能用 —— 这里读的是 `agent.config`：
        治理面可能收紧过配置，真正生效的是那一份。

        真源在 `remote/degrade.py`（只依赖 Config，供提示词与端点共用）。
        """
        return is_remote_workspace(self.agent.config)

    def remote_error(self) -> str:
        """远程工作区连接失败的原因（空串 = 正常）。给端点与界面显示用的。"""
        return self._remote_error

    def _on_task_state(self, payload: dict[str, Any]) -> None:
        """后台任务状态变化 → 广播 `task.state` 事件给界面（M9-D）。"""
        self.bus.publish(P.event(P.TASK_STATE, **payload))

    def _on_job_state(self, payload: dict[str, Any]) -> None:
        """定时任务状态变化 → 广播 `schedule.state` 事件给界面（M10）。"""
        self.bus.publish(P.event(P.SCHEDULE_STATE, **payload))

    def _on_files_changed(self, paths: list[str]) -> None:
        """文件增删改 → 广播 `preview.changed` 事件（P0）。

        唯一用途是让已打开的预览标签决定要不要重载 —— 改动可能来自 Agent，
        也可能来自用户在编辑器里手存，所以它不属于 `run.*`。

        刻意**只带相对路径**：内容留在本机（ADR-006），这条事件也不进审计链
        （落链的只有计数类事件）。截断策略：一次 poll 可能扫出成百上千条变化
        （切分支、批量生成），路径最多带 200 条并置 `truncated`，界面改为刷新
        全部预览标签 —— 这比让「恰好排在 201 位之后」的那个文件静默不刷新诚实。
        """
        if not paths:
            return
        ordered = sorted(dict.fromkeys(str(p) for p in paths))
        limit = 200
        self.bus.publish(
            P.event(
                P.PREVIEW_CHANGED,
                paths=ordered[:limit],
                count=len(ordered),
                truncated=len(ordered) > limit,
            )
        )

    def close(self) -> None:
        """释放会话持有的进程资源（索引 watch 线程 + 交互式终端）。

        切换工作区时由 HostServer 调用：旧 Session 可以丢弃，但它的后台线程
        与子进程不能跟着「丢」在进程里 —— 那会留下一串持续运行、无人使用的
        终端进程（见 terminal/manager 的 atexit 兜底）。
        """
        watcher, self.watcher = self.watcher, None
        if watcher is not None:
            watcher.stop()
        # 定时任务调度（M10）：先停调度线程，再关掉它投递的任务池
        # （否则调度线程可能在池已关后再 submit）。
        scheduler, self.scheduler = self.scheduler, None
        if scheduler is not None:
            scheduler.stop()
        # 后台任务池（M9-D）：会话收尾时一起关掉（wait=False，进程内任务随会话结束）。
        tasks, self.tasks = self.tasks, None
        if tasks is not None:
            tasks.shutdown()
        # 记忆库（P2-15）：切工作区会换掉整个 Session，旧库连接必须关掉 ——
        # Windows 下未关闭的 sqlite 会锁住库文件（新会话连同一个用户级库时踩到）。
        if self.memory is not None:
            self.memory.close()
        # 远程设备工作面（M11）：关掉活动连接的传输（含复用中的 ControlMaster），
        # 不留悬挂的 ssh 子进程。`close()` 里由 RemoteSessions 精确关**这一条**。
        remote, self._remote = self._remote, None
        if remote is not None:
            remote.close()
        # 远端预览转发（P3）：会话收尾必须收干净 —— 它们是**本机监听口**，
        # 留着比留着 ssh 子进程更糟（下次启动会撞「端口被占」）。
        tunnels, self._tunnels = self._tunnels, None
        if tunnels is not None:
            tunnels.close_all()
        term_ids, self._term_ids = set(self._term_ids), set()
        if term_ids:
            manager = get_terminal_manager()
            for term_id in term_ids:
                try:
                    manager.close(term_id)
                except TerminalError:
                    pass

    def index_status(self) -> dict[str, Any]:
        """`GET /api/index/status` 的载荷：索引健康度（只读，绝不建索引）。"""
        watcher = self.watcher
        if watcher is not None:
            return watcher.status()
        config = self.agent.config
        index = peek_index(
            config.workspace,
            config.semantic_max_files,
            WINDOW_LINES,
            OVERLAP_LINES,
            config.retrieval_enhanced,
        )
        return {
            "watching": False,
            "interval": int(config.index_watch_interval),
            # watch 关时索引只在第一次 semantic_search 之后才存在，没有就如实回 0。
            "files": len(index.files) if index is not None else 0,
            "chunks": len(index.chunks) if index is not None else 0,
            "edges": index.graph.edge_count() if index is not None else 0,
            "lastUpdate": 0,
            "pending": 0,
        }

    # ------------------------------------------------------------------ 后台任务（M9-D）
    def task_submit(self, prompt: str) -> dict[str, Any]:
        """`POST /api/tasks`：用户本人发起一个后台任务，返回 task_id。"""
        if self.tasks is None:
            raise AgentError("后台任务未启用（VIBE_ASYNC_AGENT=0）")
        try:
            task_id = self.tasks.submit(prompt)
        except ValueError as exc:
            raise AgentError(str(exc)) from exc
        return {"ok": True, "taskId": task_id}

    def task_list(self) -> list[dict[str, Any]]:
        """`GET /api/tasks`：后台任务列表（只回摘要，不含正文）。"""
        return self.tasks.list_tasks() if self.tasks is not None else []

    def task_status(self, task_id: str) -> dict[str, Any] | None:
        """`GET /api/tasks/{id}`：单个任务状态 / 结果（完成后含正文）。"""
        if self.tasks is None:
            return None
        return self.tasks.result(task_id)

    def task_cancel(self, task_id: str) -> bool:
        """`POST /api/tasks/{id}/cancel`：请求取消。"""
        if self.tasks is None:
            return False
        return self.tasks.cancel(task_id)

    # ------------------------------------------------------------------ 定时任务（M10）
    def _require_scheduler(self) -> Scheduler:
        """取调度器；未启用一律报错（host 层转 400，fail-closed）。"""
        if self.scheduler is None:
            raise AgentError(
                "定时任务未启用（VIBE_SCHEDULER=0，或未开启 VIBE_ASYNC_AGENT=1）"
            )
        return self.scheduler

    def job_create(
        self, name: str, schedule: str, prompt: str, *, enabled: bool = True
    ) -> dict[str, Any]:
        """`POST /api/schedules`：用户本人新建一个定时任务。"""
        from ..scheduler import ScheduleError

        scheduler = self._require_scheduler()
        try:
            job = scheduler.add_job(name, schedule, prompt, enabled=enabled)
        except (ScheduleError, ValueError) as exc:
            raise AgentError(str(exc)) from exc
        return {"ok": True, "jobId": job.job_id}

    def job_list(self) -> list[dict[str, Any]]:
        """`GET /api/schedules`：任务列表（只回元数据，不含 prompt 正文）。"""
        return self.scheduler.list_jobs() if self.scheduler is not None else []

    def job_update(
        self,
        job_id: str,
        *,
        name: str | None = None,
        schedule: str | None = None,
        prompt: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any] | None:
        """`POST /api/schedules/{id}`：整体更新。不存在返回 None。"""
        from ..scheduler import ScheduleError

        scheduler = self._require_scheduler()
        try:
            job = scheduler.update_job(
                job_id, name=name, schedule=schedule, prompt=prompt, enabled=enabled
            )
        except (ScheduleError, ValueError) as exc:
            raise AgentError(str(exc)) from exc
        return job.describe() if job is not None else None

    def job_toggle(self, job_id: str, enabled: bool) -> dict[str, Any] | None:
        """启用 / 停用一个任务。不存在返回 None。"""
        scheduler = self._require_scheduler()
        job = scheduler.set_enabled(job_id, enabled)
        return job.describe() if job is not None else None

    def job_delete(self, job_id: str) -> bool:
        """`POST /api/schedules/{id}/delete`：删除。"""
        return self._require_scheduler().remove_job(job_id)

    def job_run_now(self, job_id: str) -> str | None:
        """`POST /api/schedules/{id}/run`：立即投一次后台任务。"""
        return self._require_scheduler().run_now(job_id)

    def schedule_status(self) -> dict[str, Any] | None:
        """调度器状态（只读）：未启用返回 None。"""
        return self.scheduler.status() if self.scheduler is not None else None

    # ------------------------------------------------------------------ 远程设备工作面（M11）
    # 这一层是**用户面**（ADR-011：用户本人动作），与 Agent 侧的 8 个
    # `tool.remote_*` 工具是两条互不干扰的路：工具面重启始终走审批漏斗，
    # 用户面是用户在「远程」面板里亲手做的动作，不需要重复审批 ——
    # 但写入路径**必须**满足留底 + 乐观锁 + 受控覆盖（见 save_remote_file）。
    # 清单读写一律复用 `remote/store.py` 的原子写，不另造一条配置通道。
    def _require_remote(self) -> None:
        """门控：未启用一律报错（host 层转 400，fail-closed，不静默降级）。"""
        if not self.agent.config.remote_enabled:
            raise AgentError("远程工作面未启用（VIBE_REMOTE=0）")

    def _remote_store(self) -> RemoteStore:
        """载入连接清单并顺手校验每条 —— 一份手动改坏的清单不该等到「用的时候」才炸。"""
        store = RemoteStore.load(store_path_for(self.agent.config))
        for conn in store.list():
            conn.validate()
        return store

    def remote_sessions(self) -> RemoteSessions:
        """取会话级远程状态层（惰性建）。

        `denied_patterns` / `allow_sensitive` 直接取自本地 PathGuard ——
        治理面下发的路径拒绝清单与敏感读取策略对远端同样生效，不另开策略通道。
        """
        self._require_remote()
        sessions = self._remote
        if sessions is None:
            paths = self.agent.ctx.paths
            sessions = RemoteSessions(
                self.agent.config,
                allow_sensitive=paths.allow_sensitive,
                denied_patterns=tuple(paths.denied_patterns),
            )
            self._remote = sessions
        return sessions

    def _remote_audit(
        self,
        action: str,
        conn: RemoteConnection | None,
        *,
        ok: bool = True,
        ms: int = 0,
    ) -> None:
        """记一次远程连接管理动作（只记元数据，host / 密钥路径不出机，ADR-006）。"""
        self.agent.audit.record(
            "remote.connection",
            connection=conn.name if conn is not None else "",
            action=action,
            allow_write=bool(conn.allow_write) if conn is not None else False,
            allow_shell=bool(conn.allow_shell) if conn is not None else False,
            ok=ok,
            ms=ms,
        )

    def _publish_remote_state(self) -> None:
        """把当前远程状态广播给界面（`remote.state`）。未建状态层则什么都不发。"""
        sessions = self._remote
        if sessions is None:
            return
        self.bus.publish(P.event(P.REMOTE_STATE, **sessions.snapshot()))

    def remote_state(self) -> dict[str, Any]:
        """`GET /api/remote/state`：活动连接 + 浏览位置 + 权限位 + 能力降级（**不含正文**）。"""
        sessions = self.remote_sessions()
        return {"enabled": True, **sessions.snapshot()}

    def remote_connections(self) -> dict[str, Any]:
        """`GET /api/remote/connections`：清单 + 默认项 + 权限位 + 本机 ssh 探测结论。"""
        self._require_remote()
        store = self._remote_store()
        active = self._remote.connection if self._remote is not None else None
        return {
            "enabled": True,
            "configPath": str(store.path),
            "default": store.default_name,
            "active": active.name if active is not None else "",
            "ssh": _ssh_probe(),
            "connections": [
                {
                    **conn.to_dict(),
                    "display": conn.display,
                    "default": conn.name == store.default_name,
                    # 结构问题如实列出（界面据此在原表单上标红），而不是等保存时才报。
                    "problems": conn.problems(),
                }
                for conn in store.list()
            ],
        }

    def remote_connection_add(self, spec: object) -> dict[str, Any]:
        """`POST /api/remote/connections`：新增一条连接。

        **写前校验**：字段结构（`problems()`）与私钥存在性（`validate_local()`）
        都必须在落盘之前过 —— 一份写进清单却连不上的配置，排查时会被误当成网络问题。
        """
        self._require_remote()
        store = self._remote_store()
        conn = _connection_from_spec(spec, base=None)
        conn.validate_local()
        saved = store.upsert(conn, make_default=bool(_spec_flag(spec, "default", False)))
        self._remote_audit("added", saved)
        self._publish_remote_state()
        return {"ok": True, "connection": _connection_view(saved, store)}

    def remote_connection_update(self, name: str, spec: object) -> dict[str, Any]:
        """`POST /api/remote/connections/{name}`：整体更新（缺省字段沿用原值）。"""
        self._require_remote()
        store = self._remote_store()
        base = store.require(name)
        conn = _connection_from_spec(spec, base=base)
        conn.validate_local()
        saved = store.upsert(conn)
        self._refresh_active_connection(name)
        self._remote_audit("updated", saved)
        return {"ok": True, "connection": _connection_view(saved, store)}

    def remote_connection_remove(self, name: str) -> dict[str, Any]:
        """`POST /api/remote/connections/{name}/delete`：删除一条连接。

        删默认连接后**不自动选接任者**（`store.remove` 的既有语义）——
        静默改默认会让下一次「不带名字」的操作打到用户没预期的设备上。
        """
        self._require_remote()
        store = self._remote_store()
        conn = store.require(name)
        store.remove(name)
        # 删掉的正是在用的那条：断掉它的传输，但**保留**改动台账（审查线索）。
        if self._remote is not None:
            self._remote.disconnect(name)
        self._remote_audit("removed", conn)
        self._publish_remote_state()
        return {"ok": True, "removed": name, "default": store.default_name}

    def remote_connection_default(self, name: str) -> dict[str, Any]:
        """`POST /api/remote/connections/{name}/default`：设默认连接。"""
        self._require_remote()
        store = self._remote_store()
        conn = store.require(name)
        store.set_default(name)
        self._remote_audit("default", conn)
        return {"ok": True, "default": store.default_name}

    def remote_connection_permissions(
        self,
        name: str,
        *,
        allow_write: bool | None = None,
        allow_shell: bool | None = None,
    ) -> dict[str, Any]:
        """`POST /api/remote/connections/{name}/permissions`：改两个权限位。

        权限位是**热**开关：改完立刻对活动连接生效（`_refresh_active_connection`
        会把连接对象换成新值），不必重开面板。
        """
        self._require_remote()
        changes: dict[str, Any] = {}
        if allow_write is not None:
            changes["allow_write"] = bool(allow_write)
        if allow_shell is not None:
            changes["allow_shell"] = bool(allow_shell)
        if not changes:
            raise AgentError("permissions 需至少给出 allow_write 或 allow_shell 一项")
        store = self._remote_store()
        conn = store.require(name)
        saved = store.upsert(conn.with_changes(**changes))
        self._refresh_active_connection(name)
        self._remote_audit("permissions", saved)
        self._publish_remote_state()
        return {"ok": True, "connection": _connection_view(saved, store)}

    def _refresh_active_connection(self, name: str) -> None:
        """若 `name` 正是活动连接，按新值刷新会话状态（权限位 / 目标随时可能变过）。

        经 `RemoteSessions.open()` 走一遍：同一条链路只换连接对象（不重握手），
        target / root / 密钥变了才真正重连 —— 这正是 `_swap` 里那条分叉的意义。
        """
        sessions = self._remote
        if sessions is None or sessions.active() is None:
            return
        if sessions.active().name != name:  # type: ignore[union-attr]
            return
        try:
            sessions.open(name)
        except RemoteError as exc:
            # 刷新失败不该把「改配置」这个动作本身判成失败：配置已落盘，
            # 只是暂时连不上。如实提示，让界面提示用户去「测试」里看细节。
            self._record_notice(f"远程连接 {name} 刷新失败：{exc}")
        self._publish_remote_state()

    def remote_connection_test(self, name: str = "") -> dict[str, Any]:
        """`POST /api/remote/connections/{name}/test`：测连通性 + 环境 + **能力降级**。

        **不落盘、不改清单、不改活动连接** —— 只探一次，把 `report()` 原样回给界面。
        连不上返回 `ok:false` 而不是抛错：连不上是**正常结果**，界面上要能解释原因。
        """
        self._require_remote()
        store = self._remote_store()
        conn = store.resolve(name or None)
        started = time.monotonic()
        try:
            workspace = open_workspace(
                conn,
                allow_sensitive=self.agent.ctx.paths.allow_sensitive,
                denied_patterns=tuple(self.agent.ctx.paths.denied_patterns),
            )
            caps = workspace.capabilities()
        except RemoteError as exc:
            ms = int((time.monotonic() - started) * 1000)
            self._remote_audit("test", conn, ok=False, ms=ms)
            return {
                "ok": False,
                "connection": conn.name,
                "display": conn.display,
                "kind": getattr(exc, "kind", "remote"),
                "reason": str(exc),
                "ms": ms,
            }
        ms = int((time.monotonic() - started) * 1000)
        self._remote_audit("test", conn, ok=True, ms=ms)
        return {
            "ok": True,
            "connection": conn.name,
            "display": conn.display,
            "root": workspace.guard.root_resolved,
            "ms": ms,
            "report": workspace.report(),
            "capabilities": {
                "os": caps.os_name,
                "arch": caps.arch,
                "kernel": caps.kernel,
                "user": caps.user,
                "cwd": caps.cwd,
                "git": caps.git_version,
                "python": caps.python_version,
                "multiplexed": caps.multiplexed,
                "root_writable": caps.root_writable,
                "degraded": list(caps.degraded),
            },
        }

    # ------------------------------------------------------------------ 远程工作区根（M12）
    # 「把远端目录加入工作区」是**用户本人动作**（与连接管理同一条纪律），
    # 但它的后果落在模型侧：登记之后，模型用本地同名文件工具写
    # `@remote/<连接名>/<子目录>` 就能直接操作远端（见 remote/routing.py）。
    # 因此根变更后必须让**当轮会话**的提示词立刻知道新根 —— 否则会出现
    # 「用户刚加完，模型却说这个目录没加入工作区」的错位。
    def _roots_payload(self, store: RemoteStore) -> dict[str, Any]:
        """根清单的对外视图（含纯文本校验结论与**被跳过的坏根**原因）。

        `ok/reason` 是**纯文本校验**结论（连接是否存在、sub 写法是否合法），
        **不探远端** —— 离线也能把根加好，不可达留到展开目录时如实报错。
        """
        roots: list[dict[str, Any]] = []
        for root in store.roots:
            reason = validate_root(root, store)
            conn = store.get(root.connection)
            roots.append(
                {
                    "connection": root.connection,
                    "sub": root.sub,
                    "nsPath": root.ns_path(),
                    "label": root.label or f"{root.connection}:{root.sub or '.'}",
                    "target": getattr(conn, "target", "") if conn is not None else "",
                    "ok": not reason,
                    "reason": reason,
                }
            )
        return {"roots": roots, "problems": list(store.problems)}

    def _after_roots_change(self, store: RemoteStore, action: str, root: RemoteRoot) -> None:
        """根增删后的统一收尾：审计 → 失效路由快照 → 重算提示词 → 广播状态。"""
        self._remote_audit(action, store.get(root.connection))
        router = getattr(self.agent, "remote_router", None)
        if router is not None:
            router.invalidate()
        refresh = getattr(self.agent, "refresh_system_prompt", None)
        if callable(refresh):
            refresh()
        self._publish_remote_state()

    def remote_roots(self) -> dict[str, Any]:
        """`GET /api/remote/roots`：已加入工作区的远端根清单（只读）。

        **不读文件正文** —— 只回命名空间路径与校验结论，供界面渲染工作区树的远端根。
        """
        self._require_remote()
        store = self._remote_store()
        return {"enabled": True, **self._roots_payload(store)}

    def remote_root_add(
        self, connection: str, sub: str = "", label: str = ""
    ) -> dict[str, Any]:
        """`POST /api/remote/roots`：把一个远端目录加入工作区。

        校验是**纯文本**的（连接是否存在、sub 是否越出 root），落盘经既有原子写。
        未知连接 / 写法非法 / 重复登记一律人话报错（400），**不静默成功**。
        """
        self._require_remote()
        store = self._remote_store()
        root = store.add_root(connection, sub, label)
        self._after_roots_change(store, "root_add", root)
        return {"ok": True, **self._roots_payload(store)}

    def remote_root_remove(self, connection: str, sub: str = "") -> dict[str, Any]:
        """`POST /api/remote/roots/delete`：把一个远端目录移出工作区。

        不存在则报错（不静默成功）—— 「以为移出了、其实还在」会让模型继续
        写一个用户以为已经撤掉的位置。
        """
        self._require_remote()
        store = self._remote_store()
        root = store.remove_root(connection, sub)
        self._after_roots_change(store, "root_remove", root)
        return {"ok": True, **self._roots_payload(store)}

    # ------------------------------------------------------------------ 远程文件面（M11-C）
    # 浏览 / 读取 / 保存 / 补丁都作用于**活动连接**（一次只活动一条，见
    # remote/session.py 的纪律）。写入恒走「留底 → 乐观锁 → 受控覆盖 → 回读校验」，
    # 与本地编辑面同一套纪律；正文与 diff 只走端点响应与界面，审计只记元数据。
    def remote_open(self, name: str = "") -> dict[str, Any]:
        """`POST /api/remote/open`：打开 / 切换活动连接。

        换连接 = 关旧开新（见 `RemoteSessions.open`），并广播 `remote.state`。
        连不上时 `RemoteError` 如实上抛（由 host 层按 kind 映射状态码），
        不做「假装连上了」的降级 —— 一个连不上的面板比一个空面板更误导。
        """
        sessions = self.remote_sessions()
        workspace = sessions.open(name or None)
        conn = sessions.active()
        self._remote_audit("open", conn)
        self._publish_remote_state()
        return {
            "ok": True,
            "root": workspace.guard.root_resolved,
            **sessions.snapshot(),
        }

    def remote_tree(self, path: str = "", connection: str = "") -> dict[str, Any]:
        """`GET /api/remote/tree?connection=&path=`：列某连接的目录（一层，懒加载）。

        不一次递归到底：远端往往是高延迟链路（内网/VPN），铺整棵树意味着每条
        子目录都要一次往返。界面按需展开，点一次拉一层。

        `connection` 给出时（M12：工作区树的远端根会指着**非活动**连接）：
        只在它**正是活动连接**时才列 —— 不在位则如实回「该连接未打开」，
        **不替用户偷偷换连接**（一次只活动一条连接是 M11 的纪律；静默换连接
        会把用户当前的浏览位置和改动台账串到另一台机器上）。
        `path` 与既有语义一致：**相对该连接 root** 的路径（根自己的 sub 就是起点）。
        """
        sessions = self.remote_sessions()
        wanted = str(connection or "").strip()
        if wanted:
            active = sessions.active()
            if active is None or active.name != wanted:
                return {
                    "ok": False,
                    "reason": "该连接未打开",
                    "connection": wanted,
                    "path": "",
                    "entries": [],
                    "degraded": [],
                }
        workspace = sessions.require_workspace()
        rel = sessions.chdir(path or "")
        entries = workspace.list_dir(rel or None)
        nodes = [
            {
                "name": str(entry.get("name") or ""),
                "path": f"{rel}/{entry.get('name')}" if rel else str(entry.get("name") or ""),
                # `?` 档（远端无 python3/find）如实标 unknown，不假装成文件或目录。
                "type": _REMOTE_KINDS.get(str(entry.get("type") or ""), "unknown"),
                "size": int(entry.get("size") or 0),
                # 符号链接单列标出：远端 jail 的第二道防线（realpath）只在读/写时
                # 生效，列目录阶段不跟随链，界面据此提示「点进去会不会越界」。
                "link": str(entry.get("type") or "") == "l",
            }
            for entry in entries
        ]
        nodes.sort(key=lambda node: (node["type"] != "dir", node["name"].lower()))
        conn = sessions.active()
        self._publish_remote_state()
        return {
            "ok": True,
            "connection": conn.name if conn is not None else "",
            "path": rel,
            "entries": nodes,
            "degraded": list(workspace.capabilities().degraded),
        }

    def remote_read_file(self, path: str) -> dict[str, Any]:
        """`GET /api/remote/file?path=`：读活动连接上的单个文件。

        正文只走**端点响应与界面**，一个字符都不进审计（ADR-006）；审计只记
        路径 / 字节数 / 指纹。返回的 `digest` 是保存时的乐观锁基准 ——
        与本地 `/api/file` 同一语义（磁盘已被改则拒绝覆盖）。
        """
        sessions = self.remote_sessions()
        workspace = sessions.require_workspace()
        result = workspace.read(path, max_bytes=MAX_SCAN_BYTES)
        # 二进制如实拒绝（与本地 read_file 同一条口径：头部出现 NUL 即视为二进制）。
        # 不做「latin-1 误解码出来的伪文本」预览 —— 一份看着像文本的乱码比明说
        # 「不支持预览」更容易让人误判，也更可能在界面上被当成真内容改回去。
        if b"\x00" in result.data[:4096]:
            raise AgentError(
                f"{result.rel_path} 看起来是二进制文件，界面不支持预览。"
            )
        conn = sessions.active()
        digest = hashlib.sha256(result.data).hexdigest()[:16]
        self._remote_file_audit(
            "read", conn, result.rel_path, bytes=result.size, sha256=digest
        )
        return {
            "connection": conn.name if conn is not None else "",
            "path": result.rel_path,
            "text": result.text,
            "size": result.size,
            "encoding": result.encoding,
            "newline": result.newline_label,
            "bom": result.had_bom,
            "lines": len(result.text.split("\n")),
            "digest": digest,
        }

    def remote_save(
        self, path: str, content: str, expected_digest: str = ""
    ) -> dict[str, Any]:
        """`POST /api/remote/save`：整文件保存（留底 + 乐观锁 + 回读校验 + 审计）。

        与本地 `save()` 同一套纪律，只是落点换成远端：
          - 执行中 409（不打断在跑的任务）；
          - `allow_write=off` 的连接**拒绝**（只读连接不该被写）；
          - `expected_digest` 不匹配 → `StaleFileError`（409），绝不静默覆盖；
          - 覆盖已有文件前先在本机留底，存不下就中止（不落盘）。
        """
        with self._lock:
            if self._state != P.STATE_IDLE:
                raise BusyError("任务执行中，无法保存远端文件，请等任务结束或先中断")
        sessions = self.remote_sessions()
        workspace = sessions.require_workspace()
        conn = workspace.conn
        if not conn.allow_write:
            raise AgentError(
                f"连接 {conn.name} 是只读的（allow_write=off），已拒绝写入。\n"
                "  如需修改该设备上的代码，请先在「远程」面板里为它打开可写。"
            )
        abs_path = workspace.guard.check_write(path)
        rel = workspace.guard.relative(abs_path)
        existed = workspace.transport.exists(abs_path)

        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        newline = "\n"
        old_text = ""
        old_digest = ""
        backed_up = False
        backup_path = ""
        if existed:
            old = workspace.read(rel, max_bytes=MAX_SCAN_BYTES)
            old_text = old.text
            old_digest = hashlib.sha256(old.data).hexdigest()[:16]
            newline = old.newline
            if expected_digest and old_digest != expected_digest:
                raise StaleFileError(
                    "远端文件已被改动（可能来自远端 Agent、其他协作者或外部编辑器），"
                    "请重新加载后再保存"
                )
            # 先留底、后改动：顺序不能反（与 tools/remote.py 同一条原则）。
            backup_path = str(_backup_before_write(self.agent.ctx, conn, rel, old.data))
            backed_up = True
            if normalized == old_text.replace("\r\n", "\n"):
                return {
                    "ok": True,
                    "changed": False,
                    "connection": conn.name,
                    "path": rel,
                    "digest": old_digest,
                    "notice": "内容与远端一致，无需保存",
                }

        info = workspace.write(rel, normalized, newline=newline)
        fresh = workspace.read(rel, max_bytes=MAX_SCAN_BYTES)
        new_digest = hashlib.sha256(fresh.data).hexdigest()[:16]
        diff_text, added, removed = _diff(old_text, normalized, rel)
        action = "save" if existed else "create"
        sessions.note_change(
            ChangeEntry(
                rel_path=rel,
                connection=conn.name,
                abs_path=abs_path,
                action=action,
                bytes=len(fresh.data),
                sha256=new_digest,
                added=added,
                removed=removed,
                backed_up=backed_up,
                backup_path=backup_path,
            )
        )
        self._remote_file_audit(
            action,
            conn,
            rel,
            bytes=int(info.get("bytes") or 0),
            sha256=new_digest,
            added=added,
            removed=removed,
            backed_up=backed_up,
        )
        self.bus.publish(
            P.event(
                P.REMOTE_EDIT,
                connection=conn.name,
                path=rel,
                action=action,
                added=added,
                removed=removed,
                strategy="overwrite",
                sha256=new_digest,
                diff=diff_text,
            )
        )
        note = f"已保存远端 {conn.name}:{rel}（+{added}/-{removed}）"
        self._record_notice(note)
        self._publish_remote_state()
        return {
            "ok": True,
            "changed": True,
            "connection": conn.name,
            "path": rel,
            "action": action,
            "added": added,
            "removed": removed,
            "bytes": int(info.get("bytes") or 0),
            "digest": new_digest,
            "diff": diff_text,
            "notice": note,
        }

    def remote_patch(
        self,
        path: str,
        search: str,
        replace: str,
        *,
        replace_all: bool = False,
    ) -> dict[str, Any]:
        """`POST /api/remote/patch`：结构化补丁（复用本地三级匹配引擎）。

        `changed=false`（search 与现有内容等价）时**不写回** —— 与
        `RemoteWorkspace.edit` 的语义一致：换行是「按多数判定」，无改动却写回
        会把整份文件的换行统一掉，属于静默的破坏性副作用。
        """
        with self._lock:
            if self._state != P.STATE_IDLE:
                raise BusyError("任务执行中，无法修改远端文件，请等任务结束或先中断")
        sessions = self.remote_sessions()
        workspace = sessions.require_workspace()
        conn = workspace.conn
        if not conn.allow_write:
            raise AgentError(
                f"连接 {conn.name} 是只读的（allow_write=off），已拒绝写入。"
            )
        abs_path = workspace.guard.check_write(path)
        rel = workspace.guard.relative(abs_path)
        old_bytes = workspace.transport.read_bytes(abs_path, max_bytes=MAX_SCAN_BYTES)
        backup_path = str(_backup_before_write(self.agent.ctx, conn, rel, old_bytes))

        result = workspace.edit(path, search, replace, replace_all=replace_all)
        if not result.changed:
            return {
                "ok": True,
                "changed": False,
                "connection": conn.name,
                "path": result.rel_path,
                "notice": "search 与远端现有内容等价，未写入",
            }

        fresh = workspace.read(result.rel_path, max_bytes=MAX_SCAN_BYTES)
        digest = hashlib.sha256(fresh.data).hexdigest()[:16]
        sessions.note_change(
            ChangeEntry(
                rel_path=result.rel_path,
                connection=conn.name,
                abs_path=result.abs_path,
                action="edit",
                bytes=len(fresh.data),
                sha256=digest,
                added=result.added,
                removed=result.removed,
                backed_up=True,
                backup_path=backup_path,
            )
        )
        self._remote_file_audit(
            "edit",
            conn,
            result.rel_path,
            bytes=len(fresh.data),
            sha256=digest,
            added=result.added,
            removed=result.removed,
            backed_up=True,
        )
        self.bus.publish(
            P.event(
                P.REMOTE_EDIT,
                connection=conn.name,
                path=result.rel_path,
                action="edit",
                added=result.added,
                removed=result.removed,
                strategy=result.strategy,
                sha256=digest,
                diff=result.diff,
            )
        )
        note = (
            f"已在远端 {conn.name}:{result.rel_path} 应用补丁"
            f"（+{result.added}/-{result.removed}，策略 {result.strategy}）"
        )
        self._record_notice(note)
        self._publish_remote_state()
        return {
            "ok": True,
            "changed": True,
            "connection": conn.name,
            "path": result.rel_path,
            "added": result.added,
            "removed": result.removed,
            "strategy": result.strategy,
            "digest": digest,
            "diff": result.diff,
            "notice": note,
        }

    def remote_restore(self, checkpoint_id: str) -> dict[str, Any]:
        """`POST /api/remote/restore`：把远端恢复到某一轮开始之前（P4）。

        只认本会话留底里**存在 manifest 的轮次**（前端传什么都不会被当成路径）。
        两阶段校验与执行全部在 `restore_turn` 里，这里只负责门控、播报与审计入口。
        """
        with self._lock:
            if self._state != P.STATE_IDLE:
                raise BusyError("任务执行中，无法回退远端文件，请等任务结束或先中断")
        self._require_remote()
        result = restore_turn(self.agent.ctx, checkpoint_id)
        restored = len(result.get("restored") or [])
        deleted = len(result.get("deleted") or [])
        skipped = len(result.get("skipped") or [])
        note = f"已回退远端 {result.get('connection') or ''}：恢复 {restored} 个、删除 {deleted} 个文件"
        if skipped:
            note += f"，跳过 {skipped} 个（见详情）"
        note += "。"
        self._record_notice(note)
        self.bus.publish(P.event(P.RUN_NOTICE, runId=self.run_id, text=note))
        self._publish_remote_state()
        return {"ok": True, **result, "notice": note}

    def _remote_file_audit(
        self,
        action: str,
        conn: RemoteConnection | None,
        path: str,
        *,
        bytes: int = 0,
        sha256: str = "",
        added: int = 0,
        removed: int = 0,
        backed_up: bool = False,
    ) -> None:
        """记一次远端文件读 / 写动作（只记元数据；正文与 diff 不出机，ADR-006）。"""
        self.agent.audit.record(
            "remote.file",
            connection=conn.name if conn is not None else "",
            path=path,
            action=action,
            bytes=int(bytes),
            sha256=sha256,
            added=int(added),
            removed=int(removed),
            backed_up=bool(backed_up),
        )

    # ------------------------------------------------------------------ 远程审查面（M11-D）
    # 台账由 `remote_save` / `remote_patch` 在改动落地后登记（只记元数据），
    # diff 按需现算：**旧内容取本机留底、新内容取远端当前**，两端都在本机算，
    # 因此不必把远端文件往回传两遍，也天然覆盖「保存后又被远端别人改过」的情形。
    def remote_changes(self) -> dict[str, Any]:
        """`GET /api/remote/changes`：本会话远端改动台账（**只读元数据**）。

        返回的是台账的只读视图 —— 路径、动作、增删行、改动后指纹、是否留底，
        **不含任何正文**（ADR-006）。审查界面据此列出「这次会话在远端改过哪些
        文件」，正文按需从 `/api/remote/file` 取、diff 从 `/api/remote/diff` 取。
        """
        sessions = self.remote_sessions()
        conn = sessions.active()
        return {
            "enabled": True,
            "connection": conn.name if conn is not None else "",
            "changes": sessions.list_changes(),
        }

    def remote_diff(self, path: str, connection: str = "") -> dict[str, Any]:
        """`GET /api/remote/diff?connection=&path=`：留底 vs 远端当前的 unified diff。

        三种「没有 diff 可看」如实分档，不假装成功：
          - 台账里没有该文件的改动记录 → 没改过，没什么可审；
          - 留底缺失（纯新建）→ 按**全新增**处理，旧内容为空串；
          - 远端文件已不存在 → 明确报「远端已删除」，不臆造内容。
        diff 正文只走端点响应（**不进审计**，ADR-006）。
        """
        sessions = self.remote_sessions()
        entry = sessions.get_change(path, connection or None)
        if entry is None:
            return {
                "available": False,
                "connection": connection or "",
                "path": path,
                "reason": "该文件在本会话中没有改动记录",
            }
        workspace = sessions.require_workspace()
        conn = workspace.conn
        if conn.name != entry.connection:
            # 一次只活动一条连接：别的连接的改动，现时读不到「远端当前内容」。
            return {
                "available": False,
                "connection": entry.connection,
                "path": entry.rel_path,
                "reason": f"该改动属于连接 {entry.connection}，请先切换到该连接再查看 diff",
            }

        old_text = ""
        if entry.backed_up and entry.backup_path:
            backup = Path(entry.backup_path)
            if backup.is_file():
                old_text = TextDoc.load(backup).text
        try:
            fresh = workspace.read(entry.rel_path, max_bytes=MAX_SCAN_BYTES)
        except RemoteError as exc:
            reason = (
                "远端文件已不存在（可能已被删除或移动）"
                if getattr(exc, "kind", "") == "notfound"
                else f"无法读取远端当前内容：{exc}"
            )
            return {
                "available": False,
                "connection": conn.name,
                "path": entry.rel_path,
                "reason": reason,
            }

        new_text = fresh.text
        diff_text, added, removed = _diff(old_text, new_text, entry.rel_path)
        hunks = split_hunks(old_text, new_text)
        return {
            "available": True,
            "connection": conn.name,
            "path": entry.rel_path,
            "added": added,
            "removed": removed,
            "diff": diff_text,
            "hunks": [hunk.to_api() for hunk in hunks],
        }

    # ------------------------------------------------------------------ 远端预览转发（P3 / ADR-040）
    # 这一档解决的问题：代码在开发机上、`npm run dev` 也在开发机上，人坐在本机前。
    # 做法是 `ssh -L` 把**远端环回端口**映射到**本机环回**，再交给 P2 的跨源帧渲染。
    # 因此它不新增任何 CSP 放宽点 —— 转发落地在 127.0.0.1，正好在 ADR-039 已放行
    # 的那一条 `frame-src` 里（这一点由测试钉住，避免将来有人再加一条）。
    def _require_preview_remote(self) -> None:
        """双门控：`VIBE_PREVIEW_REMOTE` 打开这一档，`VIBE_REMOTE` 提供连接与 SSH 加固。

        两个都必须是**显式**打开 —— 远端预览会把一个远端端口变成本机监听口，
        属于「新增一条网络路径」，不该是任何一次升级的默认结果。
        """
        if not bool(getattr(self.agent.config, "preview_remote", False)):
            raise AgentError(
                "远端预览未启用（VIBE_PREVIEW_REMOTE=0）；"
                "开启后界面才能把远端设备的环回端口转发到本机"
            )
        self._require_remote()

    def _tunnel_manager(self) -> TunnelManager:
        manager = self._tunnels
        if manager is None:
            manager = TunnelManager(on_state=self._on_tunnel_state)
            self._tunnels = manager
        return manager

    def _on_tunnel_state(self, snapshot: dict[str, Any]) -> None:
        """转发状态变化 → 广播 `preview.tunnel`，并对**非用户发起**的跃迁落审计。

        这个回调跑在监工线程上（断线重连时），所以这里刻意只做两件事：
        发事件、写审计 —— 两者各自线程安全，且**都不带正文与主机名**。
        用户发起的 open / close 由端点自己记，避免同一次动作写两条。
        """
        tunnel_id = str(snapshot.get("id") or "")
        state = str(snapshot.get("state") or "")
        self.bus.publish(P.event(P.PREVIEW_TUNNEL, **snapshot))
        if not tunnel_id:
            return
        previous = self._tunnel_states.get(tunnel_id, "")
        self._tunnel_states[tunnel_id] = state
        if state == previous:
            return
        action = ""
        if state == "down" and previous not in ("", "closed"):
            action = "down"
        elif state == "up" and previous == "reconnecting":
            action = "recovered"
        if not action:
            return
        self.agent.audit.record(
            "preview.tunnel",
            action=action,
            connection=str(snapshot.get("connection") or ""),
            remote_port=int(snapshot.get("remotePort") or 0),
            local_port=int(snapshot.get("localPort") or 0),
            ok=action == "recovered",
        )

    def preview_tunnels(self) -> dict[str, Any]:
        """`GET /api/preview/tunnel`：门控状态 + 当前所有转发（**不含任何内容**）。

        门控关着时如实回 `enabled:false` 与空列表，而不是 403 —— 界面首帧就要知道
        「入口该不该出现」，让它先撞一次错误再隐藏是更差的体验（P2 同口径）。
        """
        enabled = bool(getattr(self.agent.config, "preview_remote", False)) and bool(
            self.agent.config.remote_enabled
        )
        if not enabled:
            return {"enabled": False, "tunnels": [], "max": MAX_TUNNELS}
        return {
            "enabled": True,
            "tunnels": self._tunnel_manager().list(),
            "max": MAX_TUNNELS,
        }

    def preview_tunnel_open(
        self, connection: str = "", remote_port: Any = None
    ) -> dict[str, Any]:
        """`POST /api/preview/tunnel`：把远端环回端口转发到本机，返回可预览的地址。

        返回值里的 `url` 必须能通过 `validate_external_preview_url` —— 那条校验
        与 CSP 放行的集合是同一个，两者不一致就会出现「开得成、白屏」。
        """
        self._require_preview_remote()
        store = self._remote_store()
        conn = store.resolve(connection or None)
        started = time.monotonic()
        try:
            info = self._tunnel_manager().open(conn, remote_port)
        except ValueError as exc:
            self.agent.audit.record(
                "preview.tunnel",
                action="open",
                connection=conn.name,
                remote_port=_int_or_zero(remote_port),
                local_port=0,
                ok=False,
                ms=int((time.monotonic() - started) * 1000),
            )
            raise AgentError(str(exc)) from exc
        except RemoteError as exc:
            self.agent.audit.record(
                "preview.tunnel",
                action="open",
                connection=conn.name,
                remote_port=_int_or_zero(remote_port),
                local_port=0,
                ok=False,
                ms=int((time.monotonic() - started) * 1000),
                reason=getattr(exc, "kind", "transport"),
            )
            raise
        ms = int((time.monotonic() - started) * 1000)
        self.agent.audit.record(
            "preview.tunnel",
            action="open",
            connection=conn.name,
            remote_port=int(info.get("remotePort") or 0),
            local_port=int(info.get("localPort") or 0),
            ok=True,
            ms=ms,
        )
        return {"ok": True, **info}

    def preview_tunnel_close(self, tunnel_id: str = "") -> dict[str, Any]:
        """`POST /api/preview/tunnel/close`：释放一次引用；减到 0 才真的收转发。"""
        self._require_preview_remote()
        manager = self._tunnel_manager()
        # 先把快照取出来：关掉之后 tunnel 就不在名单里了，而审计要能回答
        # 「关的是哪条、哪个端口」。这属于元数据，不含内容（ADR-006）。
        info = manager.get(tunnel_id).snapshot() if manager.get(tunnel_id) else {}
        try:
            result = manager.close(tunnel_id)
        except ValueError as exc:
            raise AgentError(str(exc)) from exc
        if result.get("closed"):
            self._tunnel_states.pop(tunnel_id, None)
        self.agent.audit.record(
            "preview.tunnel",
            action="close",
            connection=str(info.get("connection") or ""),
            remote_port=int(info.get("remotePort") or 0),
            local_port=int(info.get("localPort") or 0),
            closed=bool(result.get("closed")),
            refs=int(result.get("refs") or 0),
        )
        return {"ok": True, **result}

    # ------------------------------------------------------------------ 状态
    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def run_id(self) -> str:
        with self._lock:
            return self._run_id

    @property
    def pending_approval(self) -> dict[str, Any] | None:
        with self._lock:
            items = list(self._pending.values())
        if not items:
            return None
        return _approval_payload(items[0])

    @property
    def changed_files(self) -> list[str]:
        return list(dict.fromkeys(self.agent.ctx.written_files))

    def describe(self) -> dict[str, Any]:
        """会话快照。界面初次连接时用它铺出全貌。"""
        cfg = self.agent.config
        base = {
            "sessionId": cfg.session_id,
            "workspace": str(cfg.workspace),
            "model": cfg.model,
            "baseUrl": cfg.base_url,
            "state": self.state,
            "runId": self.run_id,
            "auditPath": str(cfg.audit_path),
            "tools": [
                {
                    "name": spec.name,
                    "risk": spec.risk.value,
                    "description": spec.description.splitlines()[0],
                }
                for spec in (self.registry.get(n) for n in self.registry.names())
                if spec is not None
            ],
            "config": {
                "temperature": cfg.temperature,
                "maxIterations": cfg.max_iterations,
                "maxContextChars": cfg.max_context_chars,
                "toolOutputLimit": cfg.tool_output_limit,
                "allowWrite": cfg.allow_write,
                "allowShell": cfg.allow_shell,
                "allowNetworkCommands": cfg.allow_network_commands,
                "commandTimeout": cfg.command_timeout,
                "autoApproveReview": cfg.auto_approve_review,
                "stream": cfg.stream_enabled,
                # 补全能力（D）：fimEnabled=false 时界面可提示「已禁用 FIM，走 chat 回退」
                "fimEnabled": cfg.fim_enabled,
                # 补全参数（D3+）：前端据此算截断窗口，并把面板回填成当前值
                "completionMaxTokens": cfg.completion_max_tokens,
                "completionPrefixChars": cfg.completion_prefix_chars,
                "completionSuffixChars": cfg.completion_suffix_chars,
                # 交互式终端（M/P1-7）：false（VIBE_TERM=0）时界面隐藏整个终端入口
                "terminalEnabled": cfg.terminal_enabled,
                # 多模态输入（N/P1-10）：off（默认）时界面隐藏贴图/选图入口并提示开法；
                # 非 off 时界面上传后走 image 引用（会随请求外发到用户配置的端点）。
                "visionMode": cfg.vision_mode,
                "visionMaxImageBytes": cfg.vision_max_image_bytes,
                # 跨会话记忆（P2-15）：false（VIBE_MEMORY=0）时界面隐藏记忆面板
                "memoryEnabled": cfg.memory_enabled,
                # 后台 / 并行 Agent（M9-D）：false（VIBE_ASYNC_AGENT=0）时界面隐藏任务入口
                "asyncAgentEnabled": cfg.async_agent_enabled,
                "asyncAgentMax": cfg.async_agent_max,
                # 定时任务调度（M10）：false 时界面隐藏「定时任务」页签入口
                #（调度器本身也依赖 VIBE_ASYNC_AGENT，两者都真才可用）
                "schedulerEnabled": self.scheduler is not None,
                "schedulerTick": cfg.scheduler_tick,
                # Skill / 斜杠命令（M9-A）：false（VIBE_SKILLS=0）时界面不出现 Skill 入口
                "skillsEnabled": cfg.skills_enabled,
                # 代码审阅（M9-H）：false（VIBE_REVIEW=0）时界面不出现审阅浮层
                "reviewEnabled": cfg.review_enabled,
                # 模型选择器（M9-I）：false（VIBE_MODEL_ROUTER=0）时界面隐藏选择入口
                "modelRouterEnabled": cfg.model_router_enabled,
                "modelRouterPolicy": cfg.model_router_policy,
                "modelRouterDefault": cfg.model_router_default,
                # 远程设备文件工作面（M11）：false（VIBE_REMOTE=0，默认）时界面隐藏
                #「远程设备」页签入口 —— 与端点门控同一道门（无端点 / 无页签）。
                "remoteEnabled": cfg.remote_enabled,
            },
            # 已加载的 Skill 工作流（M9-A）：只给 name + description（正文与来源不出机，
            # 供界面「运行 Skill…」命令面板枚举用）；关闭时恒为空列表。
            "skills": [
                {"name": s.name, "description": s.description}
                for s in getattr(self.agent, "skills", None) or []
            ],
            # 项目规则（C1）：source 为空 = 工作区没有 VIBE.md，UI 不显示徽标
            "projectInstructions": (
                {
                    "source": self.agent.project_instructions.source,
                    "chars": self.agent.project_instructions.chars,
                    "truncated": self.agent.project_instructions.truncated,
                }
                if self.agent.project_instructions.source
                else None
            ),
            "changedFiles": self.changed_files,
            "notices": list(self._notices),
            # 计划（P2-12）：运行态，但**页面刷新不能丢** —— 快照带出去，
            # 界面重新连接时直接铺回计划卡片（不落盘，见 protocol.py 的说明）。
            "plan": self.agent.plan_snapshot(),
            # 上下文用量（P2-17）：预算条的首帧数据 —— 界面重连后不必额外发
            # 一次请求就能画出「用在哪、还剩多少」。
            "context": self.agent.context_stats(),
            # 治理面接入状态（M3）：None = 纯本地（界面不显示治理徽标）。
            # 视图由 GovernanceRuntime.describe() 提供，**不含任何令牌**。
            "governance": self.governance.describe() if self.governance is not None else None,
            # 客户端自动更新（M9）：只读状态文件（不联网、不检查）。界面据此在
            # 重连后立刻恢复 `#chip-update` 的可见性，不必先发一次 check。
            "update": self.update_view(),
            "lastAnswer": self._answer,
            "pendingApproval": self.pending_approval,
        }
        # 远程工作区（M13 / ADR-042）：本机模式**一个键都不追加** —— `describe()`
        # 的形状必须与未启用时逐字节一致（决策 11）。
        return {**base, **self._remote_describe()}

    def _remote_describe(self) -> dict[str, Any]:
        """`describe()` 里的远程工作区段。非远程工作区时恒返回空字典。"""
        root = str(getattr(self.agent.config, "remote_workspace", "") or "")
        if not root or not self.agent.config.remote_enabled:
            return {}
        connection, sub = "", ""
        try:
            connection, sub = parse_ns_root(root)
        except Exception:  # noqa: BLE001 — 坏写法不该让整个 describe() 起不来
            pass
        return {
            "remoteWorkspace": root,
            "remoteConnection": connection,
            "remoteSub": sub,
            "remoteError": self._remote_error,
            "remoteDegraded": self.remote_degraded(),
        }

    def remote_degraded(self) -> dict[str, str]:
        """远程工作区模式下的「本机能力已不可用」清单。**全项目唯一真源。**

        键是能力名、值是人话原因。界面、端点、提示词三处都从这里取，
        于是不会出现「界面说支持、内核说不支持」这种错位（ADR-042 决策 9）。

        本机模式恒返回空字典 —— 这也是「本机模式行为不变」的一条可断言锚点。

        真源在 `remote/degrade.py`（一个只依赖 Config 的纯函数）—— HTTP 端点的
        降级守卫、系统提示词都从那里取，于是三处口径必然一致。
        """
        return remote_degraded(self.agent.config)

    # ------------------------------------------------------------------ 治理面登录（M3+）
    def governance_login(
        self, email: str, password: str, *, server_url: str = "", device_name: str = ""
    ) -> dict[str, Any]:
        """`POST /api/governance/login`：在本机登录治理面（用户本人动作）。

        桌面端过去只能「先用 CLI 登录、再重启」；这里把「填地址 → 登录 → 热接入」
        做成闭环，**不必重启**。令牌由 `GovernanceClient` 写进用户级
        `~/.vibe/credentials.json`（0o600 原子写），响应只回掩码视图，绝不回明文。
        """
        self._governance_guard()
        target = self._resolve_governance_server(server_url)
        credentials = GovernanceClient(target, store=CredentialStore()).login(
            email=email, password=password, device_name=device_name
        )
        runtime = self._install_governance(target)
        self.agent.audit.record(
            "governance.login",
            ok=True,
            status=runtime.status,
            user_id=str(getattr(credentials, "user_id", "") or ""),
            device_id=str(getattr(credentials, "device_id", "") or ""),
        )
        return self._governance_response()

    def governance_register(
        self,
        enrollment_token: str,
        email: str,
        password: str,
        *,
        display_name: str = "",
        server_url: str = "",
        device_name: str = "",
    ) -> dict[str, Any]:
        """`POST /api/governance/register`：用邀请码兑换账号并登录（用户本人动作）。"""
        self._governance_guard()
        target = self._resolve_governance_server(server_url)
        credentials = GovernanceClient(target, store=CredentialStore()).register(
            enrollment_token=enrollment_token,
            email=email,
            password=password,
            display_name=display_name,
            device_name=device_name,
        )
        runtime = self._install_governance(target)
        self.agent.audit.record(
            "governance.register",
            ok=True,
            status=runtime.status,
            user_id=str(getattr(credentials, "user_id", "") or ""),
            device_id=str(getattr(credentials, "device_id", "") or ""),
        )
        return self._governance_response()

    def governance_logout(self, *, all_devices: bool = False) -> dict[str, Any]:
        """`POST /api/governance/logout`：退出登录并清除本机凭证（用户本人动作）。

        网络失败**不阻止**本地清除凭证 —— 否则一个不可达的治理面会让用户永远
        退不出去。清除后按配置重新装配，当前会话立即回落到本地直连。
        """
        self._governance_guard()
        target = str(self.agent.config.server_url or "").strip().rstrip("/")
        user_id = ""
        if self.governance is not None and self.governance.governance is not None:
            existing = self.governance.governance.credentials
            user_id = str(getattr(existing, "user_id", "") or "")
        store = CredentialStore()
        if target:
            try:
                GovernanceClient(target, store=store).logout(all_devices=all_devices)
            except GovernanceError:
                pass  # logout() 内部已吞网络错误；这里再兜一层
        store.clear()  # 兜底：即使没走到 client.logout，也确保本地一定退出
        self._install_governance(target)
        self.agent.audit.record("governance.logout", ok=True, user_id=user_id)
        return self._governance_response()

    def governance_config(self, *, server_url: str = "") -> dict[str, Any]:
        """`GET /api/governance/config`：探测治理面注册策略（登录门用）。

        只读、零副作用：不写凭证、不热接入、执行中也不 409。治理面不可达时
        回 `ok=False` 而不是抛错 —— 界面据此退化为「邀请码按需填写」。
        """
        try:
            target = self._resolve_governance_server(server_url)
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}
        try:
            body = GovernanceClient(target, store=CredentialStore()).auth_config()
        except GovernanceError as exc:
            return {"ok": False, "reason": str(exc)}
        return {"ok": True, "registrationOpen": bool(body.get("registrationOpen"))}

    def _governance_guard(self) -> None:
        """登录 / 登出的前置闸门：执行中 409。

        它们会换掉模型客户端与策略闸门，与「执行中不可改模型设置」同一纪律
        （见 apply_settings）—— 不给运行中的任务换一套客户端引用。
        """
        if self.state != P.STATE_IDLE:
            raise BusyError("任务执行中，无法登录/登出治理面，请先中断或等任务结束")

    def _resolve_governance_server(self, server_url: str) -> str:
        """定治理面地址：入参 > 当前配置；都没有 / 非法则 ValueError（→ 400）。"""
        target = str(server_url or "").strip().rstrip("/")
        if not target:
            target = str(self.agent.config.server_url or "").strip().rstrip("/")
        if not target:
            raise ValueError("请先填写治理面地址（例如 https://vibe.内网域名）")
        if not target.startswith(("http://", "https://")):
            raise ValueError("治理面地址必须以 http:// 或 https:// 开头")
        return target

    def _install_governance(self, server_url: str) -> Any:
        """按最新凭证重新装配并**热接入**当前会话，返回新的 GovernanceRuntime。

        与启动时的 `wire()` 走同一个入口：写入 server_url 后重新装配策略、网关
        客户端与审计上传器，再把结果换进当前 Agent（见 `Agent.install_governance`）。
        当前对话、检查点与审计链都原样保留。`wire()` 永不抛异常 —— 降级信息在
        runtime 的 notices / degradedReason 里，由界面按治理面视图展示。
        """
        base = self.agent.config
        if str(base.server_url or "") != server_url:
            base = replace(base, server_url=server_url)
        runtime = wire(base)
        self.governance = runtime
        self.config = runtime.config
        self.agent.install_governance(
            config=runtime.config, client=runtime.client, constraints=runtime.constraints
        )
        self._client = self.agent.client
        return runtime

    def _governance_response(self) -> dict[str, Any]:
        """登录 / 登出后的统一回包：广播新快照 + 只回掩码视图。"""
        self.bus.publish(P.event(P.SESSION_READY, **self.describe()))
        return {
            "ok": True,
            "governance": self.governance.describe() if self.governance is not None else None,
            "session": self.describe(),
        }

    def stats(self) -> dict[str, Any]:
        s = self.agent.stats
        return {
            "iterations": s.iterations,
            "toolCalls": s.tool_calls,
            "toolFailures": s.tool_failures,
            "promptTokens": s.prompt_tokens,
            "completionTokens": s.completion_tokens,
            "approvals": s.approvals,
            "denials": s.denials,
            "trimmed": s.trimmed,
            "changedFiles": self.changed_files,
        }

    def context_stats(self) -> dict[str, Any]:
        """上下文用量（P2-17）：只读、无副作用，直接透传 Agent 的快照。

        不在这里另算一遍 —— 预算口径的真相只有 Agent 有（token 还是字符），
        两处各算一份迟早会漂移，界面就会显示一个和裁剪行为对不上的数字。
        """
        return self.agent.context_stats()

    def audit_report(self, recent: int = 40) -> dict[str, Any]:
        path: Path = self.agent.config.audit_path
        passed, detail = verify_chain(path)
        entries: list[dict[str, Any]] = []
        if path.exists():
            import json

            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            for line in lines[-recent:]:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return {"passed": passed, "detail": detail, "path": str(path), "entries": entries}

    def diffs(self) -> list[dict[str, Any]]:
        """本会话累计产生的 diff，按文件归并。"""
        return [
            {
                "path": change.path,
                "diff": change.diff,
                "added": change.added,
                "removed": change.removed,
                "created": change.created,
            }
            for change in self.agent.ctx.changes_by_file()
        ]

    # ------------------------------------------------------------------ 记忆（P2-15）
    def list_memories(self) -> dict[str, Any]:
        """`GET /api/memories`：当前用户级记忆库的只读视图。"""
        store = self.memory
        if store is None:
            return {"enabled": False, "count": 0, "maxItems": 0, "items": []}
        return {
            "enabled": True,
            "count": store.count(),
            "maxItems": store.max_items,
            "items": [record.to_dict() for record in store.list()],
        }

    def add_memory(self, text: str, tags: Any = None) -> dict[str, Any]:
        """`POST /api/memories`：用户本人新增一条记忆。

        「用户本人动作」（ADR-011 同范式）：不走审批浮层、不写工作区源码，
        但记审计（只记 scope/长度/指纹，不记正文，ADR-006）。模型侧的写入
        必须走 `remember` 工具的审批闸门（Risk.REVIEW），两条路径分开。
        """
        store = self.memory
        if store is None:
            raise ValueError("记忆功能未启用（VIBE_MEMORY=0）")
        if isinstance(tags, str):
            tags = [tags]
        if tags is not None and not isinstance(tags, list):
            raise ValueError("tags 必须是字符串数组")
        record = store.add(text, tags=tags, scope="user", source_digest=digest(str(text)))
        self.agent.audit.record(
            "memory.add",
            scope="user",
            chars=len(record.text),
            digest=digest(record.text),
        )
        return {"ok": True, "item": record.to_dict(), "count": store.count()}

    def delete_memory(self, mem_id: int) -> dict[str, Any]:
        """`POST /api/memories/delete`：用户本人删除一条记忆。"""
        store = self.memory
        if store is None:
            raise ValueError("记忆功能未启用（VIBE_MEMORY=0）")
        removed = store.remove(int(mem_id))
        return {"ok": True, "removed": removed, "count": store.count()}

    # ------------------------------------------------------------------ 计划（P2-12）
    def plan_toggle(self, step_id: str, status: str) -> dict[str, Any]:
        """人工标注某一步的状态（G7）：「用户本人动作」，不审批、不写盘，但记审计。

        运行中**不** 409 —— 只是改内存里的一个状态，不与写盘路径争用；
        Agent 跑着时手动更正某一步，恰恰是这个功能最有用的时刻（见 protocol.py）。
        """
        status = (status or "").strip().lower()
        if status not in PLAN_STATUSES:
            raise ValueError(
                f"status 非法：{status!r}。可选值：{'/'.join(PLAN_STATUSES)}"
            )
        step_id = (step_id or "").strip()
        if not step_id:
            raise ValueError("缺少 id 参数")
        with self._lock:
            snapshot = self.agent.set_plan_step(step_id, status)
        if snapshot is None:
            raise ValueError(f"计划中没有 id={step_id!r} 的步骤")
        # 审计只记元数据（ADR-006）：id 是短标识、状态是枚举、条数是计数
        self.agent.audit.record(
            "plan.toggle",
            id=step_id,
            status=status,
            steps_count=len(snapshot["steps"]),
            revision=snapshot["revision"],
        )
        self.bus.publish(
            P.event(
                P.RUN_PLAN,
                runId=self.run_id,
                steps=snapshot["steps"],
                revision=snapshot["revision"],
            )
        )
        return {
            "ok": True,
            "id": step_id,
            "status": status,
            "steps": snapshot["steps"],
            "revision": snapshot["revision"],
        }

    # ------------------------------------------------------------------ 测试树（K/P2-14）
    def tests_tree(self, refresh: bool = False) -> dict[str, Any]:
        """`GET /api/tests`：收集用例（树）。缺省用缓存，`refresh=1` 强制重收。

        收集要起一次子进程（`pytest --collect-only`），面板刷新时不该每次都付这份
        成本；缓存只在显式刷新时失效 —— 没有 TTL 这种会「莫名变旧」的隐式状态。
        未配置 `test_command` 时 runner 直接返回 `configured=false`（不起子进程）。
        """
        cfg = self.agent.config
        with self._tests_lock:
            if refresh or self._tests_cache is None:
                self._tests_cache = collect_tests(
                    command=cfg.test_command,
                    collect_args=cfg.test_collect_args,
                    workspace=cfg.workspace,
                    guard=self.agent.commands,
                )
            return self._tests_cache.to_dict()

    def tests_run(self, node_ids: list[str]) -> dict[str, Any]:
        """`POST /api/tests/run`：跑指定用例（空 = 全部），结果走 SSE。

        运行中**允许**（不与写盘争用，Agent 跑着时也能点）；但**同一时刻只跑一批**
        —— 两个 pytest 在同一个工作区里并发跑会互相踩 junit 报告与临时文件。
        前端回传的 node id 必须落在已收集的集合里：它是 argv 的一部分，
        放任意字符串进来等于给 pytest 传任意开关（argv 注入）。
        """
        cfg = self.agent.config
        if not (cfg.test_command or "").strip():
            raise ValueError("未配置测试命令（可通过 VIBE_TEST_COMMAND 或 VIBE.md 的 test: 行设置）")

        tree = self.tests_tree()
        known = {case["nodeId"] for case in tree.get("cases", [])}
        requested: list[str] = []
        for raw in node_ids or []:
            node = str(raw).strip()
            if not node:
                continue
            if node not in known:
                raise ValueError(f"未知用例：{node}")
            requested.append(node)

        if not self._tests_run_lock.acquire(blocking=False):
            raise BusyError("已有测试正在运行，请等它结束再点")
        try:
            self._tests_seq += 1
            run_id = f"test_{self._tests_seq}"
            thread = threading.Thread(
                target=self._run_tests, args=(run_id, requested), daemon=True
            )
            self._tests_thread = thread
            thread.start()
        except Exception:  # noqa: BLE001 — 起线程失败不能把锁留在手里
            self._tests_run_lock.release()
            raise
        return {"runId": run_id}

    def _run_tests(self, run_id: str, node_ids: list[str]) -> None:
        """后台跑一批测试：事件 + 审计 + 最近结果，最后**务必**释放运行锁。"""
        cfg = self.agent.config
        try:
            self.bus.publish(
                P.event(
                    P.TEST_STARTED,
                    runId=run_id,
                    total=len(node_ids),
                    nodeIds=list(node_ids),
                )
            )
            try:
                result = run_tests(
                    command=cfg.test_command,
                    node_ids=node_ids,
                    workspace=cfg.workspace,
                    guard=self.agent.commands,
                )
            except Exception as exc:  # noqa: BLE001 — 跑测试的意外不该掀掉会话
                result = {
                    "configured": True, "results": [],
                    "total": 0, "passed": 0, "failed": 0, "skipped": 0,
                    "ms": 0, "exitCode": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                }

            failures = [
                {
                    "nodeId": item["nodeId"],
                    "file": item["failureFile"],
                    "line": item["failureLine"],
                    "head": item["messageHead"],
                }
                for item in result["results"]
                if item["status"] in ("failed", "error")
            ]
            first = failures[0] if failures else {}
            # 审计只记计数与首个失败的位置（路径/行号属元数据，ADR-006）；
            # 失败正文（messageHead）只留本机。
            self.agent.audit.record(
                "test.run",
                scope="subset" if node_ids else "all",
                requested=len(node_ids) if node_ids else int(result["total"]),
                passed=int(result["passed"]),
                failed=int(result["failed"]),
                skipped=int(result["skipped"]),
                ms=int(result["ms"]),
                file=str(first.get("file") or ""),
                line=int(first.get("line") or 0),
            )
            with self._lock:
                self._tests_last = {
                    "runId": run_id,
                    "nodeIds": list(node_ids),
                    "result": {**result, "failures": failures},
                }
            self.bus.publish(
                P.event(
                    P.TEST_FINISHED,
                    runId=run_id,
                    total=int(result["total"]),
                    passed=int(result["passed"]),
                    failed=int(result["failed"]),
                    skipped=int(result["skipped"]),
                    ms=int(result["ms"]),
                    failures=failures,
                    error=result.get("error", ""),
                )
            )
        finally:
            self._tests_run_lock.release()

    def tests_last(self) -> dict[str, Any]:
        """`GET /api/tests/last`：最近一次跑测试的结果（内存态，不落盘）。"""
        configured = bool((self.agent.config.test_command or "").strip())
        with self._lock:
            last = copy.deepcopy(self._tests_last)
        if last is None:
            return {"configured": configured, "runId": "", "result": None}
        return {"configured": configured, **last}

    # ------------------------------------------------------------------ 检查点（B1）
    def checkpoints(self) -> list[dict[str, Any]]:
        """检查点列表（摘要，不含正文）。"""
        return [cp.to_api() for cp in self.agent.checkpoints.list_checkpoints()]

    def restore_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        """一键回滚到指定检查点。运行中拒绝；失败信息面向用户。"""
        with self._lock:
            if self._state != P.STATE_IDLE:
                raise BusyError("任务执行中，无法回滚，请等任务结束或先中断")
        from ..checkpoint import CheckpointError

        try:
            report = self.agent.checkpoints.restore(checkpoint_id)
        except CheckpointError as exc:
            raise ValueError(str(exc)) from exc  # server 层转 400
        note = f"已回滚到检查点 {checkpoint_id}：{report.summary()}。"
        if report.redo_id:
            note += f"回滚本身已留检查点（{report.redo_id}），可再次回滚撤销。"
        self._record_notice(note)
        self.bus.publish(P.event(P.RUN_NOTICE, runId=self.run_id, text=note))
        return {
            "ok": True,
            "id": checkpoint_id,
            "restored": report.restored,
            "deleted": report.deleted,
            "unchanged": len(report.unchanged),
            "redoId": report.redo_id,
            "notice": note,
        }

    def rollback(self, user_seq: Any) -> dict[str, Any]:
        """逐条回退（P3）：「回到这条提问被发送之前」。

        三件事一起做：**本地/远端文件**回到这一轮之前、**对话**删掉这条提问及其
        之后的全部消息、然后重建 Agent。顺序刻意是「先校验 → 再写文件 → 再删消息」：
        任何一步失败都不会出现「文件回了一半、对话已经没了」的中间态。

        `user_seq` 是消息表的 seq（界面从 `GET /api/sessions/{id}/messages` 拿），
        检查点 manifest 里的 `userSeq` 是同一套编号 —— 两边靠它对齐。
        """
        with self._lock:
            if self._state != P.STATE_IDLE:
                raise BusyError("任务执行中，无法回退，请等任务结束或先中断")
        if self.store is None:
            raise ValueError("会话未持久化，无法逐条回退")
        try:
            seq = int(user_seq)
        except (TypeError, ValueError) as exc:
            raise ValueError("userSeq 必须是整数") from exc
        if seq <= 0:
            raise ValueError("userSeq 必须是正整数")

        sid = self.config.session_id
        messages = self.store.get_messages(sid, with_seq=True)
        target = next((m for m in messages if m.get("seq") == seq), None)
        if target is None:
            raise ValueError("这条提问已不存在（可能刚被回退过，请刷新页面）")
        if target.get("role") != "user":
            raise ValueError("只能回退到用户提问之前（该消息不是用户消息）")
        # 剩余消息必须先成组校验：孤立 tool 消息会让上游直接拒绝整个序列；
        # 一旦截断完才发现，对话已经删了 —— 所以校验必须在任何写入之前。
        remaining = [m for m in messages if m["seq"] < seq]
        for m in remaining:
            m.pop("seq", None)  # seq 只服务界面，不能跟着消息发给模型
        error = validate_message_group(remaining)
        if error:
            raise ValueError(f"回退后消息不成组，已中止：{error}")

        # ① 本地文件：manifest.userSeq 与这条提问**精确相等**的 kind=run 检查点。
        # 没有 = 本轮没改过文件（或检查点已被淘汰），对话照常回退。
        checkpoints = [
            cp for cp in self.agent.checkpoints.list_checkpoints() if cp.kind == "run"
        ]
        local = next((cp for cp in checkpoints if cp.user_seq == seq), None)
        restored: list[str] = []
        deleted: list[str] = []
        unchanged = 0
        redo_id = ""
        summary = ""
        checkpoint_id = ""
        if local is not None:
            from ..checkpoint import CheckpointError

            try:
                report = self.agent.checkpoints.restore(local.id)
            except CheckpointError as exc:
                # 文件回退失败即整体中止：此刻消息一条都没删，用户可以直接重试
                raise ValueError(f"文件回退失败，未做任何改动：{exc}") from exc
            restored = list(report.restored)
            deleted = list(report.deleted)
            unchanged = len(report.unchanged)
            redo_id = report.redo_id
            summary = report.summary()
            checkpoint_id = local.id

        # ② 远端轮次恢复（P4）：同一轮用同一个检查点 id，见 remote 通路。
        remote = self._rollback_remote(checkpoint_id, seq)
        restored.extend(remote.get("restored") or [])
        deleted.extend(remote.get("deleted") or [])

        # ③ 会话截断。对话不可恢复 —— 先把条数与首末 seq 写进审计再删。
        removed = self.store.truncate_from(sid, seq)
        if removed:
            self.agent.audit.record(
                "rollback.truncate",
                count=removed,
                first_seq=seq,
                last_seq=int(messages[-1]["seq"]) if messages else seq,
                checkpoint_id=checkpoint_id,
                restored=len(restored),
                deleted=len(deleted),
            )

        # ④ 重建 Agent：与「恢复历史会话」同一套，Agent.messages 与库重新对齐
        after = self.store.get_messages(sid)
        self.resume(after, session_id=sid)

        if local is None:
            expired = any(cp.user_seq > seq for cp in checkpoints)
            note = f"已回退对话（删除 {removed} 条消息）。"
            note += (
                "没有找到本轮的检查点：本轮可能没有文件改动，"
                "或该轮检查点已被淘汰（仅保留最近 10 轮）。"
                if expired
                else "本轮没有文件改动，只回退了对话。"
            )
        else:
            note = f"已回退到这条提问之前：{summary}；删除 {removed} 条消息（不可恢复）。"
            if redo_id:
                note += f"文件回退可撤销（检查点 {redo_id}）。"
        # 远端与本地合成一句话：用户只点了一次「回退」，不该看到两套结论。
        r_restored = len(remote.get("restored") or [])
        r_deleted = len(remote.get("deleted") or [])
        if r_restored or r_deleted:
            note += f" 远端：恢复 {r_restored} 个、删除 {r_deleted} 个文件。"
        r_skipped = len(remote.get("skipped") or [])
        if r_skipped:
            note += f" 远端跳过 {r_skipped} 个文件（未强行覆盖）。"
        self._record_notice(note)
        self.bus.publish(P.event(P.RUN_NOTICE, runId=self.run_id, text=note))
        return {
            "ok": True,
            "userSeq": seq,
            "restored": restored,
            "deleted": deleted,
            "unchanged": unchanged,
            "redoId": redo_id,
            "checkpointId": checkpoint_id,
            "truncated": removed,
            # 界面按这份重铺（带 seq，可再次点击回退）
            "messages": self.store.get_messages(sid, with_seq=True),
            "remote": remote,
            "notice": note,
        }

    def _rollback_remote(self, checkpoint_id: str, user_seq: int) -> dict[str, Any]:
        """远端轮次文件回退（P4）：远端未启用 / 本轮没有远端改动时原样返回空。

        真正的远端恢复由 remote 工作区的轮次 manifest 驱动（见
        `tools/remote.py::restore_turn`）；这里只做「有就调、没有就当空」的合成，
        保证本地与远端是**一条 UX**。
        """
        empty: dict[str, Any] = {
            "restored": [],
            "deleted": [],
            "skipped": [],
            "connection": "",
            "redoId": "",
        }
        if not checkpoint_id:
            return empty
        impl = self._remote_rollback_impl
        try:
            result = impl(checkpoint_id, user_seq)
        except Exception as exc:  # noqa: BLE001 — 远端失败不吞掉本地回退，但要如实报告
            empty["skipped"] = [{"path": "", "reason": f"远端回退失败：{exc}"}]
            return empty
        # 本地与远端可能撞名（两边的 src/app.py），加 remote:<连接>: 前缀区分
        conn = str(result.get("connection") or "")
        merged: dict[str, Any] = {**empty, **result}
        if conn:
            merged["restored"] = [f"remote:{conn}:{p}" for p in result.get("restored") or []]
            merged["deleted"] = [f"remote:{conn}:{p}" for p in result.get("deleted") or []]
        return merged

    def _remote_rollback_impl(
        self, checkpoint_id: str, user_seq: int
    ) -> dict[str, Any]:
        """`POST /api/rollback` 的远端臂：没有远端留底就当这一轮没碰过远端。"""
        empty: dict[str, Any] = {
            "restored": [],
            "deleted": [],
            "skipped": [],
            "connection": "",
            "redoId": "",
        }
        if not self.agent.config.remote_enabled:
            return empty
        # 先探测是否存在这一轮的 manifest：没有就安静返回空，
        # 不能把「本轮没改远端」误报成「远端回退失败」。
        if locate_remote_turn(self.agent.ctx, checkpoint_id) is None:
            return empty
        return restore_turn(self.agent.ctx, checkpoint_id)

    # ------------------------------------------------------------------ Git（B3/B4/B5）
    # 全部是「用户本人在 Git 面板上点」的动作（ADR-028：Git 写操作 = 用户本人
    # 动作），与 Agent 通道彻底隔离：不给 Agent 加任何 git 写工具，也不进审批
    # 浮层。固定 argv + shell=False + scrub_env 的纪律由 gitinfo 统一保证。
    # 破坏性动作（discard）强制前置检查点，误丢的工作可从检查点面板找回。
    def _git_root(self) -> Path:
        return self.agent.config.workspace

    def _git_guard_idle(self, action: str) -> None:
        """Git 写操作与编辑面同口径：任务执行中一律 409，避免与 Agent 写盘争用。"""
        with self._lock:
            if self._state != P.STATE_IDLE:
                raise BusyError(f"任务执行中，无法{action}，请等任务结束或先中断")

    def _git_rel_paths(self, raw_paths: Any) -> list[str]:
        """把前端给的相对路径逐个过 PathGuard 写校验。

        越界 / 敏感 / `.vibe` 内一律抛 `SandboxViolation`（调用方转 400）——
        与 `check_write` 同一口径，杜绝「借 git 动作绕过写保护」。
        """
        if raw_paths is None:
            return []
        if not isinstance(raw_paths, list):
            raise ValueError("paths 必须是数组")
        paths = self.agent.paths
        out: list[str] = []
        for raw in raw_paths:
            text = str(raw or "").strip()
            if not text:
                continue
            out.append(paths.relative(paths.check_write(text)))
        return out

    @staticmethod
    def _git_paths_digest(rels: list[str]) -> str:
        # 批量路径只上传「条数 + 指纹」（ADR-006）：路径本身可能含业务名词。
        return digest("\n".join(sorted(rels)))

    def git_stage(self, paths: Any = None, all: bool = False) -> dict[str, Any]:
        self._git_guard_idle("暂存改动")
        rels = self._git_rel_paths(paths)
        result = gitinfo.stage(self._git_root(), rels, all=bool(all))
        self.agent.audit.record(
            "git.stage", count=len(rels), paths_digest=self._git_paths_digest(rels)
        )
        return result

    def git_unstage(self, paths: Any = None, all: bool = False) -> dict[str, Any]:
        self._git_guard_idle("取消暂存")
        rels = self._git_rel_paths(paths)
        result = gitinfo.unstage(self._git_root(), rels, all=bool(all))
        self.agent.audit.record(
            "git.unstage", count=len(rels), paths_digest=self._git_paths_digest(rels)
        )
        return result

    def git_discard(self, paths: Any = None) -> dict[str, Any]:
        """放弃改动（破坏性）：**先落检查点再执行**（B5）。

        检查点快照失败必须让本次 discard 失败 —— 「不能回滚的破坏性动作宁可不
        发生」（与审批闸门 / 检查点写入同一失效安全哲学）。
        """
        self._git_guard_idle("放弃改动")
        rels = self._git_rel_paths(paths)
        if not rels:
            raise ValueError("paths 不能为空")

        paths_guard = self.agent.paths
        self.agent.checkpoints.begin()
        for rel in rels:
            self.agent.ctx.snapshot_before(paths_guard.check_write(rel), rel)
        result = gitinfo.discard(self._git_root(), rels)
        info = self.agent.checkpoints.commit(prompt_digest=self._git_paths_digest(rels))
        checkpoint_id = info.id if info is not None else ""
        self.agent.audit.record(
            "git.discard",
            count=len(rels),
            paths_digest=self._git_paths_digest(rels),
            checkpoint_id=checkpoint_id,
        )
        if result.get("ok"):
            note = f"已放弃 {len(rels)} 个文件的改动（检查点 {checkpoint_id or '无'}，可回滚）"
            self._record_notice(note)
            self.bus.publish(P.event(P.RUN_NOTICE, runId=self.run_id, text=note))
        return {**result, "checkpointId": checkpoint_id}

    def git_commit(self, message: str) -> dict[str, Any]:
        self._git_guard_idle("提交")
        text = str(message or "")
        if not text.strip():
            raise ValueError("提交信息不能为空")
        # message 是用户手打的，可能在描述里复述敏感细节 —— 本地存原文、上传只留指纹
        result = gitinfo.commit(self._git_root(), text)
        self.agent.audit.record(
            "git.commit", message=text, oid=str(result.get("oid") or "")
        )
        return result

    def git_push(self) -> dict[str, Any]:
        self._git_guard_idle("推送")
        root = self._git_root()
        state = gitinfo.status(root)
        result = gitinfo.push(root)
        # 远端错误文本（stderr 首行）可能引述带凭据的远端 URL —— 与 web 工具同口径，
        # 本地存原文、上传只留指纹（ADR-006）。
        self.agent.audit.record(
            "git.push",
            remote=str(state.get("upstream") or ""),
            branch=str(state.get("branch") or ""),
            ok=bool(result.get("ok")),
            reason=str(result.get("reason") or ""),
        )
        return result

    def git_pull(self) -> dict[str, Any]:
        self._git_guard_idle("拉取")
        root = self._git_root()
        state = gitinfo.status(root)
        result = gitinfo.pull_ff(root)
        self.agent.audit.record(
            "git.pull",
            remote=str(state.get("upstream") or ""),
            branch=str(state.get("branch") or ""),
            ok=bool(result.get("ok")),
            reason=str(result.get("reason") or ""),
        )
        return result

    def git_branch(self, name: str, action: str) -> dict[str, Any]:
        self._git_guard_idle("切换分支")
        text = str(name or "").strip()
        act = str(action or "").strip().lower()
        if act == "create":
            result = gitinfo.branch_create(self._git_root(), text)
        elif act == "switch":
            result = gitinfo.branch_switch(self._git_root(), text)
        else:
            raise ValueError("action 只能是 create 或 switch")
        self.agent.audit.record("git.branch", name=text, action=act)
        return result

    # ------------------------------------------------------------------ 编辑面（D）
    def _ensure_write_enabled(self) -> None:
        if not self.agent.config.allow_write:
            raise ValueError("写入被禁用（allow_write=false）")

    def complete(
        self,
        path: str,
        prefix: str,
        suffix: str = "",
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """内联补全（D5）：直接调模型，**不进 Agent 循环、不动消息历史、不写盘**。

        路径前置校验与「界面能打开的文件」同一口径（`check_read`）—— 越权/敏感
        文件在这里就被拒，补全绝不会把读不到的正文喂给模型。

        与改写/保存不同，这里**不设运行中 409**：补全只产文本，不与 Agent 的写盘
        路径争用，任务跑着时给用户补全反而是想要的行为。

        增强（D3+）三件事：

        - **上下文窗口截断**：只把光标附近 `completion_prefix_chars` /
          `completion_suffix_chars` 个字符喂给模型，大文件不再整份上送（行对齐）。
        - **token 上限回退**：调用方没给 `max_tokens` 时用 `cfg.completion_max_tokens`，
          `VIBE_COMPLETION_MAX_TOKENS` 因此真正生效。
        - **请求去重缓存**：相同（路径 + 截断后上下文 + 上限 + FIM 开关）在 TTL 内
          直接复用，不重复调模型；缓存只在内存、随会话消亡，绝不落盘。
        """
        cfg = self.agent.config
        paths = self.agent.paths
        self._ensure_write_enabled()
        abs_path = paths.check_read(path)  # 越权 → SandboxViolation → 403
        if not abs_path.is_file():
            raise ValueError(f"{paths.relative(abs_path)} 不存在或不是普通文件")

        rel = paths.relative(abs_path)
        # 截断：只把光标附近上下文发出去（前端也会先截一道，这里是权威口径）
        prefix, suffix, truncated = window_completion_context(
            prefix,
            suffix,
            prefix_chars=cfg.completion_prefix_chars,
            suffix_chars=cfg.completion_suffix_chars,
        )
        limit = max_tokens if (max_tokens and max_tokens > 0) else cfg.completion_max_tokens

        key = _completion_cache_key(rel, prefix, suffix, limit, cfg.fim_enabled)
        cached = self._completion_cache_get(key)
        if cached is not None:
            return cached

        client = self.agent.client
        complete_fn = getattr(client, "complete", None)
        if callable(complete_fn):
            text, backend = complete_fn(prefix, suffix, limit, fim=cfg.fim_enabled)
        else:
            # 客户端没有 complete（治理网关等）：退化为一次 chat，行为一致
            response = client.chat(
                [{"role": "user", "content": build_completion_prompt(prefix, suffix)}],
                temperature=0.0,
            )
            text, backend = clean_completion_text(response.content or ""), "chat"

        self.agent.audit.record(
            "edit.complete",
            path=rel,
            prefix_digest=digest(prefix),
            suffix_digest=digest(suffix),
            chars=len(text),
            backend=backend,
            # 补全正文不入审计（ADR-006）：只留指纹与长度
        )
        payload: dict[str, Any] = {
            "text": text,
            "backend": backend,
            "path": rel,
            "chars": len(text),
            "truncated": truncated,
            "cached": False,
        }
        self._completion_cache_put(key, payload)
        return payload

    def _completion_cache_get(self, key: str) -> dict[str, Any] | None:
        """取缓存；过期视为未命中。命中标记 `cached=true`（只进响应、不进审计）。"""
        with self._lock:
            hit = self._completions.get(key)
        if hit is None or time.monotonic() - hit[0] > COMPLETION_CACHE_TTL:
            return None
        payload = dict(hit[1])
        payload["cached"] = True
        return payload

    def _completion_cache_put(self, key: str, payload: dict[str, Any]) -> None:
        """写缓存并在超出上限时淘汰最旧条目（补全不必 LRU 那么精细）。"""
        with self._lock:
            self._completions[key] = (time.monotonic(), dict(payload))
            while len(self._completions) > COMPLETION_CACHE_MAX:
                oldest = min(self._completions, key=lambda k: self._completions[k][0])
                self._completions.pop(oldest, None)

    def invalidate_completion_cache(self) -> None:
        """清空补全去重缓存。换模型 / 改补全参数后调用，避免给出过期补全。"""
        with self._lock:
            self._completions.clear()

    def rewrite(
        self,
        path: str,
        start_line: int,
        end_line: int,
        instruction: str,
    ) -> dict[str, Any]:
        """行内改写（D8 第一步）：按指令改写选区，**只算不落盘**。

        返回 hunk 列表 + 一个 token；真正的落盘在 `rewrite_apply`，且必须带回这个
        token —— 服务端按 token 取**自己缓存**的改写结果，前端回传的 new_text 一律
        不被信任（否则伪造整段内容就能绕过 hunk 的「逐块勾选」语义）。
        """
        instruction = (instruction or "").strip()
        if not instruction:
            raise ValueError("改写指令为空")
        paths = self.agent.paths
        self._ensure_write_enabled()
        abs_path = paths.check_write(path)
        if not abs_path.is_file():
            raise ValueError(f"{paths.relative(abs_path)} 不存在或不是普通文件")

        doc = TextDoc.load(abs_path)
        lines = doc.text.split("\n")
        start, end = self._line_range(start_line, end_line, len(lines))
        old_selection = "\n".join(lines[start:end])
        if not old_selection.strip():
            raise ValueError("选区为空，没有可改写的内容")
        if len(old_selection) > MAX_REWRITE_CHARS:
            raise ValueError(
                f"选区过大（{len(old_selection)} 字符，上限 {MAX_REWRITE_CHARS}），请缩小后重试"
            )

        before = "\n".join(lines[max(0, start - REWRITE_CONTEXT_LINES) : start])
        after = "\n".join(lines[end : end + REWRITE_CONTEXT_LINES])
        prompt = build_rewrite_prompt(instruction, old_selection, before=before, after=after)
        response = self.agent.client.chat(
            [{"role": "user", "content": prompt}], temperature=0.0
        )
        new_selection = clean_completion_text(response.content or "")
        new_selection = new_selection.replace("\r\n", "\n").replace("\r", "\n")

        hunks: list[Hunk] = [] if new_selection == old_selection else split_hunks(old_selection, new_selection)
        token = uuid.uuid4().hex[:16]
        rel = paths.relative(abs_path)
        with self._lock:
            self._rewrites[token] = {
                "abs": abs_path,
                "path": rel,
                "start": start,
                "end": end,
                "old_text": doc.text,
                "old_selection": old_selection,
                "hunks": hunks,
            }
        self.agent.audit.record(
            "edit.rewrite.preview",
            path=rel,
            start=start + 1,
            end=end,
            hunks=len(hunks),
            selection_digest=digest(old_selection),
            chars=len(new_selection),
        )
        return {
            "token": token,
            "path": rel,
            "startLine": start + 1,
            "endLine": end,
            "hunks": [hunk.to_api() for hunk in hunks],
            "noChange": not hunks,
        }

    def rewrite_apply(self, token: str, accepted: list[int]) -> dict[str, Any]:
        """行内改写（D8 第二步）：按勾选的 hunk 子集落盘。

        这是「用户本人动作」，因此**不走审批浮层**（ADR-011），但仍受两条边界约束：
        运行中拒绝（409）、落盘前收检查点并写审计。写盘复用 `plan_overwrite` ——
        「勾掉一半」与「整份替换」共享同一套落盘引擎，不新增写盘路径（ADR-004）。
        """
        with self._lock:
            if self._state != P.STATE_IDLE:
                raise BusyError("任务执行中，无法应用改写，请等任务结束或先中断")
            draft = self._rewrites.get(token)
        if draft is None:
            raise ValueError("改写草稿已失效，请重新生成")

        abs_path: Path = draft["abs"]
        rel: str = draft["path"]
        paths = self.agent.paths
        paths.check_write(rel)  # 切换工作区后 token 失效，这里再兜一层
        if not abs_path.is_file():
            raise StaleFileError("目标文件已不存在，改写已失效")

        current = TextDoc.load(abs_path)
        if current.text != draft["old_text"]:
            raise StaleFileError("文件已被改动，改写已失效，请重新打开后再试")

        accepted_set = {int(i) for i in (accepted or [])}
        new_selection = apply_hunks(draft["old_selection"], draft["hunks"], accepted_set)
        lines = current.text.split("\n")
        merged = lines[: draft["start"]] + new_selection.split("\n") + lines[draft["end"] :]
        new_text = "\n".join(merged)

        applied = len([h for h in draft["hunks"] if h.index in accepted_set])
        if new_text == current.text:
            with self._lock:
                self._rewrites.pop(token, None)
            note = "未选中任何 hunk，文件未改动"
            self._record_notice(note)
            return {"ok": True, "changed": False, "path": rel, "applied": 0, "notice": note}

        plan = plan_overwrite(abs_path, new_text, rel_path=rel)
        self.agent.ctx.snapshot_before(abs_path, rel)
        commit_plan(plan)
        self.agent.ctx.record_change(
            path=rel, diff=plan.diff, added=plan.added, removed=plan.removed
        )
        with self._lock:
            self._rewrites.pop(token, None)
        self.agent.audit.record(
            "edit.rewrite.apply",
            path=rel,
            strategy="hunk-subset",
            applied=applied,
            total=len(draft["hunks"]),
            added=plan.added,
            removed=plan.removed,
        )
        note = f"已应用改写：{rel}（{applied}/{len(draft['hunks'])} 个 hunk，+{plan.added}/-{plan.removed}）"
        self._record_notice(note)
        self.bus.publish(P.event(P.RUN_NOTICE, runId=self.run_id, text=note))
        return {
            "ok": True,
            "changed": True,
            "path": rel,
            "applied": applied,
            "total": len(draft["hunks"]),
            "added": plan.added,
            "removed": plan.removed,
            "notice": note,
        }

    def save(self, path: str, content: str, expected_digest: str = "") -> dict[str, Any]:
        """编辑模式「保存」（D11）：乐观锁 + 检查点 + 补丁 + 审计。

        `expected_digest` 是文件**打开时**的指纹。磁盘已被改（Agent 落盘、外部
        编辑器、另一次保存）则不匹配 → `StaleFileError`（409），逼用户重载，
        绝不静默覆盖 —— 这正是「编辑面不能污染 Agent 改动」的落点。
        """
        with self._lock:
            if self._state != P.STATE_IDLE:
                raise BusyError("任务执行中，无法保存，请等任务结束或先中断")
        paths = self.agent.paths
        self._ensure_write_enabled()
        abs_path = paths.check_write(path)
        if not abs_path.is_file():
            raise ValueError(f"{paths.relative(abs_path)} 不存在或不是普通文件")
        rel = paths.relative(abs_path)

        if expected_digest and file_digest(abs_path) != expected_digest:
            raise StaleFileError(
                "文件已被改动（可能来自 Agent 或外部编辑器），请重新加载后再保存"
            )

        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        plan = plan_overwrite(abs_path, normalized, rel_path=rel)
        if not plan.changed:
            return {
                "ok": True,
                "changed": False,
                "path": rel,
                "digest": file_digest(abs_path),
                "notice": "内容与磁盘一致，无需保存",
            }

        self.agent.ctx.snapshot_before(abs_path, rel)
        commit_plan(plan)
        self.agent.ctx.record_change(
            path=rel, diff=plan.diff, added=plan.added, removed=plan.removed
        )
        self.agent.audit.record(
            "edit.save",
            path=rel,
            strategy="overwrite",
            added=plan.added,
            removed=plan.removed,
            chars=len(normalized),
        )
        note = f"已保存 {rel}（+{plan.added}/-{plan.removed}）"
        self._record_notice(note)
        self.bus.publish(P.event(P.RUN_NOTICE, runId=self.run_id, text=note))
        return {
            "ok": True,
            "changed": True,
            "path": rel,
            "added": plan.added,
            "removed": plan.removed,
            "digest": file_digest(abs_path),
            "notice": note,
        }

    # ------------------------------------------------------------------ MCP（L）
    def mcp_status(self) -> dict[str, Any]:
        """`GET /api/mcp/status`：是否启用 + 每个 server 的可用性与工具清单。

        只读、永不抛。未启用时如实回 `enabled:false` 且不碰任何进程。
        """
        manager = get_mcp_manager()
        payload = manager.status()
        return {
            "enabled": bool(getattr(self.agent.config, "mcp_enabled", False)),
            "configPath": str(self.agent.config.mcp_config_path),
            "servers": payload.get("servers") or [],
            "configError": payload.get("configError") or "",
        }

    def mcp_reload(self) -> dict[str, Any]:
        """`POST /api/mcp/reload`：重读 servers 配置（用户本人动作，ADR-011 同范式）。

        **不落盘**（无检查点、无 409）：只重起外部进程、重建工具清单。
        先摘掉旧的 `mcp__*` 工具并关掉全部实例，再按新配置重新并入 ——
        因而「删掉一个 server」也会真的从工具清单里消失。
        重连过程中的 `mcp.server_start` 事件即这条动作的审计留痕。
        """
        config = self.agent.config
        manager = get_mcp_manager()
        for name in [
            n for n in self.registry.names() if n.startswith(MCP_NAME_PREFIX)
        ]:
            self.registry.unregister(name)
        manager.detach_all()

        servers = _mcp_registration(config)
        if servers:
            merge_mcp(self.registry, servers, audit=self.agent.audit, manager=manager)
        return self.mcp_status()

    def web_reload(self) -> None:
        """联网开关热切换（M7）：按当前 config 摘掉 / 并入 web 工具。

        `apply_settings` 保存 webEnabled 时调用 —— 关→摘除、开→并入，
        与 `mcp_reload` 同一范式（区别：web 工具是内置 spec，无外部进程）。
        同时把 `agent.ctx.config` 换成新 Config 对象：其余 web 字段
        （host 名单 / 超时 / 上限 / allow_private）不增删工具，工具执行时
        从 `ctx.config` 读 —— 引用一换即热生效，无需重建会话。

        工具面纪律不变：web 工具恒 REVIEW、不进只读面、结果 wrap_untrusted
        （见 tools/web.py 文档），本方法只做清单增删，不改任何行为面。
        """
        config = self.agent.config
        self.agent.ctx.config = config
        for name in ("web_search", "web_fetch"):
            if name in self.registry.names():
                self.registry.unregister(name)
        if config.web_enabled:
            self.registry.register(WEB_SEARCH_SPEC)
            self.registry.register(WEB_FETCH_SPEC)

    def _mcp_write_guard(self) -> None:
        """MCP 管理写入的前置闸门：未启用 400、执行中 409、治理面接管 400。

        与 ``apply_settings`` 同一纪律：**先过闸门再落盘**，绝不留下
        「文件改了、运行时没改」的错位。MCP 未启用时也拒绝写入 —— 否则用户会
        配好一堆 server 却发现一个都不生效（``_mcp_registration`` 直接返回 None）。

        治理面只在**真的接管运行时**（模型请求经云端网关）时才拒绝写入 ——
        与模型配置同一判据（``is_config_locked``）。治理面从不托管 MCP 目录，
        禁云 / 降级时本机文件就是唯一真源，此时挡住保存只会让用户改不了。
        """
        if not getattr(self.agent.config, "mcp_enabled", False):
            raise ValueError("MCP 未启用（VIBE_MCP）；请先在设置里开启后再管理 server")
        if self.state != P.STATE_IDLE:
            raise BusyError("任务执行中，无法修改 MCP server 配置，请先中断或等任务结束")
        if self.governance is not None and is_config_locked(self.governance):
            raise ValueError("治理面已接管，界面不支持修改 MCP server 配置")

    def _mcp_load_existing(self) -> dict[str, Any]:
        """读取既有 servers（按 name 建索引）。坏配置抛 ``ValueError``，绝不覆盖。"""
        try:
            return {s.name: s for s in load_mcp_servers(self.agent.config.mcp_config_path)}
        except McpClientError as exc:
            raise ValueError(f"MCP 配置文件无法读取，已拒绝写入：{exc}") from exc

    def mcp_upsert_server(self, body: dict[str, Any]) -> dict[str, Any]:
        """`POST /api/mcp/servers`：新增 / 替换一个 server 配置（用户本人动作，ADR-011）。

        只**写配置文件**、不拼命令：argv 仍只来自受信配置文件（ADR-015）。单条
        先经 ``_to_server()`` 校验，非法即 400 且整份不写（不做部分写入）。成功后
        记 ``mcp.server_added`` 并自动 reload —— 新 server 的 `mcp__*` 工具即刻可查。
        """
        self._mcp_write_guard()
        item = body.get("server")
        if not isinstance(item, dict):
            raise ValueError("缺少 server 对象")
        parsed = _parse_mcp_server(str(item.get("name") or "").strip(), item)
        if parsed is None:
            raise ValueError("server 配置非法：name 不能为空，stdio 需要 command，http 需要 url")

        existing = self._mcp_load_existing()
        existing[parsed.name] = parsed
        save_mcp_servers(existing.values(), self.agent.config.mcp_config_path)
        self.agent.audit.record(
            "mcp.server_added",
            server=parsed.name,
            transport=parsed.transport,
            argc=len(parsed.args),
        )
        return self.mcp_reload()

    def mcp_delete_server(self, name: str) -> dict[str, Any]:
        """`POST /api/mcp/servers/remove`：从配置里移除一个 server（用户本人动作）。

        不存在即 400；存在则写回配置文件、关掉实例并 reload —— 该 server 的
        `mcp__*` 工具随之从 registry 消失。只记 ``mcp.server_removed`` 元数据。
        """
        self._mcp_write_guard()
        server_name = str(name or "").strip()
        if not server_name:
            raise ValueError("缺少 name")
        existing = self._mcp_load_existing()
        removed = existing.pop(server_name, None)
        if removed is None:
            raise ValueError(f"server 不存在：{server_name}")
        save_mcp_servers(existing.values(), self.agent.config.mcp_config_path)
        self.agent.audit.record(
            "mcp.server_removed",
            server=server_name,
            transport=removed.transport,
            argc=len(removed.args),
        )
        # reload 内部 detach_all() 会连该 server 的实例一并关掉，并从清单摘除工具。
        return self.mcp_reload()

    def mcp_test_server(self, name: str) -> dict[str, Any]:
        """`POST /api/mcp/test`：起一次该 server 并列工具（复用 manager 惰性启动）。

        与 ``list_tools`` 同路径：首次调用即 spawn + 握手；失败**不抛**，如实回
        `{ok:false, error}` 供界面就地提示。这次动作的审计由 ``mcp.server_start``
        覆盖（manager 里记账），无需新事件。
        """
        server_name = str(name or "").strip()
        if not server_name:
            raise ValueError("缺少 name")
        manager = get_mcp_manager()
        try:
            tools = manager.list_tools(server_name)
        except McpClientError as exc:
            return {"ok": False, "name": server_name, "error": str(exc), "tools": []}
        return {
            "ok": True,
            "name": server_name,
            "count": len(tools),
            "tools": [
                f"{MCP_NAME_PREFIX}{server_name}__{str(t.get('name') or '')}"
                for t in tools
            ],
        }

    # ------------------------------------------------------------------ 多模态图片（N/P1-10）
    def upload_image(
        self, name: str, data: str, media_type: str = ""
    ) -> dict[str, Any]:
        """`POST /api/upload-image`：把前端贴/选的图片落进 host 侧 `.vibe/images/`。

        「用户本人动作」：前端把图片 base64 发来，后端**按魔数**判真类型（不信
        文件名与后缀），落到 `PathGuard` 保护的 `.vibe/images/<uuid>.<ext>`，再返回
        一个 `image` 引用。正文（base64）**只留本机** —— 审计只记字节数、扩展名与
        摘要（ADR-006）。`vision_mode=off` 时明确拒绝（界面也会隐藏入口）。
        """
        cfg = self.agent.config
        if cfg.vision_mode == "off":
            raise ValueError(
                "图片输入未开启（VIBE_VISION=off）；如需据图改代码请先显式开启"
            )

        raw = str(data or "").strip()
        if not raw:
            raise ValueError("图片数据为空")
        # 允许直接传 data URL；即使带了前缀，真类型仍以魔数为准，绝不信前缀。
        if raw.startswith("data:"):
            _, _, raw = raw.partition(",")

        try:
            blob = base64.b64decode(raw)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"图片不是合法的 base64 数据：{exc}") from exc

        limit = max(1, int(cfg.vision_max_image_bytes))
        if len(blob) > limit:
            raise ValueError(
                f"图片过大（{len(blob)} 字节，上限 {limit}）；请压缩后重试，"
                "或调大 VIBE_VISION_MAX_BYTES"
            )

        identified = identify_image(blob)
        if identified is None:
            raise ValueError("不是受支持的图片格式（仅 png / jpg / webp / gif）")
        ext, _mime = identified

        dest_dir = cfg.workspace / ".vibe" / "images"
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{uuid.uuid4().hex}{ext}"
            dest.write_bytes(blob)
        except OSError as exc:
            raise ValueError(f"图片落盘失败：{exc}") from exc

        rel = self.agent.paths.relative(dest)
        # 指纹取落盘文件的**原始字节**（sha256 前 16 位，与 file_digest 同一口径）
        fingerprint = file_digest(dest)
        # 审计只记元数据（ADR-006）：字节数、扩展名、内容摘要；**图片正文不出本机**。
        self.agent.audit.record(
            "image.upload", bytes=len(blob), ext=ext, digest=fingerprint
        )
        return {
            "ref": {"type": "image", "value": rel},
            "bytes": len(blob),
            "ext": ext,
            "digest": fingerprint,
        }

    # ------------------------------------------------------------------ 浏览器预览 / 网页自检（O/P1-11）
    def visual_check(
        self,
        url: str,
        width: int = 1280,
        height: int = 800,
        image: str = "",
        baseline: str = "",
        set_baseline: bool = False,
    ) -> dict[str, Any]:
        """`POST /api/visual/check`：把一张屏幕截图与基线比对（「用户本人动作」范式）。

        截图由**壳**提供（Electron 的 `vibe:capture-page` 离屏截图）；host 这一层
        只负责：解码 PNG、与 `.vibe/visual/<hash>.png` 基线做**粗粒度**差异、
        必要时覆盖基线。浏览器直开时前端拿不到截图，于是不带 `image` 调用 ——
        这里如实回 `{available: false, reason}`，**不是 500**（与 LSP/MCP 同范式）。

        绝不写工作区源码：基线只落 host 侧的 `.vibe/visual/`（模型经 PathGuard
        写不进该目录）。审计只记 URL 摘要、尺寸、差异千分比与是否缺基线
        （ADR-006：**不记截图内容**）。
        """
        cfg = self.agent.config
        started = time.monotonic()
        raw_url = str(url or "").strip()
        if not raw_url:
            raise ValueError("缺少 url")
        w = max(1, min(int(width or 1280), 8000))
        h = max(1, min(int(height or 800), 8000))

        raw_image = str(image or "").strip()
        if raw_image.startswith("data:"):
            _, _, raw_image = raw_image.partition(",")
        if not raw_image:
            return {
                "available": False,
                "ok": False,
                "reason": (
                    "当前环境没有屏幕捕获能力（浏览器直开请用桌面端；"
                    "Electron 壳提供离屏截图桥）"
                ),
            }

        try:
            blob = base64.b64decode(raw_image)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"截图不是合法的 base64 数据：{exc}") from exc
        if not visual.is_png(blob):
            raise ValueError("截图不是 PNG 数据")
        limit = visual.DEFAULT_MAX_SCREENSHOT_BYTES
        if len(blob) > limit:
            raise ValueError(f"截图过大（{len(blob)} 字节，上限 {limit}）")

        # 基线键 = URL + 尺寸 + 可选命名。同一页面同一尺寸共用一条基线；
        # 换设备宽度会落到另一条键上（否则手机/桌面截图会互相判为「变了」）。
        key = hashlib.sha256(
            f"{raw_url}|{w}x{h}|{baseline or ''}".encode("utf-8", "replace")
        ).hexdigest()[:16]
        base_dir = cfg.workspace / ".vibe" / "visual"
        base_path = base_dir / f"{key}.png"
        current_digest = hashlib.sha256(blob).hexdigest()[:16]

        def _audit(diff_permille: int, missing: bool) -> None:
            self.agent.audit.record(
                "visual.check",
                url_digest=digest(raw_url),
                width=w,
                height=h,
                diff_ratio=diff_permille,
                baseline_missing=missing,
                ms=int((time.monotonic() - started) * 1000),
            )

        # 「设为基线」：把当前截图写成新基线，差异比恒为 0。
        if set_baseline:
            try:
                base_dir.mkdir(parents=True, exist_ok=True)
                base_path.write_bytes(blob)
            except OSError as exc:
                raise ValueError(f"写入基线失败：{exc}") from exc
            _audit(0, False)
            return {
                "available": True,
                "ok": True,
                "saved": True,
                "baselineMissing": False,
                "diffRatio": 0.0,
                "changedBlocks": 0,
                "totalBlocks": 0,
                "diffImageDigest": current_digest,
            }

        # 没有基线：如实回 baselineMissing，让界面引导「设为基线」（不是错误）。
        if not base_path.is_file():
            _audit(0, True)
            return {
                "available": True,
                "ok": True,
                "baselineMissing": True,
                "diffRatio": 0.0,
                "changedBlocks": 0,
                "totalBlocks": 0,
                "diffImageDigest": current_digest,
            }

        try:
            baseline_bytes = base_path.read_bytes()
        except OSError as exc:
            raise ValueError(f"读取基线失败：{exc}") from exc

        try:
            ratio, changed, total = visual.diff_ratio(blob, baseline_bytes)
        except ValueError as exc:
            # 基线/当前图解析不了：如实回原因，不假装比过了，更不 500。
            return {
                "available": True,
                "ok": False,
                "reason": f"无法比对（{exc}）",
                "baselineMissing": False,
                "diffRatio": 0.0,
                "diffImageDigest": current_digest,
            }

        _audit(int(round(ratio * 1000)), False)
        return {
            "available": True,
            "ok": True,
            "baselineMissing": False,
            "diffRatio": ratio,
            "changedBlocks": changed,
            "totalBlocks": total,
            "diffImageDigest": current_digest,
            "ms": int((time.monotonic() - started) * 1000),
        }

    # ------------------------------------------------------------------ 交互式终端（M/P1-7）
    def term_open(self, cols: int = 80, rows: int = 24) -> dict[str, Any]:
        """`POST /api/term/open`：起一个交互式终端（「用户本人动作」，ADR-011 范式）。

        不走审批浮层（是用户自己敲的命令），但**记审计**（`term.open`，只记 argc /
        argv 指纹 / 尺寸，见 ADR-006）；Agent 想交互仍只能提需求，由用户执行 ——
        Agent 通道未被打通。`VIBE_TERM=0` 时明确拒绝（界面也会隐藏入口）。
        """
        cfg = self.agent.config
        if not cfg.terminal_enabled:
            raise ValueError("交互式终端已被配置关闭（VIBE_TERM=0）")
        cols = max(1, min(int(cols), 1000))
        rows = max(1, min(int(rows), 1000))

        manager = get_terminal_manager()
        manager.set_max_sessions(cfg.terminal_max_sessions)
        # 终端跟随工作区所在的设备：本机模式起本地 shell；远程工作区（M13）
        # `@remote/<连接>/<子目录>` 起 `ssh -tt`；清单驱动的远端项目工作区（M11 §7）
        # 起 `ssh -tt` 落在该连接的 root。三者共同保证不会有「文件在远端、终端在本机」
        # 这种最容易误伤的对错配。
        label = ""
        kind = "local"
        connection = ""
        argv = default_shell_argv(cfg.terminal_shell)
        if cfg.remote_enabled and str(getattr(cfg, "remote_workspace", "") or ""):
            sessions = self.remote_sessions()
            if sessions.active() is None:
                # 连不上时给出**具体原因**（公钥/指纹/网络），而不是让用户对着
                # 「终端起不来」猜；这里也绝不静默回退本机 shell。
                raise ValueError(self._remote_error or "远程工作区尚未连上，无法启动远程终端")
            try:
                argv, connection = remote_terminal_argv(
                    sessions, str(cfg.remote_workspace)
                )
            except (RemoteConfigError, RemoteError) as exc:
                raise ValueError(str(exc)) from None
            kind = "remote"
            label = connection
        elif cfg.remote_enabled:
            # 清单驱动的远端项目工作区（M11 §7）：目录里只有 `.vibe/remote.json`，
            # 代码全在设备上。终端必须跟过去，否则用户会在一个空的本机目录里敲命令。
            try:
                manifest = manifest_terminal_argv(cfg)
            except (RemoteConfigError, RemoteError) as exc:
                raise ValueError(str(exc)) from None
            if manifest is not None:
                argv, connection = manifest
                kind = "remote"
                label = connection
        started = time.monotonic()
        try:
            session = manager.open(
                argv=argv,
                cwd=str(cfg.workspace),
                env=self._terminal_env(),
                cols=cols,
                rows=rows,
                on_data=self._term_data,
                on_exit=self._term_exit,
                label=label,
                kind=kind,
                connection=connection,
                # ssh 要等握手 + 远端登录，比本地 shell 慢得多；宽限期短了会把
                # 一条好伪终端误判成「哑终端」直接降级。
                probe_seconds=REMOTE_TERM_PROBE_SECONDS if kind == "remote" else None,
            )
        except TerminalError as exc:
            raise ValueError(str(exc)) from None

        with self._lock:
            self._term_ids.add(session.id)
        # 审计只记元数据（ADR-006）：termId 是短标识、argc 是计数、
        # argv_digest 是命令指纹；**命令正文只留本机**（不进审计记录）。
        self.agent.audit.record(
            "term.open",
            termId=session.id,
            argc=len(argv),
            argv_digest=digest(" ".join(argv)),
            cols=cols,
            rows=rows,
            mode=session.mode,
            remote=connection,
            ms=int((time.monotonic() - started) * 1000),
        )
        return {
            "termId": session.id,
            "mode": session.mode,
            "shell": session.label or shell_label(argv),
            "kind": kind,
            "connection": connection,
            "cols": cols,
            "rows": rows,
            "pid": session.pid,
            "fallbackReason": manager.fallback_reason,
        }

    def term_list(self) -> dict[str, Any]:
        """`GET /api/term`：列出活着的终端会话（界面刷新后据此恢复标签页）。

        只回元数据（id / mode / shell / 尺寸 / 空闲秒数），**不含任何终端输出**
        —— 回滚缓冲只在浏览器里，服务端从来没有过正文（ADR-006）。
        """
        cfg = self.agent.config
        if not cfg.terminal_enabled:
            return {"enabled": False, "maxSessions": 0, "sessions": []}
        status = get_terminal_manager().status()
        return {
            "enabled": True,
            "maxSessions": int(status.get("maxSessions") or 0),
            "idleSeconds": int(status.get("idleSeconds") or 0),
            "fallbackReason": str(status.get("fallbackReason") or ""),
            "sessions": list(status.get("sessions") or []),
        }

    def term_input(self, term_id: str, data: str) -> dict[str, Any]:
        """`POST /api/term/input`：把按键/粘贴内容写进终端。

        **不记审计**：逐次键入会爆量（计划 M8 明确只在 open/close 记）。
        """
        raw = str(data or "")
        if raw == "":
            return {"ok": True, "chars": 0}
        manager = get_terminal_manager()
        try:
            manager.write(term_id, raw.encode("utf-8", errors="replace"))
        except TerminalError as exc:
            raise ValueError(str(exc)) from None
        return {"ok": True, "chars": len(raw)}

    def term_resize(self, term_id: str, cols: int, rows: int) -> dict[str, Any]:
        """`POST /api/term/resize`：容器尺寸变化时重设窗口，交互程序据此重排。"""
        manager = get_terminal_manager()
        try:
            manager.resize(term_id, int(cols), int(rows))
        except TerminalError as exc:
            raise ValueError(str(exc)) from None
        return {"ok": True, "cols": int(cols), "rows": int(rows)}

    def term_close(self, term_id: str) -> dict[str, Any]:
        """`POST /api/term/close`：关闭终端并收掉子进程树（记审计 `term.close`）。"""
        manager = get_terminal_manager()
        try:
            result = manager.close(term_id)
        except TerminalError as exc:
            raise ValueError(str(exc)) from None
        with self._lock:
            self._term_ids.discard(term_id)
        self.agent.audit.record(
            "term.close",
            termId=term_id,
            argc=0,
            argv_digest="",
            cols=0,
            rows=0,
            mode=str(result.get("mode") or ""),
            exit_code=int(result.get("exitCode") or 0),
            ms=int(result.get("ms") or 0),
        )
        # 前端据此收起终端视图（显式关闭与进程自退走同一条事件）。
        self.bus.publish(
            P.event(
                P.TERM_EXIT,
                termId=term_id,
                exitCode=int(result.get("exitCode") or 0),
            )
        )
        return {"ok": True, **result}

    def _terminal_env(self) -> dict[str, str]:
        """终端的子进程环境：**继承用户环境**（这是他自己的 shell）。

        与 Agent 的 `run_command` 不同，这里**不做凭据脱敏** —— 用户在自己的终端里
        跑 `git push` 期待的就是他配好的凭据。只补 `TERM`/`COLORTERM`，
        让交互程序按彩色输出（见计划验收）。
        """
        env = dict(os.environ)
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("COLORTERM", "truecolor")
        return env

    def _term_data(self, term_id: str, chunk: bytes) -> None:
        """读线程回调：把终端输出推成 `TERM_DATA` 事件（base64 承载原始字节）。

        与 `run.*` 事件流**分离** —— 终端输出不属于 Agent 的一轮任务，
        混进去会污染会话视图（见 protocol.py 的「终端是独立通道」）。
        """
        self.bus.publish(
            P.event(
                P.TERM_DATA,
                termId=term_id,
                data=base64.b64encode(chunk).decode("ascii"),
            )
        )

    def _term_exit(self, term_id: str, code: int | None) -> None:
        """终端进程自退（用户敲了 exit），推 `TERM_EXIT` 让界面收起视图。"""
        with self._lock:
            self._term_ids.discard(term_id)
        self.bus.publish(
            P.event(P.TERM_EXIT, termId=term_id, exitCode=int(code or 0))
        )

    # ------------------------------------------------------------------ 语言服务器（E）
    def lsp_status(self) -> dict[str, Any]:
        """`GET /api/lsp/status`：模式 + 全量探测清单 + 当前活实例。只读，永不抛。"""
        return get_manager().status(self.agent.config)

    def lsp_diagnostics(self, path: str) -> dict[str, Any]:
        """`GET /api/lsp/diagnostics`：单文件诊断（只读，不落盘）。

        语言服务器不可用时返回 `{available: false, reason}` 而不是报错 ——
        界面据此显示「LSP 不可用」并把 gutter 留空，不该让用户看到 500。
        """
        paths = self.agent.paths
        abs_path = paths.check_read(path)
        if not abs_path.is_file():
            raise ValueError(f"{paths.relative(abs_path)} 不存在或不是普通文件")
        rel = paths.relative(abs_path)
        result = get_manager().diagnostics(self.agent.config, abs_path, rel)
        if not result.get("available"):
            return {
                "available": False,
                "path": rel,
                "reason": result.get("reason") or "LSP 不可用",
                "counts": {},
                "diagnostics": [],
            }
        return {
            "available": True,
            "path": rel,
            "language": result.get("language") or "",
            "server": result.get("server") or "",
            "counts": result.get("counts") or {},
            "diagnostics": [
                _diagnostic_api(item) for item in (result.get("diagnostics") or [])
            ],
        }

    def lsp_definition(self, path: str, line: int, char: int) -> dict[str, Any]:
        """`GET /api/lsp/definition`：跳转定义（只读）。

        `line` 对前端是 **1 基**（与 gutter、`rewrite` 的 `startLine` 一致），
        `char` 是 **0 基 UTF-16 列**（与编辑器 caret 一致）；转给 LSP 时行号减一。
        """
        paths = self.agent.paths
        abs_path = paths.check_read(path)
        if not abs_path.is_file():
            raise ValueError(f"{paths.relative(abs_path)} 不存在或不是普通文件")
        rel = paths.relative(abs_path)
        result = get_manager().definition(
            self.agent.config, abs_path, rel, int(line) - 1, int(char)
        )
        if not result.get("available"):
            return {
                "available": False,
                "path": rel,
                "reason": result.get("reason") or "LSP 不可用",
                "locations": [],
            }
        return {
            "available": True,
            "path": rel,
            "locations": [
                {
                    "path": item.get("path") or "",
                    "outside": bool(item.get("outside")),
                    "line": int(item.get("line") or 0) + 1,
                    "char": int(item.get("char") or 0),
                    "endLine": int(item.get("endLine") or 0) + 1,
                    "endChar": int(item.get("endChar") or 0),
                }
                for item in (result.get("locations") or [])
            ],
        }

    def lsp_rename_preview(
        self, path: str, line: int, char: int, new_name: str
    ) -> dict[str, Any]:
        """`POST /api/lsp/rename/preview`（E5）：算重命名的逐文件 diff，**不落盘**。

        与 `rewrite` 同一范式：返回 `token`，落盘只能靠这个 token 取服务端缓存；
        前端回传的正文一律不被信任。落盘在 `lsp_rename_apply`。
        """
        new_name = (new_name or "").strip()
        if not new_name:
            raise ValueError("新名称不能为空")
        if len(new_name) > MAX_RENAME_NAME:
            raise ValueError(f"新名称过长（上限 {MAX_RENAME_NAME} 字符）")
        if any(ch.isspace() for ch in new_name):
            raise ValueError("新名称不能包含空白字符")

        paths = self.agent.paths
        self._ensure_write_enabled()
        # 重命名会落盘，因此按**写**口径校验起点（只读检查会放过工作区内但
        # 受保护的文件，那类文件不该出现在重命名预览里）。
        abs_path = paths.check_write(path)
        if not abs_path.is_file():
            raise ValueError(f"{paths.relative(abs_path)} 不存在或不是普通文件")
        rel = paths.relative(abs_path)

        result = get_manager().rename(
            self.agent.config, abs_path, rel, int(line) - 1, int(char), new_name
        )
        if not result.get("available"):
            return {
                "available": False,
                "path": rel,
                "reason": result.get("reason") or "LSP 不可用",
                "files": [],
                "edits": 0,
            }

        files: list[dict[str, Any]] = []
        for item in result.get("edits") or []:
            target = self._rename_target(item)
            if target is not None:
                files.append(target)
        total = sum(len(item["edits"]) for item in files)

        token = uuid.uuid4().hex[:16]
        with self._lock:
            self._renames[token] = {"files": files, "nameChars": len(new_name)}
        self.agent.audit.record(
            "lsp.rename.preview",
            path=rel,
            line=int(line),
            char=int(char),
            files=len(files),
            edits=total,
            # 新名称只留指纹与长度（ADR-006），不记正文
            new_name_digest=digest(new_name),
            new_name_chars=len(new_name),
        )
        return {
            "available": True,
            "token": token,
            "path": rel,
            "edits": total,
            "noChange": not total,
            "files": [
                {
                    "path": item["rel"],
                    "diff": item["diff"],
                    "added": item["added"],
                    "removed": item["removed"],
                    "edits": len(item["edits"]),
                }
                for item in files
            ],
        }

    def lsp_rename_apply(self, token: str) -> dict[str, Any]:
        """`POST /api/lsp/rename/apply`：按 token 应用重命名（E5）。

        「用户本人动作」：不走审批浮层（ADR-011），但仍受三条边界约束 ——
        运行中 409、落盘前收检查点、写审计。**先整批校验再落盘**：任一文件
        过期就整批不动，避免「改了一半」这种最难收拾的中间态。
        """
        with self._lock:
            if self._state != P.STATE_IDLE:
                raise BusyError("任务执行中，无法应用重命名，请等任务结束或先中断")
            draft = self._renames.get(token)
        if draft is None:
            raise ValueError("重命名草稿已失效，请重新生成")

        files: list[dict[str, Any]] = draft["files"]
        paths = self.agent.paths
        prepared: list[dict[str, Any]] = []
        for item in files:
            paths.check_write(item["rel"])  # 切换工作区后 token 失效，这里再兜一层
            if not item["abs"].is_file():
                raise StaleFileError(f"{item['rel']} 已不存在，重命名已失效")
            if TextDoc.load(item["abs"]).text != item["old_text"]:
                raise StaleFileError(
                    f"{item['rel']} 已被改动，重命名已失效，请重新打开后再试"
                )
            prepared.append(item)

        touched: list[str] = []
        added_total = removed_total = 0
        # 整批落盘：走事务（P1-8），任一文件写失败即把已写文件回滚为原内容。
        txn = EditTransaction(paths=paths, snapshot=self.agent.ctx.snapshot_before)
        for item in prepared:
            txn.add_overwrite(item["abs"], item["new_text"], overwrite=True)
        try:
            report = txn.commit()
        except TransactionError as exc:
            self.agent.audit.record(
                "edit.transaction",
                files_count=len(prepared),
                changed_count=0,
                added=0,
                removed=0,
                strategy="atomic",
                rolled_back=exc.rolled_back,
            )
            raise
        by_rel = {item["rel"]: item for item in prepared}
        for plan in report.changed:
            item = by_rel[plan.rel_path]
            self.agent.ctx.record_change(
                path=plan.rel_path, diff=plan.diff, added=plan.added, removed=plan.removed
            )
            self.agent.audit.record(
                "lsp.rename.apply",
                path=plan.rel_path,
                edits=len(item["edits"]),
                added=plan.added,
                removed=plan.removed,
                strategy="lsp-rename",
            )
            touched.append(plan.rel_path)
            added_total += plan.added
            removed_total += plan.removed

        with self._lock:
            self._renames.pop(token, None)
        if not touched:
            note = "重命名没有产生实际改动"
            self._record_notice(note)
            return {"ok": True, "changed": False, "files": 0, "paths": [], "notice": note}
        note = f"已应用重命名：{len(touched)} 个文件（+{added_total}/-{removed_total}）"
        self._record_notice(note)
        self.bus.publish(P.event(P.RUN_NOTICE, runId=self.run_id, text=note))
        return {
            "ok": True,
            "changed": True,
            "files": len(touched),
            "paths": touched,
            "added": added_total,
            "removed": removed_total,
            "notice": note,
        }

    def _rename_target(self, item: dict[str, Any]) -> dict[str, Any] | None:
        """把一个 uri 的 edits 归一成「读旧文 → 算新文 → 出 diff」的落盘素材。

        工作区外的文件（`outside`）与不产生实际改动的文件直接跳过 ——
        前者越权，后者会污染预览列表。
        """
        path = _uri_to_path(str(item.get("uri") or ""))
        if path is None or not path.is_file():
            return None
        paths = self.agent.paths
        try:
            paths.check_write(path)
        except SandboxViolation:
            return None
        rel = paths.relative(path)
        edits = item.get("edits") or []
        doc = TextDoc.load(path)
        new_text = _apply_lsp_edits(doc.text, edits)
        if new_text is None or new_text == doc.text:
            return None
        plan = plan_overwrite(path, new_text, rel_path=rel)
        return {
            "abs": path,
            "rel": rel,
            "old_text": doc.text,
            "new_text": new_text,
            "diff": plan.diff,
            "added": plan.added,
            "removed": plan.removed,
            "edits": edits,
        }

    @staticmethod
    def _line_range(start_line: int, end_line: int, total: int) -> tuple[int, int]:
        """把 1 基、双端包含的行号转成 ``split("\\n")`` 的 0 基半开区间 [start, end)。"""
        start = max(1, int(start_line)) - 1
        end = int(end_line)
        if end <= start:  # 单行选区或参数颠倒：至少覆盖一行
            end = start + 1
        if start >= total:
            raise ValueError(f"起始行超出文件范围（文件共 {total} 行）")
        return start, min(end, total)

    # ------------------------------------------------------------------ 运行
    def resume(self, messages: list[dict[str, Any]], session_id: str | None = None) -> None:
        """恢复历史会话（A2）：用已持久化的消息重建 Agent。

        校验（workspace 一致、成组合法、非忙）由 server 层完成，这里只管
        重建本身。历史消息不再触发 on_message —— 它们已经落过盘。
        """
        with self._lock:
            if self._state != P.STATE_IDLE:
                raise BusyError("已有任务正在执行，不能恢复会话")
        config = self.config
        if session_id:
            # 「继续那个会话」语义：后续消息落回原会话，而不是 fork 新行。
            # Config 是 frozen dataclass，只能用 replace 换实例 —— 旧 Agent
            # 随即被丢弃，新旧配置不需要保持同步。
            config = replace(config, session_id=session_id)
        agent = Agent(
            config=config,
            registry=self.registry,
            client=self._client,
            constraints=self.governance.constraints if self.governance else None,
            hooks=_SessionHooks(self),
            approver=self._approve,
            should_stop=self._interrupt.is_set,
            initial_messages=messages,
        )
        with self._lock:
            self.config = config
            self.agent = agent
            self._answer = ""
            self._error = ""
            self._notices = [f"已恢复历史会话（{len(messages)} 条消息），可以继续对话。"]
        self.bus.publish(P.event(P.SESSION_READY, **self.describe()))

    def _remote_ref_resolver(self):
        """`@remote/...` 引用的解析器；未启用远端路由时返回 None。

        与本地引用一样在**提交线程、会话锁外**解析：远端不可达 / 目录未加入工作区
        只让这一条引用降级为提示，不阻断整条任务。连接与根的判定完全复用
        `RemoteRouter` —— 「右键加入的远端文件」与「模型用 read_file 读同一路径」
        因此走同一条路由，不存在两套口径。
        """
        router = getattr(self.agent, "remote_router", None)
        if router is None:
            return None

        def resolve(rtype: str, value: str) -> tuple[str, str]:
            hit = router.match(value)
            if hit is None:  # 只有 @remote/ 前缀才会进来，理论上不可达
                raise ToolError(f"无法解析远端引用：{value}")
            conn = router.store().get(hit.connection)
            if conn is None:
                raise ToolError(f"远端连接不存在：{hit.connection}")
            workspace = open_workspace(
                conn,
                allow_sensitive=self.agent.paths.allow_sensitive,
                denied_patterns=tuple(self.agent.paths.denied_patterns),
            )
            rel = hit.remote_rel()
            if rtype == "folder":
                entries = workspace.list_dir(rel)
                return hit.ns_path, _remote_listing_text(hit.ns_path, entries)
            result = workspace.read(rel, max_bytes=MAX_SCAN_BYTES)
            return hit.ns_path, "\n".join([_FILE_HEADER, result.text, _FILE_FOOTER])

        return resolve

    def submit(self, text: str, refs: list[dict[str, Any]] | None = None) -> str:
        """提交一个任务，立即返回 runId；实际执行在后台线程。

        `refs`（P0-A）是前端的「引用意图」列表，形如
        `[{"type": "file", "value": "src/app.py"}]`。正文由**后端**解析并前置
        到提示里 —— 前端只传意图，越权路径在前端根本碰不到（见 context/references）。

        不传 refs 时 prompt 与 text 逐字节相同，行为与今天一行不变。
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("任务内容为空")

        cfg = self.agent.config
        # 图片输入（N / P1-10）：只有显式开启（vision_mode != off）时才会解析图片；
        # off 时 `image` 引用逐条降级为提示（见 resolve_refs），消息仍是纯字符串。
        # 引用解析要读磁盘，放在锁外完成，避免占着会话锁做 IO。
        prompt, resolution = build_refs_prompt(
            self.agent.paths,
            text,
            refs,
            allow_images=cfg.vision_mode != "off",
            max_image_bytes=cfg.vision_max_image_bytes,
            remote_resolver=self._remote_ref_resolver(),
        )
        # 无图片时保持纯字符串 —— 逐字节兼容（请求体与改造前完全一致）；
        # 有图片时组装成 parts 列表（先文字、后图片）。
        message: str | list[dict[str, Any]] = prompt
        if resolution.images:
            message = to_message_parts(prompt, resolution.images)

        with self._lock:
            if self._state != P.STATE_IDLE:
                raise BusyError("已有任务正在执行，请等待完成或先中断")
            self._run_seq += 1
            run_id = f"run-{self._run_seq}"
            self._run_id = run_id
            self._state = P.STATE_RUNNING
            self._interrupt.clear()
            self._error = ""
            self._title_hint = text
            # 本轮用户消息的 seq 锚点：此刻消息还没落库（run 开始后才 append），
            # 但 seq 是 max+1 单调分配的，可以**预先**算出来；run 结束时用它给
            # 本轮检查点打标，界面就能把「这条提问」与「它改过的文件」对上。
            self._run_user_seq = 0
            if self.store is not None:
                try:
                    self._run_user_seq = self.store.max_seq(self.config.session_id) + 1
                except Exception:  # noqa: BLE001 — 打标失败只影响逐条回退，不影响任务
                    self._run_user_seq = 0
            # 每个新任务都从零开始：清掉上一轮残留的增量缓冲，否则上一轮回答
            # 末尾的片段会串进本轮首个增量（界面表现为「回答重复」）。
            reset = getattr(self.agent.hooks, "reset_deltas", None)
            if callable(reset):
                reset()
            self.bus.publish(
                P.event(P.RUN_STARTED, runId=run_id, prompt=text, userSeq=self._run_user_seq)
            )

            # 逐条引用解析失败降级为提示，不让整条任务失败
            for line in resolution.errors:
                note = f"引用未解析：{line}"
                self._record_notice(note)
                self.bus.publish(P.event(P.RUN_NOTICE, runId=run_id, text=note))

            thread = threading.Thread(
                target=self._worker, args=(run_id, message), name=f"vibe-{run_id}", daemon=True
            )
            self._thread = thread
        thread.start()
        return run_id

    def _worker(self, run_id: str, text: str | list[dict[str, Any]]) -> None:
        try:
            answer = self.agent.run(text)
        except LLMError as exc:
            self._finish_failed(run_id, str(exc), "llm")
        except AgentError as exc:
            self._finish_failed(run_id, str(exc), type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 — 绝不让工作线程静默死掉
            self._finish_failed(run_id, f"{type(exc).__name__}: {exc}", "internal")
        else:
            interrupted = self._interrupt.is_set()
            # 逐条回退（P3）：检查点已在 Agent.run 的 finally 里提交，这里用
            # 提交时预计算的用户消息 seq 给它打标 —— 界面按「这条提问」找到
            # 「它改过的文件」就靠 manifest 里的 userSeq（重启后依然有效）。
            # 打标失败只影响回退精度，绝不影响本轮任务本身。
            last_id = self.agent.checkpoints.last_id
            if last_id:
                try:
                    self.agent.checkpoints.annotate(last_id, self._run_user_seq)
                except Exception:  # noqa: BLE001 — 标注是尽力而为
                    pass
            with self._lock:
                self._state = P.STATE_IDLE
                self._answer = "" if interrupted else answer
            # 收尾补报一次用量（P2-17）：本轮的 `run.context` 都发在各轮**开头**，
            # 不补这一次的话预算条会停在上一次裁剪之前的中间态，看不出最终占用。
            self.bus.publish(
                P.event(P.RUN_CONTEXT, runId=run_id, **self.agent.context_stats())
            )
            if interrupted:
                self.bus.publish(P.event(P.RUN_INTERRUPTED, runId=run_id, answer=answer))
            else:
                self.bus.publish(
                    P.event(
                        P.RUN_FINISHED,
                        runId=run_id,
                        answer=answer,
                        stats=self.stats(),
                        changedFiles=self.changed_files,
                        checkpointId=self.agent.checkpoints.last_id,
                    )
                )
        finally:
            with self._lock:
                self._state = P.STATE_IDLE
                self._thread = None
                self._pending.clear()
                # 任务收尾 = 所有审批都已了结，草稿必须一起作废：留着的 token
                # 就是一段「内容来自模型、却没落盘、也再没人审」的悬挂读取面。
                self._drafts.clear()

    def _finish_failed(self, run_id: str, error: str, kind: str) -> None:
        with self._lock:
            self._state = P.STATE_IDLE
            self._error = error
        self.agent.audit.record("agent.run.failed", kind=kind, error=error[:300])
        self.bus.publish(P.event(P.RUN_FAILED, runId=run_id, error=error, kind=kind))

    def interrupt(self) -> bool:
        """请求中断。返回是否确实有任务被中断。

        三级生效，越靠前越快：

        1. **模型流式生成**：下一个 SSE 帧就断开连接（`consume_chat_sse` 的
           `should_stop`），不再等整段回答跑完；
        2. **等待中的审批**：立即释放为拒绝，不会让用户白等；
        3. **正在执行的工具**：外部掐不断（子进程/远端命令），跑完当前这一个；
           同一批里尚未执行的调用补一条「未执行」结果，保证消息序列自洽。

        历史始终以 assistant 收尾，因此停下后可以立刻接着对话。
        """
        with self._lock:
            if self._state == P.STATE_IDLE:
                return False
            self._interrupt.set()
            waiting = list(self._pending.values())

        for pending in waiting:
            pending.decision = "deny"
            pending.gate.set()
        self.agent.audit.record("agent.run.interrupt_requested")
        return True

    def end_session(self, reason: str) -> None:
        """结束当前这一段会话（在审计链上写一条 session.end）。

        切换工作区（W1）时用：旧工作区的链要有一个明确收尾，新会话会在
        **新工作区**的链上重新 session.start —— 两条链各自完整、互不污染。
        """
        with self._lock:
            record_session_end(
                self.agent.audit,
                reason=reason,
                iterations=self.agent.stats.iterations,
                duration_ms=int((time.time() - self._started_at) * 1000),
            )

    def reset(self) -> None:
        with self._lock:
            if self._state != P.STATE_IDLE:
                raise BusyError("任务执行中，无法重置")
            # 重置即结束当前这一段会话，再开新的一段。
            # 注意：进程被强杀时不会有 session.end —— 这是刻意的取舍，
            # 不去做「退出钩子」那类不可靠的补偿；审计里靠 session.start 与
            # agent.run.* 仍然能把一段工作界定出来。
            self.end_session("reset")
            self.agent.reset()
            self.agent.ctx.written_files.clear()
            self.agent.ctx.applied_diffs.clear()
            self.agent.ctx.changes.clear()
            self.agent.ctx.read_files.clear()
            self.agent.stats = RunStats()
            self._notices.clear()
            self._rewrites.clear()
            self._renames.clear()
            self._drafts.clear()
            self._completions.clear()
            self._answer = ""
            self._error = ""
            self._run_seq = 0
            self._started_at = time.time()
            record_session_start(self.agent.audit, self.config)
        self.bus.publish(P.event(P.SESSION_READY, **self.describe()))

    # ------------------------------------------------------------------ 审批
    def _approve(self, request: ApprovalRequest) -> str:
        """审批会合点。运行在 Agent 工作线程上，会阻塞到这个函数被唤醒。"""
        # 失效安全之一：已被请求中断
        if self._interrupt.is_set():
            return "deny"

        # 失效安全之二：没有界面连着，没人能按按钮 —— 不能干等着超时
        if self.bus.subscriber_count == 0:
            self.agent.audit.record(
                "approval.no_ui", tool=request.tool, decision="deny"
            )
            return "deny"

        approval_id = uuid.uuid4().hex[:12]
        pending = _PendingApproval(
            id=approval_id, request=request, gate=threading.Event()
        )
        with self._lock:
            self._pending[approval_id] = pending
            self._state = P.STATE_AWAITING_APPROVAL

        self.bus.publish(
            P.event(
                P.RUN_APPROVAL,
                runId=self.run_id,
                approvalId=approval_id,
                tool=request.tool,
                arguments=_safe_arguments(request.arguments),
                risk=request.risk.value,
                reason=request.reason,
                preview=serialize_preview(request.preview),
                draftPaths=[f.path for f in request.draft],
            )
        )

        signaled = pending.gate.wait(timeout=self.approval_timeout)
        with self._lock:
            self._pending.pop(approval_id, None)
            if self._state == P.STATE_AWAITING_APPROVAL:
                self._state = P.STATE_RUNNING
            # 审批已了结（放行 / 拒绝 / 超时都算）→ 草稿立即作废。
            # 放在**等待返回之后**而不是 resolve 里：超时这条路径也要清，
            # 而超时根本不经过 resolve_approval。
            self._revoke_drafts(approval_id)

        if not signaled:
            decision = "deny"
            self.agent.audit.record(
                "approval.timeout", tool=request.tool, timeout_s=self.approval_timeout
            )
        else:
            decision = pending.decision

        self.bus.publish(
            P.event(
                P.RUN_APPROVAL_RESOLVED,
                runId=self.run_id,
                approvalId=approval_id,
                decision=decision,
                timedOut=not signaled,
            )
        )
        return decision

    def resolve_approval(self, approval_id: str, decision: str) -> bool:
        """由 HTTP 线程调用：把用户的决定送达等待中的 Agent 线程。"""
        if decision not in P.DECISIONS:
            raise ValueError(f"非法的决定: {decision!r}")
        with self._lock:
            pending = self._pending.get(approval_id)
            if pending is None:
                return False
            pending.decision = decision
            # 先作废草稿再放行：决定一旦成立，这份「未落盘的正文」就不该再被读到。
            # Agent 线程醒来后还会再作废一次（超时路径不走这里），两处都清才没有漏网。
            self._revoke_drafts(approval_id)
            pending.gate.set()
        return True

    # ------------------------------------------------------- 草稿预览（P1-2）
    def preview_draft(self, approval_id: str) -> dict[str, Any]:
        """为「正在等着的这次审批」发一个草稿 token（同一审批幂等）。

        草稿正文来自 `ApprovalRequest.draft` —— 它是在**审批发起那一刻**算好的，
        与审批人眼前的 diff 描述同一个结果。这里只做登记，不重算、不读盘。

        抛异常的两类情况（服务端分别映射为 409 / 400）：
        `LookupError` —— 审批已结束（已批 / 已拒 / 已超时），草稿不再可用；
        `ValueError` —— 这次改动没有可渲染的文件。
        """
        with self._lock:
            pending = self._pending.get(approval_id)
            if pending is None:
                raise LookupError("该审批已结束（已批 / 已拒 / 已超时），草稿不再可用")
            if pending.draft_token:
                existing = self._drafts.get(pending.draft_token)
                if existing is not None:
                    return _draft_payload(existing, pending.draft_token)

            files = pending.request.draft
            if not files:
                raise ValueError("这次改动没有可预览的页面（只有网页类文件能生成草稿）")

            token = uuid.uuid4().hex[:16]
            primary = min((f.path for f in files), key=draft_rank)
            job: dict[str, Any] = {
                "approvalId": approval_id,
                "token": token,
                "primary": primary,
                # 正文按路径索引：HTML 引用的 CSS/JS 若在同一批改动里，
                # 取到的必须是**改后**的内容，否则「预览」会拿旧样式渲染新结构。
                "files": {f.path: f.text for f in files},
                "created": sorted(f.path for f in files if f.created),
            }
            self._drafts[token] = job
            pending.draft_token = token

        # 审计：只记 token 指纹与目标路径，不记正文（ADR-006）。
        # 「谁在什么时候看过一份未落盘的草稿」是需要留痕的，
        # 「草稿里写了什么」不需要 —— 那个内容本身就在审批 diff 里。
        self.agent.audit.record(
            "preview.draft",
            path=primary,
            token=digest(token),
            files=len(files),
            chars=sum(len(text) for text in job["files"].values()),
        )
        return _draft_payload(job, token)

    def draft_scope(self, token: str) -> dict[str, Any]:
        """取一份草稿的只读视图。失效（已撤销 / 从未存在）一律 `ValueError`。"""
        with self._lock:
            job = self._drafts.get(token)
            if job is None:
                return {}
            return {
                "approvalId": job["approvalId"],
                "primary": job["primary"],
                "files": dict(job["files"]),
                "created": list(job["created"]),
            }

    def _revoke_drafts(self, approval_id: str) -> None:
        """撤销某个审批名下的全部草稿 token（调用方需持有 `_lock`，或由 RLock 兜底）。"""
        stale = [
            token
            for token, job in self._drafts.items()
            if job.get("approvalId") == approval_id
        ]
        for token in stale:
            self._drafts.pop(token, None)

    # ------------------------------------------------------------------ 更新（M9）
    def _update_service(self) -> UpdateService:
        """按当前配置装配更新服务（懒建；options 变了就重建）。

        `client` 取治理面客户端（未接入则为 None → 只走本地镜像 S2）；
        `audit` 用 Agent 的审计实例，`update.*` 事件因此自动进同一条链；
        `run_active` 让下载前的 409 判定复用会话本来的「有任务在跑」语义。
        """
        options = UpdateOptions.from_config(self.agent.config)
        service = self._update
        if service is None or service.options != options:
            runtime = self.governance
            service = UpdateService(
                options,
                client=runtime.governance if runtime is not None else None,
                audit=self.agent.audit,
                run_active=lambda: self.state != P.STATE_IDLE,
            )
            self._update = service
        return service

    def update_check(self, *, auto: bool = True) -> dict[str, Any]:
        """`GET /api/update/check`：检查更新（只读、不下载）。

        发现有可装版本时补发 `update.available` + `run.notice`（与启动通知同一
        通道：都不是 Agent 的一轮任务，所以 `runId` 留空）。失败则发
        `update.failed`，让界面能显示「检查没成功」，而不是静默。
        """
        view = self._update_service().check(auto=auto)
        status = str(view.get("status") or "")
        if status == "available":
            note = (
                f"发现新版本 v{view.get('version')}（当前 v{view.get('currentVersion')}）"
                + ("，该版本为强制更新" if view.get("mandatory") else "")
            )
            self._record_notice(note)
            self.bus.publish(P.event(P.RUN_NOTICE, runId="", text=note))
            self.bus.publish(P.event(P.UPDATE_AVAILABLE, runId="", **view))
        elif status == "failed":
            self.bus.publish(
                P.event(
                    P.UPDATE_FAILED,
                    runId="",
                    reason=str(view.get("reason") or ""),
                    kind="check",
                )
            )
        return view

    def update_download(self, version: str = "") -> dict[str, Any]:
        """`POST /api/update/download`：下载安装包到 staging。

        与写盘路径同一纪律：**有任务在跑就 409**（下载会占带宽并写文件，
        与 Agent 的落盘/检查点争用没有好处）。校验通过后发 `update.staged`
        带 staging 路径 —— 壳拿它做形态校验后自己 spawn 安装器。
        """
        if self.state != P.STATE_IDLE:
            raise BusyError("任务执行中，请先中断或等结束再下载更新包")
        service = self._update_service()

        def on_progress(done: int, total: int) -> None:
            self.bus.publish(
                P.event(P.UPDATE_PROGRESS, version=version, done=done, total=total)
            )

        try:
            view = service.download(version=version, on_progress=on_progress)
        except AgentError as exc:
            kind = str(getattr(exc, "kind", "") or "network")
            self.bus.publish(P.event(P.UPDATE_FAILED, runId="", reason=str(exc), kind=kind))
            raise
        self.bus.publish(
            P.event(
                P.UPDATE_STAGED,
                version=str(view.get("version") or ""),
                stagingPath=str(view.get("stagingPath") or ""),
                size=int(view.get("size") or 0),
                source=str(view.get("source") or ""),
                reused=bool(view.get("reused")),
            )
        )
        return view

    def update_status(self) -> dict[str, Any]:
        return self._update_service().status()

    def update_view(self) -> dict[str, Any]:
        """`describe()` 用的精简视图：读状态文件得结论，**不联网、不检查**。

        与 `update_status()` 的区别只在字段面：这里只回界面画 chip 需要的几项，
        避免把 `lastError`/`maxSeq` 这类本机细节塞进每次 state 快照。
        """
        options = UpdateOptions.from_config(self.agent.config)
        view = self._update_service().status()
        return {
            "enabled": not options.disabled,
            "channel": str(view.get("channel") or options.channel),
            "status": str(view.get("status") or "idle"),
            "availableVersion": str(view.get("availableVersion") or ""),
            "stagedPath": str(view.get("stagedPath") or ""),
            "currentVersion": str(view.get("currentVersion") or ""),
        }

    # ------------------------------------------------------------------ 通知
    def _record_notice(self, text: str) -> None:
        with self._lock:
            self._notices.append(text)


class _SessionHooks(AgentHooks):
    """把 Agent 的内部动作翻译成事件。界面所见即此处所发。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        # 增量节流状态（A1）：片段先进缓冲，攒够时间/字数再发布。
        # 片段按序拼接仍然等于全文 —— 只延迟，不丢弃。
        self._delta_buffer: list[str] = []
        self._delta_chars = 0
        self._last_flush = time.monotonic()
        self._titled = False  # 首条用户消息做会话标题（A2）
        # 思考增量缓冲（reasoning_content）：与回答同一套节流参数，但**独立一份**，
        # 免得两股文本互相串味（回答里混进半截思考、或反过来）。
        self._reason_buffer: list[str] = []
        self._reason_chars = 0
        self._last_reason_flush = time.monotonic()

    @property
    def _bus(self) -> EventBus:
        return self.session.bus

    @property
    def _run_id(self) -> str:
        return self.session.run_id

    def reset_deltas(self) -> None:
        """新任务开始：清掉上一轮残留的增量缓冲。

        必须清 —— 残留会把「上一轮回答末尾的片段」拼进下一轮的首个增量，
        界面上看起来就像上一轮回答又被重播了一遍（跨轮串味）。缓冲跨轮驻留
        在同一个 hooks 实例上，而每个新任务都该从零开始。
        """
        self._delta_buffer.clear()
        self._delta_chars = 0
        self._last_flush = time.monotonic()
        self._reason_buffer.clear()
        self._reason_chars = 0
        self._last_reason_flush = time.monotonic()

    def flush_deltas(self) -> None:
        """把回答缓冲的残留立即发布。工具调用/收口前调用，保证不丢字。"""
        if not self._delta_buffer:
            return
        piece = "".join(self._delta_buffer)
        self._delta_buffer.clear()
        self._delta_chars = 0
        self._last_flush = time.monotonic()
        self._bus.publish(
            P.event(P.RUN_ASSISTANT_DELTA, runId=self._run_id, text=piece)
        )

    def _flush_reasoning(self) -> None:
        """把思考缓冲的残留立即发布（与 flush_deltas 同范式）。"""
        if not self._reason_buffer:
            return
        piece = "".join(self._reason_buffer)
        self._reason_buffer.clear()
        self._reason_chars = 0
        self._last_reason_flush = time.monotonic()
        self._bus.publish(
            P.event(P.RUN_REASONING_DELTA, runId=self._run_id, text=piece)
        )

    def on_iteration(self, index: int, total: int) -> None:
        self._bus.publish(
            P.event(P.RUN_ITERATION, runId=self._run_id, index=index, total=total)
        )

    def on_assistant_delta(self, text: str) -> None:
        if not text:
            return
        self._delta_buffer.append(text)
        self._delta_chars += len(text)
        now = time.monotonic()
        if (
            now - self._last_flush >= DELTA_FLUSH_INTERVAL
            or self._delta_chars >= DELTA_FLUSH_CHARS
        ):
            self.flush_deltas()

    def on_reasoning_delta(self, text: str) -> None:
        """模型思考（reasoning）增量：与回答同款节流，发独立事件。

        纯呈现，不落盘、不进会话库 —— 刷新页面靠会话快照补齐（reasoning 属
        运行态，与 `run.plan` 同理，不进消息历史）。
        """
        if not text:
            return
        self._reason_buffer.append(text)
        self._reason_chars += len(text)
        now = time.monotonic()
        if (
            now - self._last_reason_flush >= DELTA_FLUSH_INTERVAL
            or self._reason_chars >= DELTA_FLUSH_CHARS
        ):
            self._flush_reasoning()

    def on_message(self, message: dict[str, Any]) -> None:
        """会话持久化（A2）：每条真实消息同步追加进 SQLite。

        落盘失败不弄死会话 —— 记一条提醒就继续（审计在，正文丢了可接受）。
        """
        store = self.session.store
        if store is None:
            return
        config = self.session.config
        role = str(message.get("role") or "")
        content = message.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False) if content is not None else ""
        extra = {k: v for k, v in message.items() if k not in ("role", "content")}
        meta = json.dumps(extra, ensure_ascii=False) if extra else None

        title: str | None = None
        if role == "user" and not self._titled:
            self._titled = True
            # 标题优先用用户原文（run.started 的 prompt）：带引用块的前置提示
            # 若拿来当标题，会变成「[引用上下文] …」，既没信息量又千篇一律。
            base = self.session._title_hint.strip() or content.strip()
            lines = base.splitlines()
            title = (lines[0] if lines else base)[:40]

        try:
            store.append_message(
                config.session_id,
                workspace=str(config.workspace),
                model=config.model,
                role=role,
                content=content,
                tool_meta=meta,
                title=title,
            )
        except Exception as exc:  # noqa: BLE001 — 落盘失败绝不弄死会话
            self.on_notice(f"会话落盘失败：{type(exc).__name__}: {exc}")

    def on_tool_call(self, call: Any, risk: Risk, reason: str) -> None:
        # 本轮文本先补齐再上卡片：否则本轮末尾滞留缓冲的片段会拖到下一轮，
        # 界面上表现为「回答被截断 + 尾巴串到后面」。
        self.flush_deltas()
        self._bus.publish(
            P.event(
                P.RUN_TOOL_CALL,
                runId=self._run_id,
                callId=call.id,
                tool=call.name,
                arguments=_safe_arguments(call.arguments),
                risk=risk.value,
                reason=reason,
            )
        )

    def on_tool_result(self, call: Any, result: ToolResult) -> None:
        body, clipped = _clip(result.content)
        self._bus.publish(
            P.event(
                P.RUN_TOOL_RESULT,
                runId=self._run_id,
                callId=call.id,
                tool=call.name,
                ok=result.ok,
                content=body,
                clipped=clipped,
                diff=result.diff[:MAX_PREVIEW_BYTES],
                meta={k: v for k, v in result.meta.items() if _jsonable(v)},
            )
        )

    def on_tool_output(self, call: Any, stream: str, chunk: str) -> None:
        """命令流式输出：把 `run_command` 子进程的增量片段转发给界面。

        与 on_assistant_delta 同定位 —— **纯呈现**：不落盘、不进会话库，
        刷新页面靠快照补齐；最终结果仍以 on_tool_result 为准（覆盖式收口）。
        文本经 `_clip` 限幅，防止单块异常巨大。
        """
        body, clipped = _clip(chunk)
        self._bus.publish(
            P.event(
                P.RUN_TOOL_OUTPUT,
                runId=self._run_id,
                callId=call.id,
                tool=call.name,
                stream=stream,
                text=body,
                clipped=clipped,
            )
        )

    def on_subagent(self, event: dict[str, Any]) -> None:
        """子 Agent 进度（M3）：把一次 `task` 委派的生命周期帧转发给界面。

        `callId` 关联父侧 `task` 工具卡片，界面据此把子列表归位。**只转发元数据**：
        `event` 由 `Agent._delegate` / `_SubAgentHooks` 组装，里面没有子 Agent 正文
        （正文只随 `task` 工具的返回值回到父上下文，经 `wrap_untrusted` 包裹）。
        """
        self._bus.publish(P.event(P.RUN_SUBAGENT, runId=self._run_id, **event))

    def on_notice(self, text: str) -> None:
        self.session._record_notice(text)
        self._bus.publish(P.event(P.RUN_NOTICE, runId=self._run_id, text=text))

    def on_plan(self, steps: list[dict[str, Any]], revision: int) -> None:
        """计划更新（P2-12）：转发模型给出的完整步骤表。

        不节流：与 delta 不同，计划一次只有一个版本，且每次更新都是用户
        该看到的状态跃迁（「第 2 步做完了」），攒着发只会让界面看起来卡住。
        """
        self._bus.publish(
            P.event(P.RUN_PLAN, runId=self._run_id, steps=steps, revision=revision)
        )

    def on_context(self, stats: dict[str, Any]) -> None:
        """上下文用量（P2-17）：每轮**裁剪之后**转发一次。

        不节流：一轮最多一条，且它表达的是「这一轮模型真正看到的长度」，
        攒着发只会让预算条落后于实际（同 on_plan 的理由）。
        """
        self._bus.publish(P.event(P.RUN_CONTEXT, runId=self._run_id, **stats))


# --------------------------------------------------------------------------- 工具
def _jsonable(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool, type(None), list, dict))


def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """参数可能带超大字段（如 write_file 的整份内容），裁剪后再进事件流。"""
    out: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if isinstance(value, str) and len(value) > 4_000:
            out[key] = value[:4_000]
            out[f"{key}__clipped"] = True
        elif _jsonable(value):
            out[key] = value
        else:
            out[key] = str(value)
    return out


def _draft_payload(job: dict[str, Any], token: str) -> dict[str, Any]:
    """草稿的对外描述。**不含正文** —— 正文只经 `/preview-draft/<token>/…` 取。

    这样「列有哪些草稿文件」与「读某一份草稿」是两个权限面：前者只暴露路径，
    后者才给内容，且必须带 token。
    """
    return {
        "token": token,
        "primary": job["primary"],
        "paths": sorted(job["files"]),
        "created": list(job["created"]),
    }


def _approval_payload(pending: _PendingApproval) -> dict[str, Any]:
    request = pending.request
    return {
        "approvalId": pending.id,
        "tool": request.tool,
        "arguments": _safe_arguments(request.arguments),
        "risk": request.risk.value,
        "reason": request.reason,
        "preview": serialize_preview(request.preview),
        # 有草稿可看时才给路径 —— 界面据此决定要不要显示「预览效果」。
        # 只给路径，不给内容：内容要单独向 /api/preview/draft 换 token 才拿得到。
        "draftPaths": [f.path for f in request.draft],
    }


__all__ = [
    "BusyError",
    "Session",
    "StaleFileError",
    "serialize_preview",
]
