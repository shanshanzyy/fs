"""多人共用同一个集中治理面的端到端冒烟（真进程）。

**要证明的那一件事**：一台集中治理面，能让多名同事各自的本机客户端接入，
策略一改全体生效、审计全落到同一处 —— 即「多人共用模式」真的成立，
而不只是「单机能登录」。

**验什么**（6 档，全部打在真实 HTTP / 进程边界上，脚本内不 mock 任何 HTTP）：

1. 起真 `vibe_server`（`PORT=0`，从 stdout 解析真实 URL）。全程只碰临时目录，
   绝不写用户的 `~/.vibe` 或仓库里的 `.vibe-governance`。
2. `bootstrap --enrollment-uses 0` 只建管理员、**不签发**令牌 —— 先把
   「组织建好之后怎么拉人」这个缺口摆出来（bootstrap 只能在库为空时跑一次）。
3. 用 `invite` 子命令一次签两枚邀请码（同事 A / B）。**这两枚必须都能用** ——
   只断言「输出里有令牌」是不够的，之前那版引导命令甚至指向了不存在的子命令。
4. 同事 A、B 各自注册并登录：**各自独立的 `CredentialStore`**，模拟两台不同的机器
   （同一台机器会共用凭证文件，证不出「多人」）。
5. 管理员发布全局策略（`allowCloud=false`）→ A、B 各自 `wire()` 后都必须是
   `governed` 且 `constraints.allow_cloud is False`：**同一份策略管住了两个人**。
6. 两人各自产生审计并上报 → 服务端收到的事件里出现**两个不同的 deviceId**
   （且分属两次会话）：一份审计面，多台设备。

**失败通常意味着什么**：

* 档 2/3 → `bootstrap` 或 `invite` 链路断了（看打印的 stdout/stderr）。
* 档 4 → 邀请码签发/占用有问题，或多设备注册冲突。
* 档 5 → 策略下发或客户端装配（`wire()`）出问题。
* 档 6 → 审计上报翻译层或服务端脱敏闸门出问题。

**用法**（工作目录 `packages/agent-core`）：

    python scripts/e2e_multiuser.py

退出码 0 = 全绿；非 0 = 有失败项，原因与定位信息都打印在 stdout，
并写一份到 `%TEMP%/e2e_multiuser_report.txt`（后台任务吞 stdout 时的兜底）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

# 复用 M2 冒烟那套「真进程」基础设施，避免两份各自维护。
from e2e_governance import (  # noqa: E402
    PYTHON,
    SERVER_DIR,
    all_pids,
    http_json,
    kill_tree,
    server_env,
    start_server,
)

OWNER_EMAIL = "owner@corp.example"
OWNER_PASSWORD = "Str0ng!Passw0rd"
DEV_A = ("alice@corp.example", "Al1ce!Passw0rd")
DEV_B = ("bob@corp.example", "B0b!Passw0rd")


# --------------------------------------------------------------------------- CLI
def run_cli(tmp: Path, *argv: str) -> tuple[int, str]:
    """以真进程跑一条 vibe_server 子命令（与 serve 共用同一份 data_dir / db）。"""
    proc = subprocess.run(
        [PYTHON, "-m", "vibe_server",
         "--data-dir", str(tmp / "data"), "--db", str(tmp / "vibe.db"), *argv],
        cwd=str(SERVER_DIR),
        env=server_env(tmp, {}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    return proc.returncode, proc.stdout + proc.stderr


def bootstrap_owner(tmp: Path) -> tuple[int, str]:
    """只建管理员、**不签发**令牌：把「拉人」这件事显式留给 invite。"""
    return run_cli(
        tmp, "bootstrap",
        "--email", OWNER_EMAIL, "--password", OWNER_PASSWORD,
        "--enrollment-uses", "0",
    )


def invite(tmp: Path, count: int) -> tuple[int, str, list[str]]:
    code, out = run_cli(
        tmp, "invite",
        "--email", OWNER_EMAIL, "--password", OWNER_PASSWORD, "--count", str(count),
    )
    return code, out, re.findall(r"^\s*\[\d+\]\s+(\S+)\s*$", out, flags=re.MULTILINE)


# --------------------------------------------------------------------------- 主流程
def main() -> int:  # noqa: C901 —— 冒烟脚本按验收档顺序线性展开，拆函数反而更难对照
    from agent_core import audit as audit_mod
    from agent_core.audit import AuditLog
    from agent_core.config import Config
    from agent_core.governance.client import GovernanceClient
    from agent_core.governance.credentials import CredentialStore
    from agent_core.governance.runtime import wire

    failures: list[str] = []
    report: list[str] = []

    def say(text: str = "") -> None:
        print(text)
        report.append(text)

    def expect(cond: Any, label: str, detail: str = "") -> bool:
        if cond:
            say(f"  [OK ] {label}")
        else:
            say(f"  [FAIL] {label} {detail}".rstrip())
            failures.append(f"{label} {detail}".strip())
        return bool(cond)

    tmp = Path(tempfile.mkdtemp(prefix="vibe_e2e_multi_"))
    procs: list[tuple[str, subprocess.Popen]] = []
    pids_before = all_pids()

    say("=== 多人共用集中治理面 E2E 冒烟（真进程）===")
    say(f"临时目录: {tmp}")

    try:
        # ---------------- 档 1：起集中治理面 ----------------
        say("\n[档1] 启动集中治理面…")
        server_proc, server_log, server_url = start_server(tmp, {})
        procs.append(("vibe_server", server_proc))
        say(f"  治理面: {server_url}")
        expect(server_url.startswith("http://127.0.0.1:"), "档1 治理面起在回环地址上", server_url)
        expect(str(tmp) in next((ln for ln in server_log if ln.startswith("VIBE_SRV_DB=")), ""),
               "档1 库文件落在临时目录")
        status, health = http_json(server_url + "/healthz")
        expect(status == 200 and health.get("ok") is True, "档1 /healthz 健康", str(health))

        # ---------------- 档 2：只建管理员，不签发令牌 ----------------
        say("\n[档2] bootstrap 只建管理员（--enrollment-uses 0），把「拉人」留给 invite…")
        code, out = bootstrap_owner(tmp)
        expect(code == 0, "档2 管理员创建成功", out[-240:])
        expect("已创建组织所有者" in out, "档2 打印了管理员信息")
        expect("已签发注册令牌" not in out,
               "档2 未自动签发令牌（证明 invite 才是拉人路径）")

        # ---------------- 档 3：一次签两枚邀请码 ----------------
        say("\n[档3] 用 invite 一次签两枚邀请码…")
        code, out, tokens = invite(tmp, 2)
        expect(code == 0, "档3 invite 成功", out[-240:])
        expect(len(tokens) == 2, "档3 签出两枚邀请码", f"实得 {len(tokens)}")
        expect(len(set(tokens)) == 2, "档3 两枚邀请码互不相同")
        # 引导命令必须指向真实存在的子命令（旧文案曾指向不存在的 enroll）
        expect("governance register" in out and "--enrollment-token" in out,
               "档3 引导命令指向 governance register")

        # ---------------- 档 4：两位同事各自注册并登录 ----------------
        say("\n[档4] 两位同事用各自独立的凭证存储注册（模拟两台机器）…")
        clients: list[tuple[str, str, Any, Any]] = []  # (名字, 邮箱, client, store)
        for (name, (email, password), token) in (
            ("A", DEV_A, tokens[0]),
            ("B", DEV_B, tokens[1]),
        ):
            store = CredentialStore(tmp / f"cred-{name}.json")
            client = GovernanceClient(server_url, store=store)
            creds = client.register(
                enrollment_token=token, email=email, password=password,
                display_name=f"同事{name}", device_name=f"laptop-{name}",
            )
            expect(bool(creds.access_token) and bool(creds.device_id),
                   f"档4 同事{name} 注册并拿到令牌/设备")
            expect(creds.email == email, f"档4 同事{name} 身份正确", creds.email)
            clients.append((name, email, client, store))
        expect(clients[0][3].load().device_id != clients[1][3].load().device_id,
               "档4 两位同事是两台不同设备")

        # ---------------- 档 5：一份全局策略管住两个人 ----------------
        say("\n[档5] 管理员发布 allowCloud=false，两位同事 wire() 后都必须受同一策略约束…")
        owner_client = GovernanceClient(server_url, store=CredentialStore(tmp / "cred-owner.json"))
        owner = owner_client.login(email=OWNER_EMAIL, password=OWNER_PASSWORD, device_name="admin")
        expect(bool(owner.access_token), "档5 管理员登录成功")
        published = owner_client.request(
            "POST", "/policy",
            body={"policy": {"allowCloud": False}, "scopeType": "global", "note": "多人冒烟"},
        )
        expect(published.status in (200, 201), "档5 发布全局策略成功",
               f"HTTP {published.status} {published.body}")

        runtimes: list[tuple[str, Any]] = []
        for (name, email, _client, store) in clients:
            workspace = tmp / f"ws-{name}"
            workspace.mkdir(parents=True, exist_ok=True)
            config = Config.load(
                workspace,
                server_url=server_url,
                base_url="http://127.0.0.1:1/v1",  # 本次不真正调模型，占位即可
                api_key="mock",
                model="mock-agent-model",
            )
            rt = wire(config, store=store)
            expect(rt.status == "governed", f"档5 同事{name} status=governed", rt.status)
            expect(rt.constraints.allow_cloud is False,
                   f"档5 同事{name} 受 allowCloud=false 约束",
                   str(rt.constraints.allow_cloud))
            expect(rt.uploader is not None, f"档5 同事{name} 装配出审计上传器",
                   "；".join(rt.notices)[:200])
            runtimes.append((name, rt))

        # ---------------- 档 6：审计全落到同一处，且能分辨两台设备 ----------------
        say("\n[档6] 两位同事各自产生审计并上报，服务端须见到两个不同 deviceId…")
        seen_devices: set[str] = set()
        for (name, rt) in runtimes:
            log = AuditLog(path=rt.config.audit_path, session_id=rt.config.session_id)
            audit_mod.record_session_start(log, rt.config)
            log.record("tool.read_file", path=f"src/{name}.py", chars=42, lines="1-3")
            result = rt.uploader.upload()
            expect(result.uploaded > 0, f"档6 同事{name} 审计已上传", str(result.describe()))
            expect(len(result.rejected) == 0, f"档6 同事{name} 无被拒事件",
                   str(result.rejected[:3]))

            q_status, q_body = http_json(
                server_url + "/api/v1/audit/events?sessionId=" + rt.config.session_id,
                token=owner.access_token,
            )
            events = q_body.get("events") if isinstance(q_body, dict) else None
            expect(q_status == 200 and isinstance(events, list) and len(events) > 0,
                   f"档6 服务端收到同事{name} 的事件",
                   f"HTTP {q_status} events={len(events) if isinstance(events, list) else events}")
            for event in (events or []):
                if isinstance(event, dict) and event.get("deviceId"):
                    seen_devices.add(str(event["deviceId"]))

        expect(len(seen_devices) == 2,
               "档6 同一治理面收到两台不同设备的事件", f"实得 {sorted(seen_devices)}")

    except Exception as exc:  # noqa: BLE001 —— 冒烟脚本要如实报告任何异常
        failures.append(f"脚本异常: {type(exc).__name__}: {exc}")
        say(f"  [FAIL] 脚本异常: {type(exc).__name__}: {exc}")
    finally:
        say("\n[清理] 按进程树终止治理面并比对进程快照…")
        for _name, proc in procs:
            kill_tree(proc.pid)
        time.sleep(0.8)
        pids_after = all_pids()
        started = {str(proc.pid) for _name, proc in procs}
        stray = sorted(started & pids_after)
        expect(not stray, "清理 本次启动的进程已全部退出", f"残留 PID {stray}")
        shutil.rmtree(tmp, ignore_errors=True)

    say("")
    if failures:
        say("=== 冒烟失败 ===")
        for item in failures:
            say(f"  ✗ {item}")
    else:
        say("=== 冒烟通过：6 档验收全绿（多人共用模式成立）===")

    report_path = Path(tempfile.gettempdir()) / "e2e_multiuser_report.txt"
    try:
        report_path.write_text("\n".join(report), encoding="utf-8")
        print(f"报告已写入 {report_path}")
    except OSError:
        pass
    print(f"退出码 {'1' if failures else '0'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
