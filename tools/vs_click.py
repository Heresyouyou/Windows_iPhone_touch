"""在虚拟屏上合成一次点击，验证它有没有被转到 iPhone。

这不是单元测试，是**真机联调**的工具：只在 virtual display 已经存在时才有意义。
运行前先启动主程序（./run.sh -v --new），再跑：

    .venv/bin/python tools/vs_click.py [x] [y]

默认点在虚拟屏中心。日志里应该出现 `[tap] 按钮 ... → 转给 iPhone`，
iPhone 上对应的位置应当有反应。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import Quartz
from AppKit import NSScreen

from ios_screen import mac_input


def main() -> int:
    screens = NSScreen.screens()
    if len(screens) < 2:
        print("只有一块屏：先启动主程序，让它把虚拟屏建起来")
        return 1

    # 第二块就是 vdisplay 建的屏（AppKit 坐标，左下原点）
    f = screens[1].frame()
    main_h = float(screens[0].frame().size.height)
    cx = float(f.origin.x) + float(f.size.width) / 2.0
    cy = main_h - (float(f.origin.y) + float(f.size.height) / 2.0)

    if len(sys.argv) >= 3:
        cx, cy = float(sys.argv[1]), float(sys.argv[2])

    print(f"目标 ({cx:.0f},{cy:.0f})  虚拟屏 {f.size.width:.0f}x{f.size.height:.0f}")
    Quartz.CGWarpMouseCursorPosition((cx, cy))
    time.sleep(0.6)
    print(f"光标现在在 {mac_input.cursor_pos()}")

    for down in (True, False):
        kind = Quartz.kCGEventLeftMouseDown if down else Quartz.kCGEventLeftMouseUp
        ev = Quartz.CGEventCreateMouseEvent(None, kind, (cx, cy), 0)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.15)

    print("已合成一次左键 down/up")
    return 0


if __name__ == "__main__":
    sys.exit(main())
