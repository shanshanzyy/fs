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
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

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
from ..errors import AgentError, LLMError, SandboxViolation
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
from ..sandbox import Risk
from ..terminal.manager import (
    TerminalError,
    default_shell_argv,
    get_manager as get_terminal_manager,
)
from ..testing import collect as collect_tests, run as run_tests
from ..tools import ToolRegistry, ToolResult, ToolPreview, build_default_registry
from ..tools.plan import PLAN_STATUSES
from ..governance.runtime import wire
from . import protocol as P
from . import visual
from .bus import EventBus
from .files import file_digest
from .store import SessionStore

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
    """
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
        if self.agent.config.index_watch:
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
            )
            self.watcher.start()

    def close(self) -> None:
        """释放会话持有的进程资源（索引 watch 线程 + 交互式终端）。

        切换工作区时由 HostServer 调用：旧 Session 可以丢弃，但它的后台线程
        与子进程不能跟着「丢」在进程里 —— 那会留下一串持续运行、无人使用的
        终端进程（见 terminal/manager 的 atexit 兜底）。
        """
        watcher, self.watcher = self.watcher, None
        if watcher is not None:
            watcher.stop()
        # 记忆库（P2-15）：切工作区会换掉整个 Session，旧库连接必须关掉 ——
        # Windows 下未关闭的 sqlite 会锁住库文件（新会话连同一个用户级库时踩到）。
        if self.memory is not None:
            self.memory.close()
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
        return {
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
            },
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
            "lastAnswer": self._answer,
            "pendingApproval": self.pending_approval,
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

    def _mcp_write_guard(self) -> None:
        """MCP 管理写入的前置闸门：未启用 400、执行中 409、治理面接管 400。

        与 ``apply_settings`` 同一纪律：**先过闸门再落盘**，绝不留下
        「文件改了、运行时没改」的错位。MCP 未启用时也拒绝写入 —— 否则用户会
        配好一堆 server 却发现一个都不生效（``_mcp_registration`` 直接返回 None）。
        """
        if not getattr(self.agent.config, "mcp_enabled", False):
            raise ValueError("MCP 未启用（VIBE_MCP）；请先在设置里开启后再管理 server")
        if self.state != P.STATE_IDLE:
            raise BusyError("任务执行中，无法修改 MCP server 配置，请先中断或等任务结束")
        if self.governance is not None:
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
        argv = default_shell_argv(cfg.terminal_shell)
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
            ms=int((time.monotonic() - started) * 1000),
        )
        return {
            "termId": session.id,
            "mode": session.mode,
            "cols": cols,
            "rows": rows,
            "pid": session.pid,
            "fallbackReason": manager.fallback_reason,
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
            self.bus.publish(P.event(P.RUN_STARTED, runId=run_id, prompt=text))

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

    def _finish_failed(self, run_id: str, error: str, kind: str) -> None:
        with self._lock:
            self._state = P.STATE_IDLE
            self._error = error
        self.agent.audit.record("agent.run.failed", kind=kind, error=error[:300])
        self.bus.publish(P.event(P.RUN_FAILED, runId=run_id, error=error, kind=kind))

    def interrupt(self) -> bool:
        """请求中断。返回是否确实有任务被中断。

        中断在**轮次边界**生效：当前这一次模型调用或工具执行会跑完，
        这样消息序列始终自洽（详见 `Agent.should_stop` 的说明）。
        等待中的审批会被立即释放为拒绝，不会让用户白等。
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
            )
        )

        signaled = pending.gate.wait(timeout=self.approval_timeout)
        with self._lock:
            self._pending.pop(approval_id, None)
            if self._state == P.STATE_AWAITING_APPROVAL:
                self._state = P.STATE_RUNNING

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
        pending.gate.set()
        return True

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

    @property
    def _bus(self) -> EventBus:
        return self.session.bus

    @property
    def _run_id(self) -> str:
        return self.session.run_id

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
            self._last_flush = now
            self._delta_chars = 0
            piece = "".join(self._delta_buffer)
            self._delta_buffer.clear()
            self._bus.publish(
                P.event(P.RUN_ASSISTANT_DELTA, runId=self._run_id, text=piece)
            )

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


def _approval_payload(pending: _PendingApproval) -> dict[str, Any]:
    request = pending.request
    return {
        "approvalId": pending.id,
        "tool": request.tool,
        "arguments": _safe_arguments(request.arguments),
        "risk": request.risk.value,
        "reason": request.reason,
        "preview": serialize_preview(request.preview),
    }


__all__ = [
    "BusyError",
    "Session",
    "StaleFileError",
    "serialize_preview",
]
