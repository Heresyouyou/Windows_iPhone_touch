"""入口：

    python -m android_hid              # 窗口模式（默认）
    python -m android_hid --check      # 只验证蓝牙链路，等手机订阅
"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--check":
        from .check import main as check_main

        return check_main(argv[1:])
    from .panel import main as panel_main

    return panel_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
