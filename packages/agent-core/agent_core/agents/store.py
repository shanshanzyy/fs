"""智能体档案的持久化：`<workspace>/.vibe/agents.json`（M14-A）。

格式：

    {"version": 1, "agents": [ {...}, {...} ]}

三条纪律沿用 `remote/store.py`（同类文件、同一套教训）：

1. **原子替换**：同目录临时文件 + `os.replace`，避免写一半留下半个 JSON；
2. **权限 0600**：POSIX 上收权限；Windows 靠父目录 ACL 继承（尽力而为，不假装成功）；
3. **损坏要报错，不要静默当空**：档案是**配置**不是缓存。把一份被改坏的文件
   当成「没有档案」，用户会以为档案凭空消失，而真正的原因被藏起来。

与远程清单的**关键差异**：落点恒随 `Config.workspace`，不提供任何「把档案钉到
别处」的配置项 —— 见 `store_path_for()` 的说明。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .profile import AgentProfile, AgentProfileError, MAX_FILE_BYTES

FORMAT_VERSION = 1

# POSIX 上收权限用；Windows 上 chmod 只影响只读位，尽力而为。
_FILE_MODE = 0o600


def default_store_path(workspace: Path | str) -> Path:
    """默认位置：`<workspace>/.vibe/agents.json`。"""
    return Path(workspace).expanduser() / ".vibe" / "agents.json"


def store_path_for(config: object) -> Path:
    """按配置解析档案路径。

    **恒随 `Config.workspace`**：M13 允许把远程连接清单钉在别的工作区，
    但智能体档案属于「这个工作区里有哪几个智能体」，跟着被钉住的清单跑
    就会出现「打开新文件夹还带着旧工作区的智能体」。
    """
    return default_store_path(Path(str(getattr(config, "workspace"))))


@dataclass
class AgentStore:
    """档案文件的内存视图 + 落盘能力。"""

    path: Path
    agents: dict[str, AgentProfile] = field(default_factory=dict)

    # ------------------------------------------------------------------ 载入
    @classmethod
    def load(cls, path: Path | str) -> "AgentStore":
        file_path = Path(path).expanduser()
        if not file_path.exists():
            return cls(path=file_path)

        try:
            raw_text = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AgentProfileError(f"无法读取智能体档案 {file_path}: {exc}") from exc

        if not raw_text.strip():
            return cls(path=file_path)

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise AgentProfileError(
                f"智能体档案不是合法 JSON：{file_path}\n  错误: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise AgentProfileError(
                f"智能体档案顶层需为对象，实际: {type(data).__name__}（{file_path}）"
            )

        version = data.get("version", FORMAT_VERSION)
        if not isinstance(version, int) or version > FORMAT_VERSION:
            raise AgentProfileError(
                f"智能体档案版本 {version!r} 高于本客户端支持的 {FORMAT_VERSION}"
                f"（{file_path}）。请升级客户端，或换用兼容文件。"
            )

        entries = data.get("agents") or []
        if not isinstance(entries, list):
            raise AgentProfileError(f"agents 字段需为数组（{file_path}）")

        store = cls(path=file_path)
        for index, entry in enumerate(entries):
            try:
                profile = AgentProfile.from_dict(entry)
            except AgentProfileError as exc:
                raise AgentProfileError(
                    f"档案第 {index + 1} 条非法（{file_path}）：{exc}"
                ) from exc
            # 手改文件可能让 id 为空；补一个稳定 id 而不是整份文件报废。
            if not profile.id:
                profile = AgentProfile(
                    id=f"agent{index + 1}",
                    name=profile.name,
                    role=profile.role,
                    system_prompt=profile.system_prompt,
                    tools=profile.tools,
                    enabled=profile.enabled,
                )
            if profile.id in store.agents:
                continue  # 重复 id：留第一条，静默去重
            store.agents[profile.id] = profile
        return store

    # ------------------------------------------------------------------ 保存
    def save(self) -> None:
        payload: dict[str, Any] = {
            "version": FORMAT_VERSION,
            "agents": [self.agents[key].to_dict() for key in sorted(self.agents)],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        size = len(text.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise AgentProfileError(
                f"智能体档案总大小超过 {MAX_FILE_BYTES // 1024} KB"
                f"（当前 {size} 字节）。请缩短提示词或减少档案数量。"
            )
        write_text_atomic(self.path, text)

    # ------------------------------------------------------------------ 查询
    def list(self) -> list[AgentProfile]:
        return [self.agents[key] for key in sorted(self.agents)]

    def get(self, agent_id: str) -> AgentProfile | None:
        return self.agents.get(agent_id or "")

    def require(self, agent_id: str) -> AgentProfile:
        profile = self.get(agent_id)
        if profile is not None:
            return profile
        available = "、".join(item.name for item in self.list()) or "（无）"
        raise AgentProfileError(
            f"未找到智能体档案 {agent_id!r}。现有档案：{available}"
        )

    # ------------------------------------------------------------------ 变更
    def upsert(self, profile: AgentProfile) -> AgentProfile:
        """新增或覆盖一条档案（调用方须先 `validate_profile()`）。"""
        self.agents[profile.id] = profile
        return profile

    def remove(self, agent_id: str) -> bool:
        if agent_id not in self.agents:
            return False
        self.agents.pop(agent_id, None)
        return True


def write_text_atomic(path: Path, text: str) -> None:
    """原子写（同目录临时文件 + `os.replace`）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".vibetmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _restrict(tmp_path)
        _replace_with_retry(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    _restrict(path)


def _replace_with_retry(src: Path, dst: Path, attempts: int = 12) -> None:
    """Windows 上 `dst` 正被别的线程/进程**以读方式**打开时，`os.replace`
    会抛 PermissionError（WinError 5/32）。这不是真正的权限问题，占用通常只有
    毫秒级：退让重试即可。否则「界面在刷新列表、引擎在逐 Turn 落盘」这种正常
    并发就会偶发写失败，把后台讨论线程打挂。
    """
    delay = 0.005
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 1.6, 0.08)


def read_text_retry(path: Path, attempts: int = 12) -> str:
    """读侧的同一类退让：写方 `os.replace` 的瞬间，读会拿到 PermissionError。"""
    delay = 0.005
    for attempt in range(attempts):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 1.6, 0.08)
    raise AssertionError("unreachable")


def _restrict(path: Path) -> None:
    """尽力把文件权限收到 0600（Windows 上只是尽力而为，不报错）。"""
    try:
        os.chmod(path, _FILE_MODE)
    except OSError:
        pass


__all__ = [
    "FORMAT_VERSION",
    "AgentStore",
    "default_store_path",
    "store_path_for",
    "write_text_atomic",
]
