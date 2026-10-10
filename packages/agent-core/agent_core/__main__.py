"""支持 python -m agent_core 直接运行。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
