"""桌面端打包冒烟（D1.2）：对**真实分发包**做端到端验收。

验收链路：打包产物 → 启动 → 健康检查 → mock 模型跑通一轮对话 → 截图 → 退出。
跑两遍，分别验证 D1.1 的两条后端来源：

* 第一遍：把 `VIBE_PYTHON` 指向一个不存在的解释器 —— 若还能跑起来，
  说明用的是**内嵌二进制**，即「机器上没装 Python 也能用」（验收目标）；
* 第二遍：`VIBE_NO_EMBEDDED=1` 跳过内嵌二进制 —— 验证回退系统 Python 的路径仍然可用。
  这一遍是在**分发目录**里跑的，产物旁边没有 agent_core 源码，因此必须显式给出
  解释器与包路径；它验证的是「回退分支没被破坏」，而不是「干净机器也能回退」
  （后者没有意义：机器上没 Python 时，能用的只有内嵌二进制）。

两遍都必须拿到 `run.finished`、截到图，且后端上报的版本号与
`agent_core.__version__`、`resources/backend/version.json` 三处一致。

用法：
    python scripts/e2e_package.py                     # 用 dist/desktop 下最新的分发目录
    python scripts/e2e_package.py --dist <目录>
    python scripts/e2e_package.py --build             # 先重新打包再冒烟
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
AGENT_CORE = ROOT / "packages" / "agent-core"
DIST_ROOT = ROOT / "dist" / "desktop"
sys.path.insert(0, str(AGENT_CORE))

from agent_core import __version__  # noqa: E402

PYTHON = sys.executable
MOCK_PORT = 11605
BACKEND_EXE = "vibe-agent-core.exe"  # 内嵌后端的进程名（PyInstaller onefile 成形后）

# 冒烟剧本只用 SAFE 级工具，不触发审批 —— 打包冒烟要验证的是
# 「分发包能不能起来并跑完一轮」，审批在 e2e_desktop 里另有覆盖。
SMOKE_SCRIPT = [
    {"tool": "repo_map", "args": {"path": "."}},
    {"tool": "read_file", "args": {"path": "calc.py"}},
    {"text": "打包冒烟：我已经看过工作区结构并读取了 calc.py，链路正常。"},
]

WORKSPACE_FILE = "def add(a, b):\n    return a + b\n"


def find_dist(explicit: str | None) -> Path:
    if explicit:
        target = Path(explicit).resolve()
        if not target.is_dir():
            raise SystemExit(f"分发目录不存在: {target}")
        return target
    if not DIST_ROOT.is_dir():
        raise SystemExit(f"还没有分发目录 {DIST_ROOT}，请先执行 scripts/build_desktop.py")
    candidates = sorted(
        (p for p in DIST_ROOT.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime
    )
    if not candidates:
        raise SystemExit(f"{DIST_ROOT} 下没有分发目录，请先执行 scripts/build_desktop.py")
    return candidates[-1]


def start_mock(port: int, script_path: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [PYTHON, str(AGENT_CORE / "scripts" / "mock_model_server.py"),
         "--port", str(port), "--script", str(script_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=0.4).read()
            return proc
        except Exception:  # noqa: BLE001
            time.sleep(0.15)
    proc.terminate()
    raise SystemExit("假模型服务没能启动")


def backend_pids() -> set[str]:
    """当前在跑的内嵌后端进程 PID（非 Windows 返回空集）。"""
    if not sys.platform.startswith("win"):
        return set()
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {BACKEND_EXE}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    pids: set[str] = set()
    for line in out.splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) >= 2 and parts[0].lower() == BACKEND_EXE:
            pids.add(parts[1])
    return pids


def kill_pids(pids: set[str]) -> None:
    for pid in pids:
        subprocess.run(
            ["taskkill", "/PID", pid, "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def run_smoke(
    dist: Path,
    workspace: Path,
    report_path: Path,
    shot_path: Path,
    env_extra: dict[str, str],
    timeout_s: int = 150,
) -> dict:
    if report_path.exists():
        report_path.unlink()
    exe_name = "VibeAgent.exe" if sys.platform.startswith("win") else "VibeAgent"
    exe = dist / exe_name
    if not exe.is_file():
        raise SystemExit(f"分发目录里没有主程序: {exe}")

    env = {
        **os.environ,
        "VIBE_SMOKE": "1",
        "VIBE_MODEL": "mock-agent-model",
        "VIBE_BASE_URL": f"http://127.0.0.1:{MOCK_PORT}/v1",
        "VIBE_API_KEY": "mock",
        "VIBE_SMOKE_WORKSPACE": str(workspace),
        "VIBE_SMOKE_TASK": "看看项目结构，并读一下 calc.py。",
        "VIBE_SMOKE_REPORT": str(report_path),
        "VIBE_SMOKE_SCREENSHOT": str(shot_path),
    }
    # 先清掉调用方的 PYTHONPATH：第一遍要证明内嵌二进制不依赖它；
    # 之后再叠加 env_extra，让回退用例可以显式给出解释器与包路径。
    env.pop("PYTHONPATH", None)
    env.update(env_extra)

    before = backend_pids()  # 排除掉本就在跑的后端，避免误判成我们留下的
    proc = subprocess.Popen(
        [str(exe)],
        cwd=str(dist),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # 超时也要连子进程一起清掉：Electron 会派生 GPU/渲染进程，
        # 只 kill 主进程会留下它们继续握着管道，下一次运行就更容易卡。
        if sys.platform.startswith("win"):
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            proc.kill()
        proc.communicate()
        raise SystemExit(f"冒烟超时（{timeout_s}s），进程树已强杀")

    # 退出必须干净：内嵌后端是 PyInstaller onefile（引导进程 + 服务进程两层），
    # 只终止引导进程会把服务进程留成孤儿 —— 它照样占端口、照样往工作区写审计，
    # 而这一点在报告里完全看不出来，所以在这里单独验一次。
    strays = backend_pids() - before
    if strays:
        kill_pids(strays)
        raise SystemExit(f"冒烟退出后仍残留后端进程（PID {', '.join(sorted(strays))}）")

    if not report_path.is_file():
        raise SystemExit(
            "冒烟没有产出报告。\n"
            f"stdout: {stdout.decode('utf-8', 'replace')[-1500:]}\n"
            f"stderr: {stderr.decode('utf-8', 'replace')[-1500:]}"
        )
    return json.loads(report_path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="桌面端打包冒烟（D1.2）")
    parser.add_argument("--dist", help="分发目录（默认 dist/desktop 下最新）")
    parser.add_argument("--build", action="store_true", help="先重新打包再冒烟")
    args = parser.parse_args(argv)

    if args.build:
        subprocess.run(
            [PYTHON, str(AGENT_CORE / "scripts" / "build_desktop.py")],
            cwd=str(AGENT_CORE), check=True,
        )

    dist = find_dist(args.dist)
    print(f"=== 桌面端打包冒烟 ===\n分发目录: {dist}")

    version_file = dist / "resources" / "backend" / "version.json"
    if not version_file.is_file():
        raise SystemExit(f"缺少版本文件 {version_file}")
    stamped = json.loads(version_file.read_text(encoding="utf-8"))
    if stamped["core"] != __version__:
        raise SystemExit(f"产物版本 {stamped['core']} 与核心版本 {__version__} 不一致")
    print(f"产物版本: {stamped['core']}（与核心 {__version__} 一致）")

    tmp = Path(tempfile.mkdtemp(prefix="vibe_d1_"))
    workspace = tmp / "workspace"
    workspace.mkdir()
    (workspace / "calc.py").write_text(WORKSPACE_FILE, encoding="utf-8", newline="")
    script_path = tmp / "script.json"
    script_path.write_text(json.dumps(SMOKE_SCRIPT, ensure_ascii=False), encoding="utf-8")

    mock = start_mock(MOCK_PORT, script_path)
    failures: list[str] = []
    try:
        cases = [
            (
                "内嵌二进制（模拟无 Python：VIBE_PYTHON 指向不存在的解释器）",
                {"VIBE_PYTHON": "definitely-not-a-python"},
                "embedded",
                False,
            ),
            (
                "回退系统 Python（VIBE_NO_EMBEDDED=1，显式给出解释器与包路径）",
                {
                    "VIBE_NO_EMBEDDED": "1",
                    "VIBE_PYTHON": PYTHON,
                    "PYTHONPATH": str(AGENT_CORE),
                },
                "python",
                True,
            ),
        ]
        for index, (title, env_extra, expected_kind, expect_probe) in enumerate(cases, start=1):
            report_path = tmp / f"report_{index}.json"
            shot_path = tmp / f"screenshot_{index}.png"
            print(f"\n[{index}/{len(cases)}] {title}")
            report = run_smoke(dist, workspace, report_path, shot_path, env_extra)

            print(f"      后端来源: {report['backendKind']} -> {report['backendCommand']}")
            print(f"      健康检查版本: {report['healthVersion']}")
            print(f"      事件: {report['events']}")
            print(f"      回答: {report['answered'][:80]}")
            print(f"      截图: {report['screenshot']}")

            if not report["ok"]:
                failures.append(f"[{index}] 冒烟失败于阶段 {report['stage']}: {report['error']}")
                continue
            if report["backendKind"] != expected_kind:
                failures.append(
                    f"[{index}] 后端来源应为 {expected_kind}，实际 {report['backendKind']}"
                )
            if report["pythonProbed"] != expect_probe:
                failures.append(
                    f"[{index}] pythonProbed 应为 {expect_probe}，实际 {report['pythonProbed']}"
                )
            if report["healthVersion"] != __version__:
                failures.append(
                    f"[{index}] 后端上报版本 {report['healthVersion']} != {__version__}"
                )
            if "run.finished" not in report["events"]:
                failures.append(f"[{index}] 没有收到 run.finished")
            shot = Path(report["screenshot"])
            if not shot.is_file() or shot.stat().st_size < 5000:
                failures.append(f"[{index}] 截图缺失或过小: {shot}")

        if failures:
            print("\n=== 冒烟失败 ===")
            for line in failures:
                print(f"  ✗ {line}")
            return 1
        print("\n=== 打包冒烟通过 ===")
        print("（内嵌二进制可独立运行；回退系统 Python 路径同样可用；版本号三处一致）")
        return 0
    finally:
        mock.terminate()
        report_copy = Path(tempfile.gettempdir()) / "e2e_package_report.txt"
        try:
            report_copy.write_text(
                "\n".join(failures) or "PASS", encoding="utf-8"
            )
        except OSError:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())