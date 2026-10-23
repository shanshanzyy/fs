"""测试用的假 `ssh`：只实现 `-L` 转发 + stdin EOF 退出。

为什么需要它：真实 SSH 需要一个远端主机，而 P3 要验的恰恰是**本机这一侧**
的生命周期（本地监听、就绪判定、引用计数、断线重建、父进程死亡后的自清理）。
这些全都可以在没有远端的情况下验到 —— 只要有一个「像 ssh 一样解析 `-L`、
像 ssh 一样在 stdin EOF 时退出」的替身。

两条行为必须与真 ssh 一致，否则测出来的东西没有意义：

1. **`-L` 规格由 argv 给出**，替身不额外解释主机名 —— 它把
   `bind_host:lport:rhost:rport` 原样当成本地监听 + 上游目标。
   这样 `forward_argv()` 拼错了端口顺序会立刻在测试里暴露。
2. **stdin EOF 即退出**。这正是 ADR-040 里「孤儿不可能存在」的机制，
   所以替身必须复现它，测试才能证明「父进程死了 → 监听消失」。

刻意**不**在这里实现：主机指纹、认证、通道复用、`-o` 选项语义。
替身越像真 ssh，它自己就越需要被测试。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
from pathlib import Path


def main(argv: list[str]) -> int:
    # 「第一次就报端口占用」的开关，用于验 `_launch` 的换端口重试分支。
    # 用标记文件而不是真的去抢端口：Windows 的 SO_REUSEADDR 语义允许
    # 绑定一个已被占用的端口，真抢会得到一个在不同平台上结论相反的结果。
    fail_once = os.environ.get("VIBE_FAKE_SSH_FAIL_ONCE")
    if fail_once:
        marker = Path(fail_once)
        if not marker.exists():
            marker.write_text("x", encoding="utf-8")
            print("bind: Address already in use (fake-once)", file=sys.stderr)
            return 255
    spec = ""
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "-L":
            spec = argv[index + 1] if index + 1 < len(argv) else ""
            index += 2
        elif arg in ("-p", "-o", "-i", "-J", "-l"):
            index += 2
        else:
            index += 1

    if not spec:
        print("fake-ssh: 缺少 -L 参数", file=sys.stderr)
        return 2

    parts = spec.split(":")
    if len(parts) != 4:
        print(f"fake-ssh: -L 规格非法（{spec}）", file=sys.stderr)
        return 2
    bind_host, local_text, upstream_host, remote_text = parts
    try:
        local_port, remote_port = int(local_text), int(remote_text)
    except ValueError:
        print(f"fake-ssh: 端口不是整数（{spec}）", file=sys.stderr)
        return 2

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((bind_host, local_port))
        server.listen(16)
    except OSError as exc:
        # 与真 ssh 的措辞对齐：`_launch` 靠 "already in use" 判定「换端口重试」。
        print(f"bind: Address already in use ({exc})", file=sys.stderr)
        return 255

    print(f"fake-ssh ready {bind_host}:{local_port} -> {upstream_host}:{remote_port}",
          file=sys.stderr)

    def pump(source: socket.socket, target: socket.socket) -> None:
        try:
            while True:
                chunk = source.recv(65536)
                if not chunk:
                    break
                target.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                target.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def serve() -> None:
        while True:
            try:
                client, _ = server.accept()
            except OSError:
                return
            try:
                upstream = socket.create_connection((upstream_host, remote_port), timeout=5)
            except OSError:
                client.close()
                continue
            threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
            threading.Thread(target=pump, args=(upstream, client), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()

    # 与真 ssh 对齐的关键行为：stdin 关掉就退出。
    sys.stdin.read()
    server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
