"""极简 stdio JSON-RPC 客户端（E2）—— 用标准库跟语言服务器说话。

协议本身简单到不需要一个框架：`Content-Length: N\\r\\n\\r\\n` + JSON 正文，
请求带 `id`、通知不带。这一层只做三件事：**分帧、配对、收诊断**。

## 三条必须做对的细节

1. **按 `Content-Length` 分帧，不能按行读。** 正文里含换行是常态（文件内容、
   报错信息），按行读会把一条消息拆成好几截。这里先读头部直到空行，
   再按声明的字节数**精确**读满。
2. **后台读线程 + 事件配对。** 语言服务器会主动发通知（`publishDiagnostics`），
   把「读」和「请求」耦合在一起会死锁：你在等响应，而它先要发一条通知。
   读线程统一消费，请求侧只等自己的那个 `Event`。
3. **`shell=False` + `scrub_env()`。** 与 `run_command` / `gitinfo` 同一口径：
   剥离凭据类环境变量，argv 数组直连，不存在 shell 注入面。

进程只连本机标准输入输出，**不做任何网络连接**（ADR-014）。
"""

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..tools.shell import scrub_env


class LspError(RuntimeError):
    """语言服务器不可用、超时或返回了错误。错误信息面向开发者/日志。"""


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: str = ""


