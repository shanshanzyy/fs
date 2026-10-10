"""MCP 客户端（L1）：用标准库跟外部 MCP server 说 JSON-RPC。

## 为什么手写而不是用官方 SDK

`pyproject.toml` 的 `dependencies` 必须是 `[]`（核心链路零强制依赖）——
与 tree-sitter、语言服务器同一范式。协议本身简单到不需要框架：
`initialize` → `notifications/initialized` → `tools/list` → `tools/call`。

## 两条分帧规则必须同时支持

MCP 的 stdio 传输规范用 **NDJSON**（每条消息一行、正文里不许有裸换行），
但社区里大量 server 照抄 LSP 的 `Content-Length: N\\r\\n\\r\\n` + 正文。
按行读会把后者的正文拆碎，按头读又会把前者当成畸形帧。
这里先读一行判形态：`Content-Length:` 开头走头分帧，否则整行即一条 JSON。

## 与语言服务器（lsp/client.py）的同一口径

`shell=False` + argv 数组（**只来自受信配置文件**，不接受运行时用户输入拼命令）
+ `scrub_env()` + 启动/调用超时 + 后台读线程与请求配对。
不同点：MCP 的 server 会发**服务端→客户端请求**（`sampling/createMessage` 等），
本轮不实现这些能力，一律回 `Method not found` 的错误对象，让 server 自己降级。
"""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..tools.shell import scrub_env

# 初始化时上报给 server 的协议版本。取一个被广泛实现的稳定版本即可。
PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "vibe-agent-core"


class McpError(RuntimeError):
    """server 起不来、超时、或返回了错误对象。信息面向开发者/日志。"""


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: str = ""


class _RpcCore:
    """请求-响应配对与关闭语义。stdio / http 两种传输共用这一层。"""

    def __init__(self, *, name: str, timeout: float) -> None:
        self.name = name
        self.timeout = float(timeout)
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._seq = 0
        self._closed = False
        self._failure = ""

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def failure(self) -> str:
        with self._lock:
            return self._failure

    def _raise_if_closed(self) -> None:
        with self._lock:
            if self._closed:
                raise McpError(self._failure or f"{self.name} 已关闭")

    def _next_request(self) -> tuple[int, _Pending]:
        with self._lock:
            self._seq += 1
            pending = _Pending()
            self._pending[self._seq] = pending
            return self._seq, pending

    def _resolve(self, request_id: int, *, result: Any = None, error: str = "") -> None:
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                return
            if error:
                pending.error = error
            else:
                pending.result = result
            pending.event.set()

    def _die(self, reason: str) -> None:
        with self._lock:
            self._closed = True
            self._failure = reason
            for pending in self._pending.values():
                pending.error = pending.error or reason
                pending.event.set()
            self._pending.clear()

    def _wait(self, request_id: int, pending: _Pending, wait: float, method: str) -> Any:
        if not pending.event.wait(timeout=wait):
            with self._lock:
                self._pending.pop(request_id, None)
            raise McpError(f"{self.name} 在 {wait:.0f} 秒内没有响应 {method}")
        with self._lock:
            self._pending.pop(request_id, None)
            error = pending.error
            result = pending.result
        if error:
            raise McpError(f"{self.name} 处理 {method} 失败：{error}")
        return result


