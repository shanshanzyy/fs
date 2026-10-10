"""`python -m agent_core.host` 的入口。

桌面壳（Electron）就是通过这个模块把后端拉起来的：
    python -m agent_core.host --workspace <目录> --port 0 --print-url --no-open
"""

from __future__ import annotations

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
