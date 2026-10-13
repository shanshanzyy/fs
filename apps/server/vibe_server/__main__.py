"""CLI 入口：`python -m vibe_server`。

三个子命令，对应运维的三个真实动作：

- `serve`   —— 起服务（默认）
- `bootstrap` —— 从零建第一个组织所有者 + 签发一枚注册令牌
- `doctor`  —— 自检：库能不能打开、密钥托管通不通、策略有没有配、上游通不通

`doctor` 单独存在是有意义的：内网部署最常见的失败不是代码错，
而是「环境少配了一件事」，而那种问题在服务起来之后表现为莫名其妙的 4xx/5xx。
把检查前置，省掉一轮「先报错再猜」。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .app import bootstrap_owner, build_app
from .config import ServerConfig
from .errors import ApiError, ConfigError, StoreError
from .httpd import GovernanceServer


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    # 上游请求日志里可能带上游地址，但不会带正文；保持 INFO 即可
    logging.getLogger("urllib").setLevel(logging.WARNING)


def _load_config(args: argparse.Namespace) -> ServerConfig:
    overrides: dict[str, object] = {}
    if args.host:
        overrides["host"] = args.host
    if args.port is not None:
        overrides["port"] = args.port
    return ServerConfig.load(
        data_dir=args.data_dir,
        db_path=args.db,
        **overrides,
    )


# --------------------------------------------------------------------------- serve
def cmd_serve(args: argparse.Namespace) -> int:
    config = _load_config(args)
    app = build_app(config)
    server = GovernanceServer(app, host=config.host, port=config.port)

    info = config.describe()
    logging.getLogger("vibe.governance").info(
        "治理面启动于 %s（db=%s, secrets=%s, policyRequired=%s）",
        server.base_url,
        info["db"],
        info["secretsProvider"],
        info["policyRequired"],
    )
    if config.multi_instance_intent():
        # 单机模式不打印这一行：保持既有启动日志逐字节一致
        logging.getLogger("vibe.governance").info(
            "多实例模式：共享存储 + 统一签名密钥 + 信任转发头（db=%s）", info["db"]
        )
    if not config.require_tls and not config._is_loopback():
        logging.getLogger("vibe.governance").warning(
            "正在以明文对外提供服务。内网也应尽量上 TLS："
            "策略与审计都从这里过，同网段可被抓包。"
            "建议置于做 TLS 终止的反向代理之后并设置 VIBE_SRV_TRUST_FORWARDED_FOR=1。"
        )

    print(f"VIBE_SRV_URL={server.base_url}")
    print(f"VIBE_SRV_DB={config.store_target()}")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.getLogger("vibe.governance").info("收到中断，正在停止…")
    finally:
        server.shutdown()
        app.store.close()
    return 0


# --------------------------------------------------------------------------- bootstrap
def cmd_bootstrap(args: argparse.Namespace) -> int:
    config = _load_config(args)
    if not args.email or not args.password:
        print("需要 --email 与 --password", file=sys.stderr)
        return 2
    try:
        result = bootstrap_owner(
            config, email=args.email, password=args.password, name=args.name or ""
        )
    except ApiError as exc:
        print(exc.message, file=sys.stderr)
        return 1

    print("已创建组织所有者：")
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # 顺手签发一枚注册令牌，让「建完管理员」到「拉一个开发者进来」不用切换工具
    if not args.no_enrollment:
        from . import rbac
        from .auth.service import AuthService
        from .clock import Clock
        from .store import open_store

        store = open_store(config.store_target())
        try:
            clock = Clock()
            auth = AuthService(store=store, config=config, clock=clock)
            # 这里**刻意不走 authenticate()**。authenticate 会核对令牌里的设备，
            # 而 bootstrap 是一次性的命令行动作、根本没有设备 —— 之前的实现
            # 签了一枚 device_id="cli-bootstrap" 的令牌，服务端找不到该设备记录，
            # 于是被当成「设备已被撤销」，注册令牌永远签不出来。
            # 直接按刚写入的成员关系装配主体即可：既少一次令牌往返，
            # 也不用为了过检查去造一条假设备。
            actor = rbac.build_actor(
                user_id=result["userId"],
                email=result["email"],
                display_name=result.get("displayName") or "",
                memberships=store.list_memberships(result["userId"]),
            )
            enrollment = auth.create_enrollment(
                actor=actor,
                team_id="",
                role="developer",
                max_uses=int(args.enrollment_uses),
                note="bootstrap 自动签发",
            )
            print()
            print("已签发注册令牌（只显示这一次，请立刻分发）：")
            print(f"  {enrollment['enrollmentToken']}")
            print(f"  可用次数 {enrollment['maxUses']}，过期于 {enrollment['expiresAt']}")
            print()
            print("开发者在本机执行（把 <服务地址> / <同事邮箱> 换成实际值）：")
            print(
                "  vibe-agent governance register "
                f"--server <服务地址> --email <同事邮箱> "
                f"--enrollment-token {enrollment['enrollmentToken']}"
            )
            print("或在桌面端标题栏「治理面」→ 填地址 → 选「邀请码注册」。")
            print("后续再拉人：python -m vibe_server invite --email <管理员> --password <...>")
        finally:
            store.close()
    return 0


# --------------------------------------------------------------------------- invite
def cmd_invite(args: argparse.Namespace) -> int:
    """签发注册令牌（邀请码）—— 组织建好之后把同事拉进来的常态化入口。

    `bootstrap` 只在库为空时可用（一次性）；组织一旦建好，新账号只能靠
    SSO 或注册令牌。SSO 要运维先接 IdP，所以「明天再拉一个同事进来」的
    实际路径就是这条：管理员用自己的账号签到限次令牌，把令牌 + 服务地址
    交给同事，同事在桌面端「邀请码注册」或 CLI `governance register` 自助开通。

    **为什么必须校验管理员口令**：本命令直接读写本地库。不校验的话，
    任何能碰到服务端数据目录的人都能量一枚令牌（注册令牌还带角色），
    等于把提权做成了本地文件读权限。`bootstrap` 不校验是因为它只能在
    库为空时跑一次（此时还没有任何账号可冒充）；invite 是常态动作，
    必须先证明「你就是那个管理员」。
    """
    config = _load_config(args)
    email = (args.email or "").lower().strip()
    if not email or not args.password:
        print("需要 --email 与 --password（管理员本人账号）", file=sys.stderr)
        return 2

    from . import rbac
    from .auth import passwords
    from .auth.service import AuthService
    from .clock import Clock
    from .store import open_store

    store = open_store(config.store_target())
    try:
        user = store.find_user_by_email(email)
        # 用户不存在时也算一次哈希，抹平「账号是否存在」的时间差（同 login 的纪律）
        stored = str(user["password_hash"]) if user else passwords.hash_password("dummy-invite-timing")
        if user is None or not passwords.verify_password(args.password, stored):
            print("认证失败：邮箱或口令不正确", file=sys.stderr)
            return 1
        if str(user.get("status") or "active") != "active":
            print(f"账号状态为 {user.get('status')}，无法签发注册令牌", file=sys.stderr)
            return 1

        auth = AuthService(store=store, config=config, clock=Clock())
        # 与 bootstrap 同范式：**刻意不走 authenticate()** —— 它要核对令牌里的设备，
        # 而 CLI 不是设备。直接按库里的成员关系装配主体即可。
        actor = rbac.build_actor(
            user_id=str(user["id"]),
            email=str(user["email"]),
            display_name=str(user.get("display_name") or ""),
            memberships=store.list_memberships(str(user["id"])),
        )
        count = max(1, min(int(args.count or 1), 50))
        ttl = int(args.ttl_hours or 0) * 3600
        issued = [
            auth.create_enrollment(
                actor=actor,
                team_id=str(args.team_id or ""),
                role=str(args.role or rbac.ROLE_DEVELOPER),
                max_uses=int(args.uses or 1),
                ttl=ttl,
                note=str(args.note or ""),
            )
            for _ in range(count)
        ]
    finally:
        store.close()

    print(f"已签发 {len(issued)} 枚注册令牌（只显示这一次，请立刻分发给同事）：")
    for index, item in enumerate(issued, 1):
        print()
        print(f"  [{index}] {item['enrollmentToken']}")
        print(f"      角色 {item['role']}，可用 {item['maxUses']} 次，过期于 {item['expiresAt']}")
    print()
    print("同事在本机执行（把 <服务地址> / <同事邮箱> 换成实际值）：")
    print(
        "  vibe-agent governance register "
        "--server <服务地址> --email <同事邮箱> --enrollment-token <上面的令牌>"
    )
    print("或在桌面端标题栏「治理面」→ 填地址 → 选「邀请码注册」。")
    return 0


# --------------------------------------------------------------------------- doctor
def cmd_doctor(args: argparse.Namespace) -> int:
    config = _load_config(args)
    problems: list[str] = []
    checks: list[tuple[str, bool, str]] = []

    # 1) 数据目录与库
    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        from .store import open_store

        store = open_store(config.store_target())
        ok, detail = store.health()
        checks.append(("存储", ok, detail))
        if not ok:
            problems.append(detail)
    except (ConfigError, StoreError) as exc:
        # StoreError 也要接住：多实例下目标是 Postgres，库不可达时 open_store 会抛
        # PGConnectionError（StoreError 子类）。doctor 的职责是**报出来**，不是崩掉。
        checks.append(("存储", False, str(exc)))
        problems.append(str(exc))
        store = None

    # 2) 密钥托管
    try:
        from .gateway.secrets import open_secret_store

        secrets = open_secret_store(config)
        ok, detail = secrets.health()
        checks.append(("密钥托管", ok, f"{detail}，已知密钥 {len(secrets.names())} 个"))
    except ConfigError as exc:
        checks.append(("密钥托管", False, str(exc)))
        problems.append(str(exc))

    # 3) 上游配置
    if config.cloud_upstream_url and not config.cloud_upstream_key_ref:
        checks.append(("云端上游", False, "配了 URL 但没配密钥引用，云端调用会 503"))
        problems.append("云端上游缺少密钥引用")
    elif config.cloud_upstream_url:
        checks.append(("云端上游", True, f"已配置，密钥引用 {config.cloud_upstream_key_ref}"))
    else:
        checks.append(("云端上游", True, "未配置（云端路由不可用，本地不受影响）"))
    if not config.local_upstream_url:
        checks.append(("本地上游", True, "未配置（客户端直连本地模型，不经网关）"))

    # 4) 策略
    if store is not None:
        try:
            from .policy import PolicyService
            from .clock import Clock

            service = PolicyService(store=store, config=config, clock=Clock())
            global_policy = store.get_latest_policy("global", "")
            if global_policy is None:
                checks.append(
                    (
                        "策略",
                        not config.policy_required,
                        "尚未发布全局策略，客户端将使用内置保守默认值"
                        + ("（policyRequired=True，建议尽快发布）" if config.policy_required else ""),
                    )
                )
                if config.policy_required:
                    problems.append("未发布全局策略")
            else:
                checks.append(("策略", True, f"全局策略版本 {global_policy.get('version')}"))
            del service
        except Exception as exc:  # noqa: BLE001
            checks.append(("策略", False, f"读取失败: {exc}"))
            problems.append(f"策略读取失败: {exc}")

    # 5) 多实例前置自检（只在多实例意图下出现 —— 单机输出保持与改造前一致）
    if config.multi_instance_intent():
        gaps: list[str] = []
        if not config.jwt_secret_from_env:
            gaps.append("统一签名密钥未显式设置（VIBE_SRV_JWT_SECRET）")
        if not config.db_url:
            gaps.append("存储不是共享库（VIBE_SRV_DB=postgres://…）")
        if not config.require_tls:
            gaps.append("未开启 REQUIRE_TLS=1")
        if not config.trust_forwarded_for:
            gaps.append("未开启 TRUST_FORWARDED_FOR=1")
        if gaps:
            checks.append(("多实例", False, "；".join(gaps)))
            problems.extend(gaps)
        else:
            checks.append(("多实例", True, "共享存储 + 统一密钥 + TLS/来源 IP 已就绪"))

    # 6) 监听可达性（只在显式要求时做，避免误连别人的服务）
    if args.probe:
        import urllib.error
        import urllib.request

        url = args.probe.rstrip("/") + "/healthz"
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                url, timeout=5
            ) as response:
                checks.append(("探活", True, f"{url} -> {response.status}"))
        except (urllib.error.URLError, OSError) as exc:
            checks.append(("探活", False, f"{url} 不可达: {exc}"))
            problems.append("探活失败")

    print(f"vibe-governance {__version__} 自检")
    print(f"  数据目录: {config.data_dir}")
    print(f"  数据库  : {config.store_target()}")
    print(f"  监听    : {config.host}:{config.port}（TLS={'on' if config.require_tls else 'off'}）")
    print()
    for name, ok, detail in checks:
        print(f"  [{'OK ' if ok else 'FAIL'}] {name:<10} {detail}")
    print()
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for item in problems:
            print(f"  · {item}")
        if store is not None:
            store.close()
        return 1
    print("自检通过。")
    if store is not None:
        store.close()
    return 0


# --------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibe_server",
        description="本地化 Vibe Coding 智能体的服务端治理面（认证 / RBAC / LLM 网关 / 配额 / 策略 / 审计）",
    )
    parser.add_argument("--version", action="version", version=f"vibe-governance {__version__}")
    parser.add_argument("--data-dir", help="数据目录（默认 ./.vibe-governance）")
    parser.add_argument("--db", help="数据库路径，或 sqlite:/// 与 postgres:// 形式的 URL")
    parser.add_argument("--host", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, help="监听端口（默认 8787）")
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志")

    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动服务（默认）")
    serve.set_defaults(func=cmd_serve)

    boot = sub.add_parser("bootstrap", help="创建第一个组织所有者并签发注册令牌")
    boot.add_argument("--email", required=True)
    boot.add_argument("--password", required=True)
    boot.add_argument("--name", default="")
    boot.add_argument("--enrollment-uses", type=int, default=1, help="自动签发注册令牌的可用次数（0 表示跳过签发）")
    boot.set_defaults(func=cmd_bootstrap, no_enrollment=False)

    invite = sub.add_parser("invite", help="签发注册令牌（邀请码），把同事拉进组织")
    invite.add_argument("--email", required=True, help="管理员本人账号（用于鉴权）")
    invite.add_argument("--password", required=True, help="管理员本人口令")
    invite.add_argument("--role", default="developer", help="新账号角色，默认 developer")
    invite.add_argument("--uses", type=int, default=1, help="每枚令牌可用次数（1–100）")
    invite.add_argument("--count", type=int, default=1, help="一次签发几枚（1–50）")
    invite.add_argument("--ttl-hours", type=int, default=0, help="令牌有效期（小时），0 = 用服务端默认")
    invite.add_argument("--team-id", default="", help="限定令牌开通的账号所属团队（留空 = 默认团队）")
    invite.add_argument("--note", default="", help="备注，只进服务端审计（截断展示）")
    invite.set_defaults(func=cmd_invite)

    doctor = sub.add_parser("doctor", help="环境自检")
    doctor.add_argument("--probe", help="额外探测一个已运行实例的地址，如其 http://127.0.0.1:8787")
    doctor.set_defaults(func=cmd_doctor, probe=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    _setup_logging(getattr(args, "verbose", False))

    if getattr(args, "no_enrollment", None) is False and getattr(args, "enrollment_uses", 1) == 0:
        args.no_enrollment = True

    func = getattr(args, "func", cmd_serve)
    try:
        return int(func(args))
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except ApiError as exc:
        print(f"操作失败：{exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
