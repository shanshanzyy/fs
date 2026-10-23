"""工具系统。build_default_registry() 是唯一需要 Agent 知道的入口。"""

from .base import (
    DEFAULT_IGNORED_DIRS,
    ChangeRecord,
    DraftFile,
    ToolContext,
    ToolPreview,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    UNTRUSTED_BEGIN,
    UNTRUSTED_END,
    validate_arguments,
    wrap_untrusted,
)
from .diagnostics import LSP_DIAGNOSTICS_SPEC
from .editing import EDIT_FILE_SPEC
from .fs import LIST_DIR_SPEC, READ_FILE_SPEC, WRITE_FILE_SPEC
from .git import GIT_DIFF_SPEC
from .memory import REMEMBER_SPEC
from .multi_edit import EDIT_FILES_SPEC
from .plan import UPDATE_PLAN_SPEC
from .repo_map import REPO_MAP_SPEC
from .repo_wiki import REPO_WIKI_SPEC
from .review import REVIEW_CHANGES_SPEC
from .search import FIND_FILES_SPEC, GREP_SPEC
from .semantic import SEMANTIC_SEARCH_SPEC
from .shell import RUN_COMMAND_SPEC
from .skill import RUN_SKILL_SPEC
from .task import TASK_SPEC
from .test_loop import TEST_LOOP_SPEC
from .web import WEB_FETCH_SPEC, WEB_SEARCH_SPEC

__all__ = [
    "DEFAULT_IGNORED_DIRS",
    "EDIT_FILES_SPEC",
    "EDIT_FILE_SPEC",
    "FIND_FILES_SPEC",
    "GIT_DIFF_SPEC",
    "GREP_SPEC",
    "LIST_DIR_SPEC",
    "LSP_DIAGNOSTICS_SPEC",
    "READ_FILE_SPEC",
    "REMEMBER_SPEC",
    "REPO_MAP_SPEC",
    "REPO_WIKI_SPEC",
    "REVIEW_CHANGES_SPEC",
    "RUN_COMMAND_SPEC",
    "RUN_SKILL_SPEC",
    "SEMANTIC_SEARCH_SPEC",
    "TASK_SPEC",
    "TEST_LOOP_SPEC",
    "UPDATE_PLAN_SPEC",
    "WEB_FETCH_SPEC",
    "WEB_SEARCH_SPEC",
    "ChangeRecord",
    "DraftFile",
    "ToolContext",
    "ToolPreview",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "UNTRUSTED_BEGIN",
    "UNTRUSTED_END",
    "WRITE_FILE_SPEC",
    "build_default_registry",
    "build_readonly_registry",
    "validate_arguments",
    "wrap_untrusted",
]