class LspClient:
    """一个语言服务器进程 + 一条 JSON-RPC 会话。非线程安全地复用于多线程？——安全。

    所有写操作走同一把写锁（避免两个请求的字节交错），
    所有状态读写在 `self._lock` 下。
    """

    def __init__(
        self,
        argv: tuple[str, ...] | list[str],
        cwd: str | Path,
        *,
        name: str = "",
        timeout: float = 20.0,
    ) -> None:
        if not argv:
            raise LspError("语言服务器 argv 为空")
        self.argv = tuple(argv)
        self.cwd = str(cwd)
        self.name = name or Path(self.argv[0]).name
        self.timeout = float(timeout)

        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._pending: dict[int, _Pending] = {}
        self._seq = 0
        self._closed = False
        self._failure = ""  # 进程死掉的原因（供上层展示）
        self._diags: dict[str, list[dict[str, Any]]] = {}
        self._versions: dict[str, int] = {}
        # 诊断「新鲜度」：`_diag_epoch` 是每个 uri 收到过几次 publish；
        # `_synced_epoch` 是最后一次同步文档时前者的取值。两者一起回答
        # 「手上这份诊断，是不是这份内容之后发的」。
        # 少了这一对，改完文件再问诊断会**立刻**拿到改动前的旧结果 —— 不报错、
        # 只是错，而且看上去完全正常。
        self._diag_epoch: dict[str, int] = {}
        self._synced_epoch: dict[str, int] = {}

    # ------------------------------------------------------------------ 生命周期
    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def failure(self) -> str:
        with self._lock:
            return self._failure

    def start(
        self,
        root: str | Path,
        *,
        init_options: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """启动进程并完成 `initialize` / `initialized` 握手。失败即抛 `LspError`。"""
        if self._proc is not None:
            raise LspError("客户端已经启动")
        try:
            self._proc = subprocess.Popen(  # noqa: S603 — argv 数组 + shell=False
                list(self.argv),
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=scrub_env(),
                shell=False,
                bufsize=0,
            )
        except FileNotFoundError as exc:
            raise LspError(f"语言服务器无法启动（文件不存在）：{self.argv[0]}") from exc
        except OSError as exc:
            raise LspError(f"语言服务器启动失败：{exc}") from exc

        self._reader = threading.Thread(
            target=self._read_loop, name=f"lsp-{self.name}", daemon=True
        )
        self._reader.start()

        root_path = Path(root).resolve()
        params: dict[str, Any] = {
            "processId": None,  # None = 我们不做进程级联回收，退出时自己收尾
            "clientInfo": {"name": "vibe-agent-core"},
            "rootUri": root_path.as_uri(),
            "workspaceFolders": [
                {"uri": root_path.as_uri(), "name": root_path.name}
            ],
            "capabilities": {
                "textDocument": {
                    "synchronization": {"dynamicRegistration": False},
                    "publishDiagnostics": {"relatedInformation": False},
                    "definition": {"linkSupport": False},
                    "rename": {"prepareSupport": False},
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": False},
                },
                "workspace": {"workspaceFolders": True, "configuration": False},
            },
        }
        if init_options:
            params["initializationOptions"] = init_options

        try:
            result = self.request("initialize", params)
        except LspError:
            self.stop()
            raise
        self.notify("initialized", {})
        if settings:
            # 部分服务器（pylsp / 老版 pyright）靠 didChangeConfiguration 拿配置；
            # 不支持的话它只会忽略这条通知，不影响主流程。
            self.notify("workspace/didChangeConfiguration", {"settings": settings})
        return result if isinstance(result, dict) else {}

    def stop(self) -> None:
        """尽力优雅收尾：`shutdown` → `exit` → 必要时强杀。幂等。"""
        proc = self._proc
        if proc is None:
            with self._lock:
                self._closed = True
            return
        try:
            if proc.poll() is None:
                try:
                    self.request("shutdown", None, timeout=3.0)
                    self.notify("exit", None)
                except LspError:
                    pass
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        pass
        finally:
            for stream in (proc.stdin, proc.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            with self._lock:
                self._closed = True
                self._proc = None
                # 唤醒所有等待者，避免它们卡到超时
                for pending in self._pending.values():
                    pending.error = pending.error or "语言服务器已关闭"
                    pending.event.set()
                self._pending.clear()

    # ------------------------------------------------------------------ 收发
    def request(self, method: str, params: Any, *, timeout: float | None = None) -> Any:
        with self._write_lock:
            with self._lock:
                if self._closed or self._proc is None:
                    raise LspError(self._failure or "语言服务器未启动或已关闭")
                self._seq += 1
                request_id = self._seq
                pending = _Pending()
                self._pending[request_id] = pending
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})

        wait = self.timeout if timeout is None else float(timeout)
        if not pending.event.wait(timeout=wait):
            with self._lock:
                self._pending.pop(request_id, None)
            raise LspError(f"{self.name} 在 {wait:.0f} 秒内没有响应 {method}")
        with self._lock:
            self._pending.pop(request_id, None)
            error = pending.error
            result = pending.result
        if error:
            raise LspError(f"{self.name} 处理 {method} 失败：{error}")
        return result

    def notify(self, method: str, params: Any) -> None:
        with self._write_lock:
            with self._lock:
                if self._closed or self._proc is None:
                    raise LspError(self._failure or "语言服务器未启动或已关闭")
            self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise LspError("语言服务器标准输入不可写")
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        try:
            proc.stdin.write(frame)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self._die(f"向 {self.name} 写入失败：{exc}")
            raise LspError(f"向 {self.name} 写入失败：{exc}") from exc

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stream = proc.stdout
        try:
            while True:
                length = self._read_headers(stream)
                if length is None:
                    break
                if length <= 0:
                    continue
                body = self._read_exactly(stream, length)
                if body is None:
                    break
                try:
                    message = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue  # 畸形帧：跳过，但不因此杀掉整个会话
                self._dispatch(message)
        except OSError:
            pass
        finally:
            self._die("语言服务器进程已退出")

    @staticmethod
    def _read_headers(stream: Any) -> int | None:
        """读头部直到空行，返回 `Content-Length`；流结束返回 None。"""
        length = -1
        while True:
            line = stream.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                break
            key, _, value = line.decode("ascii", "replace").partition(":")
            if key.strip().lower() == "content-length":
                try:
                    length = int(value.strip())
                except ValueError:
                    length = -1
        return length

    @staticmethod
    def _read_exactly(stream: Any, size: int) -> bytes | None:
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = stream.read(remaining)
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _dispatch(self, message: dict[str, Any]) -> None:
        if not isinstance(message, dict):
            return
        if "id" in message and ("result" in message or "error" in message):
            try:
                request_id = int(message["id"])
            except (TypeError, ValueError):
                return
            with self._lock:
                pending = self._pending.get(request_id)
                if pending is not None:
                    if message.get("error"):
                        err = message["error"]
                        pending.error = str(err.get("message") or err) if isinstance(err, dict) else str(err)
                    else:
                        pending.result = message.get("result")
                    pending.event.set()
            return

        method = message.get("method")
        if method == "textDocument/publishDiagnostics":
            params = message.get("params") or {}
            uri = str(params.get("uri") or "")
            items = params.get("diagnostics") or []
            if uri:
                with self._cond:
                    self._diags[uri] = list(items)
                    self._diag_epoch[uri] = self._diag_epoch.get(uri, 0) + 1
                    self._cond.notify_all()
        elif method == "workspace/configuration":
            # 不实现配置协商：回一组空对象，让服务器继续走默认值。
            request_id = message.get("id")
            if request_id is not None:
                count = len((message.get("params") or {}).get("items") or [])
                try:
                    with self._write_lock:
                        self._send(
                            {"jsonrpc": "2.0", "id": request_id, "result": [{} for _ in range(count)]}
                        )
                except LspError:
                    pass
        # 其它通知（window/logMessage 等）忽略：日志不该灌进模型上下文

    def _die(self, reason: str) -> None:
        with self._cond:
            self._closed = True
            self._failure = reason
            for pending in self._pending.values():
                pending.error = pending.error or reason
                pending.event.set()
            self._pending.clear()
            self._cond.notify_all()

    # ------------------------------------------------------------------ 文档
    def open_document(self, uri: str, language_id: str, text: str) -> None:
        with self._lock:
            version = self._versions.get(uri, 0) + 1
            self._versions[uri] = version
            # 必须在发出通知**之前**记下基线：发完才记的话，服务器回得太快时
            # 基线会盖住刚到的 publish，wait 就永远等不到「更新的一份」。
            self._synced_epoch[uri] = self._diag_epoch.get(uri, 0)
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": version,
                    "text": text,
                }
            },
        )

    def change_document(self, uri: str, text: str) -> None:
        with self._lock:
            version = self._versions.get(uri, 0) + 1
            self._versions[uri] = version
            self._synced_epoch[uri] = self._diag_epoch.get(uri, 0)
        self.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            },
        )

    def close_document(self, uri: str) -> None:
        try:
            self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        except LspError:
            pass

    # ------------------------------------------------------------------ 诊断
    def diagnostics_for(self, uri: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._diags.get(uri) or [])

    def wait_diagnostics(self, uri: str, *, timeout: float = 8.0) -> list[dict[str, Any]]:
        """等**与当前内容对应**的那一份诊断。

        「等第一份」是不够的：改完文件再问诊断时，缓存里躺着的是改动前的旧结果，
        直接返回它不会报错，只会让用户看着一份过期的诊断改代码。因此这里等的是
        「比最后一次同步文档**更新**的一份 publish」；若服务器压根不再推
        （没实现 didChange 的动态诊断），超时后返回已有的结果，不抛 ——
        「暂时没有诊断」与「服务器坏了」是两回事，前者不该打断用户。
        """
        deadline = self.timeout if timeout is None else float(timeout)
        with self._cond:
            baseline = self._synced_epoch.get(uri, 0)
            self._cond.wait_for(
                lambda: self._diag_epoch.get(uri, 0) > baseline or self._closed,
                timeout=deadline,
            )
            return list(self._diags.get(uri) or [])


__all__ = ["LspClient", "LspError"]