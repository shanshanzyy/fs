"""运行时配置。

优先级：显式参数 > 环境变量 > 默认值。
所有与环境变量相关的键统一使用 VIBE_ 前缀，便于内网统一注入。
"""

from __future__ import annotations

import os
import urllib.parse
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from .errors import ConfigError
from .index.watch import MIN_INTERVAL as MIN_WATCH_INTERVAL
from .project_rules import parse_test_command, parse_verify_command

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"环境变量 {name} 需要布尔值，实际收到: {raw!r}")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 需要整数，实际收到: {raw!r}") from exc


# LSP 模式是枚举而不是布尔：三态（auto/on/off）里「探测不到」与「被关掉」
# 是两种不同的降级原因，界面要分开显示，所以不能压成布尔。
_LSP_MODES = ("auto", "on", "off")
# prompt caching（P2-17）同样是三态。
_CACHE_MODES = ("auto", "on", "off")
# 多模态输入（N / P1-10）同样是三态：off 不接受图片、auto 探测上游是否支持、
# on 强制发送并把报错如实回传。
_VISION_MODES = ("auto", "on", "off")
# 联网检索上游（M7）两形态：gateway = 经治理面网关中转（客户端零密钥）；
# searxng = 直连 SearXNG JSON 端点（内网自部署/公共实例）。
_WEB_SEARCH_PROVIDERS = ("gateway", "searxng")


def _env_choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    """枚举型环境变量：小写归一后必须落在白名单里，否则启动前报错。"""
    value = _env_str(name, default).lower()
    if value not in allowed:
        raise ConfigError(
            f"环境变量 {name} 需要 {'/'.join(allowed)} 之一，实际收到: {value!r}"
        )
    return value


def _env_lsp_mode(name: str, default: str) -> str:
    return _env_choice(name, default, _LSP_MODES)


