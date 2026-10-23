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
# 客户端更新渠道（M9）：stable/beta/canary。服务端按渠道选最新未撤销版本。
_UPDATE_CHANNELS = ("stable", "beta", "canary")


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

    # --- 客户端模型选择器（M9-I）---
    # 默认**关**：关着时走既有单模型路径，请求体与行为与未实现前**逐字节一致**。
    # 开启后按任务难度在「服务端已授权的模型集合」内选型：简单任务走小模型、
    # 复杂任务走大模型。选型**只能是授权集合的子集**（只能收紧，不能选出治理面
    # 未允许的上游，红线 1「代码不出本机」不变）；选不出或越界时 fail-closed
    # 回退默认模型（宁可退回也不硬选未授权上游）。
    model_router_enabled: bool = False
    # 选型策略：none（不选型，逐字节一致）/ static（固定映射表）/ size（按请求
    # 上下文大小启发式选型）。策略非法时**回退 none 并告警**，不静默生效。
    model_router_policy: str = "none"
    # 兜底模型。空 = 沿用 `model`（VIBE_MODEL）。fail-closed 回退时用这个。
    model_router_default: str = ""
    # static 策略的映射表（小模型名 | 大模型名，用 | 分隔）。simple→小、complex→大。
    # 留空时 static 策略退化、按「未匹配回退默认」处理。
    model_router_static_map: str = ""

    # --- 循环 ---
    max_iterations: int = 25
    max_context_chars: int = 400_000
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
    # 全局规则目录（M9-B）：指向一个**跨仓库共享**的规则目录（如公司统一编码规范），
    # 作为比「用户级」更泛的最外层被继承。未设（默认空串）则无继承、合并结果与
    # 改造前**逐字节一致**。只做只读合并，不新增写盘面（ADR-021 注入纪律沿用）。
    global_rules: str = ""
    # 全局层规则合计字节上限（跨仓库共享目录可能很杂，单独设上限防把系统提示挤爆）。
    # 超限时先丢最泛层（与 M6-P「先丢最泛层」纪律一致），并如实标注。
    global_rules_max_bytes: int = 64_000
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

    # --- 真实应用预览（P2 / ADR-039）---
    # 允许预览面内嵌**本机 dev server**（`http://127.0.0.1:<port>`），用来看
    # 真实跑起来的应用，而不是静态文件渲染结果。
    #
    # 默认**关**，因为开启后界面 CSP 的 `frame-src` 会从 `'self'` 放宽到
    # `'self' http://127.0.0.1:*` —— 这是全项目唯一一处放宽 CSP 的地方。
    # 关着时 CSP 响应头与改造前逐字节一致，`/api/preview/external` 直接 403。
    #
    # 隔离不靠 CSP 而靠**跨源**：预览帧是另一个源，读不到 `/api/*`。
    # 所以这里放宽的只是「能不能把环回地址嵌进 iframe」，不是「能不能读数据」。
    preview_external: bool = False

    # --- 远端设备预览（P3 / ADR-040）---
    # 允许把**远端设备上的环回端口**经 SSH 本地转发映射到本机环回，再用预览帧打开。
    # 典型场景：代码在开发机上、`npm run dev` 也在开发机上，人坐在自己电脑前。
    #
    # 默认**关**，且要求 `VIBE_REMOTE=1`（要复用连接清单与既有 SSH 加固）。
    # 它与 `preview_external` **共用同一处 CSP 放宽** —— 转发落地在 127.0.0.1，
    # 正好落在 ADR-039 已经放行的那一条 `frame-src` 里，不新增第二个放宽点。
    preview_remote: bool = False

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
    # 同时最多几个终端会话。**0 = 不限**（默认）：用户可以开任意多个终端，
    # 资源兜底交给空闲回收（30 分钟）。显式设正整数才回到有界模式。
    terminal_max_sessions: int = 0

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

    # --- Skill / 斜杠命令（M9-A）---
    # 默认**关**：开启后 Agent 多出一个 `run_skill` 工具（SAFE，纯展开指令），
    # 并从 `skills_dir` 加载用户手工放置的工作流（`<skills_dir>/**/SKILL.md`）。
    # 关着时工具清单、系统提示与未实现前**逐字节一致**。Skill 是「组合既有工具
    # 的高阶指令」，不新增底层工具、不新增写盘路径；正文与规则同源、注入一律
    # wrap_untrusted（ADR-021）。
    skills_enabled: bool = False
    # Skill 落点（相对工作区）。**不得默认 .vibe/** —— .vibe 受 PathGuard 保护、
    # 模型不可写；但 Skill 是**用户本人**手工放置的受信资产，需单独约定目录。
    skills_dir: str = ".vibe/skills"
    # 单次最多加载多少个 Skill 文件，防止目录里堆满垃圾把内存拉爆。
    skills_max_files: int = 50
    # 加载的 Skill 正文总量上限（字节）。超限从队首（按名字排序）丢弃并标注。
    skills_max_bytes: int = 256_000

    # --- 测试驱动的自动修复闭环（M9-C）---
    # 默认**关**：开启后 Agent 多出一个 `test_loop` 工具（SAFE，只调度既有工具，
    # 不直接写盘/执行），可驱动「跑测试 → 失败 → 喂回失败摘要 → 修复（恒走审批）
    # → 重跑」的自治循环。关着时工具清单与行为与未实现前**逐字节一致**。
    # 测试命令执行仍是 run_command（用户本人动作口径），Agent 的修复动作恒走审批
    # （ADR-008，不可配置绕过）。
    test_loop_enabled: bool = False
    # 单任务重试上限。到顶仍未绿时**如实报告并停止**（不死循环），对齐 M0 的
    # MAX_AUTO_VERIFY 防死循环纪律。
    test_loop_max_rounds: int = 5
    # 单次测试循环的总超时（秒）。超时即终止本轮循环并报告。
    test_loop_timeout: int = 300

    # --- 后台 / 并行 Agent（M9-D）---
    # 默认**关**：开启后 host 侧才暴露 `/api/tasks` 端点，允许**用户本人**经界面
    # 发起后台任务（异步执行、结果异步回传）。关着时无端点、行为与未实现前
    # **逐字节一致**。后台任务由**用户本人**发起（对齐 ADR-011「用户本人动作」
    # 口径），**不新增** Agent 发起的派生工具 —— 规避「Agent 自己派 Agent」的
    # 越权 / 递归风险。子任务工具面 = 只读白名单（build_readonly_registry），
    # 写回仍回父会话走审批漏斗（ADR-008）。
    async_agent_enabled: bool = False
    # 并行上限：同时最多跑多少个后台任务。超限明确拒绝新提交，而不是无限起线程。
    async_agent_max: int = 4
    # 单个后台任务的超时（秒）。超时即终止并报告（防单个任务把线程池占满）。
    async_agent_timeout: int = 600

    # --- 定时任务调度（M10）---
    # 默认**关**：开启后 host 侧才暴露 `/api/schedules` 端点，允许**用户本人**
    # 经界面 / API 创建定时任务（到点把 job 的 prompt 投进 M9-D 后台任务池）。
    # 关着时无端点、无线程、无页签、无库文件，事件流与未实现前**逐字节一致**。
    # **依赖 M9-D 池**：`VIBE_SCHEDULER=1` 但 `VIBE_ASYNC_AGENT=0` → 调度器不启动
    # 并在界面 notices 说明（fail-closed，不静默降级）。定时任务由**用户本人**
    # 创建，**不向 Agent 暴露任何调度工具**（对齐 ADR-011；堵死「Agent 自己给
    # 自己排期」）。触发动作只有一种：投**只读**后台任务（ADR-032 沿用）。
    scheduler_enabled: bool = False
    # 任务库位置（显式值时覆盖）。空 → 默认 `<workspace>/.vibe/scheduler.db`
    # （宿主资产，与 .vibe/checkpoints 同范式；模型仍不可写 .vibe）。
    scheduler_db: str = ""
    # 轮询粒度（秒），下限 5（防忙轮询）。
    scheduler_tick: int = 30
    # job 数上限。到顶后新建明确拒绝（400），而不是无限堆积。
    scheduler_max_jobs: int = 50
    # 单轮最多触发几个 job（防「到点任务同时起一堆」的风暴）。
    scheduler_max_per_tick: int = 3
    # 重启补跑开关：进程重启后对逾期 job **各补跑一次**（catch-up once，
    # 不连补 N 次）；置 0 则只把 next_run_at 推到未来、不触发。
    scheduler_catch_up: bool = True

    # --- 代码审阅工作流（M9-H）---
    # 默认**关**：开启后 Agent 多出一个 `review_changes` 工具（SAFE，只读：不改盘、
    # 不执行、不派子 Agent），对「当前工作区相对 HEAD 的改动」产出一份结构化意见
    # （严重级别 / 类别 / 定位 / 说明 / 建议）。关着时工具清单与行为与未实现前
    # **逐字节一致**。审阅是**自审**，产出「意见」而非「补丁」，**不替代**人审
    # （ADR-008 不变）—— 修复仍走既有 edit_* 审批漏斗。
    review_enabled: bool = False
    # 单次审阅最多覆盖多少个文件，防止在超大改动集上把上下文冲爆。
    review_max_files: int = 50

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

    # --- 客户端自动更新（M9）---
    # 默认**启用**：这条链路只在本机发生（检查走治理面/内网镜像、下载的是
    # 一个已验签的安装器），不涉及源码外发，因此与 vision/web 那类「会外发
    # 的能力」不同，不需要默认关。高合规环境可用 VIBE_UPDATE_DISABLE=1 整体
    # 关掉 —— 关掉后**零网络请求**，不是「请求了但不用」。
    update_channel: str = "stable"
    # 本地/内网镜像基址（file:// 或 http，或裸路径）。空 = 仅走治理面通道。
    # 两种源**逐源独立验签**：谁不可信只作废谁，可用性与信任解耦。
    update_base_url: str = ""
    # 整体开关（关掉后零网络请求）。
    update_disabled: bool = False
    # 启动时是否自动静默检查（手动「检查更新」不受它影响）。
    update_check_on_start: bool = True
    # 单包字节上限（本机兜底）。清单声明的 size 是发布者的承诺，这是本机自己
    # 的闸；下载时取两者更紧的一条。默认 512 MiB。
    update_max_bytes: int = 512 * 1024 * 1024
    # 落盘根。空 = ~/.vibe/updates（用户级，跨工作区共享）。家目录不可写时
    # （沙箱/CI）用它逃生，与 VIBE_MEMORY 同范式。
    update_dir: str = ""
    # 发布公钥覆盖（JSON 文本或文件路径）。空 = 用构建期内置公钥（发行版写入）。
    # 支持客户自签内网镜像；显式设置时需记审计（见 update/keys.py 模块头）。
    update_pubkey: str = ""

    # --- 远程设备（M10）---
    # 默认**关**：关着时远程工具**根本不注册**，工具列表与行为与未实现前
    # 逐字节一致（与 M7 联网检索、M9 Skill 同范式）。
    #
    # 开启后 Agent 才能读写**用户自己的远端设备**。注意它改动的不是
    # 「代码不上传治理面」这条红线（那条约束的是本机→治理面方向，
    # 审计里依旧只有哈希与计数、没有正文），而是「执行面全在本机」
    # 这一条实现假设 —— 详见 agent_core/remote/__init__.py 的说明。
    remote_enabled: bool = False
    # 连接清单路径覆盖。空 = <workspace>/.vibe/remote.json。
    # 显式给出时可在多个工作区间共用同一份清单。
    remote_config: str = ""
    # 默认连接名。空 = 由清单里的 default 决定；两者都空且清单只有一条连接时
    # 自动选那一条（降低使用门槛）。优先级：工具参数 > 本项 > 清单 default。
    remote_default: str = ""

    # --- 远程工作区（M13 / ADR-042）---
    # 非空时该远端目录成为**默认落点**：模型写裸路径（`src/app.py`）也会被路由到
    # 那条连接的那个子目录下（等价于写 `@remote/<连接名>/<子目录>/src/app.py`）。
    # 取值必须写成 `@remote/<连接名>/<子目录>`；空串 = 本机模式（现状，逐字节不变）。
    #
    # 它改变的不是 `Config.workspace` 的语义 —— 后者恒为本机路径，此刻它会指向内核
    # 自动生成的**本机锚点目录**（`<家目录>/.vibe/workspaces/remote/…`），
    # 审计链 / 会话库 / 检查点 / 远端写的本机留底全部落在那里。
    # 换句话说：**jail 的根由本项决定，`.vibe` 资产的落点仍在本机**（ADR-006 不动）。
    #
    # 这不是门控 —— 真正的开关是 `VIBE_REMOTE`；本项只是「启动时的默认锚点」，
    # 与 `remote_default` 同范式：给了也只是省一次手动切换，绕不开 `VIBE_REMOTE`。
    remote_workspace: str = ""

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
            # 客户端模型选择器（M9-I）：默认关，关着时请求体逐字节不变
            model_router_enabled=_env_bool(
                "VIBE_MODEL_ROUTER", cls.model_router_enabled
            ),
            model_router_policy=_env_str(
                "VIBE_MODEL_ROUTER_POLICY", cls.model_router_policy
            ),
            model_router_default=_env_str(
                "VIBE_MODEL_ROUTER_DEFAULT", cls.model_router_default
            ),
            model_router_static_map=_env_str(
                "VIBE_MODEL_ROUTER_STATIC_MAP", cls.model_router_static_map
            ),
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
            # 全局规则目录（M9-B）：默认空 = 无继承、逐字节一致
            global_rules=_env_str("VIBE_RULES_GLOBAL", cls.global_rules),
            global_rules_max_bytes=_env_int(
                "VIBE_RULES_GLOBAL_MAX_BYTES", cls.global_rules_max_bytes
            ),
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
            # 真实应用预览（P2）：默认关；开启才会放宽界面 CSP 的 frame-src。
            preview_external=_env_bool("VIBE_PREVIEW_EXTERNAL", cls.preview_external),
            # 远端设备预览（P3）：默认关；开启后界面可把远端环回端口转发到本机。
            preview_remote=_env_bool("VIBE_PREVIEW_REMOTE", cls.preview_remote),
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
            # Skill / 斜杠命令（M9-A）：默认关，启用后才加载并注册 run_skill 工具
            skills_enabled=_env_bool("VIBE_SKILLS", cls.skills_enabled),
            skills_dir=_env_str("VIBE_SKILLS_DIR", cls.skills_dir),
            skills_max_files=_env_int("VIBE_SKILLS_MAX_FILES", cls.skills_max_files),
            skills_max_bytes=_env_int("VIBE_SKILLS_MAX_BYTES", cls.skills_max_bytes),
            # 测试驱动的自动修复闭环（M9-C）：默认关，启用后才注册 test_loop 工具
            test_loop_enabled=_env_bool("VIBE_TEST_LOOP", cls.test_loop_enabled),
            test_loop_max_rounds=_env_int(
                "VIBE_TEST_LOOP_MAX", cls.test_loop_max_rounds
            ),
            test_loop_timeout=_env_int(
                "VIBE_TEST_LOOP_TIMEOUT", cls.test_loop_timeout
            ),
            # 后台 / 并行 Agent（M9-D）：默认关，启用后才暴露 /api/tasks 端点
            async_agent_enabled=_env_bool("VIBE_ASYNC_AGENT", cls.async_agent_enabled),
            async_agent_max=_env_int("VIBE_ASYNC_AGENT_MAX", cls.async_agent_max),
            async_agent_timeout=_env_int(
                "VIBE_ASYNC_AGENT_TIMEOUT", cls.async_agent_timeout
            ),
            # 定时任务调度（M10）：默认关，启用后才暴露 /api/schedules 端点
            scheduler_enabled=_env_bool("VIBE_SCHEDULER", cls.scheduler_enabled),
            scheduler_db=_env_str("VIBE_SCHEDULER_DB", cls.scheduler_db),
            scheduler_tick=_env_int("VIBE_SCHEDULER_TICK", cls.scheduler_tick),
            scheduler_max_jobs=_env_int("VIBE_SCHEDULER_MAX", cls.scheduler_max_jobs),
            scheduler_max_per_tick=_env_int(
                "VIBE_SCHEDULER_MAX_PER_TICK", cls.scheduler_max_per_tick
            ),
            scheduler_catch_up=_env_bool(
                "VIBE_SCHEDULER_CATCHUP", cls.scheduler_catch_up
            ),
            # 代码审阅工作流（M9-H）：默认关，启用后才注册 review_changes 工具
            review_enabled=_env_bool("VIBE_REVIEW", cls.review_enabled),
            review_max_files=_env_int("VIBE_REVIEW_MAX_FILES", cls.review_max_files),
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
            # 客户端自动更新（M9）
            update_channel=_env_choice(
                "VIBE_UPDATE_CHANNEL", cls.update_channel, _UPDATE_CHANNELS
            ),
            update_base_url=_env_str("VIBE_UPDATE_URL", cls.update_base_url),
            update_disabled=_env_bool("VIBE_UPDATE_DISABLE", cls.update_disabled),
            update_check_on_start=_env_bool(
                "VIBE_UPDATE_CHECK_ON_START", cls.update_check_on_start
            ),
            update_max_bytes=_env_int("VIBE_UPDATE_MAX_BYTES", cls.update_max_bytes),
            update_dir=_env_str("VIBE_UPDATE_DIR", cls.update_dir),
            update_pubkey=_env_str("VIBE_UPDATE_PUBKEY", cls.update_pubkey),
            # 远程设备（M10）：默认关，关着时远程工具不注册、工具列表逐字节不变
            remote_enabled=_env_bool("VIBE_REMOTE", cls.remote_enabled),
            remote_config=_env_str("VIBE_REMOTE_CONFIG", cls.remote_config),
            remote_default=_env_str("VIBE_REMOTE_DEFAULT", cls.remote_default),
            # 远程工作区（M13 / ADR-042）：初值可选，空 = 本机模式。
            # 真正的门控是 `VIBE_REMOTE` —— 本项给值也绕不开它。
            remote_workspace=_env_str("VIBE_REMOTE_WORKSPACE", cls.remote_workspace),
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
    def scheduler_db_path(self) -> Path:
        """定时任务库落点（M10）：显式 `scheduler_db` 优先，否则 `.vibe/scheduler.db`。

        宿主资产（与 `.vibe/checkpoints` 同范式）—— 宿主用绝对路径直写，
        不走模型写盘通道，模型仍不可写 `.vibe/`。
        """
        if self.scheduler_db:
            return Path(self.scheduler_db).expanduser()
        return self.workspace / ".vibe" / "scheduler.db"

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
