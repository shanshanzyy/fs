"""远程连接的持久化：`<workspace>/.vibe/remote.json`。

格式刻意做成**单个 JSON 对象**而不是「一行一条连接」的 JSONL：

    {"version": 1, "default": "devbox", "connections": [ {...}, {...} ]}

理由是这份文件需要表达**默认连接**这种文件级状态，而 JSONL 天然只适合
「追加若干条彼此独立的记录」。默认连接写在一行记录里，会让「改默认」变成
重写整份文件——不如直接承认它就是文件级状态。

落盘的三条硬要求：
1. **原子替换**：同目录临时文件 + `os.replace`，避免写一半断电留下半个 JSON。
2. **权限 0600**：文件里可能有私钥路径与内网主机名。POSIX 上收权限；
   Windows 上 ACL 由父目录继承，`chmod` 只能尽力而为（不报错、不假装成功）。
3. **损坏要报错，不要静默当空**：这是**配置**不是缓存。把一份被改坏的
   清单当成「没有连接」，会让用户以为连接凭空消失，而真正的原因被藏起来。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .connection import RemoteConnection
from .errors import RemoteConfigError
from .roots import PREFIX, RemoteRoot, normalize_sub, validate_root

FORMAT_VERSION = 1

# POSIX 上收权限用；Windows 上 chmod 只影响只读位，尽力而为。
_FILE_MODE = 0o600


def default_store_path(workspace: Path | str) -> Path:
    """默认位置：`<workspace>/.vibe/remote.json`。"""
    return Path(workspace).expanduser() / ".vibe" / "remote.json"


def store_path_for(config: object) -> Path:
    """按配置解析清单路径。

    `remote_config` 显式给出时用它（多工作区共用一份清单、
    或把清单放到受控目录的场景）；否则落回默认位置。
    """
    explicit = str(getattr(config, "remote_config", "") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return default_store_path(Path(str(getattr(config, "workspace"))))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RemoteStore:
    """连接清单的内存视图 + 落盘能力。"""

    path: Path
    connections: dict[str, RemoteConnection] = field(default_factory=dict)
    default_name: str = ""
    # 已登记的「工作区根」（M12）：远端连接上的某个子目录，出现在工作区树里，
    # 模型可直接用本地同名工具写 `@remote/<连接>/<子目录>` 操作它。
    roots: list[RemoteRoot] = field(default_factory=list)
    # 载入时被丢弃的坏根（连接不存在 / 子目录写法非法）的原因清单。
    # **不抛错** —— 一条坏根不该让整份连接清单不可用（用户会以为连接凭空消失）。
    # 端点把它原样展示出来，让用户知道「有东西被跳过了、为什么」。
    problems: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ 载入
    @classmethod
    def load(cls, path: Path | str) -> "RemoteStore":
        file_path = Path(path).expanduser()
        if not file_path.exists():
            # 文件不存在是**正常状态**（还没加过任何连接），与「损坏」区别对待。
            return cls(path=file_path)

        try:
            raw_text = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RemoteConfigError(
                f"无法读取远程连接清单 {file_path}: {exc}"
            ) from exc

        if not raw_text.strip():
            return cls(path=file_path)

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise RemoteConfigError(
                f"远程连接清单不是合法 JSON：{file_path}\n"
                f"  错误: {exc}\n"
                "  修复方式：改对语法，或删除该文件后重新用 "
                "`python -m agent_core.remote add` 添加连接。"
            ) from exc

        if not isinstance(data, dict):
            raise RemoteConfigError(
                f"远程连接清单顶层需为对象，实际: {type(data).__name__}（{file_path}）"
            )

        version = data.get("version", FORMAT_VERSION)
        if not isinstance(version, int) or version > FORMAT_VERSION:
            raise RemoteConfigError(
                f"远程连接清单版本 {version!r} 高于本客户端支持的 {FORMAT_VERSION}（{file_path}）。"
                "请升级客户端，或换用兼容清单。"
            )

        store = cls(path=file_path)
        entries = data.get("connections") or []
        if not isinstance(entries, list):
            raise RemoteConfigError(f"connections 字段需为数组（{file_path}）")

        for index, entry in enumerate(entries):
            try:
                conn = RemoteConnection.from_dict(entry)
            except RemoteConfigError as exc:
                raise RemoteConfigError(
                    f"清单第 {index + 1} 条连接非法（{file_path}）：{exc}"
                ) from exc
            store.connections[conn.name] = conn

        default = data.get("default") or ""
        if isinstance(default, str) and default:
            if default not in store.connections:
                # 默认指向一条不存在的连接：不报错，直接丢弃并留空 ——
                # 用户删掉默认连接是常见操作，此时让整份清单不可用是过度反应。
                default = ""
        store.default_name = default

        # 工作区根（M12）：条目非法（连接不存在 / 子目录写法不对）一律**丢弃并记录**，
        # 不抛错 —— 与上面 default 指向不存在的连接同一条理由。
        raw_roots = data.get("roots") or []
        if not isinstance(raw_roots, list):
            raise RemoteConfigError(f"roots 字段需为数组（{file_path}）")
        seen: set[str] = set()
        for index, entry in enumerate(raw_roots):
            try:
                root = RemoteRoot.from_dict(entry)
            except RemoteConfigError as exc:
                store.problems.append(f"第 {index + 1} 个远端根格式非法：{exc}")
                continue
            normalized = root.sub
            try:
                normalized = normalize_sub(root.sub)
            except RemoteConfigError as exc:
                store.problems.append(
                    f"远端根 {root.connection or '(未命名)'} 被跳过：{exc}"
                )
                continue
            candidate = RemoteRoot(
                connection=root.connection, sub=normalized, label=root.label
            )
            reason = validate_root(candidate, store)
            if reason:
                store.problems.append(
                    f"远端根 {candidate.connection or '(未命名)'} 被跳过：{reason}"
                )
                continue
            if candidate.key in seen:
                continue  # 重复登记：留第一条，静默去重
            seen.add(candidate.key)
            store.roots.append(candidate)
        return store

    # ------------------------------------------------------------------ 保存
    def save(self) -> None:
        payload = {
            "version": FORMAT_VERSION,
            "default": self.default_name,
            "connections": [self.connections[name].to_dict() for name in sorted(self.connections)],
            "roots": [root.to_dict() for root in self.roots],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".vibetmp", dir=str(self.path.parent)
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            _restrict(tmp_path)
            os.replace(tmp_path, self.path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        _restrict(self.path)

    # ------------------------------------------------------------------ 查询
    def names(self) -> list[str]:
        return sorted(self.connections)

    def list(self) -> list[RemoteConnection]:
        return [self.connections[name] for name in self.names()]

    def get(self, name: str) -> RemoteConnection | None:
        return self.connections.get(name or "")

    def require(self, name: str) -> RemoteConnection:
        """取一条连接，取不到就抛出**带可选项列表**的错误。

        弱模型经常用错连接名；把可用名字直接写进错误消息，能省掉一轮来回。
        """
        conn = self.get(name)
        if conn is not None:
            return conn
        available = "、".join(self.names()) or "（无）"
        raise RemoteConfigError(
            f"未找到名为 {name!r} 的远程连接。可用连接：{available}。\n"
            "  添加连接：python -m agent_core.remote add <名称> <用户@主机> --root <远端目录>"
        )

    def resolve(self, name: str | None = None) -> RemoteConnection:
        """解析要操作的连接：显式名 > 默认连接 > 唯一的那条。

        三条兜底顺序的理由：显式名永远优先；没写名字时用户的意图几乎必然是
        「我配的那台」；只配了一条时连默认都不用设，降低使用门槛。
        有多条又没设默认 —— 这时猜是危险的（可能把改动写到错的机器上），
        因此**拒绝并要求显式指定**。
        """
        if name:
            return self.require(name)
        if self.default_name:
            conn = self.get(self.default_name)
            if conn is not None:
                return conn
        if len(self.connections) == 1:
            return next(iter(self.connections.values()))
        if not self.connections:
            raise RemoteConfigError(
                "尚未配置任何远程连接。\n"
                "  添加连接：python -m agent_core.remote add <名称> <用户@主机> --root <远端目录>"
            )
        raise RemoteConfigError(
            "存在多条远程连接且未指定用哪条，已拒绝猜测（避免把改动写到错误的机器上）。\n"
            f"  可用连接：{'、'.join(self.names())}\n"
            "  请显式传入连接名，或用 `python -m agent_core.remote set-default <名称>` 设默认。"
        )

    # ------------------------------------------------------------------ 变更
    def upsert(
        self, conn: RemoteConnection, *, make_default: bool = False
    ) -> RemoteConnection:
        """新增或覆盖一条连接并落盘。覆盖时保留原 created_at。"""
        existing = self.connections.get(conn.name)
        if existing is not None and not conn.created_at:
            conn = conn.with_changes(created_at=existing.created_at)
        if not conn.created_at:
            conn = conn.with_changes(created_at=_now())
        self.connections[conn.name] = conn
        if make_default or not self.default_name:
            self.default_name = conn.name
        self.save()
        return conn

    def remove(self, name: str) -> None:
        self.require(name)
        self.connections.pop(name, None)
        if self.default_name == name:
            # 删掉默认连接后不自动选一个接任者：静默改默认的后果是
            # 下一次「不带名字」的操作打到一台用户没预期的机器上。
            self.default_name = ""
        self.save()

    def set_default(self, name: str) -> None:
        self.require(name)
        self.default_name = name
        self.save()

    # ------------------------------------------------------------------ 工作区根
    def roots_for(self, connection: str) -> list[RemoteRoot]:
        return [root for root in self.roots if root.connection == connection]

    def get_root(self, connection: str, sub: str = "") -> RemoteRoot | None:
        try:
            normalized = normalize_sub(sub)
        except RemoteConfigError:
            return None
        for root in self.roots:
            if root.connection == connection and root.sub == normalized:
                return root
        return None

    def add_root(
        self, connection: str, sub: str = "", label: str = ""
    ) -> RemoteRoot:
        """登记一个工作区根并落盘（原子写 + 0600，与连接同一份文件同一纪律）。"""
        normalized = normalize_sub(sub)
        candidate = RemoteRoot(connection=str(connection or "").strip(), sub=normalized, label=label)
        reason = validate_root(candidate, self)
        if reason:
            raise RemoteConfigError(reason)
        if self.get_root(candidate.connection, candidate.sub) is not None:
            raise RemoteConfigError(
                f"该目录已在工作区：{candidate.ns_path()}"
            )
        self.roots.append(candidate)
        self.save()
        return candidate

    def remove_root(self, connection: str, sub: str = "") -> RemoteRoot:
        """移出一个工作区根并落盘。不存在则报错（不静默成功）。"""
        root = self.get_root(connection, sub)
        if root is None:
            try:
                normalized = normalize_sub(sub)
            except RemoteConfigError as exc:
                raise RemoteConfigError(str(exc)) from exc
            raise RemoteConfigError(
                f"该远端目录不在工作区里：{PREFIX}{connection}/{normalized}"
            )
        self.roots = [item for item in self.roots if item.key != root.key]
        self.save()
        return root

    def match_root(self, connection: str, rel_to_root: str) -> RemoteRoot | None:
        """在给定连接上，找覆盖 `rel_to_root` 的**最具体**的那个登记根。

        「最具体」= sub 最长的那个。允许一个根套在另一个根里面（用户先加了
        `项目/smart-writer`，后来又加了整个 `项目`），取最长是为了让模型的路径
        解析结果与「看起来在哪个根里」一致。返回 None 表示该路径未被任何根覆盖。
        """
        target = str(rel_to_root or "").strip("/")
        best: RemoteRoot | None = None
        for root in self.roots:
            if root.connection != connection:
                continue
            if not root.sub:
                # 根本身：覆盖一切（长度视为 -1，好让任何具体子根盖过它）。
                if best is None:
                    best = root
                continue
            if target == root.sub or target.startswith(root.sub + "/"):
                if best is None or len(root.sub) > len(best.sub):
                    best = root
        return best

    def best_root(self, connection: str) -> RemoteRoot | None:
        """该连接上「最能代表它」的根：sub 最短的（""=root 本身优先）。

        供提示词与界面挑一条根做展示，避免把同一个连接的多个根都堆进提示词。
        """
        candidates = self.roots_for(connection)
        if not candidates:
            return None
        return min(candidates, key=lambda item: len(item.sub))


def _restrict(path: Path) -> None:
    """尽力把文件权限收到 0600。

    Windows 上 `chmod` 只映射到只读位，拿不到 POSIX 语义 —— 这里
    **不报错也不假装成功**，真正的保护靠父目录 ACL（`%USERPROFILE%`
    默认只有本人可读）。这是明确的降级，不是疏漏。
    """
    try:
        os.chmod(path, _FILE_MODE)
    except OSError:
        pass


__all__ = ["FORMAT_VERSION", "RemoteStore", "default_store_path", "store_path_for"]