@dataclass(frozen=True)
class Config:
    """一次会话的全部可变配置。frozen 防止运行中被意外改写。"""

    # --- 工作区 ---
    workspace: Path

    # --- 模型 ---
    model: str = "qwen2.5-coder:7b"
    base_url: str = "http://127.0.0.1:11434/v1"
    api_key: str = "ollama"
    temperature: float = 0.1
    request_timeout: int = 300
    # 流式输出（A1）。推理端不支持 SSE 时会自动回退一次性请求，
    # 这个开关是给「回退也失败」的极端环境留的手动闸。
    stream_enabled: bool = True

    # --- 循环 ---
    max_iterations: int = 25
    max_context_chars: int = 120_000
    tool_output_limit: int = 8_000

    # --- 上下文窗口管理（P2-17）---
    # token 预算口径：>0 时用 token 估算决定何时裁剪（H3），0 = 沿用字符口径
    # （max_context_chars），保持向后兼容。单位是 token 而非字符 ——
    # 中文一个字约 1 token、英文约 1/4，字符数同量级下 token 量可差 4 倍。
    context_budget_tokens: int = 0
    # prompt caching：auto = 探测一次（上游拒收未知字段即永久回退不注入）；
    # on = 恒注入稳定前缀键；off = 不注入。三态枚举，非法值启动前报错。
    prompt_cache: str = "auto"

    # --- 上下文裁剪升级（C4）---
    # 开启后，裁剪前先把待删的那段历史交给当前模型压成一段摘要，
    # 而不是直接丢弃。默认**关**：摘要会给每次裁剪多一轮模型调用
    #（有成本、可能失败），而 C1/C3 已经把上下文质量做上去了。
    compact_on_trim: bool = False

    # --- 安全开关（注意：高危审批不受这里控制，见 ADR-008）---
    allow_write: bool = True
    allow_shell: bool = True
    allow_network_commands: bool = False
    allow_sensitive_read: bool = False
    command_timeout: int = 60

    # --- 审批 ---
    auto_approve_safe: bool = True   # SAFE 级工具自动放行
    # REVIEW 级**恒**需人工确认（ADR-008：不可配置关闭）。该字段保留只为兼容
    # 治理面策略协议与 UI 展示，审批闸门不再读它（见 loop/agent.py 闸门 2）。
    auto_approve_review: bool = False

    # --- 审计 ---
    audit_enabled: bool = True
    session_id: str = ""

    # --- 会话持久化（A2）---
    # 本地库存的是本机会话正文（与「服务端不存正文」不冲突，ADR-006）。
    # 高合规团队可整体关闭；库位置可用 VIBE_SESSION_DB 覆盖（测试/便携场景）。
    session_persist: bool = True
    session_db: str = ""  # 空 = ~/.vibe/sessions.db

    # --- 验证闭环（C2）---
    # Agent 在收尾前自动执行的验证命令（如跑测试）；空 = 不验证。
    # 优先级：VIBE_VERIFY_COMMAND > VIBE.md 的 `verify:` 约定行 > 空。
    # 命令本身免审批但过 CommandGuard 白名单，模型无法在运行时改写它。
    verify_command: str = ""

    # --- IDE 内测试树（K / P2-14）---
    # 跑单个/整批用例的命令（如 `python -m pytest -q`）；空 = 测试面板显示
    # 「未配置测试命令」。与 verify 同范式：来自**用户配置**（环境变量 / VIBE.md
    # 的 `test:` 约定行），模型在运行时改不了它，执行前仍过 CommandGuard 白名单。
    test_command: str = ""
    # 收集用例时追加的参数（pytest 的 `--collect-only -q` 会逐行输出 node id）。
    # 单独成字段而不是写死：换框架时只改这一条（本轮只保证 pytest）。
    test_collect_args: str = "--collect-only -q"

    # --- 规则与记忆（P2-15）---
    # 额外规则路径 / glob（逗号分隔，相对工作区），叠加在内置的多层规则之后。
    rules_extra: str = ""
    # 记忆总开关。关掉后：不读不写记忆、不注册 remember 工具、不注入记忆段
    #（行为与未实现前逐字节一致）。默认**开** —— 记忆只在本机、写入恒经人审。
    memory_enabled: bool = True
    # 记忆条数上限（用户级库的容量闸）。超了明确拒绝新写入，而不是静默丢旧的。
    memory_max_items: int = 200
    # 每轮任务注入几条相关记忆（按词法相关度取 top-N）；0 = 不注入。
    memory_inject_limit: int = 5
    # 记忆库位置。空 = ~/.vibe/memories.db（用户级，跨工作区可见）。
    memory_db: str = ""

    # --- 语义检索（B5）---
    # 空 = 不启用向量，semantic_search 走纯词法（功能可用，只是精度降一档）。
    # 设置后走 OpenAI 兼容 /embeddings；探不到端点自动回退词法，绝不因此失败。
    # 注意：若把它配成公网端点，源码片段会离开本机 —— 这是用户的显式选择。
    embed_model: str = ""
    # 单次建索引最多读多少个文件，防止在超大仓库上把内存与耗时拉爆。
    semantic_max_files: int = 800

    # --- 增量索引 / 文件监听（J / P1-9）---
    # 默认**关**：开启后会常驻一个轮询线程，不显式打开就不该有这份开销。
    # 开启后文件增删改会增量合进索引，改完不必重启也能搜到。
    index_watch: bool = False
    # 轮询间隔（秒），下限 2（见 index/watch.MIN_INTERVAL）。再密就是白烧 CPU。
    index_watch_interval: int = 5

    # --- 内联补全 / 行内改写（D3）---
    # 内联补全（Tab）开关。补全只产文本、不落盘，因此不触发审批闸门；
    # 关掉后 /api/complete 直接拒绝（界面据此隐藏补全能力）。
    fim_enabled: bool = True
    # 单次补全的输出 token 上限。补全贵在「快」，给太大反而拖慢首字。
    completion_max_tokens: int = 256
    # 补全时喂给模型的光标前/后上下文字符数（行对齐截断）。大文件不能整份上送，
    # 否则 token 不可控、首字变慢。0 = 不截断。
    completion_prefix_chars: int = 4000
    completion_suffix_chars: int = 2000

    # --- 语言服务器（E）---
    # auto = 探测到就启用、探测不到就降级（默认）；on = 强制要求（探测不到时端点
    # 仍返回 available:false 及原因，不做任何远端回退）；off = 完全关闭。
    lsp_mode: str = "auto"
    # 空闲多少秒后自动 shutdown 语言服务器进程（不常驻）。0 = 不自动回收。
    lsp_idle_seconds: int = 300

    # --- MCP 客户端（L / P1-6）---
    # 默认**关**：开启后会按需 spawn 外部 MCP server 进程（那是别人的代码）。
    # 关着时这一层完全不存在，会话行为与未实现前逐字节一致。
    mcp_enabled: bool = False
    # MCP servers 配置文件路径。空 = <workspace>/.vibe/mcp.json（见 mcp_config_path）。
    mcp_config: str = ""
    # 单次握手 / 工具调用的超时（秒）。外部进程不可信，超时是唯一的兜底。
    mcp_timeout: int = 30

    # --- 真实交互式终端（M / P1-7）---
    # 默认**开**：终端是「用户在 IDE 里敲命令」，与 Agent 通道彻底隔离
    #（Agent 跑命令仍走受审批的 run_command）。关掉（VIBE_TERM=0）时整个
    # 终端入口隐藏，行为与未实现前逐字节一致。
    terminal_enabled: bool = True
    # 终端启动的程序：空 = 自动（Unix 取 $SHELL，Windows 用 powershell）。
    # 只接受**单条命令名/路径**，不接受带 shell 语法的整串 —— argv 由 /api/term/open
    # 固定组装，模型与前端都改不了它。
    terminal_shell: str = ""
    # 同时最多几个终端会话；超了明确拒绝而不是无限起进程。
    terminal_max_sessions: int = 3

    # --- 多模态输入（N / P1-10）---
    # 默认**关**：图片会随请求发往**用户配置的模型端点**，端点非本地时图片就离开
    # 了本机 —— 这是会外发的能力，必须显式开（与 embed_model 同一纪律）。
    # off = 不接受图片（界面无入口、refs 里的 image 降级为提示，行为与未实现前一致）；
    # auto = 首次带图请求若上游以 400/422 判「不支持图片」，永久回退为纯文本并提示；
    # on = 不探测，错误如实回传。
    vision_mode: str = "off"
    # 单张图片字节上限（解码后）。截图/导出图通常几十 KB ~ 几 MB，上限防止
    # 把整段上下文与内存一并拉爆；超限明确拒绝而不是默默截断。
    vision_max_image_bytes: int = 4_000_000

    # --- 多 Agent 编排（M3）---
    # 默认**关**：开启后父 Agent 才多出一个 `task` 工具，可派生**只读**子 Agent
    # 去做探索/分析/检索。关着时工具清单与行为与未实现前**逐字节一致**
    #（与 mcp_enabled / vision_mode 同范式：不改工具清单即不改行为）。
    subagent_enabled: bool = False
    # 子 Agent 自己的迭代上限。委派是一次「探索子任务」，不该与父任务同预算；
    # 单独设上限既防成本失控，也逼子 Agent 把范围收敛后再回话。
    subagent_max_iterations: int = 8

    # --- 检索增强（M3+）---
    # 默认**关**：开启后 semantic_search 走增强路径（标识符子词化 + 符号感知分块
    # + RRF 融合 + 按文件去冗余）；关着时结果与改造前**逐字节一致**。
    # 增强不新增依赖：embedding 仍是可选端点，缺则回退纯词法。
    retrieval_enhanced: bool = False

    # --- Repo Wiki（M3+）---
    # 默认**关**：开启后 Agent 多出一个 repo_wiki 工具（恒 REVIEW，写盘走审批，
    # 与 write_file 同一条编辑面）；关着时工具清单与行为与未实现前**逐字节一致**。
    wiki_enabled: bool = False
    # Wiki 落点（相对工作区）。**不得默认 .vibe/** —— 那是审计与检查点专用目录，
    # 由 PathGuard 拒绝写入。默认 docs/wiki/。
    wiki_dir: str = "docs/wiki"
    # 单次生成最多覆盖多少个源文件，防止在超大仓库上拉爆内存与耗时。
    wiki_max_files: int = 400

    # --- 受控联网检索（M7）---
    # 默认**关**：web_search / web_fetch 都会把内容送出本机（查询词进搜索上游、
    # URL 进目标服务器），会外发的能力必须显式开（与 vision_mode / mcp_enabled
    # 同一纪律）。关着时两个工具不注册，行为与未实现前逐字节一致。
    web_enabled: bool = False
    # 搜索上游形态：gateway = 经治理面网关中转（客户端零密钥、零公网出口，
    # 密钥托管在服务端，ADR-007 同范式）；searxng = 直连 SearXNG JSON 端点。
    web_search_provider: str = "gateway"
    # SearXNG 的 JSON 端点（searxng 形态用）；gateway 形态忽略
    #（由 server_url 派生 <server>/api/v1/search）。
    web_search_url: str = ""
    # 是否允许 fetch 私网/环回地址（内网文档服务器场景）。
    # 默认关：仅公网可取 —— SSRF 的最基本闸（解析后校验 IP 见 tools/web.py）。
    web_fetch_allow_private: bool = False
    # host 白名单（逗号分隔 glob）。非空时仅白名单内 host 可 fetch。
    web_allow_hosts: str = ""
    # host 黑名单（逗号分隔 glob），**恒优先于白名单**；
    # 治理面策略下发的 webDeniedHosts 并进这里。
    web_deny_hosts: str = ""
    # 单次 web 请求超时（秒）。
    web_timeout: int = 20
    # 单次 fetch 字节上限，超限拒绝而非截断（截断的正文比没有更危险）。
    web_max_bytes: int = 2_000_000
    # 搜索返回条数上限。
    web_search_max_results: int = 8

    # --- 治理面接入（M3，历史标注：此处指 M2 完成的治理接线）---
    # 空 = 未接入治理面，M0/M1 的纯本地行为一行不变。
    # 设置后：启动时拉策略收紧本机配置、模型请求走网关（客户端永不持云端密钥）、
    # 审计事件上报服务端。治理是增强而不是单点依赖：服务端不可达时降级本地继续。
    server_url: str = ""
    # 网关的 OpenAI 兼容基址。空 = 由 server_url 派生（<server>/api/v1/gateway/v1）。
    # 只有网关部署在另一台机器上时才需要单独指定。
    gateway_url: str = ""
    # 策略缓存超过该秒数视为过期，进入受限模式（仅本地、写操作强制审批）。
    # 0 = 不做过期判定（单机内网、服务端常驻的场景可接受）。
    policy_max_staleness: int = 0

    @classmethod
    def load(cls, workspace: str | Path, **overrides: object) -> "Config":
        """从环境变量 + 显式覆盖构建配置。"""
        ws = Path(workspace).expanduser()
        if not ws.exists():
            raise ConfigError(f"工作区不存在: {ws}")
        if not ws.is_dir():
            raise ConfigError(f"工作区不是目录: {ws}")
        ws = ws.resolve()

        # 会话 ID 必须非空，否则审计记录无法按会话归组（排查问题时是刚需）
        session_id = _env_str("VIBE_SESSION_ID", "") or uuid.uuid4().hex[:12]

        cfg = cls(
            workspace=ws,
            session_id=session_id,
            model=_env_str("VIBE_MODEL", cls.model),
            base_url=_env_str("VIBE_BASE_URL", cls.base_url).rstrip("/"),
            api_key=_env_str("VIBE_API_KEY", cls.api_key),
            temperature=float(_env_str("VIBE_TEMPERATURE", str(cls.temperature))),
            request_timeout=_env_int("VIBE_REQUEST_TIMEOUT", cls.request_timeout),
            stream_enabled=_env_bool("VIBE_STREAM", cls.stream_enabled),
            max_iterations=_env_int("VIBE_MAX_ITERATIONS", cls.max_iterations),
            max_context_chars=_env_int("VIBE_MAX_CONTEXT_CHARS", cls.max_context_chars),
            tool_output_limit=_env_int("VIBE_TOOL_OUTPUT_LIMIT", cls.tool_output_limit),
            # 上下文窗口管理（P2-17）
            context_budget_tokens=_env_int(
                "VIBE_CONTEXT_BUDGET_TOKENS", cls.context_budget_tokens
            ),
            prompt_cache=_env_choice("VIBE_PROMPT_CACHE", cls.prompt_cache, _CACHE_MODES),
            compact_on_trim=_env_bool("VIBE_COMPACT_ON_TRIM", cls.compact_on_trim),
            allow_write=_env_bool("VIBE_ALLOW_WRITE", cls.allow_write),
            allow_shell=_env_bool("VIBE_ALLOW_SHELL", cls.allow_shell),
            allow_network_commands=_env_bool(
                "VIBE_ALLOW_NETWORK_COMMANDS", cls.allow_network_commands
            ),
            allow_sensitive_read=_env_bool(
                "VIBE_ALLOW_SENSITIVE_READ", cls.allow_sensitive_read
            ),
            command_timeout=_env_int("VIBE_COMMAND_TIMEOUT", cls.command_timeout),
            # 注意：REVIEW 级自动放行不可配置（ADR-008 红线），
            # 已移除 VIBE_AUTO_APPROVE_REVIEW 环境变量入口；
            # 字段保留默认值，仅由治理面策略做「只收紧」，审批闸门不读它。
            audit_enabled=_env_bool("VIBE_AUDIT", cls.audit_enabled),
            session_persist=_env_bool("VIBE_SESSION_PERSIST", cls.session_persist),
            session_db=_env_str("VIBE_SESSION_DB", cls.session_db),
            # 验证闭环（C2）：环境变量优先，未设置时回落 VIBE.md 约定行
            verify_command=(
                _env_str("VIBE_VERIFY_COMMAND", "")
                or parse_verify_command(ws)
            ),
            # 测试树（K）：同 verify 的两级兜底（环境变量 > VIBE.md 的 `test:` 行）
            test_command=(
                _env_str("VIBE_TEST_COMMAND", "")
                or parse_test_command(ws)
            ),
            test_collect_args=_env_str("VIBE_TEST_COLLECT_ARGS", cls.test_collect_args),
            # 规则与记忆（P2-15）
            rules_extra=_env_str("VIBE_RULES_EXTRA", cls.rules_extra),
            memory_enabled=_env_bool("VIBE_MEMORY", cls.memory_enabled),
            memory_max_items=_env_int("VIBE_MEMORY_MAX", cls.memory_max_items),
            memory_inject_limit=_env_int(
                "VIBE_MEMORY_INJECT", cls.memory_inject_limit
            ),
            memory_db=_env_str("VIBE_MEMORY_DB", cls.memory_db),
            # 语义检索（B5）
            embed_model=_env_str("VIBE_EMBED_MODEL", cls.embed_model),
            semantic_max_files=_env_int(
                "VIBE_SEMANTIC_MAX_FILES", cls.semantic_max_files
            ),
            # 增量索引 / 文件监听（J）：间隔下限 2 秒，防止配成 0 变成忙轮询。
            index_watch=_env_bool("VIBE_INDEX_WATCH", cls.index_watch),
            index_watch_interval=max(
                MIN_WATCH_INTERVAL,
                _env_int("VIBE_INDEX_WATCH_INTERVAL", cls.index_watch_interval),
            ),
            # 内联补全 / 行内改写（D3）
            fim_enabled=_env_bool("VIBE_FIM", cls.fim_enabled),
            completion_max_tokens=_env_int(
                "VIBE_COMPLETION_MAX_TOKENS", cls.completion_max_tokens
            ),
            completion_prefix_chars=_env_int(
                "VIBE_COMPLETION_PREFIX_CHARS", cls.completion_prefix_chars
            ),
            completion_suffix_chars=_env_int(
                "VIBE_COMPLETION_SUFFIX_CHARS", cls.completion_suffix_chars
            ),
            # 语言服务器（E）
            lsp_mode=_env_lsp_mode("VIBE_LSP", cls.lsp_mode),
            lsp_idle_seconds=_env_int("VIBE_LSP_IDLE", cls.lsp_idle_seconds),
            # MCP 客户端（L / P1-6）
            mcp_enabled=_env_bool("VIBE_MCP", cls.mcp_enabled),
            mcp_config=_env_str("VIBE_MCP_CONFIG", cls.mcp_config),
            mcp_timeout=_env_int("VIBE_MCP_TIMEOUT", cls.mcp_timeout),
            # 真实交互式终端（M / P1-7）
            terminal_enabled=_env_bool("VIBE_TERM", cls.terminal_enabled),
            terminal_shell=_env_str("VIBE_TERM_SHELL", cls.terminal_shell),
            terminal_max_sessions=_env_int("VIBE_TERM_MAX", cls.terminal_max_sessions),
            # 多模态输入（N / P1-10）：默认 off —— 会外发，必须显式开
            vision_mode=_env_choice("VIBE_VISION", cls.vision_mode, _VISION_MODES),
            vision_max_image_bytes=_env_int(
                "VIBE_VISION_MAX_BYTES", cls.vision_max_image_bytes
            ),
            # 多 Agent 编排（M3）：默认关，启用后才注册 task 工具
            subagent_enabled=_env_bool("VIBE_SUBAGENT", cls.subagent_enabled),
            subagent_max_iterations=_env_int(
                "VIBE_SUBAGENT_MAX_ITERATIONS", cls.subagent_max_iterations
            ),
            # 检索增强（M3+）：默认关，关着时 semantic_search 结果逐字节一致
            retrieval_enhanced=_env_bool(
                "VIBE_RETRIEVAL_ENHANCED", cls.retrieval_enhanced
            ),
            # Repo Wiki（M3+）：默认关，启用后才注册 repo_wiki 工具
            wiki_enabled=_env_bool("VIBE_WIKI", cls.wiki_enabled),
            wiki_dir=_env_str("VIBE_WIKI_DIR", cls.wiki_dir),
            wiki_max_files=_env_int("VIBE_WIKI_MAX_FILES", cls.wiki_max_files),
            # 受控联网检索（M7）：默认关 —— 会外发，必须显式开
            web_enabled=_env_bool("VIBE_WEB", cls.web_enabled),
            web_search_provider=_env_choice(
                "VIBE_WEB_SEARCH", cls.web_search_provider, _WEB_SEARCH_PROVIDERS
            ),
            web_search_url=_env_str("VIBE_WEB_SEARCH_URL", cls.web_search_url),
            web_fetch_allow_private=_env_bool(
                "VIBE_WEB_ALLOW_PRIVATE", cls.web_fetch_allow_private
            ),
            web_allow_hosts=_env_str("VIBE_WEB_ALLOW_HOSTS", cls.web_allow_hosts),
            web_deny_hosts=_env_str("VIBE_WEB_DENY_HOSTS", cls.web_deny_hosts),
            web_timeout=_env_int("VIBE_WEB_TIMEOUT", cls.web_timeout),
            web_max_bytes=_env_int("VIBE_WEB_MAX_BYTES", cls.web_max_bytes),
            web_search_max_results=_env_int(
                "VIBE_WEB_MAX_RESULTS", cls.web_search_max_results
            ),
            # 治理面接入（M3）
            server_url=_env_str("VIBE_SERVER_URL", "").rstrip("/"),
            gateway_url=_env_str("VIBE_GATEWAY_URL", "").rstrip("/"),
            policy_max_staleness=_env_int(
                "VIBE_POLICY_MAX_STALENESS", cls.policy_max_staleness
            ),
        )

        if overrides:
            unknown = set(overrides) - {f.name for f in cls.__dataclass_fields__.values()}
            if unknown:
                raise ConfigError(f"未知配置项: {', '.join(sorted(unknown))}")
            cfg = replace(cfg, **overrides)  # type: ignore[arg-type]
        return cfg

    @property
    def audit_path(self) -> Path:
        """审计日志落盘位置（工作区内的 .vibe/ 目录）。"""
        return self.workspace / ".vibe" / "audit.jsonl"

    @property
    def checkpoint_dir(self) -> Path:
        return self.workspace / ".vibe" / "checkpoints"

    @property
    def mcp_config_path(self) -> Path:
        """MCP servers 配置位置：显式 `mcp_config` 优先，否则 `.vibe/mcp.json`。"""
        if self.mcp_config:
            return Path(self.mcp_config).expanduser()
        return self.workspace / ".vibe" / "mcp.json"

    @property
    def web_search_endpoint(self) -> str:
        """搜索的实际请求端点。searxng 形态用配置值；gateway 形态由
        server_url 派生 `<server>/api/v1/search`（客户端零密钥，认证头由
        governance.gateway_llm 注入）。gateway 形态且未接入治理面 → 空，
        web_search 调用时明确报错，不静默降级。"""
        if self.web_search_provider == "searxng":
            return self.web_search_url
        if self.server_url:
            return f"{self.server_url}/api/v1/search"
        return ""

    @property
    def provider(self) -> str:
        """模型服务的短标签，供审计回答「这次会话走的是本地还是远端」。

        刻意只给**主机名**（本地再带端口），不给完整 URL：
        base_url 可能带路径，甚至被写成 `http://user:token@host/v1` 这种形式，
        而审计需要的只是「能区分是哪一家」。
        """
        try:
            parts = urllib.parse.urlsplit(self.base_url)
        except ValueError:
            return "unknown"
        host = (parts.hostname or "").lower()
        if not host:
            return "unknown"
        if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
            return f"local:{parts.port}" if parts.port else "local"
        return host[:64]

    def with_workspace(self, workspace: str | Path) -> "Config":
        ws = Path(workspace).expanduser().resolve()
        if not ws.is_dir():
            raise ConfigError(f"工作区不是目录: {ws}")
        return replace(self, workspace=ws)
