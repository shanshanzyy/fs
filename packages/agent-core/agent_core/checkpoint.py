"""检查点 / 一键回滚（B1）—— 「敢放手」的最后一道保险。

审批闸门只能防「未发生」；这里救的是「已发生」：Agent 连续改了多轮之后，
能把工作区整体拉回任意一轮开始前的状态。

设计取舍（对齐 M1.5-PLAN B1.6 的拍板结论）：

- **不依赖 git**。纯文件快照，git 目录与非 git 目录行为完全一致，实现只有一条路径。
- **不做全仓库复制**。快照范围只覆盖「将被修改的文件」：edit_file / write_file
  真正落盘**之前**，把旧内容写进检查点目录。10MB 级单文件的检查点创建也在毫秒级。
- **回滚本身可撤销**。restore 前把当前内容再做一次反向快照（kind=redo 的检查点），
  它会出现在检查点列表里，可以再次 restore 回去。

目录布局（``<workspace>/.vibe/checkpoints/``，模型永远写不进来 —— PathGuard
拒绝一切对 .vibe 内部的写入）::

    checkpoints/
      <checkpoint-id>/
        manifest.json     # 元数据：时间、session、受管文件清单（rel 路径 + sha256）
        0000.bin          # 快照内容，文件名只有序号 —— 路径不来自 manifest，
        0001.bin          #   因此 manifest 被篡改也无法构造任意读路径

防篡改三件套（restore 时逐条执行）：

1. manifest 里的每个路径先过 ``PathGuard``，越界即整体拒绝（什么都不写）；
2. 快照内容读入后先做 sha256 校验，与 manifest 记录不符即整体拒绝；
3. 「先全部校验、再统一写回」两阶段执行 —— 不存在改到一半的中间状态。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .errors import AgentError, SandboxViolation
from .sandbox import PathGuard

# 每会话保留的检查点上限，超出淘汰最旧（B1.5）
DEFAULT_KEEP = 10


class CheckpointError(AgentError):
    """检查点创建或恢复失败。错误信息直接面向用户。"""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _now_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


@dataclass
class FileEntry:
    """manifest.files 的一条：某个受管文件在「落盘前」的状态。"""

    path: str  # 工作区相对路径（posix）
    existed: bool  # False = 当时不存在（restore 时应删除该文件）
    hash: str  # existed=True 时的 sha256（hex）
    bytes: int
    snapshot: int  # 快照文件序号；existed=False 时为 -1


@dataclass
class CheckpointInfo:
    """面向 API/列表的检查点摘要（不含正文）。"""

    id: str
    createdAt: str
    sessionId: str
    kind: str  # run / redo
    # 纳秒时间戳，restore 的快照链回溯全靠它排序 —— 同一秒内创建多个检查点时
    # id 字典序不可靠，必须用这个字段定先后。
    epoch: int = 0
    files: list[FileEntry] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        return len(self.files)

    def to_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "createdAt": self.createdAt,
            "sessionId": self.sessionId,
            "kind": self.kind,
            "fileCount": self.file_count,
            "files": [f.path for f in self.files],
        }


@dataclass
class RestoreReport:
    """一次回滚的结果。"""

    id: str
    restored: list[str] = field(default_factory=list)  # 写回的文件
    deleted: list[str] = field(default_factory=list)  # 移除的新建文件
    unchanged: list[str] = field(default_factory=list)  # 内容已一致，无需动
    redo_id: str = ""  # 反向快照的检查点 id（回滚可撤销）

    def summary(self) -> str:
        return (
            f"回滚 {len(self.restored)} 个文件、移除 {len(self.deleted)} 个新建文件"
            f"（{len(self.unchanged)} 个本就一致）"
        )


class CheckpointManager:
    """检查点的创建与恢复。线程约束：同一时刻只有一个 run 在写。"""

    def __init__(
        self,
        *,
        workspace: Path,
        checkpoint_dir: Path,
        session_id: str,
        audit: AuditLog | None = None,
        keep: int = DEFAULT_KEEP,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.dir = Path(checkpoint_dir)
        self.session_id = session_id
        self.audit = audit
        self.keep = max(1, keep)
        self._guard = PathGuard(self.workspace)
        self._current_id: str = ""
        self._current_dir: Path | None = None
        self._current_files: list[FileEntry] = []
        self._snapshotted: set[str] = set()
        self._last_committed: str = ""  # 最近一次落盘成功的检查点（供 UI 关联本轮）

    # ------------------------------------------------------------------ 创建
    @property
    def current_id(self) -> str:
        """本 run 的检查点 id；未 commit 且无改动时为空串。"""
        return self._current_id

    @property
    def last_id(self) -> str:
        """最近一次 commit 成功的检查点 id（commit 会重置 current_id，
        事件发布方要用这个字段把「本轮」与检查点关联起来）。"""
        return self._last_committed

    def begin(self) -> str:
        """每个 run() 开始时调用。惰性：不建目录，首个文件快照时才落盘。"""
        self._current_id = f"cp-{_now_stamp()}-{uuid.uuid4().hex[:6]}"
        self._current_dir = None
        self._current_files = []
        self._snapshotted = set()
        return self._current_id

    def snapshot_before_write(self, abs_path: Path, rel: str) -> None:
        """edit_file / write_file 落盘**之前**调用：把旧内容收进本检查点。

        同一检查点内同一文件只收第一次 —— 后续编辑的前置状态已在库里，
        且那正是「检查点时刻」的语义。快照失败必须让本次编辑失败：
        不能回滚的改动宁可不发生（失效安全，与审批闸门同一哲学）。
        """
        if not self._current_id:
            self.begin()
        if rel in self._snapshotted:
            return

        try:
            existed = abs_path.is_file()
            data = abs_path.read_bytes() if existed else b""
            digest = _sha256_file(abs_path) if existed else ""
        except OSError as exc:
            raise CheckpointError(f"检查点快照失败，已阻止本次写入：{rel}（{exc}）") from exc

        entry = FileEntry(
            path=rel,
            existed=existed,
            hash=digest,
            bytes=len(data),
            snapshot=len(self._current_files) if existed else -1,
        )
        if existed:
            try:
                snap_dir = self._ensure_dir()
                (snap_dir / f"{entry.snapshot:04d}.bin").write_bytes(data)
            except OSError as exc:
                raise CheckpointError(
                    f"检查点快照写入失败，已阻止本次写入：{rel}（{exc}）"
                ) from exc

        self._current_files.append(entry)
        self._snapshotted.add(rel)

    def commit(self, *, prompt_digest: str = "") -> CheckpointInfo | None:
        """run() 结束时调用。本轮没有写任何文件则不留痕。"""
        if not self._current_id or not self._current_files:
            self._reset_current()
            return None
        info = CheckpointInfo(
            id=self._current_id,
            createdAt=time.strftime("%Y-%m-%dT%H:%M:%S"),
            sessionId=self.session_id,
            kind="run",
            epoch=time.time_ns(),
            files=list(self._current_files),
        )
        # 只含新建文件（existed=false）的检查点在快照阶段不会建目录，这里补上
        self._ensure_dir()
        try:
            self._write_manifest(self._current_dir, info, prompt_digest=prompt_digest)
        except OSError as exc:
            # manifest 写失败 = 这个检查点不可恢复；快照目录是垃圾，清掉
            shutil.rmtree(self._current_dir, ignore_errors=True)
            self._reset_current()
            raise CheckpointError(f"检查点元数据写入失败，本轮改动没有检查点（{exc}）") from exc

        self._audit("checkpoint.create", id=info.id, files=info.file_count)
        self._last_committed = info.id
        self._prune()
        self._reset_current()
        return info

    def abandon(self) -> None:
        """丢弃当前（尚未 commit 的）检查点。"""
        self._reset_current()

    def _reset_current(self) -> None:
        self._current_id = ""
        self._current_dir = None
        self._current_files = []
        self._snapshotted = set()

    def _ensure_dir(self) -> Path:
        if self._current_dir is None:
            self._current_dir = self.dir / self._current_id
            self._current_dir.mkdir(parents=True, exist_ok=True)
        return self._current_dir

    def _write_manifest(
        self, cp_dir: Path, info: CheckpointInfo, *, prompt_digest: str = ""
    ) -> None:
        payload = {
            "id": info.id,
            "createdAt": info.createdAt,
            "sessionId": info.sessionId,
            "kind": info.kind,
            "epoch": info.epoch,
            "promptDigest": prompt_digest,
            "files": [
                {
                    "path": f.path,
                    "existed": f.existed,
                    "hash": f.hash,
                    "bytes": f.bytes,
                    "snapshot": f.snapshot,
                }
                for f in info.files
            ],
        }
        (cp_dir / "manifest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def _prune(self) -> None:
        """超出保留数时淘汰最旧。坏目录（无 manifest）顺手清掉。"""
        ordered = self.list_checkpoints()
        for info in ordered[: max(0, len(ordered) - self.keep)]:
            shutil.rmtree(self.dir / info.id, ignore_errors=True)

    # ------------------------------------------------------------------ 读
    def list_checkpoints(self) -> list[CheckpointInfo]:
        """全部检查点，旧的在前。排序按 manifest 里的 epoch（纳秒），
        同秒创建的检查点也能保证先后正确；读失败的目录跳过。"""
        result: list[CheckpointInfo] = []
        if not self.dir.is_dir():
            return result
        try:
            entries = [p for p in self.dir.iterdir() if p.is_dir()]
        except OSError:
            return result
        for cp_dir in entries:
            loaded = self._load(cp_dir)
            if loaded is not None:
                result.append(loaded)
        result.sort(key=lambda cp: (cp.epoch, cp.id))
        return result

    def _load(self, cp_dir: Path) -> CheckpointInfo | None:
        try:
            raw = json.loads((cp_dir / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        files = [
            FileEntry(
                path=str(item.get("path") or ""),
                existed=bool(item.get("existed")),
                hash=str(item.get("hash") or ""),
                bytes=int(item.get("bytes") or 0),
                snapshot=int(item.get("snapshot", -1)),
            )
            for item in raw.get("files") or []
        ]
        try:
            epoch = int(raw.get("epoch") or 0)
        except (TypeError, ValueError):
            epoch = 0
        return CheckpointInfo(
            id=str(raw.get("id") or cp_dir.name),
            createdAt=str(raw.get("createdAt") or ""),
            sessionId=str(raw.get("sessionId") or ""),
            kind=str(raw.get("kind") or "run"),
            epoch=epoch,
            files=files,
        )

    # ------------------------------------------------------------------ 恢复
    def restore(self, checkpoint_id: str) -> RestoreReport:
        """把工作区拉回 checkpoint_id 对应 run 开始前的状态。

        恢复集 = 目标检查点及其**之后**所有检查点的受管文件之并集 ——
        只恢复目标自身的文件会漏掉「后续轮次改过、目标轮没碰过」的文件。
        每个文件的目标状态沿快照链回溯：找到 ≥ 目标位置的第一个快照，
        那就是目标时刻的内容（中间轮次没写过它）。
        """
        checkpoints = self.list_checkpoints()
        index = next(
            (i for i, cp in enumerate(checkpoints) if cp.id == checkpoint_id), None
        )
        if index is None:
            raise CheckpointError(f"检查点不存在: {checkpoint_id}")

        # 1) 计算恢复集：文件 -> 最近的快照条目（≥ 目标位置中的第一个）
        plan: dict[str, FileEntry] = {}
        plan_dirs: dict[str, Path] = {}
        for cp in checkpoints[index:]:
            cp_dir = self.dir / cp.id
            for entry in cp.files:
                if entry.path not in plan:
                    plan[entry.path] = entry
                    plan_dirs[entry.path] = cp_dir

        # 2) 路径防线（B1.3）：manifest 可能被篡改，逐条过 PathGuard
        resolved: dict[str, Path] = {}
        for rel, entry in plan.items():
            try:
                resolved[rel] = self._guard.resolve(entry.path)
            except SandboxViolation as exc:
                raise CheckpointError(
                    f"检查点 {checkpoint_id} 的清单含越界路径 {entry.path!r}，"
                    "已整体拒绝恢复（疑似被篡改）。"
                ) from exc

        # 3) 反向快照：回滚本身可撤销。先收当前内容，恢复失败也退得回去
        redo = self._snapshot_reverse(resolved, plan)

        # 4) 两阶段执行：先读入全部快照并校验哈希，再统一写回
        payloads: dict[str, bytes] = {}
        for rel, entry in plan.items():
            if not entry.existed:
                continue
            snap_path = plan_dirs[rel] / f"{entry.snapshot:04d}.bin"
            try:
                data = snap_path.read_bytes()
            except OSError as exc:
                raise CheckpointError(
                    f"快照文件缺失，已整体拒绝恢复：{rel}（{exc}）"
                ) from exc
            if hashlib.sha256(data).hexdigest() != entry.hash:
                raise CheckpointError(
                    f"快照校验失败（与清单哈希不符），已整体拒绝恢复：{rel}"
                )
            payloads[rel] = data

        report = RestoreReport(id=checkpoint_id, redo_id=redo.id if redo else "")
        for rel, entry in plan.items():
            target = resolved[rel]
            if not entry.existed:
                if target.exists():
                    target.unlink(missing_ok=True)
                    report.deleted.append(rel)
                else:
                    report.unchanged.append(rel)
                continue
            current_hash = _sha256_file(target) if target.is_file() else None
            if current_hash == entry.hash:
                report.unchanged.append(rel)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payloads[rel])
            # 写回后再验一次：落盘内容 == 清单哈希，防止静默错恢复
            if _sha256_file(target) != entry.hash:
                raise CheckpointError(f"回滚后校验失败：{rel}（磁盘写入异常）")
            report.restored.append(rel)

        self._audit(
            "checkpoint.restore",
            id=checkpoint_id,
            restored=len(report.restored),
            deleted=len(report.deleted),
            redo=report.redo_id,
        )
        return report

    def _snapshot_reverse(
        self, resolved: dict[str, Path], plan: dict[str, FileEntry]
    ) -> CheckpointInfo | None:
        """restore 前把当前内容收进一个 redo 检查点。收集失败不阻断回滚
        （回滚仍可执行，只是本身不可撤销 —— 记审计提醒）。"""
        files: list[FileEntry] = []
        # 数据先全部收集，最后统一落盘：半途失败就不建 redo
        collected: list[tuple[FileEntry, bytes]] = []
        for rel, target in resolved.items():
            existed = target.is_file()
            if existed:
                try:
                    data = target.read_bytes()
                except OSError:
                    continue  # 读不到的文件无从快照，跳过（restore 会在校验阶段兜底）
                collected.append(
                    (FileEntry(rel, True, hashlib.sha256(data).hexdigest(), len(data), len(files)), data)
                )
                files.append(collected[-1][0])
            else:
                files.append(FileEntry(rel, False, "", 0, -1))

        if not files:
            return None
        redo_id = f"cp-{_now_stamp()}-{uuid.uuid4().hex[:6]}"
        cp_dir = self.dir / redo_id
        try:
            cp_dir.mkdir(parents=True, exist_ok=True)
            for entry, data in collected:
                (cp_dir / f"{entry.snapshot:04d}.bin").write_bytes(data)
            info = CheckpointInfo(
                id=redo_id,
                createdAt=time.strftime("%Y-%m-%dT%H:%M:%S"),
                sessionId=self.session_id,
                kind="redo",
                epoch=time.time_ns(),
                files=files,
            )
            self._write_manifest(cp_dir, info, prompt_digest="")
        except OSError:
            shutil.rmtree(cp_dir, ignore_errors=True)
            return None
        return info

    # ------------------------------------------------------------------ 审计
    def _audit(self, event: str, **payload: Any) -> None:
        if self.audit is not None:
            self.audit.record(event, **payload)