def build_default_registry(
    *,
    lsp: bool | None = None,
    mcp: list | None = None,
    memory: bool = False,
    subagent: bool = False,
    wiki: bool = False,
    web: bool = False,
    skills: bool = False,
    test_loop: bool = False,
    review: bool = False,
    remote: bool = False,
    audit: object | None = None,
) -> ToolRegistry:
    """注册全部工具。

    核心 12 个，刻意保持精简 —— 工具越多，弱模型的选错率越高。
    每个工具都对应一类高频动作，没有为了「看起来强」而凑数的工具。
    （M0 是 8 个；M5-B 新增 semantic_search 后为 9 个，M5-C 新增 git_diff 后为 10 个，
    P1/P2-G 新增 update_plan 后为 11 个，P1/P2-I 新增 edit_files 后为 12 个；
    与 grep/repo_map 的分工见各自的 description。）

    `lsp_diagnostics`（E6）是**条件注册**：机器上装没装语言服务器只有运行时
    才知道，而一个必然失败的工具会白烧弱模型一轮上下文。`lsp=None` 表示自动
    探测（`shutil.which`），显式传 True/False 用于测试与「强制关闭」。

    `mcp`（L/P1-6）是**外部工具来源**：传入 `McpServerConfig` 列表即把这些
    server 的工具并入（名字 `mcp__<server>__<tool>`，恒 REVIEW）。`None` = 不并入。
    MCP 工具只经 `mcp.registry.merge_mcp` 进入本函数建出的这一个 registry ——
    与内置工具同一审批漏斗（见 `ToolRegistry.execute`）。`audit` 用于把
    `mcp.server_start` 记进本地审计（host 侧在 Agent 建好后传入）。

    `memory`（P2-15）是**条件注册**：`remember` 只在记忆启用时才注册 ——
    关掉记忆（`VIBE_MEMORY=0`）时工具列表与行为与未实现前逐字节一致。

    `subagent`（M3）同样是**条件注册**：`task` 只在多 Agent 编排启用时才注册 ——
    关掉（`VIBE_SUBAGENT=0`，默认）时工具列表与行为与未实现前逐字节一致。

    `wiki`（M3+）同样是**条件注册**：`repo_wiki` 只在 Repo Wiki 启用时才注册 ——
    关掉（`VIBE_WIKI=0`，默认）时工具列表与行为与未实现前逐字节一致。

    `web`（M7）同样是**条件注册**：`web_search` / `web_fetch` 只在联网检索
    启用时才注册 —— 关掉（`VIBE_WEB=0`，默认）时工具列表与行为与未实现前
    逐字节一致。两个工具**恒 REVIEW**（查询词/URL 是外发内容，ADR-008/015），
    且**不进 build_readonly_registry** —— 子 Agent 恒不联网。

    `skills`（M9-A）同样是**条件注册**：`run_skill` 只在 Skill 启用时才注册 ——
    关掉（`VIBE_SKILLS=0`，默认）时工具列表与行为与未实现前逐字节一致。
    `run_skill` 恒 **SAFE**（只展开指令、不写盘、不执行），且**不进
    build_readonly_registry** —— 子 Agent 不加载 Skill 工作流，保持只读。

    `test_loop`（M9-C）同样是**条件注册**：只在测试闭环启用时才注册 —— 关掉
    （`VIBE_TEST_LOOP=0`，默认）时工具列表与行为与未实现前逐字节一致。
    `test_loop` 恒 **SAFE**（只调度既有工具，不直接写盘/执行），且**不进
    build_readonly_registry** —— 子 Agent 不承担「自动修复」这一父级职责。

    `review`（M9-H）同样是**条件注册**：只在代码审阅启用时才注册 —— 关掉
    （`VIBE_REVIEW=0`，默认）时工具列表与行为与未实现前逐字节一致。
    `review_changes` 恒 **SAFE**（只读：不改盘、不执行、不派子 Agent），且**不进
    build_readonly_registry** —— 子 Agent 不承担「提交前自审」这一父级职责。

    `remote`（M10）同样是**条件注册**：只在远程设备能力启用时才注册 —— 关掉
    （`VIBE_REMOTE=0`，默认）时工具列表与行为与未实现前逐字节一致。
    这组工具把本机 Agent 接到**用户自己的远端设备**上（详见
    agent_core/remote/__init__.py 对红线口径的说明）。三个要点：
      - 只读类（connections/probe/read/list/grep）恒 SAFE，`remote_grep` 例外
        —— 检索模式串会发到远端，恒 REVIEW；
      - 写类（edit/write）与执行类（run_command）恒 REVIEW，走与本地工具
        完全相同的审批漏斗；目标连接另有 allow_write / allow_shell 能力位；
      - **不进** build_readonly_registry() —— 子 Agent 不得触达其他机器。
    """
    registry = ToolRegistry()
    specs = [
        # 只读：理解代码库
        REPO_MAP_SPEC,
        LIST_DIR_SPEC,
        READ_FILE_SPEC,
        GREP_SPEC,
        FIND_FILES_SPEC,
        SEMANTIC_SEARCH_SPEC,
        GIT_DIFF_SPEC,
        # 写：改代码
        EDIT_FILE_SPEC,
        WRITE_FILE_SPEC,
        # 写：多文件原子事务（P1-8）—— 要么全落盘，要么全不落盘
        EDIT_FILES_SPEC,
        # 计划（P2-12）：纯内存，不落盘、不进审批
        UPDATE_PLAN_SPEC,
        # 执行
        RUN_COMMAND_SPEC,
    ]
    if memory:
        # 记忆（P2-15）：恒 REVIEW（人审后才落库）；未启用时列表逐字节不变
        specs.append(REMEMBER_SPEC)
    if subagent:
        # 子 Agent（M3）：只读委派（SAFE）；未启用时列表逐字节不变。
        # 注意 `task` **不进** build_readonly_registry() —— 无递归（子 Agent
        # 不能再往下派生）。
        specs.append(TASK_SPEC)
    if wiki:
        # Repo Wiki（M3+）：恒 REVIEW（写盘，落盘走审批）；未启用时列表逐字节不变。
        # 注意 `repo_wiki` **不进** build_readonly_registry() —— 子 Agent 恒只读。
        specs.append(REPO_WIKI_SPEC)
    if web:
        # 联网检索（M7）：恒 REVIEW（查询词/URL 外发必经人审，ADR-008/015）；
        # 未启用时列表逐字节不变。同样**不进** build_readonly_registry()。
        specs.append(WEB_SEARCH_SPEC)
        specs.append(WEB_FETCH_SPEC)
    if skills:
        # Skill / 斜杠命令（M9-A）：恒 SAFE（只展开指令，不写盘、不执行）；
        # 未启用时列表逐字节不变。**不进** build_readonly_registry() ——
        # 子 Agent 不加载 Skill 工作流，保持只读。
        specs.append(RUN_SKILL_SPEC)
    if test_loop:
        # 测试闭环（M9-C）：恒 SAFE（只调度既有工具，不直接写盘/执行）；
        # 未启用时列表逐字节不变。**不进** build_readonly_registry() ——
        # 子 Agent 不承担「自动修复」这一父级职责。
        specs.append(TEST_LOOP_SPEC)
    if review:
        # 代码审阅（M9-H）：恒 SAFE（只读：不改盘、不执行、不派子 Agent）；
        # 未启用时列表逐字节不变。**不进** build_readonly_registry() ——
        # 子 Agent 不承担「提交前自审」这一父级职责。
        specs.append(REVIEW_CHANGES_SPEC)
    if remote:
        # 远程设备（M10）：注册顺序即使用顺序 —— 先看有哪些机器、再读、
        # 最后才是改与跑。写/执行类恒 REVIEW，只读类恒 SAFE（grep 除外）。
        # 未启用时列表逐字节不变；**不进** build_readonly_registry()。
        #
        # 延迟导入：tools 与 remote 互相引用（remote.workspace 要用
        # tools.base 的扫描上限，tools.remote 要用 remote.workspace），
        # 模块级 import 会成环 —— 与 mcp / lsp 同一处理方式。
        from .remote import REMOTE_TOOL_SPECS

        specs.extend(REMOTE_TOOL_SPECS)
    if lsp is None:
        # 延迟导入：tools 与 lsp 互相引用，模块级 import 会成环（见 diagnostics.py）
        from ..lsp import servers

        lsp = servers.any_available()
    if lsp:
        specs.append(LSP_DIAGNOSTICS_SPEC)
    if remote:
        # 远端透明路由（M12）：把六个文件工具的 spec 换成「按 `path` 前缀选臂」的
        # **同名路由壳** —— name / description / parameters 原样，换的只是实现。
        # 因此工具清单与 schema 一字不改，模型无需知道「有远端」这回事。
        #
        # 位置很关键：必须在下面 `registry.register` 循环**之前**替换 ——
        # `ToolRegistry.register` 重名会抛错，只能「换实现」不能「再注册一个」。
        from ..remote.routing import routed_specs

        specs = routed_specs(specs)
    for spec in specs:
        registry.register(spec)
    if mcp:
        # 延迟导入：tools 与 mcp 互相引用（registry 生成 ToolSpec 要用 tools.base），
        # 模块级 import 会成环。
        from ..mcp.registry import merge_mcp

        merge_mcp(registry, list(mcp), audit=audit)
    return registry


