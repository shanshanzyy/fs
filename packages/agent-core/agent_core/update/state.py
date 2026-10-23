"""更新状态与反降级（M9）。

`state.json` 是这台机器「见过的最新版本」的记忆，存在的唯一理由是**防止
重放诱导降级**：一个攻击者（或一个配置错误的镜像）如果能把旧清单塞回来，
客户端不设防就会「干净利落地装上一个有已知漏洞的旧版本」。两种常见形态：

  · **重放**：把昨天那份合法清单（seq=7, v1.4）再发一次，诱导已升到 v1.5
    的机器「重新发现」并回退到 v1.4；
  · **回退**：直接给一份 v1.3 的清单，赌客户端只比版本号、不记历史。

对策是记住 `{maxSeq, maxVersion}` 并做**单调判定**：

  · `seq < maxSeq` → 拒绝（老清单，无论版本多新）；
  · `seq == maxSeq` 且版本没有前进 → 拒绝（重放）；
  · `seq > maxSeq` → 放行，**即使版本号倒退**。

最后一条是刻意的：`seq` 是发布者**显式递增**的序号（服务端 `next_release_seq`），
「发一份更高 seq 的旧版本」正是**回退机制**本身（见 M9 方案 §4 ⑦）——
发布者想让你退回 v1.4 时，他会重新 publish v1.4 并给一个更高的 seq。
所以「seq 更高」是发布者的**意图声明**，不该被版本号比较压掉。

`state.json` 与 `status.json` 用**同一份**原子写（同目录 tmp + os.replace）：
进程在写入中途被杀，磁盘上要么是旧的完整文件，要么是新的完整文件，
绝不出现半份 JSON。读坏时**当作空状态**（宁可多检查一次，也不能因为
一份坏文件就永久卡在「已是最新」）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .manifest import Manifest
from .version import is_newer

#: 状态文件名（放在 `<updates_dir>/state.json`）。
STATE_FILE = "state.json"

#: 允许的状态取值。界面按它决定 chip 的形态（hidden / available / required / failed）。
_STATUSES = ("idle", "checking", "available", "downloading", "staged", "failed", "applied")


@dataclass
class UpdateState:
    """`state.json` 的内存视图。字段刻意扁平，便于人工排查时直接读文件。"""

    # 反降级：见过的最大 seq / 对应版本
    max_seq: int = 0
    max_version: str = ""
    # 用户「忽略此版本」的选择
    skipped_version: str = ""
    # 最近一次流程状态
    status: str = "idle"
    available_version: str = ""
    staged_path: str = ""
    last_check_at: int = 0
    last_error: str = ""

    # ------------------------------------------------------------------ 序列化
    def to_document(self) -> dict[str, Any]:
        """camelCase 落盘（与 M9 方案、前端字段命名一致）。"""
        return {
            "maxSeq": self.max_seq,
            "maxVersion": self.max_version,
            "skippedVersion": self.skipped_version,
            "status": self.status,
            "availableVersion": self.available_version,
            "stagedPath": self.staged_path,
            "lastCheckAt": self.last_check_at,
            "lastError": self.last_error,
        }

    @classmethod
    def from_document(cls, document: Any) -> "UpdateState":
        """从任意 JSON 构造。**永不抛异常** —— 坏文件降级成空状态。"""
        if not isinstance(document, dict):
            return cls()

        def _int(value: Any) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        status = str(document.get("status") or "idle")
        return cls(
            max_seq=max(0, _int(document.get("maxSeq"))),
            max_version=str(document.get("maxVersion") or ""),
            skipped_version=str(document.get("skippedVersion") or ""),
            status=status if status in _STATUSES else "idle",
            available_version=str(document.get("availableVersion") or ""),
            staged_path=str(document.get("stagedPath") or ""),
            last_check_at=max(0, _int(document.get("lastCheckAt"))),
            last_error=str(document.get("lastError") or ""),
        )


# --------------------------------------------------------------------------- 判定
def allows(state: UpdateState, manifest: Manifest) -> tuple[bool, str]:
    """反降级判定。返回 `(是否放行, 拒绝原因)`。

    只判「历史」这一维；版本是否**新于当前安装版本**、平台是否匹配、
    灰度是否命中，都在 `service` 里另判 —— 这里放行了不代表一定会下载。
    """
    if manifest.seq < state.max_seq:
        return False, (
            f"清单 seq 倒退（本机已见最大 {state.max_seq}，此处 {manifest.seq}）"
        )
    if manifest.seq == state.max_seq and not is_newer(manifest.version, state.max_version):
        return False, (
            f"同 seq 且版本未前进（seq={manifest.seq}，"
            f"已见 {state.max_version or '无'}，此处 {manifest.version}）—— 疑似重放"
        )
    return True, ""


def accept(state: UpdateState, manifest: Manifest) -> None:
    """把一份**已放行**的清单记进历史（原地更新 `state`）。"""
    if manifest.seq >= state.max_seq:
        state.max_seq = manifest.seq
        state.max_version = manifest.version


# --------------------------------------------------------------------------- 读写
def _restrict(path: Path) -> None:
    """尽最大努力收紧到 0o600（Windows 上 chmod 基本无效，但无害）。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def state_path(updates_dir: str | Path) -> Path:
    return Path(updates_dir).expanduser() / STATE_FILE


def load(updates_dir: str | Path) -> UpdateState:
    """读状态；文件不存在或坏掉都返回空状态（fail-safe）。"""
    path = state_path(updates_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return UpdateState()
    try:
        return UpdateState.from_document(json.loads(raw))
    except ValueError:
        return UpdateState()


def save(updates_dir: str | Path, state: UpdateState) -> None:
    """原子写状态（同目录 tmp + `os.replace`）。"""
    path = state_path(updates_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{STATE_FILE}.{os.getpid()}.tmp"
    tmp.write_text(
        json.dumps(state.to_document(), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    os.replace(tmp, path)
    _restrict(path)


__all__ = [
    "STATE_FILE",
    "UpdateState",
    "accept",
    "allows",
    "load",
    "save",
    "state_path",
]
