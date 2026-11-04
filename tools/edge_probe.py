"""诊断：为什么推不到边缘。

只读，不改任何状态。每 0.5 秒打印一次：
  光标位置、离右键多远、是否算"贴边"、这一段收到的原始计数。

用法（普通终端即可，不需要 .app）：
    .venv/bin/python -m tools.edge_probe
推鼠标到屏幕右缘，看最后一行是不是 "贴边=True" 且 dx>0。
"""
from __future__ import annotations

import sys
import time

from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode

from ios_screen import mac_input
from ios_screen.edge_trigger import (
    EDGE_BAND_PX,
    Edge,
    EdgeSpec,
    EdgeTrigger,
    EdgeTriggerConfig,
    is_at_edge,
)
from ios_screen.geometry import Rect
from ios_screen.raw_mouse import RawMouse


def pump(t: float) -> None:
    CFRunLoopRunInMode(kCFRunLoopDefaultMode, t, True)


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    mon = mac_input.pick_display("primary")
    if mon is None:
        print("找不到显示器")
        return 2
    screen = Rect(mon["x"], mon["y"], mon["w"], mon["h"])
    spec = EdgeSpec(edge=Edge.Right, screen=screen)
    trig = EdgeTrigger(EdgeTriggerConfig(enter_threshold=12, exit_threshold=60))

    print(f"屏幕 {screen.w}x{screen.h} at ({screen.x},{screen.y})")
    print(f"右缘触发条件: x >= {screen.right - 1 - EDGE_BAND_PX} 且 y < {screen.bottom}")

    rm = RawMouse()
    ok = rm.start()
    print(f"原始位移: {'开' if ok else '失败'}  {rm.status}")
    if not ok:
        return 1

    t0 = time.monotonic()
    acc_dx = acc_dy = 0
    while True:
        pump(0.05)
        dx, dy = rm.take()
        acc_dx += dx
        acc_dy += dy
        now = time.monotonic()
        if now - t0 < 0.5:
            continue
        t0 = now
        cx, cy = mac_input.cursor_pos()
        at = is_at_edge(spec, int(round(cx)), int(round(cy)))
        ch = trig.update(spec, int(round(cx)), int(round(cy)), acc_dx, acc_dy)
        print(
            f"光标=({cx:7.1f},{cy:7.1f}) 距右缘={screen.right - cx:6.1f} "
            f"贴边={at!s:<5} dx={acc_dx:5d} dy={acc_dy:5d} "
            f"accum={trig.accum:3d} 事件={ch.name} 样本={rm.samples}",
            flush=True,
        )
        acc_dx = acc_dy = 0


if __name__ == "__main__":
    sys.exit(main())
