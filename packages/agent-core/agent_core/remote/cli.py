"""远程连接管理命令行。

    python -m agent_core.remote add devbox dev@10.0.0.12 --root /srv/app --key ~/.ssh/id_ed25519
    python -m agent_core.remote test devbox
    python -m agent_core.remote list
    python -m agent_core.remote set-default devbox
    python -m agent_core.remote remove devbox

为什么单独做一个模块入口，而不是塞进 `agent_core/cli.py` 的子命令：
连接管理是**会话之外的运维动作**（配机器、验密钥），与「跑一轮 Agent」
是两种使用节奏。分开之后，用户不必先进入某个工作区上下文才能改连接。
两者共用同一份 `<workspace>/.vibe/remote.json`，因此不冲突。

所有写操作都先 `validate()` 再落盘：配置类错误的成本极低，
一旦写进清单却连不上，排查时就会怀疑网络而不是怀疑自己写错了字段。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .connection import HOST_KEY_POLICIES, RemoteConnection
from .errors import RemoteConfigError, RemoteError
from .store import RemoteStore, default_store_path
from .workspace import close_all_transports, open_workspace

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_FAILED = 2


def _build_parser() -> argparse.ArgumentParser:
    # 公共参数同时挂在主解析器与每个子命令上。理由是可发现性：
    # argparse 的子解析器默认只认「写在子命令**之后**」的参数，
    # 而用户几乎一定会写成 `list --workspace .`。两边都挂上之后两种写法都成立
    #（显式值总是覆盖默认值，所以不会互相打架）。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--workspace",
        default=".",
        help="工作区目录，清单默认落在 <工作区>/.vibe/remote.json（默认当前目录）",
    )
    common.add_argument("--config", default="", help="显式指定清单文件路径（覆盖默认位置）")
    common.add_argument("--json", action="store_true", help="以 JSON 输出（便于脚本消费）")

    parser = argparse.ArgumentParser(
        prog="python -m agent_core.remote",
        description="管理远程设备连接（供 Agent 读写远端代码、在远端跑测试）。",
        parents=[common],
    )

    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="新增或更新一条连接", parents=[common])
    add.add_argument("name", help="连接名（字母/数字/下划线/点/连字符）")
    add.add_argument("target", help="目标，形如 user@host 或 host（也可用 --user 单独给）")
    add.add_argument("--user", default="", help="登录用户（target 里没写 @user 时用）")
    add.add_argument("--port", type=int, default=22, help="SSH 端口（默认 22）")
    add.add_argument("--root", default="/", help="远端工作目录，绝对路径（默认 /）")
    add.add_argument("--key", default="", help="本地私钥路径（默认交给 ssh-agent）")
    add.add_argument("--jump", default="", help="跳板机，形如 [user@]host[:port]")
    add.add_argument(
        "--host-key",
        default="accept-new",
        choices=list(HOST_KEY_POLICIES),
        help="主机指纹策略（默认 accept-new：首次记录，变化则拒绝）",
    )
    add.add_argument("--connect-timeout", type=int, default=10, help="连接超时秒数（默认 10）")
    add.add_argument("--command-timeout", type=int, default=120, help="远端命令超时秒数（默认 120）")
    add.add_argument("--allow-write", action="store_true", help="允许在该连接上写远端文件（默认只读）")
    add.add_argument("--allow-shell", action="store_true", help="允许在该连接上执行远端命令（默认禁止）")
    add.add_argument("--note", default="", help="备注")
    add.add_argument("--default", action="store_true", help="设为默认连接")
    add.add_argument("--no-probe", action="store_true", help="保存后不自动探测连通性")

    sub.add_parser("list", help="列出全部连接", parents=[common])

    show = sub.add_parser("show", help="显示一条连接的完整配置", parents=[common])
    show.add_argument("name", nargs="?", help="连接名（省略时用默认连接）")

    test = sub.add_parser("test", help="探测连通性与远端环境", parents=[common])
    test.add_argument("name", nargs="?", help="连接名（省略时用默认连接）")

    remove = sub.add_parser("remove", help="删除一条连接", parents=[common])
    remove.add_argument("name")

    setdef = sub.add_parser("set-default", help="设置默认连接", parents=[common])
    setdef.add_argument("name")

    return parser


def _store_path(args: argparse.Namespace) -> Path:
    if args.config:
        return Path(args.config).expanduser()
    return default_store_path(args.workspace)


def _load(args: argparse.Namespace) -> RemoteStore:
    store = RemoteStore.load(_store_path(args))
    # 顺手校验一遍清单里的连接：一份手动改坏的清单不应该等到「用的时候」才炸。
    for conn in store.list():
        conn.validate()
    return store


def _parse_target(target: str, user_opt: str) -> tuple[str, str]:
    """把 `user@host` / `host` 拆成 (user, host)。IPv6 的方括号不受 @ 影响。"""
    if "@" in target:
        user, _, host = target.rpartition("@")
        return user, host
    return user_opt, target


def _cmd_add(args: argparse.Namespace, store: RemoteStore) -> int:
    user, host = _parse_target(args.target, args.user)
    conn = RemoteConnection(
        name=args.name,
        host=host,
        user=user,
        port=args.port,
        root=args.root,
        identity_file=args.key,
        proxy_jump=args.jump,
        host_key_policy=args.host_key,
        connect_timeout=args.connect_timeout,
        command_timeout=args.command_timeout,
        allow_write=args.allow_write,
        allow_shell=args.allow_shell,
        note=args.note,
    )
    conn.validate_local()  # 含私钥存在性检查 —— 写到清单里才发现路径写错毫无意义
    saved = store.upsert(conn, make_default=args.default)
    print(f"已保存连接 {saved.name}: {saved.display}")
    print(f"  清单: {store.path}")
    print(f"  权限: {'可写' if saved.allow_write else '只读'} / "
          f"{'可执行命令' if saved.allow_shell else '禁止执行命令'}")
    if saved.host_key_policy == "insecure":
        print("  ⚠ 主机指纹策略为 insecure：不校验远端身份，存在中间人风险。")
    if not args.no_probe:
        print()
        return _probe_connection(store, saved.name, as_json=args.json)
    return EXIT_OK


def _cmd_list(args: argparse.Namespace, store: RemoteStore) -> int:
    if args.json:
        print(json.dumps(
            {
                "path": str(store.path),
                "default": store.default_name,
                "connections": [c.to_dict() for c in store.list()],
            },
            ensure_ascii=False,
            indent=2,
        ))
        return EXIT_OK
    if not store.connections:
        print("尚未配置任何远程连接。")
        print(f"  清单位置: {store.path}")
        return EXIT_OK
    print(f"清单: {store.path}")
    print(f"默认: {store.default_name or '（未设置）'}")
    print()
    for conn in store.list():
        caps = ("可写" if conn.allow_write else "只读") + "/" + (
            "可执行" if conn.allow_shell else "禁执行"
        )
        marker = "★" if conn.name == store.default_name else " "
        print(f"{marker} {conn.name:<16} {conn.display:<40} [{caps}]")
        if conn.note:
            print(f"  {'':<16} 备注: {conn.note}")
    return EXIT_OK


def _cmd_show(args: argparse.Namespace, store: RemoteStore) -> int:
    conn = store.resolve(args.name or "")
    print(json.dumps(conn.to_dict(), ensure_ascii=False, indent=2))
    return EXIT_OK


def _cmd_remove(args: argparse.Namespace, store: RemoteStore) -> int:
    store.remove(args.name)
    print(f"已删除连接 {args.name}")
    if not store.default_name:
        print("  提示：默认连接已清空，需要时用 set-default 重新指定。")
    return EXIT_OK


def _cmd_set_default(args: argparse.Namespace, store: RemoteStore) -> int:
    store.set_default(args.name)
    print(f"默认连接已设为 {args.name}")
    return EXIT_OK


def _cmd_test(args: argparse.Namespace, store: RemoteStore) -> int:
    return _probe_connection(store, args.name or "", as_json=args.json)


def _probe_connection(store: RemoteStore, name: str, *, as_json: bool) -> int:
    """探测并打印报告。失败时返回 EXIT_FAILED 而不是抛栈。

    这是**运维命令**，用户想看的是「哪一步不通、下一步做什么」，
    不是一段 traceback。
    """
    try:
        conn = store.resolve(name or None)
    except RemoteConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE

    try:
        workspace = open_workspace(conn)
        caps = workspace.capabilities()
    except RemoteError as exc:
        print(f"✗ 连接 {conn.name} 失败：{exc}", file=sys.stderr)
        if exc.detail:
            print(f"  远端/ssh 输出: {exc.detail.strip()[:800]}", file=sys.stderr)
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 — 探针不该因意外异常给用户一坨栈
        print(f"✗ 连接 {conn.name} 时发生未预期的错误: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return EXIT_FAILED

    if as_json:
        print(json.dumps(
            {
                "connection": conn.name,
                "display": conn.display,
                "root": conn.root_normalized,
                "writable": conn.allow_write,
                "executable": conn.allow_shell,
                "capabilities": {
                    "os": caps.os_name,
                    "arch": caps.arch,
                    "kernel": caps.kernel,
                    "user": caps.user,
                    "cwd": caps.cwd,
                    "git": caps.git_version,
                    "python": caps.python_version,
                    "multiplexed": caps.multiplexed,
                    "root_writable": caps.root_writable,
                    "degraded": caps.degraded,
                },
            },
            ensure_ascii=False,
            indent=2,
        ))
        return EXIT_OK

    print(f"✓ 连接 {conn.name} 成功")
    print(workspace.report())
    if caps.degraded:
        print("\n提示：上表中「能力降级」项不代表失败，但会限制部分功能，"
              "按提示补齐远端工具可获得完整体验。")
    return EXIT_OK


_COMMANDS = {
    "add": _cmd_add,
    "list": _cmd_list,
    "show": _cmd_show,
    "remove": _cmd_remove,
    "set-default": _cmd_set_default,
    "test": _cmd_test,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        store = _load(args)
        handler = _COMMANDS[args.command]
        return handler(args, store)
    except RemoteConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except RemoteError as exc:
        print(f"远端错误: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return EXIT_USAGE
    finally:
        # 复用连接是有状态的，退出前显式收掉，避免留下悬挂的 ssh 进程。
        close_all_transports()


if __name__ == "__main__":
    raise SystemExit(main())
