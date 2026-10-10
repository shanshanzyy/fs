"""桌面端端到端冒烟：起一个真实的 host 服务，用 HTTP 驱动完整闭环。

这个脚本刻意**不打桩任何一层** —— 假模型是真的 HTTP 服务，事件流是真的 SSE，
审批是真的走 `/api/approval`。要验证的正是这些真实边界，
用 mock 掉就没意义了。

用法：
    python scripts/e2e_desktop.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_core.config import Config  # noqa: E402
from agent_core.host.server import HostServer  # noqa: E402

PYTHON = sys.executable
MOCK_PORT = 11601


def start_mock() -> subprocess.Popen:
    proc = subprocess.Popen(
        [PYTHON, str(ROOT / "scripts" / "mock_model_server.py"), "--port", str(MOCK_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{MOCK_PORT}/v1/models", timeout=0.4
            ).read()
            return proc
        except Exception:
            time.sleep(0.15)
    proc.terminate()
    raise SystemExit("假模型服务没能启动")


def post(url: str, payload: dict, token: str) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Vibe-Token": token},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    workspace = ROOT.parents[1] / "examples" / "demo-project"
    print(f"工作区: {workspace}")

    mock = start_mock()
    config = Config.load(
        workspace,
        base_url=f"http://127.0.0.1:{MOCK_PORT}/v1",
        model="mock-agent-model",
        api_key="mock",
    )

    host = HostServer(config, port=0)
    host.start_background()
    token = host.guard.token
    base = f"http://127.0.0.1:{host.port}"
    print(f"host 服务: {base}")

    events: list[dict] = []
    stop = threading.Event()
    approved = {"done": False}

    def read_events() -> None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(
            f"{base}/api/events?token={token}",
            headers={"Accept": "text/event-stream", "X-Vibe-Token": token},
        )
        with opener.open(request, timeout=60) as response:
            event_type = None
            for raw in response:
                if stop.is_set():
                    return
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: "):
                    payload = json.loads(line[6:])
                    events.append(payload)
                    print(f"  · {event_type}")
                    if event_type == "run.approval" and not approved["done"]:
                        approved["done"] = True
                        decisions.append(payload)

    decisions: list[dict] = []
    reader = threading.Thread(target=read_events, daemon=True)
    reader.start()
    time.sleep(0.6)

    print("\n提交任务…")
    post(f"{base}/api/run", {"text": "修复 average 遇到空列表崩溃的问题"}, token)

    # 等审批请求出现，然后批准
    for _ in range(120):
        if decisions:
            break
        time.sleep(0.25)

    if decisions:
        pending = decisions[0]
        print(f"\n收到审批请求: {pending['tool']}  风险={pending['risk']}")
        print(f"  摘要: {pending['preview']['summary']}")
        print(f"  预览类型: {pending['preview']['kind']}")
        diff_lines = pending["preview"]["body"].splitlines()
        print(f"  diff 行数: {len(diff_lines)}")
        for line in diff_lines[:12]:
            print(f"    {line}")
        print("\n批准…")
        post(f"{base}/api/approval", {"approvalId": pending["approvalId"], "decision": "approve"}, token)
    else:
        print("!! 没有收到审批请求")
        stop.set()
        host.shutdown()
        mock.terminate()
        return 1

    for _ in range(160):
        if any(e.get("type") in ("run.finished", "run.failed", "run.interrupted") for e in events):
            break
        time.sleep(0.25)

    stop.set()
    time.sleep(0.3)

    finished = [e for e in events if e.get("type") == "run.finished"]
    print("\n=== 结果 ===")
    if finished:
        result = finished[0]
        print("回答:", result["answer"][:200])
        print("统计:", json.dumps(result["stats"], ensure_ascii=False))
        print("改动文件:", result["changedFiles"])
    else:
        print("未收到 run.finished，事件类型序列：")
        print([e.get("type") for e in events])

    print("\n=== 服务端视角 ===")
    print("变更记录:", json.dumps(host.session.diffs(), ensure_ascii=False, indent=2)[:600])
    report = host.session.audit_report()
    print("审计链:", report["detail"])

    host.shutdown()
    mock.terminate()
    return 0 if finished else 1


if __name__ == "__main__":
    raise SystemExit(main())
