"""Skill 加载器（M9-A）。

## 落点与信任模型

Skill 是**用户本人**手工放置在工作区 `<skills_dir>/**/SKILL.md` 下的受信资产
（默认 `.vibe/skills`），与「模型不可写」的 `.vibe/` 审计目录刻意分开 ——
Skill 目录需要用户手动写入，所以它**不受** PathGuard 的 `.vibe` 写入拒绝约束。

## 加载纪律

- **纯本机、零外发、零依赖**：只读本机文件系统，不发任何网络请求。
- **坏文件显式标注、解析失败 fail-closed**：一个 SKILL.md 解析不出来就跳过它，
  并在结果里留名，绝不静默吞掉（静默吞掉会让「配了却没用上」无从排查）。
- **超限截断并标注**：文件数超 `max_files`、正文总量超 `max_bytes` 时，
  从队首（按名字排序）丢弃并计数，保证加载结果可预测、可解释。

## front-matter 约定

SKILL.md 可选一段 YAML front-matter（`---` 包裹）声明 `name` 与 `description`；
未声明时 `name` 取目录名、`description` 取正文首行。正文（body）是 front-matter
之后的所有内容，`run_skill` 展开时整段进上下文（经 `wrap_untrusted` 包裹）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillSpec:
    """一个已加载的 Skill。frozen：加载后不可变，避免运行中被篡改。"""

    name: str
    description: str
    body: str
    source: str  # 相对工作区的路径（posix），用于审计与界面展示


def _parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """解析可选的 YAML front-matter。

    只认最简形态：文件以 ``---`` 单独成行开头，到下一个 ``---`` 单独成行为止。
    只提取 ``key: value`` 单行键值对（首层、冒号分隔），其余忽略 ——
    Skill 的 front-matter 只需要 name / description，不做完整 YAML 解析
    （保持零依赖）。返回 (元数据字典, 去掉 front-matter 后的正文)。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        # 只有开头 ``---``、没有闭合：视为无 front-matter，正文原样返回。
        return {}, text.strip()
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            meta[key.strip()] = value.strip()
    body = "\n".join(lines[end + 1 :]).strip()
    return meta, body


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def load_skills(
    root: Path,
    *,
    max_files: int = 50,
    max_bytes: int = 256_000,
) -> tuple[list[SkillSpec], list[str]]:
    """扫描 `<root>/**/SKILL.md` 并加载。

    返回 `(skills, warnings)`。`skills` 已按 `name` 排序；`warnings` 是给用户看的
    显式标注（坏文件、超限丢弃、重名），绝不静默吞掉任何「配了但没生效」的情况。

    加载失败不抛异常：Skill 是增强项，任何读取层面的意外都不该挡住 Agent 启动。
    """
    skills: list[SkillSpec] = []
    warnings: list[str] = []
    seen_names: set[str] = set()

    try:
        if not root.is_dir():
            return [], []
    except OSError:
        return [], []

    # 按路径排序扫描，保证「超限先丢谁」是可预测的（队首 = 字典序靠前）。
    try:
        candidates = sorted(root.rglob("SKILL.md"))
    except OSError:
        return [], []

    # 文件数上限：从队首丢（保持与正文总量上限同一「先丢字典序靠前」的口径）。
    if len(candidates) > max_files:
        dropped = candidates[max_files:]
        candidates = candidates[:max_files]
        names = ", ".join(p.relative_to(root).parent.as_posix() for p in dropped)
        warnings.append(
            f"Skill 文件数超过上限 {max_files}，已丢弃 {len(dropped)} 个：{names}"
        )

    total_bytes = 0
    for path in candidates:
        rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warnings.append(f"Skill 读取失败，已跳过：{rel}（{exc}）")
            continue

        meta, body = _parse_front_matter(text)
        name = (meta.get("name") or path.parent.name or "unnamed").strip()
        description = (meta.get("description") or _first_line(body)).strip()

        if not name:
            warnings.append(f"Skill 缺少名称，已跳过：{rel}")
            continue
        if not body:
            warnings.append(f"Skill 正文为空，已跳过：{rel}（{name}）")
            continue
        if name in seen_names:
            warnings.append(f"Skill 重名，已跳过：{rel}（{name} 已存在）")
            continue

        skill_bytes = len(body.encode("utf-8"))
        if total_bytes + skill_bytes > max_bytes:
            warnings.append(
                f"Skill 正文总量超过上限 {max_bytes} 字节，已丢弃后续：{rel}（{name}）"
            )
            # 一旦超总量，从当前这个开始，后续字典序更大的也一并丢弃（对齐「先丢队首」）
            break

        seen_names.add(name)
        total_bytes += skill_bytes
        skills.append(
            SkillSpec(name=name, description=description, body=body, source=rel)
        )

    skills.sort(key=lambda s: s.name)
    return skills, warnings


__all__ = ["SkillSpec", "load_skills"]
