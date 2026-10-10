"""桌面端打包（D1.1）：内嵌后端二进制 + 可双击的分发目录。

分两步，可分别执行：

1. `--backend`：用 PyInstaller 把 `agent_core`（含 host 与 UI 静态资源）
   打成**单文件** exe，落到 `apps/desktop/resources/backend/`。
   产物是「机器上没装 Python 也能跑」的后端本体。
2. `--dist`：组装可双击的分发目录 `dist/desktop/VibeAgent-<版本>-<平台>-<架构>/`：
   复制 Electron 运行时 → 改名成 VibeAgent.exe → 放入我们的 app 与内嵌后端。

刻意**不引入 electron-builder**（D1.3：本期只要求「能拿到一个可双击的目录」）：
需要的只是一次文件搬运，而 electron-builder 会额外拉一整套打包工具链与
签名/发布配置，对一个单机分发目录属于过度工程。将来要出安装包再引入不迟。

版本号（D1.2）：一律取自 `agent_core.__version__`，写进
`resources/backend/version.json` 与分发目录里的 package.json 与目录名。
运行期界面显示的版本则来自后端 `/api/health`，保证「壳说的版本 = 内核的版本」。

用法：
    python scripts/build_desktop.py              # 两步都做
    python scripts/build_desktop.py --backend    # 只打后端二进制
    python scripts/build_desktop.py --dist       # 只组装分发目录（需已有二进制）
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]           # 仓库根
AGENT_CORE = ROOT / "packages" / "agent-core"
DESKTOP = ROOT / "apps" / "desktop"
ELECTRON_DIST = DESKTOP / "node_modules" / "electron" / "dist"
BACKEND_DIR = DESKTOP / "resources" / "backend"
BUILD_DIR = ROOT / "build" / "pyinstaller"
DIST_ROOT = ROOT / "dist" / "desktop"

ENTRY = AGENT_CORE / "scripts" / "pyinstaller_entry.py"
APP_FILES = ("main.js", "backend.js", "smoke.js", "preload.js")


# --------------------------------------------------------------------- 纯函数
def core_version() -> str:
    """读核心版本号。优先读源码里的字面量，避免依赖导入成功的副作用。"""
    init_file = AGENT_CORE / "agent_core" / "__init__.py"
    for line in init_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("__version__"):
            _, _, value = stripped.partition("=")
            return value.strip().strip('"').strip("'")
    raise SystemExit(f"没能在 {init_file} 里找到 __version__")


def embedded_binary_name(platform_name: str = sys.platform) -> str:
    """内嵌后端的文件名。必须与 src/backend.js 的 embeddedBinaryName 保持一致。"""
    return "vibe-agent-core.exe" if platform_name.startswith("win") else "vibe-agent-core"


def dist_dir_name(version: str, platform_name: str = sys.platform, machine: str = "") -> str:
    arch = machine or platform.machine() or "unknown"
    return f"VibeAgent-{version}-{platform_name}-{arch}"


def backend_binary_path() -> Path:
    return BACKEND_DIR / embedded_binary_name()


# --------------------------------------------------------------------- 步骤一
def build_backend(python: str = sys.executable) -> Path:
    """PyInstaller onefile 打包。返回产物路径。"""
    ui_dir = AGENT_CORE / "agent_core" / "host" / "ui"
    if not ui_dir.is_dir():
        raise SystemExit(f"缺少 UI 静态资源目录: {ui_dir}")
    if not ENTRY.is_file():
        raise SystemExit(f"缺少打包入口: {ENTRY}")

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    BACKEND_DIR.mkdir(parents=True, exist_ok=True)

    separator = os.pathsep  # Windows 是 ';'，类 Unix 是 ':'
    args = [
        python,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--name",
        "vibe-agent-core",
        "--paths",
        str(AGENT_CORE),
        # UI 是运行时按 Path(__file__).parent / 'ui' 读的，必须一起打进去
        "--add-data",
        f"{ui_dir}{separator}agent_core/host/ui",
        "--distpath",
        str(BACKEND_DIR),
        "--workpath",
        str(BUILD_DIR / "work"),
        "--specpath",
        str(BUILD_DIR),
        str(ENTRY),
    ]
    print("[1/2] PyInstaller 打包内嵌后端 …")
    print("      " + " ".join(args))
    result = subprocess.run(args, cwd=str(AGENT_CORE))
    if result.returncode != 0:
        raise SystemExit(f"PyInstaller 失败（退出码 {result.returncode}）")

    binary = backend_binary_path()
    if not binary.is_file():
        raise SystemExit(f"打包完成但没找到产物: {binary}")
    print(f"      产物 {binary}  ({binary.stat().st_size / 1_048_576:.1f} MB)")
    return binary


# --------------------------------------------------------------------- 步骤二
def sync_package_version(version: str) -> None:
    """把核心版本同步进 apps/desktop/package.json（版本号只有一处真源）。"""
    package_json = DESKTOP / "package.json"
    data = json.loads(package_json.read_text(encoding="utf-8"))
    if data.get("version") != version:
        data["version"] = version
        package_json.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def _rmtree(path: Path) -> int:
    """尽力清空目录，返回**残留条目数**。

    Windows 上构建产物常被杀毒/索引程序以「允许读写、禁止删除」的方式占用，
    此时删除一定会失败，但构建本身完全可以继续 —— 就地覆盖即可。
    因此这里不把「删不掉」当致命错误，只把残留数量报给调用方。
    """
    if not path.exists():
        return 0
    for _ in range(3):
        try:
            shutil.rmtree(path, onerror=_force_remove)
        except OSError:
            pass
        if not path.exists():
            return 0
        time.sleep(0.3)
    return sum(1 for _ in path.rglob("*")) + 1


def _force_remove(func, target, _exc_info) -> None:
    try:
        os.chmod(target, stat.S_IWRITE)
        func(target)
    except OSError:
        pass


def assemble_dist(version: str, out_dir: Path | None = None) -> Path:
    """组装可双击的分发目录。"""
    binary = backend_binary_path()
    if not binary.is_file():
        raise SystemExit(f"缺少内嵌后端产物 {binary}，请先执行 --backend")

    electron_exe = "electron.exe" if sys.platform.startswith("win") else "electron"
    source = ELECTRON_DIST / electron_exe
    if not source.is_file():
        raise SystemExit(
            f"找不到 Electron 运行时 {source}；请先在 apps/desktop 执行 npm install"
        )

    target = out_dir or (DIST_ROOT / dist_dir_name(version))
    print(f"[2/2] 组装分发目录 {target} …")
    leftover = _rmtree(target)
    if leftover:
        print(f"      提示：有 {leftover} 个条目被杀毒/索引程序占用无法删除，将就地覆盖")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ELECTRON_DIST, target, dirs_exist_ok=True)

    # Electron 运行时里的主程序改名成我们的名字，双击的就是它
    app_exe = "VibeAgent.exe" if sys.platform.startswith("win") else "VibeAgent"
    app_exe_path = target / app_exe
    if app_exe_path.exists():  # 上一次构建的残留（删不掉时）先清掉，否则改名会失败
        try:
            app_exe_path.unlink()
        except OSError:
            pass
    (target / electron_exe).rename(app_exe_path)

    # 我们的 app：Electron 见到 resources/app 就不再加载 default_app.asar
    app_dir = target / "resources" / "app"
    (app_dir / "src").mkdir(parents=True, exist_ok=True)
    for name in APP_FILES:
        shutil.copy2(DESKTOP / "src" / name, app_dir / "src" / name)
    (app_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "vibe-agent-desktop",
                "version": version,
                "private": True,
                "description": "本地化 Vibe Coding Agent 的桌面客户端（薄壳）",
                "main": "src/main.js",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # 内嵌后端与版本信息
    backend_out = target / "resources" / "backend"
    backend_out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, backend_out / embedded_binary_name())
    (backend_out / "version.json").write_text(
        json.dumps(
            {
                "core": version,
                "binary": embedded_binary_name(),
                "platform": sys.platform,
                "arch": platform.machine(),
                "builtAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    sync_package_version(version)
    print(f"      完成：{target / app_exe}")
    return target


# --------------------------------------------------------------------- 入口
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="桌面端打包（D1）")
    parser.add_argument("--backend", action="store_true", help="只打内嵌后端二进制")
    parser.add_argument("--dist", action="store_true", help="只组装分发目录")
    parser.add_argument("--out", help="分发目录输出路径（默认 dist/desktop/<自动命名>）")
    parser.add_argument("--python", default=sys.executable, help="用于 PyInstaller 的解释器")
    args = parser.parse_args(argv)

    version = core_version()
    print(f"核心版本 {version}")

    do_backend = args.backend or not (args.backend or args.dist)
    do_dist = args.dist or not (args.backend or args.dist)

    if do_backend:
        build_backend(args.python)
    if do_dist:
        out = Path(args.out).resolve() if args.out else None
        target = assemble_dist(version, out)
        print(f"\n完成。双击运行：{target / ('VibeAgent.exe' if sys.platform.startswith('win') else 'VibeAgent')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())