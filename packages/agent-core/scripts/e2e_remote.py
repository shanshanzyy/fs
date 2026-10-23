"""远端链路**真机冒烟**：真实 SSH + 真实远端文件系统（M11 / ADR-038）。

与 `e2e_package.py` 同属「**手动执行、不进 CI**」的范式 —— 它需要一个你自有的
可登录远端设备与授权凭据，CI 里不会有。缺席时**如实报错退出**，不假装通过。

它验的是注入式 `FakeTransport` 单测覆盖不到的那一段：ssh 真握手、远端真落盘、
真回读、真留底。范围限定在远端一个临时目录内（默认 `/tmp/vibe-m11-smoke`），
末端**强制清理**（除非 `--keep`）。

用法：

```bash
cd packages/agent-core
python scripts/e2e_remote.py --target root@10.0.1.50 --key ~/.ssh/id_ed25519_remote
# 或走环境变量（CI/批处理里更顺手）
VIBE_REMOTE_SMOKE_TARGET=root@host VIBE_REMOTE_SMOKE_KEY=~/.ssh/id_ed25519 \\
    python scripts/e2e_remote.py
```

覆盖：远端环境 → 起 host（VIBE_REMOTE=1 门控）→ 连接 add/test/open → tree/file/save
→ changes/diff（留底 vs 远端）→ 负面路径（乐观锁 409、越界 403、二进制/超限 400、只读拒写）
→ patch/create → 审计只记元数据 → 清理与残留检查。

**未覆盖**：`remote_review_changes` 只读自审工具（需模型驱动一次 `run`，由单测覆盖代替）；
Agent 侧 `remote_edit` / `remote_run_command`（属 M10 既有链路，非本工作面）。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # VibeCoding/
AGENT_CORE = ROOT / "packages" / "agent-core"
CONN_NAME = "smoke"
DEFAULT_REMOTE_ROOT = "/tmp/vibe-m11-smoke"

FILE_OLD = (
    '"""demo module for the remote real-machine smoke."""\n'
    "\n"
    "\n"
    "def average(values):\n"
    "    total = 0\n"
    "    for value in values:\n"
    "        total += value\n"
    "    return total\n"
)
FILE_NEW = FILE_OLD.replace("    total = 0\n", "    total = 0.0\n    # smoke edit\n")
NEW_FILE = "generated_by_smoke.py\nvalue = 42\n"

RESULTS: list[tuple[str, bool, str]] = []
proc: subprocess.Popen[str] | None = None
# 运行时填：见 main()
SSH_TARGET = ""
SSH_KEY = Path()
SSH_PORT = 22
REMOTE_ROOT = DEFAULT_REMOTE_ROOT
WS = Path()
CONFIG = Path()
BASE = ""
TOKEN = ""


def check(name: str, ok: bool, detail: str = "", *, fatal: bool = True) -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok and fatal:
        raise SystemExit(f"真机冒烟中断：{name} — {detail}")


def ssh(remote_cmd: str, *, check_rc: bool = True) -> str:
    argv = [
        "ssh",
        "-i", str(SSH_KEY),
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "LogLevel=ERROR",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "IdentitiesOnly=yes",
        "-p", str(SSH_PORT),
        SSH_TARGET,
        remote_cmd,
    ]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    if check_rc and done.returncode != 0:
        raise RuntimeError(
            f"远端命令失败 rc={done.returncode}: {remote_cmd}\n{done.stderr.strip()[:400]}"
        )
    return done.stdout


def write_remote(rel: str, text: str) -> None:
    """字节精确写远端文件（走 base64，绕开 Windows 侧 stdin 的换行转换）。"""
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    ssh(f"printf '%s' '{b64}' | base64 -d > {REMOTE_ROOT}/{rel}")


def http(method: str, path: str, *, body: object | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("X-Vibe-Token", TOKEN)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def start_host() -> None:
    global proc, BASE, TOKEN
    env = {
        **os.environ,
        "PYTHONPATH": str(AGENT_CORE),
        "VIBE_REMOTE": "1",
        "VIBE_REMOTE_CONFIG": str(CONFIG),
        "VIBE_MEMORY": "0",
        "VIBE_SETTINGS": str(WS / "settings.json"),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_core.host",
         "--workspace", str(WS), "--port", "0", "--print-url", "--no-open"],
        cwd=str(AGENT_CORE), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    url = ""
    deadline = time.time() + 60
    while time.time() < deadline:
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            if proc.poll() is not None:
                break
            continue
        if line.startswith("VIBE_URL="):
            url = line.strip().split("=", 1)[1]
            break
    if not url:
        err = proc.stderr.read() if proc.stderr else ""
        raise SystemExit(f"没等到 VIBE_URL。stderr 摘要：\n{err[:800]}")
    head, _, token = url.partition("/?token=")
    BASE, TOKEN = head, token
    print(f"  host 已起：{BASE}（token 长度 {len(TOKEN)}）")


def kill_host() -> None:
    if proc is None or proc.poll() is not None:
        return
    # 必须按进程树杀：Windows 上 child.kill() 只终止直接子进程。
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                   capture_output=True, text=True)
    proc.wait(timeout=30)


def run_smoke(keep: bool) -> None:
    print("=== 0. 远端环境（ssh 直连，只读） ===")
    env_out = ssh(
        "uname -srm; id -un; python3 -V 2>&1; "
        "command -v stat find head ls realpath | tr '\\n' ' '; echo"
    ).strip()
    print("  " + env_out.replace("\n", " | "))
    check("远端为 Linux 环境", "Linux" in env_out)

    print("=== 1. 远端测试目录与样例文件 ===")
    ssh(f"rm -rf {REMOTE_ROOT} && mkdir -p {REMOTE_ROOT}/pkg")
    write_remote("demo.py", FILE_OLD)
    # 用 /dev/zero 造二进制（保证头 4096 字节含 NUL），不依赖 /bin/sh 的 printf 转义方言。
    ssh(f"head -c 64 /dev/zero > {REMOTE_ROOT}/bin.dat")
    ssh(f"head -c 3000000 /dev/zero | tr '\\000' a > {REMOTE_ROOT}/big.txt")
    listed = ssh(f"ls -la {REMOTE_ROOT}").strip()
    print("  " + " | ".join(listed.splitlines()[:2]))
    check("远端样例文件已就绪",
          all(n in listed for n in ("demo.py", "bin.dat", "big.txt")))

    print("=== 2. 起 host（VIBE_REMOTE=1）并验端点门控 ===")
    start_host()
    st, _ = http("GET", "/api/health")
    check("/api/health 200", st == 200, f"status={st}")
    st, state = http("GET", "/api/state")
    check("/api/state.config.remoteEnabled=true（门控已开）",
          st == 200 and bool((state.get("config") or {}).get("remoteEnabled")))

    print("=== 3. 连接管理：add → test → open ===")
    st, body = http("GET", "/api/remote/connections")
    check("清单初始为空", st == 200 and body.get("connections") == [], f"status={st}")

    target_user, _, target_host = SSH_TARGET.rpartition("@")
    spec = {
        "name": CONN_NAME,
        "user": target_user,
        "host": target_host or SSH_TARGET,
        "port": SSH_PORT,
        "root": REMOTE_ROOT,
        "identity_file": str(SSH_KEY),
        "host_key_policy": "accept-new",
        "allow_write": True,
        "allow_shell": False,
        "note": "远程真机冒烟（可清理）",
    }
    st, body = http("POST", "/api/remote/connections", body={"connection": spec})
    check("新增连接 200", st == 200 and body.get("ok") is True, f"status={st} body={str(body)[:200]}")
    check("清单文件已落盘", CONFIG.is_file(), str(CONFIG))

    st, body = http("POST", f"/api/remote/connections/{CONN_NAME}/test", body={})
    caps = body.get("capabilities") or {}
    degraded = list(caps.get("degraded") or [])
    print(f"  caps: os={caps.get('os')} kernel={caps.get('kernel')} user={caps.get('user')} "
          f"python={caps.get('python')} multiplexed={caps.get('multiplexed')} "
          f"root_writable={caps.get('root_writable')}")
    print(f"  degraded: {degraded}")
    check("test 连通 ok", st == 200 and body.get("ok") is True,
          f"status={st} {body.get('reason', '')}")
    check("root 解析为远端测试目录", body.get("root") == REMOTE_ROOT, str(body.get("root")))
    check("能力降级只有「连接复用不可用」（Windows 客户端预期）",
          all("连接复用不可用" in item for item in degraded) and len(degraded) <= 1,
          f"degraded={degraded}", fatal=False)

    st, body = http("POST", "/api/remote/open", body={"connection": CONN_NAME})
    check("open 200 且回快照",
          st == 200 and body.get("ok") is True and body.get("connection") == CONN_NAME,
          f"status={st}")

    print("=== 4. 文件面：tree → file → save ===")
    st, body = http("GET", "/api/remote/tree?path=")
    names = [e.get("name") for e in body.get("entries") or []]
    check("tree 列出远端目录一层",
          st == 200 and "demo.py" in names and "pkg" in names, str(names))
    kinds = [e.get("type") for e in (body.get("entries") or [])]
    check("目录优先排序", kinds[:1] == ["dir"] if kinds else False, str(kinds[:3]), fatal=False)

    st, body = http("GET", "/api/remote/file?path=demo.py")
    check("file 只读查看 200", st == 200 and body.get("text") == FILE_OLD, f"status={st}")
    d0 = str(body.get("digest") or "")
    print(f"  encoding={body.get('encoding')} newline={body.get('newline')} "
          f"lines={body.get('lines')} digest={d0}")
    check("远端内容带 sha256 指纹（乐观锁基准）", len(d0) == 16)
    check("换行风格如实识别（样例为 LF）", body.get("newline") == "LF", str(body.get("newline")))

    st, body = http("POST", "/api/remote/save",
                    body={"path": "demo.py", "content": FILE_NEW, "expectedDigest": d0})
    diff = str(body.get("diff") or "")
    check("save 200 且 changed=true / action=save",
          st == 200 and body.get("ok") is True and body.get("changed") is True
          and body.get("action") == "save", f"status={st} body={str(body)[:200]}")
    check("save 返回真实 diff（含 -/+ 行）",
          "-    total = 0\n" in diff and "+    total = 0.0\n" in diff, diff[:200])
    d1 = str(body.get("digest") or "")
    print(f"  added={body.get('added')} removed={body.get('removed')} "
          f"bytes={body.get('bytes')} digest={d1}")
    check("远端真落盘（ssh 回读 == 新内容）", ssh(f"cat {REMOTE_ROOT}/demo.py") == FILE_NEW)

    print("=== 5. 审查面：changes → diff（留底 vs 远端） ===")
    st, body = http("GET", "/api/remote/changes")
    entry = next((r for r in (body.get("changes") or []) if r.get("rel_path") == "demo.py"), None)
    check("台账含本次改动", st == 200 and entry is not None,
          json.dumps(body, ensure_ascii=False)[:200])
    check("台账记录留底（backed_up + backup_path）",
          bool(entry) and entry.get("backed_up") is True and bool(entry.get("backup_path")),
          json.dumps(entry, ensure_ascii=False)[:200])
    backup = Path(str(entry.get("backup_path"))) if entry else None
    check("本机留底文件真实存在", bool(backup and backup.is_file()), str(backup))
    check("留底内容 == 改动前的远端内容（先留底后改动）",
          bool(backup and backup.is_file() and backup.read_text(encoding="utf-8") == FILE_OLD),
          str(backup))
    check("留底落在 .vibe/checkpoints/remote/<连接>/<会话>/ 下",
          bool(backup) and {"checkpoints", "remote", CONN_NAME} <= set(backup.parts),
          str(backup))

    st, body = http("GET", f"/api/remote/diff?connection={CONN_NAME}&path=demo.py")
    hunks = body.get("hunks") or []
    check("diff available=true", st == 200 and body.get("available") is True,
          f"status={st} body={str(body)[:200]}")
    check("diff 含 hunk（逐块，字段齐）",
          bool(hunks) and all(
              {"index", "tag", "header", "oldStart", "oldEnd", "oldLines", "newLines",
               "added", "removed", "diff"} <= set(h) for h in hunks),
          json.dumps(hunks[:1], ensure_ascii=False)[:200])
    check("hunk oldStart 是 1 基口径",
          bool(hunks) and int(hunks[0]["oldStart"]) >= 1,
          str(hunks[0]["oldStart"]) if hunks else "")

    print("=== 6. 负面路径 ===")
    st, body = http("POST", "/api/remote/save",
                    body={"path": "demo.py", "content": FILE_OLD, "expectedDigest": d0})
    check("过期 digest → 409 乐观锁", st == 409, f"status={st} body={str(body)[:160]}")
    check("409 后远端**未被覆盖**", ssh(f"cat {REMOTE_ROOT}/demo.py") == FILE_NEW)

    st, body = http("GET", "/api/remote/file?path=../../../etc/passwd")
    check("越界读取被拒（4xx，无内容泄漏）",
          st in (400, 403) and "root:" not in json.dumps(body),
          f"status={st} body={str(body)[:160]}")
    st, _ = http("GET", "/api/remote/tree?path=../../..")
    check("越界列目录被拒（4xx）", st in (400, 403), f"status={st}")

    st, body = http("GET", "/api/remote/file?path=bin.dat")
    check("二进制文件拒绝预览（400）", st == 400, f"status={st} body={str(body)[:160]}")

    st, body = http("GET", "/api/remote/file?path=big.txt")
    check("超限（MAX_SCAN_BYTES）明确拒绝（400）", st == 400, f"status={st} body={str(body)[:160]}")

    st, _ = http("POST", f"/api/remote/connections/{CONN_NAME}/permissions",
                 body={"allowWrite": False})
    check("权限位热改（allow_write=false）200", st == 200, f"status={st}")
    st, body = http("POST", "/api/remote/save",
                    body={"path": "demo.py", "content": FILE_OLD, "expectedDigest": d1})
    check("只读连接拒写（4xx）", st in (400, 403), f"status={st} body={str(body)[:160]}")
    check("拒写后远端未动", ssh(f"cat {REMOTE_ROOT}/demo.py") == FILE_NEW)
    st, _ = http("POST", f"/api/remote/connections/{CONN_NAME}/permissions",
                 body={"allowWrite": True})
    check("权限位热改回 allow_write=true 200", st == 200, f"status={st}")

    print("=== 7. patch（三级匹配）与 create（无留底） ===")
    st, body = http("POST", "/api/remote/patch",
                    body={"path": "demo.py", "search": "    return total\n",
                          "replace": "    return round(total, 2)\n"})
    check("patch 200 且 changed=true", st == 200 and body.get("changed") is True,
          f"status={st} body={str(body)[:200]}")
    check("patch 已真落盘（ssh 回读）",
          "round(total, 2)" in ssh(f"cat {REMOTE_ROOT}/demo.py"))

    st, body = http("POST", "/api/remote/save",
                    body={"path": "generated.py", "content": NEW_FILE, "expectedDigest": ""})
    check("新建文件 action=create", st == 200 and body.get("action") == "create",
          f"status={st} body={str(body)[:200]}")
    check("新建文件已真落盘", ssh(f"cat {REMOTE_ROOT}/generated.py") == NEW_FILE)
    st, body = http("GET", "/api/remote/changes")
    created = next(
        (r for r in (body.get("changes") or []) if r.get("rel_path") == "generated.py"), None
    )
    check("新建无留底（backed_up=false）",
          bool(created) and created.get("backed_up") is False,
          json.dumps(created, ensure_ascii=False)[:200])
    st, body = http("GET", f"/api/remote/diff?connection={CONN_NAME}&path=generated.py")
    check("新建文件 diff 按全新增处理（available=true）",
          st == 200 and body.get("available") is True and body.get("removed") == 0,
          f"status={st} removed={body.get('removed')}")

    print("=== 8. 审计：只记元数据（不含远端正文与 diff 正文） ===")
    audit = WS / ".vibe" / "audit.jsonl"
    check("审计文件已生成", audit.is_file(), str(audit))
    text = audit.read_text(encoding="utf-8", errors="replace") if audit.is_file() else ""
    check("审计含 remote.connection 事件", '"remote.connection"' in text)
    check("审计含 remote.file 事件", '"remote.file"' in text)
    check("审计**不含**远端正文（def average / total += value）",
          "def average" not in text and "total += value" not in text)
    check("审计**不含** diff 正文（+++ / --- / @@）",
          "+++" not in text and "--- " not in text and "@@" not in text)

    print("=== 9. 清理与残留检查 ===")
    st, body = http("POST", f"/api/remote/connections/{CONN_NAME}/delete", body={})
    check("删除连接 200", st == 200 and body.get("ok") is True, f"status={st}")
    if keep:
        print(f"  --keep：保留远端 {REMOTE_ROOT}（请自行清理）")
    else:
        ssh(f"rm -rf {REMOTE_ROOT}")
        check("远端测试目录已清理",
              ssh(f"test -d {REMOTE_ROOT} && echo EXISTS || echo GONE").strip() == "GONE")


def main() -> int:
    global SSH_TARGET, SSH_KEY, SSH_PORT, REMOTE_ROOT, WS, CONFIG

    parser = argparse.ArgumentParser(description="远端链路真机冒烟（M11，手动执行）")
    parser.add_argument("--target", default=os.environ.get("VIBE_REMOTE_SMOKE_TARGET", ""),
                        help="[user@]host（或环境变量 VIBE_REMOTE_SMOKE_TARGET）")
    parser.add_argument("--key", default=os.environ.get("VIBE_REMOTE_SMOKE_KEY", ""),
                        help="本地私钥路径（或环境变量 VIBE_REMOTE_SMOKE_KEY）")
    parser.add_argument("--port", type=int, default=22, help="SSH 端口（默认 22）")
    parser.add_argument("--root", default=DEFAULT_REMOTE_ROOT,
                        help=f"远端测试根目录（默认 {DEFAULT_REMOTE_ROOT}）")
    parser.add_argument("--keep", action="store_true", help="保留远端测试目录（默认删除）")
    args = parser.parse_args()

    if not args.target:
        raise SystemExit(
            "--target 必填（或设 VIBE_REMOTE_SMOKE_TARGET）：需要一个你自有的可登录远端设备。\n"
            "  这条链路需外部设备与凭据，不进自动回归；无设备时**如实退出**，不假装通过。"
        )
    SSH_TARGET = args.target

    key = Path(os.path.expanduser(args.key)) if args.key else Path.home() / ".ssh" / "id_ed25519"
    if not key.is_file():
        raise SystemExit(f"找不到身份文件：{key}（用 --key 指定，或设 VIBE_REMOTE_SMOKE_KEY）")
    SSH_KEY = key
    SSH_PORT, REMOTE_ROOT = args.port, args.root

    tag = f"{os.getpid()}"
    WS = Path(os.environ.get("TEMP", ".")) / f"vibe_remote_smoke_{tag}"
    CONFIG = WS / "remote.json"  # VIBE_REMOTE_CONFIG → 不碰任何既有连接清单
    if WS.exists():
        shutil.rmtree(WS, ignore_errors=True)
    WS.mkdir(parents=True, exist_ok=True)
    # 远端会话留底落在这个工作区里；冒烟结束一并清掉（除非 --keep）。
    print(f"  目标 {SSH_TARGET}:{SSH_PORT}  身份 {SSH_KEY}  远端根 {REMOTE_ROOT}")
    print(f"  本机工作区 {WS}")

    try:
        run_smoke(args.keep)
    finally:
        kill_host()
        if not args.keep:
            shutil.rmtree(WS, ignore_errors=True)

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n=== 真机冒烟{'通过' if not failed else '失败'}："
          f"{len(RESULTS) - len(failed)}/{len(RESULTS)} 项 PASS ===")
    for name, _, detail in failed:
        print(f"  FAIL: {name} — {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
