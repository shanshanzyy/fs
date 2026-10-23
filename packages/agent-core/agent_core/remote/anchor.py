"""远程工作区的**本机锚点目录**（M13 / ADR-042）。

## 为什么需要它

把远端目录当成工作区时，`Config.workspace` 这个字段在内核里有一批既有消费者：
审计链落到 `<workspace>/.vibe/audit.jsonl`、会话库、检查点清单、远端写的本机留底。
做一个「SSH 版 PathGuard / SSH 版审计 append」把它们搬过去，等于重写内核一半，
而且一旦远端掉线，本机审计证据链就跟着断档（直接踩 ADR-006）。

ADR-042 的做法是**把两个语义拆开**：

    jail 的根        → 由 `Config.remote_workspace` 决定（`RemoteRouter` 的默认根分支）
    `.vibe` 资产的落点 → 恒为本机 —— 就是这个模块算出来的锚点目录

于是 `<workspace>/.vibe` 那一批注入点一行都不用改，`Config.workspace` 的语义
也维持不变（恒为本机路径）。

## 锚点放哪

    ~/.vibe/workspaces/remote/<安全化的连接名>/<子目录 sha256[:12]>/

用**连接名 + 子目录指纹**而不是让用户挑一个本机目录：让用户先选「放远程工作区元数据的
本机目录」是讲不清的心智负担，而这里面确实一行代码都没有 —— 只有审计链、会话库、
检查点与远端写的留底。对子目录做指纹是因为它可以含任意 UTF-8 路径段（中文目录名是常态），
直接当目录名既容易超限也可能撞上文件系统的保留字符。

## 为什么从不自动清理

删锚点 = 删审计证据 + 删改写前的留底。ADR-006 与 ADR-037 都把这两样当成事故复盘
的唯一依据，因此清理必须是用户显式动作，这里不提供任何自动 GC。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "ANCHOR_MARKER",
    "anchor_dir",
    "anchor_home",
    "ensure_anchor",
    "list_anchors",
    "read_anchor",
    "safe_name",
]

ANCHOR_MARKER = ".vibe-workspace-anchor.json"

# 目录名白名单。**故意不含句点** —— 于是 `..` 这种形态在结构上不可能出现，
# 不必依赖「记得事后检查」。连接名是用户填的自由文本，必须按最坏打算处理。
_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
_MAX_NAME = 64


def anchor_home(*, home: str | Path | None = None) -> Path:
    """锚点根目录。`home` 是测试注入点（默认走 `~/.vibe/workspaces/remote`）。"""
    if home is not None:
        return Path(home).expanduser()
    return Path.home() / ".vibe" / "workspaces" / "remote"


def safe_name(connection: str) -> str:
    """把连接名压成安全的单层目录名。**恒非空、恒不含分隔符与句点。**"""
    text = _SAFE_RE.sub("_", str(connection or "").strip())
    text = text[:_MAX_NAME].strip("_")
    # 全被替换光了（比如连接名是纯中文）→ 退回一个稳定的占位名，而不是空串：
    # 空串会让后来的路径拼接触摸到锚点根本身。
    return text or "_default"


def anchor_dir(
    connection: str,
    sub: str = "",
    *,
    home: str | Path | None = None,
) -> Path:
    """纯计算（**不建目录**）：`<锚点根>/<安全名>/<子目录指纹 12 位>`。"""
    digest = hashlib.sha256(str(sub or "").encode("utf-8")).hexdigest()[:12]
    return anchor_home(home=home) / safe_name(connection) / digest


def ensure_anchor(
    connection: str,
    sub: str = "",
    *,
    ns_path: str = "",
    home: str | Path | None = None,
) -> Path:
    """建好锚点目录并写标记文件。幂等，重复调用不会覆盖已有标记。

    标记文件让「这个空目录到底是谁的锚点」可追溯 —— 也正因为有它，
    `list_anchors()` 才能靠**扫目录**还原历史，不必再维护一份清单格式。
    """
    path = anchor_dir(connection, sub, home=home)
    root = anchor_home(home=home)
    # 双保险：即便将来有人改了 safe_name，也不能让结果逃出锚点根 ——
    # 这是唯一的跨界检查点，宁可在这里炸也不要等到写文件时才发现。
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:  # pragma: no cover - 只有改坏 safe_name 才会到这里
        raise ValueError(f"锚点路径落在锚点根之外，已拒绝：{path}") from exc

    path.mkdir(parents=True, exist_ok=True)
    (path / ".vibe").mkdir(exist_ok=True)  # 审计链与会话库的落点为后续写入提前备好
    marker = path / ANCHOR_MARKER
    if not marker.is_file():
        payload = {
            "connection": connection,
            "sub": sub,
            "nsPath": ns_path,
            "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        marker.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return path


def read_anchor(path: str | Path) -> dict[str, Any] | None:
    """读锚点标记。**读不到返回 None 而不是抛错** —— `list_anchors()` 要能容忍
    目录树里混着半旧的、没写成功的锚点，扫出多少算多少。"""
    try:
        raw = json.loads(Path(path, ANCHOR_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return raw if isinstance(raw, dict) else None


def list_anchors(*, home: str | Path | None = None) -> list[dict[str, Any]]:
    """扫锚点根反推「切过哪些远程工作区」（按最近活动时间倒序）。

    只汇报**标记读得出来**的锚点，其余跳过 —— 半截目录不该让整个列表 500。
    """
    root = anchor_home(home=home)
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for conn_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for anchor in sorted(p for p in conn_dir.iterdir() if p.is_dir()):
            info = read_anchor(anchor)
            if info is None:
                continue
            activity = anchor / ".vibe" / "audit.jsonl"
            try:
                stamp = activity.stat().st_mtime if activity.is_file() else anchor.stat().st_mtime
            except OSError:  # noqa: BLE001 — 列表排序不该因为一个坏 stat 而失败
                stamp = 0.0
            out.append(
                {
                    "path": str(anchor),
                    "connection": str(info.get("connection") or ""),
                    "sub": str(info.get("sub") or ""),
                    "nsPath": str(info.get("nsPath") or ""),
                    "createdAt": str(info.get("createdAt") or ""),
                    "_stamp": stamp,
                }
            )
    out.sort(key=lambda item: item["_stamp"], reverse=True)
    for item in out:
        item.pop("_stamp", None)
    return out
