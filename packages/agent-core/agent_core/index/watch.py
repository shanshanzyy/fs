"""文件监听（J2 / P1-9）：纯 stdlib 轮询，把磁盘变化增量喂给索引。

为什么不引 `watchdog`：核心链路零强制依赖（计划 §2 红线）。轮询的代价只是
每 `interval` 秒 stat 一遍目录树 —— 万级文件下是毫秒级动作，而多一个第三方包
在内网离线环境里是实打实的部署成本。而且 stat 不等于读内容：真正贵的
「读 + 切块 + 建倒排」只在文件真变了时才做，且只做变了的那几个（见
`SearchIndex.update`）。

## 与其他层的边界

- 只**读**文件系统，绝不写：索引依旧不落盘（ADR-013）；
- 只更新 `SearchIndex` 的内存结构，变化以审计事件 `index.rebuild` 上报，
  **仅计数**（ADR-006）；
- 单后台线程 + 可停止事件，`stop()` 与 `atexit` 双保险回收（范式同 lsp/manager.py）——
  「退出后不留孤儿」在本项目里是有冒烟用例钉着的。
"""

from __future__ import annotations

import atexit
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..audit import AuditLog
from .lexical import OVERLAP_LINES, WINDOW_LINES
from .store import SearchIndex, get_index, release_index

# 轮询下限。比这更快就是拿 CPU 换几秒的实时性 —— 编辑代码不是抢红包，
# 而「每 2 秒 stat 一次目录树」已经是本机能做到的最便宜的做法。
MIN_INTERVAL = 2


class IndexWatcher:
    """盯住一个工作区，把文件增删改增量合进它的 `SearchIndex`。

    watcher 与索引是**一对一**的：它通过 `get_index(..., pin=True)` 拿到并钉住
    实例，保证「它更新的」与「检索读到的」是同一个对象（J6）。
    """

    def __init__(
        self,
        root: Path,
        rel_of: Callable[[Path], str] | None = None,
        interval: int = 5,
        max_files: int = 800,
        window: int = WINDOW_LINES,
        overlap: int = OVERLAP_LINES,
        enhanced: bool = False,
        index: SearchIndex | None = None,
        audit: AuditLog | None = None,
        on_change: Callable[[list[str]], None] | None = None,
    ) -> None:
        self.root = Path(root)
        self.interval = max(MIN_INTERVAL, int(interval))
        self.max_files = max(1, int(max_files))
        self.window = window
        self.overlap = overlap
        # 检索增强（M3+）：watcher 必须与 semantic_search 用**同一套**索引形状
        # 去 pin，否则两边拿到的是两个不同实例（互相看不到对方的增量）。
        self.enhanced = enhanced
        self._rel_of = rel_of or (lambda path: path.relative_to(self.root).as_posix())
        # 审计日志而不是回调：写入必须经过 AuditLog 的链锁（见 audit.py），
        # 而 watcher 跑在后台线程上 —— 自己拼一条 JSON 会把哈希链写坏。
        self._audit = audit
        # `on_change` 与上面那条纪律**不冲突**：它只收「哪些相对路径变了」，
        # 唯一用途是广播给界面（预览自动刷新），既不写链也不碰内容。
        # 回调里抛异常会被吞掉 —— 后台轮询线程不能因为一次订阅方故障而死。
        self._on_change = on_change
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._index: SearchIndex | None = index
        self._snapshot: dict[str, tuple[int, int]] = {}
        self._pending = 0
        self._last_update = 0.0
        atexit.register(self.stop)

    # ------------------------------------------------------------------ 状态
    @property
    def index(self) -> SearchIndex | None:
        return self._index

    def status(self) -> dict[str, Any]:
        """`GET /api/index/status` 的载荷（只读快照）。"""
        with self._lock:
            index, thread = self._index, self._thread
            return {
                "watching": (
                    thread is not None and thread.is_alive() and not self._stop.is_set()
                ),
                "interval": self.interval,
                "files": len(index.files) if index is not None else 0,
                "chunks": len(index.chunks) if index is not None else 0,
                "edges": index.graph.edge_count() if index is not None else 0,
                "lastUpdate": int(self._last_update),
                "pending": self._pending,
            }

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """建索引（若还没有）并起后台线程。重复调用幂等。"""
        with self._lock:
            if self._thread is not None:
                return
            if self._index is None:
                # 必须走 get_index(pin=True)：若检索已经建过这个工作区的索引，
                # 这一句拿到的就是**同一个实例**（并把它钉住），不会出现两份。
                self._index = get_index(
                    self.root,
                    self._rel_of,
                    self.max_files,
                    self.window,
                    self.overlap,
                    pin=True,
                    enhanced=self.enhanced,
                )
            self._snapshot = self._scan()
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="vibe-index-watch", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """停线程 + 解钉。幂等（`atexit` 会再调一次）。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.interval + 1.0)
        self._thread = None
        with self._lock:
            index, self._index = self._index, None
            self._pending = 0
        if index is not None:
            release_index(index)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - 后台线程绝不能因一次 IO 抖动而死
                continue

    # ------------------------------------------------------------------ 轮询
    def poll_once(self) -> dict[str, int] | None:
        """扫一遍目录并把差异喂给索引；无变化返回 None。

        对比口径是 `(mtime, size)`：足以抓住所有编辑器保存动作，又不必读内容。
        """
        current = self._scan()
        with self._lock:
            previous, self._snapshot = self._snapshot, current
            index = self._index

        added = [rel for rel in current if rel not in previous]
        removed = [rel for rel in previous if rel not in current]
        changed = [
            rel
            for rel, sig in current.items()
            if rel in previous and sig != previous[rel]
        ]
        total = len(added) + len(changed) + len(removed)
        if total == 0:
            return None

        # 先广播再喂索引：预览自动刷新只关心「哪些文件动了」，与索引是否可用无关。
        # 一次 poll 就是一个天然的合并窗口（同一轮里的多文件改动只发一条事件），
        # 因此这里不需要再叠一层节流器。
        if self._on_change is not None:
            try:
                self._on_change(added + changed + removed)
            except Exception:  # noqa: BLE001 - 订阅方故障不能杀轮询线程
                pass

        if index is None:
            return None

        started = time.time()
        with self._lock:
            self._pending = total
        stats = index.update(added=added, changed=changed, removed=removed)
        with self._lock:
            self._last_update = started
            self._pending = 0

        if self._audit is not None:
            # 只报计数：几条变、索引现在多大、花了多久。路径与内容一概不记
            #（ADR-006），因此这条事件可以直接上传，无需脱敏。
            self._audit.record(
                "index.rebuild",
                added=stats["added"],
                changed=stats["changed"],
                removed=stats["removed"],
                files_count=stats["files"],
                ms=int((time.time() - started) * 1000),
            )
        return stats

    def _scan(self) -> dict[str, tuple[int, int]]:
        """只 stat，不读内容 —— 轮询的固定开销全在这里，必须便宜。"""
        from ..tools.search import _iter_files

        snapshot: dict[str, tuple[int, int]] = {}
        for path in _iter_files(self.root, max_files=self.max_files):
            try:
                stat = path.stat()
            except OSError:
                continue
            snapshot[self._rel_of(path)] = (stat.st_mtime_ns, stat.st_size)
        return snapshot