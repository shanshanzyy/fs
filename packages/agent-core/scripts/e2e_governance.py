"""M2 治理面端到端冒烟：真进程跑通「认证 → 策略 → 网关 → 脱敏 → 配额 → 审计」。

**验什么**（8 档，全部打在真实 HTTP / 进程边界上，脚本内不 mock 任何 HTTP）：

1. 起真 `vibe_server`（`PORT=0`，从 stdout 解析真实 URL）+ 两个真的假模型
   （本地 / 云端各一个）当上游。全程只碰临时目录，绝不写用户的 `~/.vibe`
   或仓库里的 `.vibe-governance`。服务端刻意用 `SECRETS_PROVIDER=file`
   启动（E4 定档：内网无 Vault），并额外跑两轮真进程 `doctor`：一份有效
   密钥文件要认得出、缺文件要 FAIL 且给修复建议。
2. 建组织 + 签发注册令牌 → 客户端 `register` 拿令牌。
3. 策略收紧：发布 `allowCloud=false` → `wire()` 后必须 `status=governed`、
   `constraints.allow_cloud is False`、模型客户端是本地 `OpenAICompatClient`。
4. 云端路由：发布 `allowCloud=true` → 一轮非流式 + 一轮流式（声明 `reasoning`），
   两端文本一致且路由结果为 `cloud`。同时断言云端上游**真的收到了密钥文件里
   那枚哨兵值的 `Bearer`**（假模型记下 Authorization 头），本地上游不带
   Authorization，且明文密钥不落服务端日志。
5. 出站脱敏：正文含 PEM 私钥块 → 路由被压回 `local` 且 `sensitive` 为真
   （本地上游同时充当「未配密钥引用就不该带 Authorization」的反向对照）。
6. 配额：把用户配额调到极小 → 云端请求得到 429 语义，并**降级到本地继续**。
7. 审计上报：本机产生审计事件 → `uploader.upload()` 上传，`uploaded>0`、
   无被拒事件，且上传载荷不含任何正文类字段（服务端 `redaction.DENIED_KEYS`）。
8. 退出时按进程树清掉全部子进程，比对起止进程快照，断言无残留。

**失败通常意味着什么**：

* 第 1~2 档 → 服务端起不来，或 bootstrap / 注册链路断了（看打印的 stderr）。
  **若服务端起不来且提示密钥文件相关，说明 `file` provider 的启动闸门生效了**
  —— 优先看临时目录里的 `secrets.json` 是否写成、权限是否为 `600`。
* 第 3 档 → 策略「只收紧」没生效，客户端仍在用网关（合规风险）。
* 第 4 档 → 网关流式或路由判定异常。**若报 400「暂不支持 stream=true」，
  说明 `gateway/proxy.py` 的 `require_json_object` 又被加回了对 `stream` 的拒绝**
  （那会让 `api/routes.py` 的流式分支变成死代码）。
  **若「云端上游收到来自密钥文件的 Bearer」失败**：说明密钥引用没解析到值
  —— 查 `SECRETS_PROVIDER`/`SECRETS_FILE`/`CLOUD_UPSTREAM_KEY_REF` 三者是否对得上。
* 第 5 档 → 敏感红线漏判（正文扫描失效），这是合规红线，必须查。
* 第 6 档 → 配额预留 / 降级链断了（云端超限应降级本地，而不是整单失败）。
* 第 7 档 → 审计翻译 / 上传链或服务端脱敏闸门出问题。
* 第 8 档 → 有残留子进程（多半是没按进程树终止，Windows 上 `kill()` 不够）。

**用法**（工作目录 `packages/agent-core`）：

    python scripts/e2e_governance.py

退出码 0 = 全绿；非 0 = 有失败项，原因与定位信息都打印在 stdout，
并写一份到 `%TEMP%/e2e_governance_report.txt`（后台任务吞 stdout 时的兜底）。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

AGENT_CORE = Path(__file__).resolve().parents[1]        # packages/agent-core
REPO = AGENT_CORE.parents[1]                            # 仓库根
SERVER_DIR = REPO / "apps" / "server"                   # apps/server（包名 vibe_server）

for _path in (AGENT_CORE, SERVER_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

PYTHON = sys.executable
IS_WINDOWS = sys.platform.startswith("win")

CLOUD_TEXT = "CLOUD-ANSWER：云端模型给出的答复。"
LOCAL_TEXT = "LOCAL-ANSWER：本地模型给出的答复。"
PEM_BLOCK = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"

# 密钥托管（E4 定档：内网无 Vault，以 file 为准）。这枚值是**哨兵**：
# 它只存在于临时 secrets.json 里，冒烟要断言它一路走到云端上游的
# Authorization 头 —— 这样「file provider 在真进程里被真的读了」才是
# 可证的，而不是「服务没报错所以应该没问题」。
SECRET_REF = "cloud-main"
CLOUD_KEY_SENTINEL = "sk-cloud-from-file-9f3a17c2"

OWNER_EMAIL = "owner@corp.example"
OWNER_PASSWORD = "Str0ng!Passw0rd"
DEV_EMAIL = "dev@corp.example"
DEV_PASSWORD = "Dev0!Passw0rd"


# --------------------------------------------------------------------------- 基础设施
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def server_env(tmp: Path, extra: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(SERVER_DIR),
            "PYTHONUNBUFFERED": "1",
            "VIBE_SRV_DATA_DIR": str(tmp / "data"),
            "VIBE_SRV_DB": str(tmp / "vibe.db"),
            "VIBE_SRV_PORT": "0",
        }
    )
    env.update(extra)
    return env


def pump(stream: Any, sink: list[str]) -> threading.Thread:
    """把子进程输出抽干，避免管道写满把子进程卡死。"""
    def run() -> None:
        try:
            for line in stream:
                sink.append(line.rstrip("\n"))
        except (ValueError, OSError):
            pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def start_server(tmp: Path, extra_env: dict[str, str]) -> tuple[subprocess.Popen, list[str], str]:
    sink: list[str] = []
    proc = subprocess.Popen(
        [PYTHON, "-m", "vibe_server", "serve"],
        cwd=str(SERVER_DIR),
        env=server_env(tmp, extra_env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    pump(proc.stdout, sink)
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        for line in sink:
            if line.startswith("VIBE_SRV_URL="):
                return proc, sink, line.split("=", 1)[1].strip()
        if proc.poll() is not None:
            raise RuntimeError("vibe_server 提前退出：\n" + "\n".join(sink[-20:]))
        time.sleep(0.1)
    raise RuntimeError("等待 VIBE_SRV_URL 超时：\n" + "\n".join(sink[-20:]))


def start_mock(script_path: Path, record: Path | None = None) -> tuple[subprocess.Popen, str]:
    port = free_port()
    cmd = [PYTHON, str(AGENT_CORE / "scripts" / "mock_model_server.py"),
           "--port", str(port), "--script", str(script_path)]
    if record is not None:
        cmd += ["--record-headers", str(record)]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url + "/v1/models", timeout=0.4).read()
            return proc, url
        except Exception:  # noqa: BLE001
            time.sleep(0.15)
    raise RuntimeError("假模型服务没能启动")


def recorded_auth(path: Path) -> list[str]:
    """读出假模型记下的 Authorization 头（每行一条 JSONL）。"""
    if not path.exists():
        return []
    values: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            values.append(str(json.loads(line).get("authorization") or ""))
        except json.JSONDecodeError:
            continue
    return values


def run_doctor(tmp: Path, extra_env: dict[str, str]) -> tuple[int, str]:
    """以**真进程**跑一次 doctor，返回 (退出码, 合并输出)。

    用独立的 data/db，避免自检动作污染主档正在用的库。
    """
    env = server_env(tmp, extra_env)
    env["VIBE_SRV_DATA_DIR"] = str(tmp / "doctor-data")
    env["VIBE_SRV_DB"] = str(tmp / "doctor.db")
    proc = subprocess.run(
        [PYTHON, "-m", "vibe_server",
         "--data-dir", str(tmp / "doctor-data"), "--db", str(tmp / "doctor.db"),
         "doctor"],
        cwd=str(SERVER_DIR),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    return proc.returncode, proc.stdout + proc.stderr


def run_bootstrap(tmp: Path, email: str, password: str) -> str:
    """建组织 + 签发一枚注册令牌，返回令牌原文（从 stdout 解析）。"""
    proc = subprocess.run(
        [PYTHON, "-m", "vibe_server",
         "--data-dir", str(tmp / "data"), "--db", str(tmp / "vibe.db"),
         "bootstrap",
         "--email", email, "--password", password,
         "--enrollment-uses", "1"],
        cwd=str(SERVER_DIR),
        env=server_env(tmp, {}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"bootstrap 失败（{proc.returncode}）：\n{proc.stdout}\n{proc.stderr}")
    lines = proc.stdout.splitlines()
    for index, line in enumerate(lines):
        if "已签发注册令牌" in line:
            for follow in lines[index + 1:index + 4]:
                token = follow.strip()
                if token and " " not in token:
                    return token
    raise RuntimeError(f"没能从 bootstrap 输出里解析出注册令牌：\n{proc.stdout}")


# --------------------------------------------------------------------------- 进程快照
def all_pids() -> set[str]:
    """当前所有 **python** 进程的 PID。

    只算 python 进程：本脚本启动的子进程全是 `sys.executable`，
    过滤掉 node/chrome 等无关进程，档 8 的「新增残留」断言才不会
    被系统里恰好起落的别的进程误伤。
    """
    if not IS_WINDOWS:
        return set()
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=40,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    pids: set[str] = set()
    for line in out.splitlines():
        parts = [part.strip().strip('"') for part in line.split(",")]
        if len(parts) >= 2 and parts[1].isdigit() and "python" in parts[0].lower():
            pids.add(parts[1])
    return pids


def kill_tree(pid: int) -> None:
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


# --------------------------------------------------------------------------- HTTP
def http_json(
    url: str, *, method: str = "GET", payload: dict | None = None,
    token: str = "", headers: dict[str, str] | None = None, timeout: int = 30,
) -> tuple[int, dict]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            return int(response.status), (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except ValueError:
            parsed = {"_raw": raw[:400]}
        return int(exc.code), parsed


# --------------------------------------------------------------------------- 主流程
def main() -> int:  # noqa: C901 —— 冒烟脚本按验收档顺序线性展开，拆函数反而更难对照
    from agent_core import audit as audit_mod
    from agent_core.audit import AuditLog
    from agent_core.config import Config
    from agent_core.governance.client import GovernanceClient
    from agent_core.governance.credentials import CredentialStore
    from agent_core.governance.gateway_llm import RouteInfo, open_governed_client
    from agent_core.governance.runtime import wire
    from vibe_server import redaction

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

    tmp = Path(tempfile.mkdtemp(prefix="vibe_e2e_gov_"))
    workspace = tmp / "workspace"
    workspace.mkdir(parents=True)
    procs: list[tuple[str, subprocess.Popen]] = []
    pids_before = all_pids()

    say("=== M2 治理面 E2E 冒烟（真进程）===")
    say(f"临时目录: {tmp}")

    try:
        # ---------------- 档 1：起服务 + 假上游 ----------------
        say("\n[档1] 启动服务端与真假模型…")
        cloud_script = tmp / "cloud.json"
        cloud_script.write_text(json.dumps([{"text": CLOUD_TEXT}], ensure_ascii=False), encoding="utf-8")
        local_script = tmp / "local.json"
        local_script.write_text(json.dumps([{"text": LOCAL_TEXT}], ensure_ascii=False), encoding="utf-8")

        # 密钥文件（E4）：服务端用 `file` provider 启动，云端上游的密钥引用
        # 指向它。哨兵值只写在这里，后面靠假模型记下的 Authorization 头来证伪。
        secrets_file = tmp / "secrets.json"
        secrets_file.write_text(json.dumps({SECRET_REF: CLOUD_KEY_SENTINEL}), encoding="utf-8")
        # POSIX 上权限过宽会让服务端启动即失败；Windows 上这条检查不适用（chmod 无害）。
        os.chmod(secrets_file, 0o600)
        cloud_records = tmp / "cloud-headers.jsonl"
        local_records = tmp / "local-headers.jsonl"

        cloud_proc, cloud_url = start_mock(cloud_script, record=cloud_records)
        procs.append(("mock-cloud", cloud_proc))
        local_proc, local_url = start_mock(local_script, record=local_records)
        procs.append(("mock-local", local_proc))
        say(f"  假模型: 云端 {cloud_url} / 本地 {local_url}")

        file_secret_env = {
            "VIBE_SRV_SECRETS_PROVIDER": "file",
            "VIBE_SRV_SECRETS_FILE": str(secrets_file),
            "VIBE_SRV_CLOUD_UPSTREAM_KEY_REF": SECRET_REF,
        }
        server_proc, server_log, server_url = start_server(
            tmp,
            {
                "VIBE_SRV_LOCAL_UPSTREAM_URL": local_url + "/v1",
                "VIBE_SRV_CLOUD_UPSTREAM_URL": cloud_url + "/v1",
                **file_secret_env,
            },
        )
        procs.append(("vibe_server", server_proc))
        say(f"  服务端: {server_url}（密钥托管 file，引用 {SECRET_REF}）")
        expect(server_url.startswith("http://127.0.0.1:"), "档1 服务端起在回环地址上", server_url)
        # 「绝不碰用户 ~/.vibe / 仓库 .vibe-governance」：断言服务端自己打印的
        # 库路径落在临时目录，而不是仓库里的 .vibe-governance。
        db_line = next((ln for ln in server_log if ln.startswith("VIBE_SRV_DB=")), "")
        expect(str(tmp) in db_line, "档1 服务端库文件落在临时目录", db_line or "未打印 VIBE_SRV_DB")
        expect((tmp / "vibe.db").exists(), "档1 临时库文件已创建")

        health_status, health = http_json(server_url + "/healthz")
        expect(health_status == 200 and health.get("ok") is True, "档1 /healthz 健康", str(health))

        # 密钥托管（E4）：同一份 file 配置在真进程里跑 doctor —— 认得出、只报数量；
        # 文件缺失时必须 FAIL 且给出可执行的修复动作，而不是只喊「少了点东西」。
        say("\n[档1b] doctor 对 file 密钥托管的自检（真进程）…")
        _, doctor_out = run_doctor(tmp, file_secret_env)
        expect("[OK ] 密钥托管" in doctor_out, "档1b doctor 认可 file 密钥配置",
               next((ln for ln in doctor_out.splitlines() if "密钥托管" in ln), "未打印密钥托管"))
        expect(CLOUD_KEY_SENTINEL not in doctor_out, "档1b doctor 输出不含明文密钥")
        bad_code, bad_out = run_doctor(
            tmp,
            {**file_secret_env, "VIBE_SRV_SECRETS_FILE": str(tmp / "missing-secrets.json")},
        )
        expect(bad_code != 0 and "[FAIL] 密钥托管" in bad_out,
               "档1b 密钥文件缺失时 doctor FAIL", f"exit={bad_code}")
        expect("修复" in bad_out, "档1b FAIL 时给出可执行的修复建议",
               next((ln for ln in bad_out.splitlines() if "修复" in ln), "未打印修复建议"))

        # ---------------- 档 2：建组织 + 注册 ----------------
        say("\n[档2] 建组织、签发注册令牌、客户端注册…")
        enrollment_token = run_bootstrap(tmp, OWNER_EMAIL, OWNER_PASSWORD)
        expect(bool(enrollment_token), "档2 bootstrap 签发了注册令牌")

        owner_client = GovernanceClient(server_url, store=CredentialStore(tmp / "owner-creds.json"))
        owner = owner_client.login(email=OWNER_EMAIL, password=OWNER_PASSWORD, device_name="owner-smoke")
        expect(bool(owner.access_token), "档2 组织所有者登录成功")

        dev_store = CredentialStore(tmp / "dev-creds.json")
        dev_client = GovernanceClient(server_url, store=dev_store)
        dev = dev_client.register(
            enrollment_token=enrollment_token,
            email=DEV_EMAIL,
            password=DEV_PASSWORD,
            display_name="Smoke Dev",
            device_name="dev-smoke",
        )
        expect(bool(dev.access_token) and bool(dev.device_id), "档2 开发者注册并拿到令牌/设备")
        dev_user_id = dev.user_id
        dev_token = dev_client.ensure_token()

        # 客户端配置：base_url 指向「本地」假模型（回退/本地直连用）
        config = Config.load(
            workspace,
            server_url=server_url,
            base_url=local_url + "/v1",
            api_key="mock",
            model="mock-agent-model",
        )
        expect(str(config.audit_path).startswith(str(tmp)), "档2 审计日志落在临时工作区")

        # ---------------- 档 3：策略收紧（禁云） ----------------
        say("\n[档3] 发布 allowCloud=false，wire() 后必须只走本地…")
        published = owner_client.request(
            "POST", "/policy",
            body={"policy": {"allowCloud": False}, "scopeType": "global", "note": "e2e 冒烟"},
        )
        expect(published.status in (200, 201), "档3 发布全局策略成功",
               f"HTTP {published.status} {published.body}")

        rt_local = wire(config, store=dev_store)
        expect(rt_local.status == "governed", "档3 status=governed", rt_local.status)
        expect(rt_local.constraints.allow_cloud is False,
               "档3 constraints.allow_cloud 为 False", str(rt_local.constraints.allow_cloud))
        expect(type(rt_local.client).__name__ == "OpenAICompatClient",
               "档3 模型客户端是本地 OpenAICompatClient",
               type(rt_local.client).__name__)

        # ---------------- 档 4：云端路由（非流式 + 流式） ----------------
        say("\n[档4] 发布 allowCloud=true，声明 reasoning 走云端（非流式 + 流式）…")
        published = owner_client.request(
            "POST", "/policy",
            body={"policy": {"allowCloud": True}, "scopeType": "global", "note": "e2e 冒烟-开云"},
        )
        expect(published.status in (200, 201), "档4 发布 allowCloud=true 成功",
               f"HTTP {published.status} {published.body}")

        rt_cloud = wire(config, store=dev_store)
        expect(rt_cloud.status == "governed" and rt_cloud.constraints.allow_cloud is True,
               "档4 wire() 后 allow_cloud 为 True",
               f"{rt_cloud.status}/{rt_cloud.constraints.allow_cloud}")

        messages = [{"role": "user", "content": "请重构整个模块的架构与依赖关系。"}]

        gateway = open_governed_client(
            config=rt_cloud.config,
            gateway_url=rt_cloud.governance.gateway_base_url(),
            token_provider=rt_cloud.governance.ensure_token,
            fallback_client=None,
            allow_gateway=True,
            task="reasoning",
        )
        non_stream = gateway.chat(messages)
        route = getattr(gateway.primary, "last_route", None)
        expect(route is not None and route.target == "cloud",
               "档4 非流式路由结果为 cloud", getattr(route, "target", "?"))
        expect(non_stream.content == CLOUD_TEXT, "档4 非流式命中云端上游",
               f"内容={non_stream.content[:60]!r}")

        # 清掉非流式那一轮留在 primary 上的 last_route，否则下面读到的
        # 是上一轮的残留值 —— 那样「流式路由」断言其实什么都没证明。
        gateway.primary.last_route = RouteInfo()
        # 非流式与流式必须各自独立地拿到路由元信息头（X-Vibe-Route-*）。
        streaming = gateway.chat_stream(messages)
        route_stream = getattr(gateway.primary, "last_route", None)
        expect(route_stream is not None and route_stream.target == "cloud",
               "档4 流式路由结果为 cloud", getattr(route_stream, "target", "?"))
        expect(streaming.content == CLOUD_TEXT, "档4 流式命中云端上游",
               f"内容={streaming.content[:60]!r}")
        expect(non_stream.content == streaming.content,
               "档4 非流式与流式文本一致",
               f"{non_stream.content[:30]!r} != {streaming.content[:30]!r}")

        # 密钥托管（E4）：证明「file provider 在真进程里真的被读了」——
        # 哨兵值只存在于临时 secrets.json，它必须原样走到云端上游的
        # Authorization 头。顺带钉住「明文密钥不落服务端日志」。
        time.sleep(0.3)  # 给 pump 线程一点时间把服务端输出抽干净
        cloud_auth = recorded_auth(cloud_records)
        expect(f"Bearer {CLOUD_KEY_SENTINEL}" in cloud_auth,
               "档4 云端上游收到来自密钥文件的 Bearer", f"实收 {cloud_auth[:2]}")
        expect(CLOUD_KEY_SENTINEL not in "\n".join(server_log),
               "档4 明文密钥未出现在服务端日志里")

        # ---------------- 档 5：出站脱敏拦截 ----------------
        say("\n[档5] 正文含 PEM 私钥块 → 必须压回本地且标记 sensitive…")
        sensitive_gateway = open_governed_client(
            config=rt_cloud.config,
            gateway_url=rt_cloud.governance.gateway_base_url(),
            token_provider=rt_cloud.governance.ensure_token,
            fallback_client=None,
            allow_gateway=True,
            task="reasoning",  # 声明重任务，若红线失效它就会上云
        )
        sensitive_resp = sensitive_gateway.chat([{"role": "user", "content": PEM_BLOCK}])
        sensitive_route = getattr(sensitive_gateway.primary, "last_route", None)
        expect(sensitive_route is not None and sensitive_route.sensitive is True,
               "档5 敏感判定为真", getattr(sensitive_route, "sensitive", "?"))
        expect(sensitive_route is not None and sensitive_route.target == "local",
               "档5 路由被压回 local", getattr(sensitive_route, "target", "?"))
        expect(sensitive_resp.content == LOCAL_TEXT, "档5 实际由本地上游答复",
               f"内容={sensitive_resp.content[:60]!r}")
        # 反向对照（E4）：本地上游没配密钥引用，就**不该**带 Authorization ——
        # 否则说明代码在无差别地塞头，档4 那条「Bearer 来自文件」也就不成立了。
        time.sleep(0.2)
        local_auth = recorded_auth(local_records)
        expect(bool(local_auth) and not any(local_auth),
               "档5 本地上游未带 Authorization（未配密钥引用）", f"实收 {local_auth[:2]}")

        # ---------------- 档 6：配额（429 + 降级本地） ----------------
        say("\n[档6] 把用户云端配额调到极小 → 期望 429 语义并降级本地继续…")
        quota_resp = owner_client.request(
            "POST", "/quota",
            body={"scopeType": "user", "scopeId": dev_user_id,
                  "dailyCloudTokens": 100, "dailyRequests": 0, "requestsPerMinute": 0},
        )
        expect(quota_resp.status in (200, 201), "档6 设置极小用户配额成功",
               f"HTTP {quota_resp.status} {quota_resp.body}")

        raw_status, raw_body = http_json(
            server_url + "/api/v1/gateway/v1/chat/completions",
            method="POST",
            payload={"model": "mock-agent-model", "messages": messages, "stream": False},
            token=dev_token,
            headers={"X-Vibe-Task": "reasoning"},
        )
        expect(raw_status == 429, "档6 网关直接返回 429（配额语义）",
               f"HTTP {raw_status} {raw_body}")

        quota_gateway = open_governed_client(
            config=rt_cloud.config,
            gateway_url=rt_cloud.governance.gateway_base_url(),
            token_provider=rt_cloud.governance.ensure_token,
            fallback_client=None,
            allow_gateway=True,
            task="reasoning",
        )
        degraded = quota_gateway.chat(messages)
        notices = " ".join(getattr(quota_gateway, "notices", []))
        expect("QuotaUnavailable" in notices or "QuotaExhausted" in notices,
               "档6 客户端识别为配额类错误（QuotaUnavailable/QuotaExhausted）", notices[:200])
        expect(quota_gateway.using_fallback is True, "档6 已切换到本地回退")
        expect(degraded.content == LOCAL_TEXT, "档6 降级到本地后请求继续完成",
               f"内容={degraded.content[:60]!r}")

        # ---------------- 档 7：审计上报 ----------------
        say("\n[档7] 产生本地审计事件并上报…")
        expect(rt_cloud.uploader is not None, "档7 装配出了审计上传器",
               "；".join(rt_cloud.notices)[:200])

        log = AuditLog(path=rt_cloud.config.audit_path, session_id=rt_cloud.config.session_id)
        audit_mod.record_session_start(log, rt_cloud.config)
        log.record("tool.read_file", path="src/app.py", chars=120, lines="1-20")
        log.record(
            "tool.edit_file", path="src/app.py", strategy="search_replace",
            added=3, removed=1,
            search_digest=audit_mod.digest("old"), replace_digest=audit_mod.digest("new"),
            search_lines=1,
        )
        log.record("agent.run.finish", iterations=2, tool_calls=2,
                   answer_chars=64, answer_digest=audit_mod.digest("answer"))
        expect(rt_cloud.config.audit_path.exists(), "档7 审计日志文件已生成")

        captured: dict[str, Any] = {}
        original_upload = rt_cloud.uploader.client.upload_audit

        def _spy(envelope: dict[str, Any]) -> dict[str, Any]:
            captured.setdefault("envelope", envelope)
            return original_upload(envelope)

        rt_cloud.uploader.client.upload_audit = _spy  # type: ignore[method-assign]
        result = rt_cloud.uploader.upload()
        summary = result.describe()
        say(f"  上传结果: {json.dumps({k: summary[k] for k in ('uploaded', 'accepted', 'rejectedCount', 'clientHeadMatch')}, ensure_ascii=False)}")
        expect(result.uploaded > 0, "档7 uploaded > 0", str(summary))
        expect(len(result.rejected) == 0, "档7 无被拒事件", str(result.rejected[:3]))
        expect(result.client_head_match is not False, "档7 链头校验通过",
               f"clientHeadMatch={result.client_head_match}")

        envelope = captured.get("envelope") or {}
        # 只检查**事件 payload** 里的键名：envelope 的结构键（payload/events/
        # deviceId 等）是协议骨架，不在「正文类字段」的语义范围内；服务端
        # redaction 的黑名单也只作用在事件 payload 上。
        payload_keys: set[str] = set()
        envelope_events = envelope.get("events") if isinstance(envelope, dict) else None
        if isinstance(envelope_events, list):
            for event in envelope_events:
                if isinstance(event, dict) and isinstance(event.get("payload"), dict):
                    payload_keys.update(str(k).lower() for k in event["payload"])
        leaked = sorted(payload_keys & set(redaction.DENIED_KEYS))
        expect(not leaked, "档7 上传载荷不含任何正文类字段", f"命中 {leaked}")
        expect(bool(payload_keys), "档7 上传载荷确实带了 payload 字段（非空壳）",
               f"payload 键={sorted(payload_keys)}")

        events_status, events_body = http_json(
            server_url + "/api/v1/audit/events?sessionId=" + rt_cloud.config.session_id,
            token=owner.access_token,
        )
        stored = events_body.get("events") if isinstance(events_body, dict) else None
        expect(events_status == 200 and isinstance(stored, list) and len(stored) > 0,
               "档7 服务端确实收到了审计事件",
               f"HTTP {events_status} events={len(stored) if isinstance(stored, list) else stored}")

    except Exception as exc:  # noqa: BLE001 —— 冒烟脚本要如实报告任何异常
        failures.append(f"脚本异常: {type(exc).__name__}: {exc}")
        say(f"  [FAIL] 脚本异常: {type(exc).__name__}: {exc}")
    finally:
        # ---------------- 档 8：按进程树清理 + 残留断言 ----------------
        say("\n[档8] 按进程树终止子进程并比对进程快照…")
        for _name, proc in procs:
            kill_tree(proc.pid)
        time.sleep(0.8)
        pids_after = all_pids()
        started = {str(proc.pid) for _name, proc in procs}
        stray = sorted(started & pids_after)
        expect(not stray, "档8 本次启动的进程已全部退出", f"残留 PID {stray}")
        new_python = sorted(
            pid for pid in (pids_after - pids_before)
            if not IS_WINDOWS or pid not in started
        )
        if IS_WINDOWS:
            expect(len(new_python) == 0, "档8 无新增残留进程", f"新增 PID {new_python[:10]}")
        shutil.rmtree(tmp, ignore_errors=True)

    say("")
    if failures:
        say("=== 冒烟失败 ===")
        for item in failures:
            say(f"  ✗ {item}")
    else:
        say("=== 冒烟通过：8 档验收全绿 ===")

    report_path = Path(tempfile.gettempdir()) / "e2e_governance_report.txt"
    try:
        report_path.write_text("\n".join(report), encoding="utf-8")
        print(f"报告已写入 {report_path}")
    except OSError:
        pass
    print(f"退出码 {'1' if failures else '0'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())