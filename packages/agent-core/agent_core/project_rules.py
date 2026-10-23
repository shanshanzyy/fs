"""项目规则文件 VIBE.md（C1 / P2-15）—— 让 Agent 一进项目就懂「这里的规矩」。

## 多层加载（P2-15 / M9-B）

规则可以来自五个层级，按「由泛到专」的顺序合并（越靠后越具体、越优先）：

1. **全局级** 跨仓库共享的规则目录（``VIBE_RULES_GLOBAL`` 指向，M9-B 新增，
   默认空 = 无继承、逐字节一致）；目录下 ``*.md`` 按文件名排序加载；
2. **用户级** ``~/.vibe/rules.md``（可被 ``VIBE_USER_RULES`` 覆盖或显式关闭）；
3. **仓库级** 根目录 ``VIBE.md``（推荐）**或** ``.vibe/VIBE.md``（不想让规则
   出现在项目根时的备选）—— 两者是**同一层的备选**，只取先命中的那个；
4. **仓库附加** ``.vibe/rules/*.md``（按文件名排序）以及 ``rules_extra`` 里
   逗号分隔的额外路径 / glob（相对工作区）；
5. **目录级** 按调用方给出的「当前编辑/引用文件」路径**逐级向上**找的
   ``VIBE.md``（就近优先：越靠近文件的目录越具体，排在越后面）。

全局层是最泛的一层，排在最前，超总量预算时**最先被丢**（与 M6-P「先丢最泛层」
纪律一致）。全局层不设抬头（单层逐字节兼容）还是带抬头，由合并时的层数决定。

## 定位取舍

- **受信但防滥用**。规则文件是用户自己写的，不走 ``wrap_untrusted``；
  但它可能被提示词注入利用（比如仓库里藏一份要求「忽略审批」的 VIBE.md），
  防线在拼接处 —— 系统提示里显式声明安全策略优先（见 prompts.py），
  且审批闸门在代码层不可绕过，提示词层面怎么写都翻不了墙。
- **两份预算**。单份上限 32KB（``MAX_RULES_BYTES``），合计上限 64KB
  （``TOTAL_RULES_BYTES``）。系统提示要进每一轮上下文，规则写成小作文
  会把真正的代码挤出预算。超预算时**先丢最泛的层**（用户级最先被压），
  并把这件事如实告知用户。
- **只在会话开始读一次**（Agent.__init__），不做热重载 —— MVP 从简，
  文件变更提示留给 UI 后续。

单层时返回的正文与旧版**逐字节一致**（不加来源抬头），这是对既有行为的
回归保护；只有多层才会带 ``### 规则来源：`` 抬头。
"""

from __future__ import annotations

import glob as _glob
import os
from dataclasses import dataclass, field
from pathlib import Path

MAX_RULES_BYTES = 32 * 1024
# 合计预算：四层叠起来的上限。超过时先丢最泛的层（见模块头）。
TOTAL_RULES_BYTES = 64 * 1024

# 仓库级查找顺序即优先级：先根目录，后 .vibe 内（两者是备选关系，只取一个）。
_CANDIDATES = ("VIBE.md", Path(".vibe") / "VIBE.md")
# 仓库附加规则的目录与文件名模式。
_EXTRA_DIR = Path(".vibe") / "rules"
# 用户级规则默认位置（相对 home）。
USER_RULES_REL = Path(".vibe") / "rules.md"

LAYER_HEADER = "### 规则来源：{source}"


@dataclass
class ProjectInstructions:
    """一次规则加载的结果。source 为空串表示没有任何规则文件。"""

    text: str = ""
    source: str = ""  # 主来源（仓库级优先），相对路径（posix），用于 UI 展示
    truncated: bool = False
    # P2-15：加载到的层数、被截断/丢弃的层数、各层来源（用于审计与调试）。
    layers: int = 0
    truncated_count: int = 0
    sources: tuple[str, ...] = field(default_factory=tuple)

    @property
    def chars(self) -> int:
        return len(self.text)


def _global_entries(
    global_rules_path: str | Path | None,
) -> list[tuple[str, Path]]:
    """全局级（M9-B）：跨仓库共享规则目录下的 ``*.md``（按文件名排序）。

    只收目录下的直接 ``*.md`` 文件（不做递归 —— 全局目录是「扁平的一组规范」，
    不是镜像一个仓库的层级）。路径不可达/不是目录时按「没有全局层」处理，
    返回空列表，调用方行为与未启用继承时逐字节一致。
    """
    if not global_rules_path:
        return []
    root = Path(global_rules_path).expanduser()
    try:
        if not root.is_dir():
            return []
        files = sorted(p for p in root.glob("*.md") if p.is_file())
    except OSError:
        return []
    found: list[tuple[str, Path]] = []
    for path in files:
        try:
            label = f"global:{path.name}"
        except Exception:
            label = path.as_posix()
        found.append((label, path))
    return found