def build_readonly_registry(*, lsp: bool | None = None) -> ToolRegistry:
    """子 Agent 的工具面（M3）：**只读**工具的白名单。

    子 Agent 只做探索 / 分析 / 检索，任何写盘都要回到父 Agent 走审批漏斗
    （ADR-008）。因此这里**只**登记只读 SAFE 工具，并刻意**不含**：

      - `edit_file` / `write_file` / `edit_files` —— 写盘
      - `run_command` —— 执行命令
      - `update_plan` —— 计划是父 Agent 的会话状态，子 Agent 不该改它
      - `remember` —— 写记忆库
      - `task` —— **无递归**：子 Agent 不能再往下派生
      - `mcp__*` —— 外部工具恒 REVIEW，且本就不在只读面内

    这份白名单是「子 Agent 只读」的**结构保证**：工具不存在，模型就选不到，
    比在运行时拦截更可靠。`loop/subagent.py` 另设 `allow_write=False` 作纵深防御。

    `lsp=None` 与 `build_default_registry` 同义：自动探测语言服务器
    （装了才登记 `lsp_diagnostics`）。
    """
    registry = ToolRegistry()
    specs = [
        # 只读：理解代码库（与 build_default_registry 的前半段一致）
        REPO_MAP_SPEC,
        LIST_DIR_SPEC,
        READ_FILE_SPEC,
        GREP_SPEC,
        FIND_FILES_SPEC,
        SEMANTIC_SEARCH_SPEC,
        GIT_DIFF_SPEC,
    ]
    if lsp is None:
        # 延迟导入：tools 与 lsp 互相引用（见 diagnostics.py）
        from ..lsp import servers

        lsp = servers.any_available()
    if lsp:
        specs.append(LSP_DIAGNOSTICS_SPEC)
    for spec in specs:
        registry.register(spec)
    return registry
