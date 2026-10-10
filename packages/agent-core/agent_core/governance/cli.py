"""`vibe-agent governance` 子命令（M3）。

登录 / 注册 / 状态 / 登出 / 设备 / 审计上传。凭证落在用户级
`~/.vibe/credentials.json`（见 credentials.py），CLI 与桌面端共用 ——
所以「CLI 登录一次，桌面端自动接入」是设计出来的行为，不是巧合。

所有命令都要求显式的服务端地址：`--server` 参数或 `VIBE_SERVER_URL`
环境变量。治理面地址是部署方分发的配置，不给默认值 ——
默认值会成为「悄悄连上错误的内网机器」这种事故的温床。
"""

from __future__ import annotations

import argparse
import getpass
from typing import Any

from .. import ui
from ..config import Config
from .client import GovernanceClient, GovernanceError
from .credentials import CredentialStore
from .runtime import upload_audit_now, wire


def register_governance_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "governance",
        help="治理面接入：登录、注册、状态、登出、设备、审计上传",
    )
    parser.add_argument(
        "action",
        choices=["login", "register", "status", "logout", "devices", "upload"],
    )
    parser.add_argument(
        "--server", default="",
        help="治理面地址（如 http://governance.corp:8787）；默认读 VIBE_SERVER_URL",
    )
    parser.add_argument("--email", default="")
    parser.add_argument("--password", default="", help="不传则交互式输入（不回显）")
    parser.add_argument("--display-name", default="")
    parser.add_argument("--enrollment-token", default="", help="register 必填")
    parser.add_argument("--all-devices", action="store_true", help="logout：注销所有设备")
    parser.set_defaults(governance=True)


def _resolve_server(args: argparse.Namespace) -> str:
    import os

    return str(args.server or os.environ.get("VIBE_SERVER_URL") or "").rstrip("/")


def _ask_password(args: argparse.Namespace) -> str:
    if args.password:
        return str(args.password)
    return getpass.getpass("口令（输入不回显）: ")


def handle(args: argparse.Namespace) -> int:
    action = str(args.action)
    server = _resolve_server(args)

    if action == "status":
        return _status(args, server)

    if not server:
        ui.error("未指定治理面地址。用 --server 或设置 VIBE_SERVER_URL。")
        return 2

    if action == "login":
        return _login(args, server)
    if action == "register":
        return _register(args, server)
    if action == "logout":
        return _logout(server, all_devices=bool(args.all_devices))
    if action == "devices":
        return _devices(server)
    if action == "upload":
        return _upload(args)
    ui.error(f"未知操作 {action}")  # argparse 已限制 choices，理论不可达
    return 2


# --------------------------------------------------------------------------- 动作
def _login(args: argparse.Namespace, server: str) -> int:
    email = str(args.email or "").strip()
    if not email:
        email = input("邮箱: ").strip()
    if not email:
        ui.error("邮箱不能为空。")
        return 2
    password = _ask_password(args)
    client = GovernanceClient(server)
    try:
        credentials = client.login(email=email, password=password)
    except GovernanceError as exc:
        ui.error(f"登录失败：{exc}")
        return 1
    ui.ok(f"已登录 {server}（{credentials.email}，设备 {credentials.device_id[:12]}…）")
    ui.info("凭证已保存到用户级目录；CLI 与桌面端共用。")
    return 0


def _register(args: argparse.Namespace, server: str) -> int:
    token = str(args.enrollment_token or "").strip()
    email = str(args.email or "").strip()
    if not token or not email:
        ui.error("register 需要 --enrollment-token 与 --email。")
        return 2
    password = _ask_password(args)
    client = GovernanceClient(server)
    try:
        credentials = client.register(
            enrollment_token=token, email=email, password=password,
            display_name=str(args.display_name or ""),
        )
    except GovernanceError as exc:
        ui.error(f"注册失败：{exc}")
        return 1
    ui.ok(f"已注册并登录 {server}（{credentials.email}）")
    return 0


def _status(args: argparse.Namespace, server: str) -> int:
    """status 走完整的 wire()：看到的就是「下次启动 Agent 会发生什么」。"""
    try:
        config = Config.load(".")
    except Exception as exc:  # noqa: BLE001 —— status 应该在哪都能跑
        ui.error(f"无法加载工作区配置（在项目目录里执行）：{exc}")
        return 2
    runtime = wire(config)
    view = runtime.describe()
    status_labels = {
        "off": "未接入（server_url 未配置，纯本地模式）",
        "unauthenticated": "已配置但未登录",
        "degraded": "已降级（本地默认策略）",
        "governed": "已接入",
    }
    ui.info(f"治理面：{status_labels.get(view['status'], view['status'])}")
    if view.get("degradedReason"):
        ui.warn(view["degradedReason"])
    if view.get("credentials"):
        cred = view["credentials"]
        ui.info(f"  身份   {cred['email']}（设备 {str(cred['deviceId'])[:12]}…）")
        ui.info(f"  令牌   {'有效' if not cred['expired'] else '已过期（可自动刷新）'}")
    constraints = view.get("constraints") or {}
    ui.info(f"  云端   {'允许' if constraints.get('allowCloud') else '禁止'}"
            f"（策略版本 {constraints.get('policyVersion') or '默认'}）")
    for name in ("deniedPaths", "deniedCommands", "requireApproval"):
        items = constraints.get(name) or []
        if items:
            ui.info(f"  {name}: {', '.join(str(i) for i in items[:8])}"
                    + ("…" if len(items) > 8 else ""))
    for note in view.get("notices") or []:
        ui.warn(note)
    return 0


def _logout(server: str, *, all_devices: bool) -> int:
    client = GovernanceClient(server)
    client.logout(all_devices=all_devices)
    ui.ok("已登出，本地凭证已清除。")
    return 0


def _devices(server: str) -> int:
    client = GovernanceClient(server)
    try:
        devices = client.list_devices()
    except GovernanceError as exc:
        ui.error(f"获取设备列表失败：{exc}")
        return 1
    if not devices:
        ui.info("没有已注册设备。")
        return 0
    for device in devices:
        current = "（本机）" if device.get("current") else ""
        ui.info(f"  {device.get('deviceId', '')[:12]}…  {device.get('name', '')} "
                f"{device.get('platform', '')} {current}")
    return 0


def _upload(args: argparse.Namespace) -> int:
    try:
        config = Config.load(".")
    except Exception as exc:  # noqa: BLE001
        ui.error(f"无法加载工作区配置（在项目目录里执行）：{exc}")
        return 2
    runtime = wire(config)
    if runtime.uploader is None:
        ui.error("审计上传不可用：" + (runtime.degraded_reason or "未接入治理面或契约不可用"))
        return 1
    result = upload_audit_now(runtime)
    if result.get("reason"):
        ui.error(f"上传失败：{result['reason']}")
        return 1
    if result.get("clientHeadMatch") is False:
        # 链头对不上：服务端认为有丢失/重排。不能推进水位线（上传器内部已停），
        # 这里要把「需要人来看」明确说出来，而不是报一句成功。
        ui.error("上传完成但链头与服务端不一致（clientHeadMatch=false），"
                 "审计上传已暂停推进，请检查服务端日志。")
        return 1
    ui.ok(f"已上传 {result.get('uploaded', 0)} 条，接受 {result.get('accepted', 0)}"
          f"（重复 {result.get('duplicates', 0)}，拒绝 {result.get('rejectedCount', 0)}）")
    return 0


__all__ = ["handle", "register_governance_parser"]