def _user_rules_path() -> Path | None:
    """用户级规则文件位置。

    优先级：``VIBE_USER_RULES`` 环境变量 > ``~/.vibe/rules.md``。
    环境变量**显式设为空串**表示关闭用户级（便携/测试场景）。
    """
    raw = os.environ.get("VIBE_USER_RULES")
    if raw is not None:
        raw = raw.strip()
        if raw == "":
            return None
        return Path(raw).expanduser()
    return Path.home() / USER_RULES_REL


def _read_capped(path: Path) -> tuple[str, bool]:
    """读一份规则文件，按单份上限截断。读取失败按「文件不存在」处理。

    截断时切点可能落在多字节字符中间，``errors="replace"`` 会把残缺尾巴换成
    一个 U+FFFD（3 字节）反而超额 —— 按字符回退到上限以内。
    """
    try:
        raw = path.read_bytes()
    except OSError:
        raise
    truncated = len(raw) > MAX_RULES_BYTES
    if truncated:
        raw = raw[:MAX_RULES_BYTES]
    text = raw.decode("utf-8", errors="replace")
    if truncated:
        while text and len(text.encode("utf-8")) > MAX_RULES_BYTES:
            text = text[:-1]
    return text, truncated


def _repo_entry(workspace: Path) -> tuple[str, Path] | None:
    """仓库级：根 VIBE.md 优先，其次 .vibe/VIBE.md（备选，不是叠加）。"""
    for rel in _CANDIDATES:
        path = workspace / rel
        try:
            if path.is_file():
                return Path(rel).as_posix(), path
        except OSError:
            continue
    return None


def _extra_entries(workspace: Path, extra: str) -> list[tuple[str, Path]]:
    """仓库附加：``.vibe/rules/*.md`` + ``rules_extra``（逗号分隔路径 / glob）。"""
    found: list[tuple[str, Path]] = []
    seen: set[Path] = set()

    def _push(path: Path) -> None:
        try:
            if not path.is_file():
                return
        except OSError:
            return
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        try:
            label = path.relative_to(workspace).as_posix()
        except ValueError:
            label = path.as_posix()
        found.append((label, path))

    extra_dir = workspace / _EXTRA_DIR
    try:
        if extra_dir.is_dir():
            for path in sorted(extra_dir.glob("*.md")):
                _push(path)
    except OSError:
        pass

    for token in str(extra or "").split(","):
        token = token.strip()
        if not token:
            continue
        if any(ch in token for ch in "*?["):
            pattern = token if Path(token).is_absolute() else str(workspace / token)
            for matched in sorted(_glob.glob(pattern, recursive=True)):
                _push(Path(matched))
        else:
            candidate = Path(token)
            _push(candidate if candidate.is_absolute() else workspace / token)
    return found


def _dir_entries(
    workspace: Path, current_files: list[str] | tuple[str, ...] | None
) -> list[tuple[str, Path]]:
    """目录级：对每个「当前编辑/引用文件」逐级向上找 ``VIBE.md``（就近优先）。

    只收工作区**内部**、且不在根目录的 ``VIBE.md``（根那份属仓库级）。
    多个文件共享的目录去重；整体按目录深度从浅到深排序，使最靠近文件的
    规则排在最后（优先级最高）。
    """
    if not current_files:
        return []
    dirs: list[Path] = []
    for item in current_files:
        raw = str(item or "").strip()
        if not raw:
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = workspace / raw
        # 带扩展名的按文件处理，其余按目录处理
        start = candidate.parent if candidate.suffix else candidate
        chain: list[Path] = []
        cur = start
        while True:
            try:
                cur.relative_to(workspace)
            except ValueError:
                break
            chain.append(cur)
            if cur == workspace:
                break
            cur = cur.parent
        for directory in reversed(chain):  # 浅 → 深
            if directory not in dirs:
                dirs.append(directory)
    dirs.sort(key=lambda d: len(d.relative_to(workspace).parts))

    found: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for directory in dirs:
        if directory == workspace:
            continue  # 根目录的 VIBE.md 已在仓库级处理
        path = directory / "VIBE.md"
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        found.append((path.relative_to(workspace).as_posix(), path))
    return found


