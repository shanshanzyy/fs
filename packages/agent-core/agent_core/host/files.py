"""给界面用的工作区浏览接口：文件树与文件内容。

安全性靠复用 —— 所有路径都过 `PathGuard`，因此界面能看到的范围与 Agent 能读到的
范围**完全一致**：越狱路径被拒，`.env` / 私钥等敏感文件同样被拒。
不另起一套「界面专用」的宽松规则，否则迟早会出现「Agent 读不到但界面能打开」
这种自相矛盾的边界。
"""

from __future__ import annotations

import hashlib
import os
import string
from pathlib import Path
from typing import Any

from ..edit import TextDoc
from ..errors import AgentError, SandboxViolation
from ..sandbox import PathGuard
from ..tools.base import DEFAULT_IGNORED_DIRS, MAX_SCAN_BYTES

MAX_TREE_ENTRIES = 4_000
DEFAULT_TREE_DEPTH = 6
MAX_DIR_ENTRIES = 500


class FileAccessError(RuntimeError):
    pass


def build_tree(
    guard: PathGuard,
    *,
    rel: str = "",
    depth: int = DEFAULT_TREE_DEPTH,
    limit: int = MAX_TREE_ENTRIES,
) -> dict[str, Any]:
    """列出工作区（或某个子目录）的树。

    只做浅层懒加载没意义 —— 侧边栏一次拿到全貌，用户才能快速跳转。
    因此这里一次递归到底（有条目数与深度上限兜底）。
    """
    root = guard.check_list(rel or None)
    if not root.is_dir():
        raise FileAccessError(f"{guard.relative(root)} 不是目录")

    counter = {"n": 0, "truncated": False}

    def walk(directory: Path, level: int) -> list[dict[str, Any]]:
        if level > depth or counter["truncated"]:
            return []
        try:
            with os.scandir(directory) as scanner:
                entries = sorted(
                    scanner, key=lambda e: (not e.is_dir(), e.name.lower())
                )
        except OSError:
            return []

        nodes: list[dict[str, Any]] = []
        for entry in entries:
            if counter["n"] >= limit:
                counter["truncated"] = True
                break
            name = entry.name
            if name in DEFAULT_IGNORED_DIRS:
                continue
            if name.endswith((".pyc", ".pyo")):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue

            counter["n"] += 1
            node: dict[str, Any] = {
                "name": name,
                "path": guard.relative(Path(entry.path)),
                "type": "dir" if is_dir else "file",
            }
            if is_dir:
                children = walk(Path(entry.path), level + 1)
                if children:
                    node["children"] = children
            else:
                try:
                    node["size"] = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    node["size"] = 0
                node["sensitive"] = guard.is_sensitive(Path(entry.path))
            nodes.append(node)
        return nodes

    return {
        "root": str(root),
        "relative": guard.relative(root),
        "nodes": walk(root, 1),
        "truncated": counter["truncated"],
    }


def file_digest(path: Path) -> str:
    """文件内容指纹（sha256 前 16 位）—— 编辑保存的乐观锁用（D11）。

    刻意取**磁盘原始字节**而非解码后的文本：乐观锁要比的是「文件有没有被别处
    改过」，字节级最严格，也绕开了编码/换行归一带来的歧义。
    """
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()[:16]


def read_text_file(guard: PathGuard, rel: str, *, max_bytes: int = MAX_SCAN_BYTES) -> dict[str, Any]:
    """读取单个文本文件。走 PathGuard，敏感文件会被拒。"""
    if not rel or not rel.strip():
        raise FileAccessError("缺少 path 参数")

    try:
        path = guard.check_read(rel)
    except SandboxViolation as exc:
        raise FileAccessError(str(exc)) from exc

    if not path.is_file():
        raise FileAccessError(f"{guard.relative(path)} 不存在或不是普通文件")

    size = path.stat().st_size
    if size > max_bytes:
        raise FileAccessError(
            f"文件过大（{size} 字节，上限 {max_bytes}），界面不做展示"
        )

    try:
        doc = TextDoc.load(path)
    except AgentError as exc:
        raise FileAccessError(f"无法作为文本读取：{exc}") from exc

    lines = doc.text.split("\n")
    return {
        "path": guard.relative(path),
        "text": doc.text,
        "lines": len(lines),
        "encoding": doc.encoding,
        "newline": "CRLF" if doc.newline == "\r\n" else "LF",
        "bom": doc.had_bom,
        "size": size,
        "truncated": False,
        # 乐观锁指纹（D11）：保存时原样回传，磁盘已被改则拒绝覆盖。
        "digest": file_digest(path),
    }


def _roots() -> list[dict[str, str]]:
    """文件系统的起点：Windows 是各盘符，其余平台就是 /。"""
    if os.name == "nt":
        return [
            {"name": f"{letter}:\\", "path": f"{letter}:\\"}
            for letter in string.ascii_uppercase
            if Path(f"{letter}:\\").is_dir()
        ]
    return [{"name": "/", "path": "/"}]


def list_dirs(raw: str) -> dict[str, Any]:
    """列出某个目录的直接子目录 —— 切换工作区弹窗里的「内置目录浏览器」。

    刻意**不走 PathGuard**：这个接口服务的正是「挑一个当前 jail 之外的新工作区」，
    用 jail 去卡它等于让功能永远不可用。风险由 host 的闸门兜住（只监听本机 +
    每次启动的随机 token + Host/Origin 双校验），且这里只回传目录名，
    **不读任何文件内容** —— 与「界面能看到的文件范围 = Agent 能读到的范围」那条
    原则不冲突，因为目录名不属于文件内容。

    `raw` 为空表示「还没选盘」：Windows 下返回各盘符（外加主目录快捷入口），
    其余平台返回 `/`。
    """
    text = str(raw or "").strip()
    if not text:
        return {
            "path": "",
            "parent": None,
            "roots": _roots(),
            "dirs": [],
            "home": str(Path.home()),
        }

    target = Path(text).expanduser()
    if not target.is_dir():
        raise ValueError(f"不是目录或不存在：{target}")

    try:
        target = target.resolve()
        with os.scandir(target) as scanner:
            found = sorted(
                (
                    entry.name
                    for entry in scanner
                    if _is_dir(entry)
                ),
                key=str.lower,
            )
    except PermissionError as exc:
        raise FileAccessError(f"没有权限读取目录：{target}") from exc
    except OSError as exc:
        raise FileAccessError(f"无法读取目录 {target}：{exc}") from exc

    parent = target.parent
    return {
        "path": str(target),
        # 到根（Windows 的 `E:\`、POSIX 的 `/`）时 parent 就是自己 —— 此时给 null，
        # 界面据此把「上级」转到盘符列表，而不是原地打转。
        "parent": None if parent == target else str(parent),
        "dirs": [{"name": name, "path": str(target / name)} for name in found[:MAX_DIR_ENTRIES]],
    }


def _is_dir(entry: os.DirEntry[str]) -> bool:
    """不跟随符号链接判定目录：目录链接会让浏览器把用户带出可预期的范围。"""
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False