class McpClient(_RpcCore):
    """一个 MCP server 进程 + 一条 stdio JSON-RPC 会话（线程安全）。"""

    def __init__(
        self,
        argv: tuple[str, ...] | list[str],
        cwd: str | Path,
        *,
        name: str = "",
        timeout: float = 30.0,
        env: dict[str, str] | None = None,
    ) -> None:
        if not argv:
            raise McpError("MCP server argv 为空")
        self.argv = tuple(argv)
        self.cwd = str(cwd)
        super().__init__(name=name or Path(self.argv[0]).name, timeout=timeout)
        # 配置里的 env 只做**追加**，底座仍是 scrub_env()（凭据一律不继承）。
        self._extra_env = dict(env or {})
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> dict[str, Any]:
        """启动进程并完成 `initialize` / `initialized` 握手。失败即抛 McpError。"""
        if self._proc is not None:
            raise McpError("客户端已经启动")
        env = scrub_env()
        env.update(self._extra_env)
        try:
            self._proc = subprocess.Popen(  # noqa: S603 — argv 数组 + shell=False
                list(self.argv),
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                shell=False,
                bufsize=0,
            )
        except FileNotFoundError as exc:
            raise McpError(f"MCP server 无法启动（文件不存在）：{self.argv[0]}") from exc
        except OSError as exc:
            raise McpError(f"MCP server 启动失败：{exc}") from exc

        self._reader = threading.Thread(
            target=self._read_loop, name=f"mcp-{self.name}", daemon=True
        )
        self._reader.start()

        params = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": "1.0"},
        }
        try:
            result = self.request("initialize", params)
        except McpError:
            self.stop()
            raise
        self.notify("notifications/initialized", {})
        return result if isinstance(result, dict) else {}

    def stop(self) -> None:
        """尽力优雅收尾，幂等。MCP 没有 LSP 那样的 shutdown/exit 握手，直接关管道。"""
        proc = self._proc
        if proc is None:
            self._die("MCP server 已关闭")
            return
        try:
            for stream in (proc.stdin, proc.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
        finally:
            self._proc = None
            self._die("MCP server 已关闭")

    # ------------------------------------------------------------------ 收发
    def request(self, method: str, params: Any, *, timeout: float | None = None) -> Any:
        with self._write_lock:
            self._raise_if_closed()
            request_id, pending = self._next_request()
            self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
        wait = self.timeout if timeout is None else float(timeout)
        return self._wait(request_id, pending, wait, method)

    def notify(self, method: str, params: Any) -> None:
        with self._write_lock:
            self._raise_if_closed()
            self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise McpError(f"{self.name} 标准输入不可写")
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        # 写 NDJSON（一行一条）。这是 MCP stdio 规范的分帧，也最省事。
        try:
            proc.stdin.write(body + b"\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self._die(f"向 {self.name} 写入失败：{exc}")
            raise McpError(f"向 {self.name} 写入失败：{exc}") from exc

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stream = proc.stdout
        try:
            while True:
                line = stream.readline()
                if not line:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.lower().startswith(b"content-length:"):
                    body = self._read_framed(stream, stripped)
                else:
                    body = stripped
                if body is None:
                    break
                try:
                    message = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue  # 畸形帧：跳过，不因此杀掉整个会话
                self._dispatch(message)
        except OSError:
            pass
        finally:
            self._die("MCP server 进程已退出")

    def _read_framed(self, stream: Any, header: bytes) -> bytes | None:
        """头分帧：解析 `Content-Length`，吃掉剩余头部，再精确读满正文。"""
        length = -1
        try:
            length = int(header.decode("ascii", "replace").partition(":")[2].strip())
        except ValueError:
            length = -1
        while True:
            line = stream.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                break
        if length <= 0:
            return b""
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            chunk = stream.read(remaining)
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        if "id" in message and ("result" in message or "error" in message):
            try:
                request_id = int(message["id"])
            except (TypeError, ValueError):
                return
            error = message.get("error")
            if error:
                text = (
                    str(error.get("message") or error)
                    if isinstance(error, dict)
                    else str(error)
                )
                self._resolve(request_id, error=text)
            else:
                self._resolve(request_id, result=message.get("result"))
            return

        # server → client 的请求：本轮不实现 sampling / roots / elicitation，
        # 回一个明确的错误，让 server 走降级路径而不是干等超时。
        if message.get("method") and message.get("id") is not None:
            try:
                with self._write_lock:
                    self._send(
                        {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "error": {"code": -32601, "message": "Method not found"},
                        }
                    )
            except McpError:
                pass
        # 其它通知（notifications/*）忽略：server 日志不该灌进模型上下文。

    # ------------------------------------------------------------------ 能力
    def list_tools(self) -> list[dict[str, Any]]:
        result = self.request("tools/list", {})
        tools = (result or {}).get("tools") if isinstance(result, dict) else None
        return [item for item in (tools or []) if isinstance(item, dict)]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        return result if isinstance(result, dict) else {}


class McpHttpClient(_RpcCore):
    """可选的 HTTP 传输（L1 的可选部分）：POST JSON-RPC，读 JSON 或 SSE 响应。

    只覆盖「一问一答」的形态：每次请求一个 POST，响应体要么是 JSON，
    要么是若干 `data:` 行（SSE）。不维护长连接 —— 对 tools/list/``tools/call``
    足够，也是标准的 streamable-HTTP 退化形态。
    """

    def __init__(self, url: str, *, name: str = "", timeout: float = 30.0) -> None:
        if not url:
            raise McpError("MCP http 传输缺少 url")
        self.url = url
        super().__init__(name=name or url, timeout=timeout)
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        # 本机地址绕过代理：代理会把 127.0.0.1 的请求转发出去，直接连不上。
        handler_cls = (
            urllib.request.ProxyHandler({})
            if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")
            else urllib.request.ProxyHandler()
        )
        self._opener = urllib.request.build_opener(handler_cls)

    def start(self) -> dict[str, Any]:
        params = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": "1.0"},
        }
        result = self.request("initialize", params)
        self.notify("notifications/initialized", {})
        return result if isinstance(result, dict) else {}

    def stop(self) -> None:
        self._die("MCP http 会话已关闭")

    def request(self, method: str, params: Any, *, timeout: float | None = None) -> Any:
        self._raise_if_closed()
        request_id, _ = self._next_request()
        wait = self.timeout if timeout is None else float(timeout)
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        response = self._post(message, wait)
        with self._lock:
            self._pending.pop(request_id, None)
        if not isinstance(response, dict):
            raise McpError(f"{self.name} 返回了非对象响应：{method}")
        error = response.get("error")
        if error:
            text = (
                str(error.get("message") or error)
                if isinstance(error, dict)
                else str(error)
            )
            raise McpError(f"{self.name} 处理 {method} 失败：{text}")
        return response.get("result")

    def notify(self, method: str, params: Any) -> None:
        # 通知无响应体，失败不抛（与 stdio 的 fire-and-forget 语义一致）。
        self._post({"jsonrpc": "2.0", "method": method, "params": params}, self.timeout, expect=False)

    def _post(self, message: dict[str, Any], timeout: float, *, expect: bool = True) -> Any:
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                content_type = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raise McpError(f"{self.name} HTTP {exc.code}: {exc.reason}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise McpError(f"{self.name} 请求失败：{exc}") from exc
        text = raw.decode("utf-8", errors="replace")
        if "text/event-stream" in content_type:
            return _parse_sse(text)
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return _parse_sse(text)


def _parse_sse(text: str) -> Any:
    """从 SSE 文本里抠出最后一条 `data:` 的 JSON（一问一答只有一个响应）。"""
    last: Any = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload:
            continue
        try:
            last = json.loads(payload)
        except json.JSONDecodeError:
            continue
    return last


__all__ = ["CLIENT_NAME", "PROTOCOL_VERSION", "McpClient", "McpError", "McpHttpClient"]