def load_project_instructions(
    workspace: Path,
    *,
    user_rules_path: Path | None = None,
    extra: str = "",
    current_files: list[str] | tuple[str, ...] | None = None,
    global_rules_path: str | Path | None = None,
    global_rules_max_bytes: int = 64 * 1024,
) -> ProjectInstructions:
    """按多层顺序加载并合并规则。

    读取失败（权限/编码）一律按「这一层没有规则」处理 —— 规则是增强项，
    任何读取层面的意外都不应该挡住 Agent 启动。
    """
    root = Path(workspace)

    # ---- 收集各层 (source, path)，顺序 = 由泛到专 ----
    entries: list[tuple[str, Path]] = []

    # 全局级（M9-B）：最泛、放队首，超预算最先被丢。
    entries.extend(_global_entries(global_rules_path))

    if user_rules_path is None:
        user_rules_path = _user_rules_path()
    if user_rules_path is not None:
        try:
            if Path(user_rules_path).is_file():
                entries.append(
                    (Path(user_rules_path).as_posix(), Path(user_rules_path))
                )
        except OSError:
            pass

    repo = _repo_entry(root)
    if repo is not None:
        entries.append(repo)
    entries.extend(_extra_entries(root, extra))
    entries.extend(_dir_entries(root, current_files))

    if not entries:
        return ProjectInstructions()

    # ---- 逐份读取 + 单份截断 ----
    pieces: list[tuple[str, str]] = []  # (source, text)
    truncated_count = 0
    # 全局层专用预算（M9-B）：跨仓库共享目录可能很杂，单独设上限。
    # 超限时丢弃后续的全局条目（文件名排序后的队尾），并计入 truncated_count。
    _global_bytes = 0
    for source, path in entries:
        is_global = source.startswith("global:")
        try:
            text, cut = _read_capped(path)
        except OSError:
            continue
        if cut:
            truncated_count += 1
        if is_global:
            _global_bytes += len(text.encode("utf-8"))
            if _global_bytes > global_rules_max_bytes:
                truncated_count += 1
                continue
        if text.strip():
            pieces.append((source, text))

    if not pieces:
        return ProjectInstructions()

    # ---- 合计预算：超了先丢最泛的层（队首），最后才截断单份 ----
    def _bytes(items: list[tuple[str, str]]) -> int:
        return sum(len(t.encode("utf-8")) for _, t in items)

    while len(pieces) > 1 and _bytes(pieces) > TOTAL_RULES_BYTES:
        pieces.pop(0)
        truncated_count += 1

    if _bytes(pieces) > TOTAL_RULES_BYTES:
        source, text = pieces[0]
        while text and len(text.encode("utf-8")) > TOTAL_RULES_BYTES:
            text = text[:-1]
        pieces[0] = (source, text)
        truncated_count += 1

    sources = tuple(source for source, _ in pieces)
    primary = next(
        (source for source in sources if source in {"VIBE.md", ".vibe/VIBE.md"}),
        sources[0],
    )

    if len(pieces) == 1:
        # 单层：正文与旧版逐字节一致（不加抬头），回归保护。
        text = pieces[0][1]
    else:
        text = "\n\n".join(
            LAYER_HEADER.format(source=source) + "\n" + body
            for source, body in pieces
        )

    return ProjectInstructions(
        text=text,
        source=primary,
        truncated=truncated_count > 0,
        layers=len(pieces),
        truncated_count=truncated_count,
        sources=sources,
    )


def _parse_convention_command(workspace: Path, key: str) -> str:
    """从规则文件提取 ``<key>: <命令>`` 约定行（行首，允许列表前缀）。

    只认第一个匹配；解析结果会在执行前过 CommandGuard 白名单，
    所以即使规则文件被注入恶意命令也不会真正跑起来。
    """
    root = Path(workspace)
    prefix = f"{key}:"
    for rel in _CANDIDATES:
        try:
            path = root / rel
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                stripped = stripped[2:].strip()
            if stripped.lower().startswith(prefix):
                command = stripped[len(prefix):].strip()
                if command:
                    return command
    return ""


def parse_verify_command(workspace: Path) -> str:
    """从 VIBE.md 提取 ``verify: <命令>`` 约定行（C2.5）。

    这是验证闭环的配置兜底：优先级为 环境变量 VIBE_VERIFY_COMMAND > 此约定行
    > 默认空（不验证）。约定行写法::

        verify: python -m unittest discover -s tests
    """
    return _parse_convention_command(workspace, "verify")


def parse_test_command(workspace: Path) -> str:
    """从 VIBE.md 提取 ``test: <命令>`` 约定行（K/P2-14）。

    IDE 内测试树（`GET /api/tests`）的配置兜底：优先级为
    环境变量 VIBE_TEST_COMMAND > 此约定行 > 默认空（面板显示「未配置」）。写法::

        test: python -m pytest -q
    """
    return _parse_convention_command(workspace, "test")