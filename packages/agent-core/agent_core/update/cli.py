"""`vibe-agent update` 子命令（M9）。

只做四件事：**看**（status）、**问**（check）、**取**（download）、**找**（rollback）。
刻意不做第五件 —— 「装」。安装器是 NSIS 静默重装，它会杀掉正在跑的本进程；
一个由本进程发起的自杀式重装没有任何办法向用户汇报结果。所以安装这件事
永远由桌面壳在「用户点了确认」之后、由壳自己去执行（见 Part C）。

照 `governance` 子命令的先例，这四条命令都**不依赖工作区**：
配置来自环境变量（`UpdateOptions.from_env()`），因此在内网任何目录下都能跑。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .. import ui
from ..errors import UpdateError
from .download import is_package_filename
from .service import UpdateOptions, UpdateService
from .version import compare, parse_version


def register_update_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "update",
        help="客户端自动更新：查看状态、检查新版本、下载安装包、定位可回退版本",
    )
    parser.add_argument(
        "action",
        choices=["status", "check", "download", "rollback"],
        help="status 看本机状态；check 问有没有新版；download 取安装包；rollback 找旧包",
    )
    parser.add_argument("--version", default="", help="download：只接受该版本（形如 1.4.2）")
    parser.add_argument("--auto", action="store_true", help="check：按启动检查开关决定是否跳过")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.set_defaults(update=True)


# --------------------------------------------------------------------------- 装配
def _governance_client() -> Any:
    """有治理面地址且本机有可用登录时，返回客户端；否则 None（只走本地镜像）。

    与 `governance/runtime.wire()` 同一口径：**没有就返回 None，绝不抛异常**。
    自动更新的 S1 通道是增强，不是依赖。
    """
    import os

    from ..governance.client import GovernanceClient
    from ..governance.credentials import CredentialStore

    server = str(os.environ.get("VIBE_SERVER_URL") or "").rstrip("/")
    if not server:
        return None
    store = CredentialStore()
    credentials = store.load()
    if credentials is None or not credentials.is_usable(server=server):
        return None
    return GovernanceClient(server, store=store, credentials=credentials)


def _service(options: UpdateOptions | None = None) -> UpdateService:
    return UpdateService(options or UpdateOptions.from_env(), client=_governance_client())


# --------------------------------------------------------------------------- 输出
def _emit(view: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(view, ensure_ascii=False, indent=2, default=str))
        return
    status = str(view.get("status") or "")
    labels = {
        "idle": "无更新",
        "available": "有新版本",
        "downloading": "下载中",
        "staged": "已就绪（待安装）",
        "applied": "已更新",
        "failed": "检查失败",
    }
    if view.get("disabled"):
        ui.warn("自动更新已禁用（VIBE_UPDATE_DISABLE=1）")
    ui.info(f"当前版本 : v{view.get('currentVersion') or '?'}（通道 {view.get('channel')}）")
    ui.info(f"状态     : {labels.get(status, status)}")
    if view.get("reason"):
        ui.info(f"说明     : {view['reason']}")
    if view.get("availableVersion"):
        ui.info(f"可用版本 : v{view['availableVersion']}")
    if view.get("version"):
        ui.info(f"目标版本 : v{view['version']}")
    if view.get("source"):
        ui.info(f"来源     : {view['source']}")
    release = view.get("release") or {}
    if release.get("notes"):
        ui.info(f"发布说明 : {release['notes']}")
    if release.get("size"):
        ui.info(f"包大小   : {int(release['size']) // 1024} KiB")
    if view.get("mandatory"):
        ui.warn("该版本为强制更新（minVersion 约束）")
    for note in view.get("notes") or []:
        ui.warn(note)
    if view.get("stagedPath"):
        ui.info(f"安装包   : {view['stagedPath']}")
    if view.get("lastError"):
        ui.error(f"上次错误 : {view['lastError']}")


# --------------------------------------------------------------------------- 动作
def _do_status(args: argparse.Namespace) -> int:
    view = _service().status()
    _emit(view, as_json=bool(args.json))
    return 0


def _do_check(args: argparse.Namespace) -> int:
    try:
        view = _service().check(auto=bool(args.auto))
    except UpdateError as exc:
        ui.error(f"检查失败：{exc}")
        return 1
    _emit(view, as_json=bool(args.json))
    return 0 if view.get("ok") else 1


def _do_download(args: argparse.Namespace) -> int:
    service = _service()
    want = str(args.version or "").strip()

    def progress(done: int, total: int) -> None:
        if args.json:
            return
        pct = f"{done * 100 // total}%" if total else f"{done} B"
        sys.stderr.write(f"\r下载中 {pct}   ")
        sys.stderr.flush()

    try:
        view = service.download(version=want, on_progress=progress)
    except UpdateError as exc:
        if not args.json:
            sys.stderr.write("\r")
        ui.error(f"下载失败（{exc.kind}）：{exc}")
        return 1
    if not args.json:
        sys.stderr.write("\r")
    _emit(view, as_json=bool(args.json))
    if not args.json:
        ui.info("安装由桌面壳在用户确认后执行；命令行不会自动安装。")
    return 0


def _version_key(text: str) -> tuple[int, ...]:
    """排序用的版本键。解析不了的一律算 `(0,)`，排在最后（不参与回退）。"""
    return parse_version(text) or (0,)


def retained_packages(updates_dir: Path) -> list[tuple[str, Path]]:
    """列出 `updates_dir` 下已 staging 的各版本安装包（新→旧）。

    `rollback` 靠它找到「上一个版本的安装器」。返回 (version, package_path)；
    版本号取自目录名，不合法/读不到的目录直接跳过。
    """
    base = Path(updates_dir).expanduser()
    if not base.is_dir():
        return []
    found: list[tuple[str, Path]] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        for pkg in child.glob("VibeAgent-Setup-*.exe"):
            if is_package_filename(pkg.name):
                found.append((child.name, pkg))
                break
    found.sort(key=lambda item: _version_key(item[0]), reverse=True)
    return found


def _do_rollback(args: argparse.Namespace) -> int:
    options = UpdateOptions.from_env()
    service = _service(options)
    current = service.current_version
    packages = retained_packages(options.updates_dir)
    older = [(v, p) for v, p in packages if compare(_version_key(v), _version_key(current)) < 0]
    if args.json:
        print(json.dumps(
            {
                "currentVersion": current,
                "staged": [{"version": v, "path": str(p)} for v, p in packages],
                "rollbackTo": str(older[0][1]) if older else "",
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0 if older else 1
    if not packages:
        ui.error(f"本机没有保留任何安装包（{options.updates_dir}）。")
        return 1
    ui.info(f"当前版本 v{current}，本机保留的安装包：")
    for version, path in packages:
        ui.info(f"  v{version}  {path}")
    if not older:
        ui.warn("没有低于当前版本的安装包，无法回退。")
        return 1
    ui.warn(f"可回退到 v{older[0][0]}：{older[0][1]}")
    ui.info("双击该安装器即可手动回到该版本；内核不执行安装。")
    return 0


def handle(args: argparse.Namespace) -> int:
    action = str(args.action)
    if action == "status":
        return _do_status(args)
    if action == "check":
        return _do_check(args)
    if action == "download":
        return _do_download(args)
    if action == "rollback":
        return _do_rollback(args)
    ui.error(f"未知操作 {action}")  # argparse 已限制 choices，理论不可达
    return 2


__all__ = ["handle", "register_update_parser", "retained_packages"]
