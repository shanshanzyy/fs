"""逐 hunk 拆分与应用（D7）—— 「逐 hunk 接受/拒绝」的引擎。

这不是另开一条写盘路径，而是**对补丁子集应用同一个补丁引擎**：

    split_hunks(old, new) -> list[Hunk]        把「模型给的新文本」切成可勾选的块
    apply_hunks(old, hunks, accepted) -> str   只把勾中的块合进去，未勾的原样保留

调用方随后仍旧走 ``plan_overwrite`` + ``commit_plan`` 落盘（ADR-004 的受控例外，
边界见 ADR-011），因此「勾掉一半」与「整份替换」共享同一套检查点与审计。

为什么用 ``SequenceMatcher.get_opcodes()`` 而不是解析 unified diff 文本：
diff 文本是有损的中间表示（上下文行、``@@`` 头都要正确反解），多一层解析就多一类
「看得到却应用不了」的边界 bug。opcode 直接给出行区间，重组是纯文本拼接，可证正确。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

# 单次改写最多产出多少个 hunk。极端情况下模型可能把整段代码打散成上百块，
# 勾选界面会变得没法用，也会把事件流撑大 —— 超过就截断（多余差异整体丢弃）。
MAX_HUNKS = 200


@dataclass
class Hunk:
    """一个可独立接受/拒绝的最小差异块。

    ``old_start``/``old_end`` 是 **0 基半开区间** [old_start, old_end)：
    ``old_lines = old.split("\\n")[old_start:old_end]``，空区间即纯插入。
    """

    index: int
    tag: str  # replace / delete / insert
    old_start: int
    old_end: int
    old_lines: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)

    @property
    def added(self) -> int:
        return len(self.new_lines)

    @property
    def removed(self) -> int:
        return len(self.old_lines)

    def header(self) -> str:
        """给界面看的一行说明（1 基行号，贴合编辑器习惯）。"""
        if self.tag == "insert":
            return f"在第 {self.old_start} 行后插入 {len(self.new_lines)} 行"
        if self.tag == "delete":
            return f"删除第 {self.old_start + 1}-{self.old_end} 行"
        return (
            f"改写第 {self.old_start + 1}-{self.old_end} 行"
            f"（{len(self.old_lines)} 行 → {len(self.new_lines)} 行）"
        )

    def diff(self) -> str:
        """该块自己的 unified diff（不做跨块合并，避免与勾选状态错位）。"""
        lines = list(
            difflib.unified_diff(
                self.old_lines,
                self.new_lines,
                fromfile="a",
                tofile="b",
                lineterm="",
                n=1,
            )
        )
        return "\n".join(lines)

    def to_api(self) -> dict[str, object]:
        return {
            "index": self.index,
            "tag": self.tag,
            "header": self.header(),
            "oldStart": self.old_start + 1,
            "oldEnd": self.old_end,
            "oldLines": list(self.old_lines),
            "newLines": list(self.new_lines),
            "added": self.added,
            "removed": self.removed,
            "diff": self.diff(),
        }


def split_hunks(old: str, new: str, *, limit: int = MAX_HUNKS) -> list[Hunk]:
    """把 old → new 的差异拆成有序、互不重叠的 hunk 列表。

    行域用 ``split("\\n")``（与 TextDoc 的归一化文本一致），因此末尾换行/
    行数变化都能被如实表达。``autojunk=False`` 是必须的：默认启发式会把
    常见行（如 ``}``、``return``）当作「垃圾」忽略，导致本该拆开的块被吞掉。
    """
    old_lines = old.split("\n")
    new_lines = new.split("\n")
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)

    hunks: list[Hunk] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        hunks.append(
            Hunk(
                index=len(hunks),
                tag=tag,
                old_start=i1,
                old_end=i2,
                old_lines=old_lines[i1:i2],
                new_lines=new_lines[j1:j2],
            )
        )
        if len(hunks) >= limit:
            break
    return hunks


def apply_hunks(old: str, hunks: list[Hunk], accepted: set[int]) -> str:
    """按勾选集合重组文本：勾中的块用 new_lines，未勾的保留 old_lines。

    ``hunks`` 必须是 ``split_hunks`` 的原样输出（有序、互不重叠）；否则区间拼不回去。
    空 ``accepted`` 返回与 ``old`` 逐字节相同的结果 —— 这是「全部拒绝」的语义，
    调用方据此可判断「无改动」而不落盘。
    """
    old_lines = old.split("\n")
    ordered = sorted(hunks, key=lambda h: h.old_start)

    out: list[str] = []
    cursor = 0
    for hunk in ordered:
        if hunk.old_start < cursor:
            raise ValueError("hunk 区间重叠，无法重组（不是 split_hunks 的输出？）")
        out.extend(old_lines[cursor : hunk.old_start])
        if hunk.index in accepted:
            out.extend(hunk.new_lines)
        else:
            out.extend(hunk.old_lines)
        cursor = hunk.old_end
    out.extend(old_lines[cursor:])
    return "\n".join(out)


__all__ = ["MAX_HUNKS", "Hunk", "apply_hunks", "split_hunks"]