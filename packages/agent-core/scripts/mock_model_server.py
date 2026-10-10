"""脚本化假模型服务 —— 不装 Ollama 也能立刻看到完整闭环。

它实现了最小的 OpenAI 兼容接口（/v1/models 与 /v1/chat/completions），
但不会真的推理，而是按预设剧本依次吐出工具调用。用途只有一个：
在没有模型可用的环境里验证「Agent 循环 + 工具 + 补丁 + 审批」这条链路是通的。

用法：
    # 终端 1：启动假模型（只监听 127.0.0.1）
    python scripts/mock_model_server.py --port 11500

    # 终端 2：让 vibe-agent 指向它
    vibe-agent -w ../../examples/demo-project \\
        --base-url http://127.0.0.1:11500/v1 --model mock \\
        -p "修复 average 遇到空列表崩溃的问题"

默认剧本针对 examples/demo-project 设计。也可以把自己的剧本写成 JSON 传进来：
    [{"tool": "read_file", "args": {"path": "a.py"}},
     {"tool": "edit_file", "args": {"path": "a.py", "search": "x", "replace": "y"}},
     {"text": "已修复。"}]

需要断言「网关真的把密钥带给了上游」时，加 `--record-headers` 把每次请求的
Authorization 头按 JSONL 记下来（治理面冒烟靠它证明密钥托管生效）：
    python scripts/mock_model_server.py --port 11500 --record-headers headers.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEFAULT_SCRIPT: list[dict[str, Any]] = [
    {"tool": "repo_map", "args": {"path": "."}},
    {"tool": "read_file", "args": {"path": "src/utils.py"}},
    {
        "tool": "edit_file",
        "args": {
            "path": "src/utils.py",
            "search": (
                "def average(values):\n"
                "    # 问题 2：空列表会 ZeroDivisionError\n"
                "    return sum(values) / len(values)"
            ),
            "replace": (
                "def average(values):\n"
                "    if not values:\n"
                "        return 0\n"
                "    return sum(values) / len(values)"
            ),
        },
    },
    {
        "text": (
            "已修复 src/utils.py 中的 average：空列表时返回 0，不再抛 ZeroDivisionError。\n"
            "现在 tests/test_utils.py 里那条失败用例应该能通过了。"
        )
    },
]


class Handler(BaseHTTPRequestHandler):
    server_version = "vibe-mock/0.1"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ 基础设施
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return  # 关掉默认的访问日志，改用我们自己的摘要输出

    def _record_header(self) -> None:
        """把本次请求的 Authorization 头追加到 JSONL（仅在 --record-headers 时）。

        用途只有一个：让治理面冒烟能断言「网关真的把**从密钥文件读到的**值
        带给了上游」，而不是只看它没报错。默认关闭，对其他使用方零影响。
        """
        path = getattr(self.server, "record_headers", None)
        if not path:
            return
        line = json.dumps(
            {"path": self.path, "authorization": self.headers.get("Authorization") or ""},
            ensure_ascii=False,
        )
        lock = self.server.record_lock  # type: ignore[attr-defined]
        try:
            with lock, open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/v1/models"):
            self._send(
                {
                    "object": "list",
                    "data": [
                        {"id": "mock-agent-model", "object": "model", "owned_by": "local"}
                    ],
                }
            )
            return
        self._send({"error": {"message": "not found"}}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.rstrip("/").endswith("/v1/chat/completions"):
            self._send({"error": {"message": "not found"}}, status=404)
            return
        self._record_header()

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            request = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send({"error": {"message": "invalid json"}}, status=400)
            return

        turn = self.server.turn  # type: ignore[attr-defined]
        script = self.server.script  # type: ignore[attr-defined]
        self.server.turn = turn + 1  # type: ignore[attr-defined]

        step = script[turn] if turn < len(script) else script[-1]
        self._trace(turn, request)

        if request.get("stream"):
            # A1：SSE 流式剧本。工具调用也会被拆成分片（arguments 分 3 段吐出），
            # 用于验证客户端的 delta 聚合逻辑，包括跨片 JSON。
            self._send_stream(turn, request, step)
            return

        if "tool" in step:
            name = step["tool"]
            arguments = step.get("args") or {}
            print(f"  -> 返回工具调用 {name}({_brief(arguments)})")
            self._send(
                {
                    "id": f"chatcmpl-mock-{turn}",
                    "object": "chat.completion",
                    "model": request.get("model", "mock"),
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": f"call_mock_{turn}",
                                        "type": "function",
                                        "function": {
                                            "name": name,
                                            "arguments": json.dumps(arguments, ensure_ascii=False),
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 800, "completion_tokens": 60, "total_tokens": 860},
                }
            )
            return

        text = step.get("text", "（剧本结束）")
        print(f"  -> 返回最终答复（{len(text)} 字）")
        self._send(
            {
                "id": f"chatcmpl-mock-{turn}",
                "object": "chat.completion",
                "model": request.get("model", "mock"),
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": text},
                    }
                ],
                "usage": {"prompt_tokens": 900, "completion_tokens": 80, "total_tokens": 980},
            }
        )

    # ------------------------------------------------------------ 流式输出
    def _sse(self, chunk: dict) -> None:
        self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _send_stream(self, turn: int, request: dict, step: dict) -> None:
        """按 SSE 分帧吐出剧本步骤。与一次性路径共用同一份剧本。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True  # 靠 EOF 分帧，客户端读到断开即结束

        model = request.get("model", "mock")
        chunk_id = f"chatcmpl-mock-{turn}"

        def frame(delta: dict, finish: str | None = None, usage: dict | None = None) -> dict:
            return {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                **({"usage": usage} if usage else {}),
            }

        try:
            if "tool" in step:
                name = step["tool"]
                arguments = json.dumps(step.get("args") or {}, ensure_ascii=False)
                print(f"  -> 流式返回工具调用 {name}({_brief(step.get('args') or {})})")
                # 第一帧：工具名 + 空 arguments（真实推理端的常见形态）
                self._sse(
                    frame(
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": f"call_mock_{turn}",
                                    "type": "function",
                                    "function": {"name": name, "arguments": ""},
                                }
                            ],
                        }
                    )
                )
                # arguments 拆成 3 个分片，验证客户端的跨片 JSON 聚合
                third = max(1, len(arguments) // 3)
                for i in range(0, len(arguments), third):
                    time.sleep(0.02)
                    self._sse(
                        frame(
                            {
                                "tool_calls": [
                                    {"index": 0, "function": {"arguments": arguments[i : i + third]}}
                                ]
                            }
                        )
                    )
                time.sleep(0.02)
                self._sse(frame({}, finish="tool_calls"))
                self._sse(
                    frame({}, usage={"prompt_tokens": 800, "completion_tokens": 60, "total_tokens": 860})
                )
            else:
                text = step.get("text", "（剧本结束）")
                print(f"  -> 流式返回最终答复（{len(text)} 字）")
                self._sse(frame({"role": "assistant", "content": ""}))
                size = 8
                for i in range(0, len(text), size):
                    time.sleep(0.02)
                    self._sse(frame({"content": text[i : i + size]}))
                self._sse(frame({}, finish="stop"))
                self._sse(
                    frame({}, usage={"prompt_tokens": 900, "completion_tokens": 80, "total_tokens": 980})
                )
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端提前断开

    # ------------------------------------------------------------ 输出
    def _trace(self, turn: int, request: dict) -> None:
        messages = request.get("messages") or []
        last = messages[-1] if messages else {}
        role = last.get("role", "?")
        content = " ".join(str(last.get("content") or "").split())
        tool_count = sum(1 for m in messages if m.get("role") == "tool")
        print(
            f"[第 {turn + 1} 次请求] 消息数={len(messages)} 工具结果数={tool_count} "
            f"末条={role}: {content[:80]}"
        )


def _brief(arguments: dict) -> str:
    parts = []
    for key in ("path", "command", "pattern"):
        if key in arguments:
            parts.append(f"{key}={str(arguments[key])[:40]}")
    return ", ".join(parts) or "..."


def load_script(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return DEFAULT_SCRIPT
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise SystemExit("剧本文件必须是一个非空 JSON 数组")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="脚本化假模型服务（仅用于本地验证链路）")
    parser.add_argument("--port", type=int, default=11500, help="监听端口，默认 11500")
    parser.add_argument("--script", help="剧本 JSON 文件路径")
    parser.add_argument(
        "--record-headers",
        help="把每次 /v1/chat/completions 的 Authorization 头追加到该文件（JSONL），供冒烟断言",
    )
    args = parser.parse_args(argv)

    script = load_script(args.script)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    server.turn = 0  # type: ignore[attr-defined]
    server.script = script  # type: ignore[attr-defined]
    server.record_headers = args.record_headers  # type: ignore[attr-defined]
    server.record_lock = threading.Lock()  # type: ignore[attr-defined]

    print("脚本化假模型服务已启动（仅供链路验证，不做真实推理）")
    print(f"  地址   http://127.0.0.1:{args.port}/v1")
    print(f"  模型   mock-agent-model")
    print(f"  剧本   {len(script)} 步" + (f"（来自 {args.script}）" if args.script else "（内置 demo 剧本）"))
    print()
    print("在另一个终端运行：")
    print(f"  vibe-agent --base-url http://127.0.0.1:{args.port}/v1 --model mock-agent-model \\")
    print("      -w <工作区> -p \"你的任务\"")
    print()
    print("Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
