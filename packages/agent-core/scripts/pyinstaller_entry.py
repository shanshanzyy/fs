"""PyInstaller 打包入口（D1.1）。

桌面壳以内嵌二进制方式启动后端时，就是执行这个脚本编译出的 exe，
参数与 `python -m agent_core.host` 完全一致。

刻意单独放一个入口文件、而不是直接拿 `agent_core/host/__main__.py` 去打包：
`__main__.py` 用的是包内相对导入（`from .server import main`），
被 PyInstaller 当顶层脚本执行时会直接 ImportError。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许从源码目录直接运行（`python scripts/pyinstaller_entry.py --help`）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.host.server import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())