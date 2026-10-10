"""会话持久化（A2）端到端冒烟：验收「对话 → 关进程 → 重开 → 恢复 → 继续」。

与 e2e_desktop.py 同一思路：不打桩任何一层。假模型是真实 HTTP 服务，
两次会话之间真的执行了 store.close()（等价于进程退出），第二阶段的
host 是全新构造（等价于进程重启后随机了新的 session_id）。

剧本走 list_dir（SAFE 级自动放行），不涉及审批会合点，冒烟保持精简。

用法：
    python scripts/e2e_session.py
结果除 stdout 外还会写入 %TEMP%/e2e_session_report.txt（后台任务吞 stdout 时的兜底）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_core.config import Config  # noqa: E402
from agent_core.host.server import HostServer  # noqa: E402
from agent_core.host.store import SessionStore  # noqa: E402

PYTHON = sys.executable
MOCK_PORT = 11603

# 第 1 轮消费前两步（工具 + 文本），恢复后的「继续」消费第三步
SCRIPT = [
    {"tool": "list_dir", "args": {"path": "."}},
    {"text": "第一轮完成：项目根目录下有 src、tests 等条目。"},
    {"text": "恢复成功：我仍然记得第一轮看过项目根目录（src、tests 等条目）。"},
]

_report: list[str] = []


def say(line: str = "") -> None:
    print(line)
    _report.append(line)


class Http:
    def __init__(self, base: str, token: str) -> None:
        self.base = base
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(self, path: str) -> tuple[int, dict]:
        request = urllib.request.Request(self.base + path)
        request.add_header("X-Vibe-Token", self.token)
        return self._go(request)

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        request.add_header("X-Vibe-Token", self.token)
        return self._go(request)

    def _go(self, request: urllib.request.Request) -> tuple[int, dict]:
        try:
            with self.opener.open(request, timeout=15) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


def wait_idle(http: Http, what: str) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        _, state = http.get("/api/state")
        if state.get("state") == "idle":
            return
        time.sleep(0.05)
    raise SystemExit(f"{what} 未在超时内结束")


def start_mock() -> subprocess.Popen:
    proc = subprocess.Popen(
        [PYTHON, str(ROOT / "scripts" / "mock_model_server.py"),
         "--port", str(MOCK_PORT), "--script", str(SCRIPT_PATH)],
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


def main() -> int:
    global SCRIPT_PATH

    workspace = ROOT.parents[1] / "examples" / "demo-project"
    tmp = tempfile.TemporaryDirectory()
    SCRIPT_PATH = Path(tmp.name) / "script.json"
    SCRIPT_PATH.write_text(json.dumps(SCRIPT, ensure_ascii=False), encoding="utf-8")
    db = Path(tmp.name) / "sessions.db"

    say("=== A2 会话持久化 E2E 冒烟 ===")
    say(f"工作区: {workspace}")
    say(f"库文件: {db}")

    mock = start_mock()
    failures: list[str] = []
    try:
        model_url = f"http://127.0.0.1:{MOCK_PORT}/v1"

        # ---------------- 第一阶段：对话后「关进程」 ----------------
        config = Config.load(
            workspace, base_url=model_url, model="mock-agent-model", api_key="mock"
        )
        store = SessionStore(db)
        host = HostServer(config, port=0, client=None, store=store)
        host.start_background()
        http = Http(f"http://127.0.0.1:{host.port}", host.guard.token)
        say("\n[阶段1] 提交任务「看看项目结构」…")
        status, _ = http.post("/api/run", {"text": "看看项目结构"})
        assert status == 200, f"/api/run -> {status}"
        wait_idle(http, "阶段1任务")

        sid = config.session_id
        status, listing = http.get("/api/sessions")
        rows = listing.get("sessions", [])
        say(f"[阶段1] 会话列表: {[(r['id'], r['title']) for r in rows]}")
        if status != 200 or [r["id"] for r in rows] != [sid]:
            failures.append("阶段1 会话列表不符合预期")
        if rows and rows[0]["title"] != "看看项目结构":
            failures.append(f"阶段1 标题错误: {rows[0]['title']!r}")

        host.shutdown()  # HostServer.shutdown 会一并 close store —— 等价进程退出
        say("[阶段1] host 已关闭（store 已落盘并关闭）")

        # ---------------- 第二阶段：全新进程「重开并恢复」 ----------------
        say("\n[阶段2] 全新 Config（新随机 session_id）+ 同一个库，重新起 host …")
        config2 = Config.load(
            workspace, base_url=model_url, model="mock-agent-model", api_key="mock"
        )
        if config2.session_id == sid:
            failures.append("阶段2 本应产生新的随机 session_id")
        store2 = SessionStore(db)
        host2 = HostServer(config2, port=0, client=None, store=store2)
        host2.start_background()
        http = Http(f"http://127.0.0.1:{host2.port}", host2.guard.token)

        status, listing = http.get("/api/sessions")
        ids = [r["id"] for r in listing.get("sessions", [])]
        say(f"[阶段2] 重开后会话列表可见: {ids}")
        if sid not in ids:
            failures.append("重开后历史会话不可见")

        status, detail = http.get(f"/api/sessions/{sid}/messages")
        roles = [m["role"] for m in detail.get("messages", [])]
        say(f"[阶段2] 历史消息角色序列: {roles}")
        if status != 200 or roles != ["user", "assistant", "tool", "assistant"]:
            failures.append(f"历史消息形态错误: {status} {roles}")

        status, body = http.post(f"/api/sessions/{sid}/resume", {})
        say(f"[阶段2] resume -> {status} {body}")
        if status != 200 or not body.get("ok") or body.get("restored") != 4:
            failures.append(f"resume 失败: {status} {body}")

        _, state = http.get("/api/state")
        if state.get("sessionId") != sid:
            failures.append(f"恢复后 sessionId 未切换: {state.get('sessionId')!r}")

        say("[阶段2] 恢复完成，继续对话「继续」…")
        status, _ = http.post("/api/run", {"text": "继续"})
        assert status == 200, f"恢复后 /api/run -> {status}"
        wait_idle(http, "阶段2任务")

        status, detail = http.get(f"/api/sessions/{sid}/messages")
        messages = detail.get("messages", [])
        roles = [m["role"] for m in messages]
        answer = messages[-1]["content"] if messages else ""
        say(f"[阶段2] 续聊后消息数: {len(messages)} 角色序列: {roles}")
        say(f"[阶段2] 最终回答: {answer[:80]}")
        if roles != ["user", "assistant", "tool", "assistant", "user", "assistant"]:
            failures.append(f"续聊后角色序列错误: {roles}")
        if "恢复成功" not in answer or "第一轮" not in answer:
            failures.append("恢复后的回答没有引用上一轮上下文（剧本第三步未命中）")
        title = store2.get_session(sid)["title"]
        if title != "看看项目结构":
            failures.append(f"续聊覆盖了原标题: {title!r}")

        host2.shutdown()
    finally:
        mock.terminate()
        if failures:
            say("\n=== 失败项 ===")
            for item in failures:
                say(f"  ✗ {item}")
        else:
            say("\n=== 冒烟通过 ===")
        tmp.cleanup()

    report = Path(tempfile.gettempdir()) / "e2e_session_report.txt"
    report.write_text("\n".join(_report), encoding="utf-8")
    print(f"报告已写入 {report}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
