"""端到端跑一遍状态机，不需要蓝牙、不需要真人操作。

验的是这条链：
    推到右缘 → 进入捕获（抓取层上屏）→ 往回推越过退出边 → 释放（抓取层撤下，
    焦点交还，光标落到边缘内侧）

    .venv/bin/python -m tools.flow_probe

这是 dry-run，报文只打印不发蓝牙。
"""
from __future__ import annotations

import os
import sys
import time

from AppKit import NSApplication

from ios_screen import mac_input

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from ios_screen.app import App, pump_events


class _Args:
    """照抄 argparse 的结果，省得跑一遍命令行解析。"""

    def __init__(self) -> None:
        self.edge = None
        self.zone_start = 0.0
        self.zone_end = 1.0
        self.screen = "primary"
        self.list = False
        self.name = "MPT-Flow"
        self.config = None
        self.calibration = os.path.join(
            ROOT, "ios_screen", "data", "calibration-iphone15pro.json"
        )
        self.mode = "absolute"
        self.gain = 3.0
        self.accel = 0.0
        self.escape_px = 6.0 * self.gain
        self.wheel_step = 2
        self.sensitivity = 1.0
        self.enter_threshold = 12
        self.exit_threshold = 60
        self.dry_run = True
        self.no_vscreen = True      # 这个探针验的是边缘模式
        self.home = False
        self.fix_cursor = False
        self.self_test = 0.0
        self.verbose = True


def wait(seconds: float, app=None) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if app is not None:
            app.win.tick()
        pump_events(0.02)


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    from ios_screen.edge_trigger import Edge, parse_edge

    NSApplication.sharedApplication().setActivationPolicy_(1)
    args = _Args()
    args.edge = parse_edge("right")
    app = App(args)

    monitor = app.screen
    print(f"屏幕 {monitor.w}x{monitor.h} at ({monitor.x},{monitor.y})", flush=True)

    start = mac_input.cursor_pos()
    print(f"光标原位置 {start}", flush=True)

    # 把光标摆到右缘，再喂一段朝外的位移
    mac_input.warp_cursor(monitor.x + monitor.w - 1, monitor.y + monitor.h // 2)
    wait(0.1, app)
    print(f"摆到右缘后 {mac_input.cursor_pos()}", flush=True)

    app._feed_mouse(app.trigger.cfg.enter_threshold + 4, 0)
    print(f"喂朝外位移后 capturing={app.capturing} 抓取层={app.win.shown}", flush=True)

    entered = app.capturing and app.win.shown
    if not entered:
        print("❌ 没有进入捕获", flush=True)
        mac_input.warp_cursor(*start)
        return 1

    # 让激活完成
    wait(0.6, app)
    key_ok = app.win.ensure_key()
    print(f"抓取层拿到键盘: {key_ok} isActive={NSApplication.sharedApplication().isActive()}", flush=True)

    # 进 iPhone 后往左推：虚拟指针在左缘（x=0），再往左超过 escape_px 就释放
    app._feed_mouse(10, 0)      # 先往右边挪一点，免得一进来就在退出边上
    print(f"在 iPhone 里右移后 vp=({app.vp.x:.0f},{app.vp.y:.0f})", flush=True)
    app._feed_mouse(-(app.push.threshold + 20), 0)
    print(f"往回推后 capturing={app.capturing} 抓取层={app.win.shown}", flush=True)

    released = not app.capturing
    app.maintain_cursor()       # 落点由定时器落地，这里手动跑一次
    wait(0.8, app)
    after = mac_input.cursor_pos()
    inset = abs(after[0] - (monitor.x + monitor.w - 120)) < 6
    active = NSApplication.sharedApplication().isActive()

    print(f"释放后光标={after} 落在内侧={inset} 仍激活={active}", flush=True)

    mac_input.warp_cursor(*start)
    print(f"已复原光标到 {mac_input.cursor_pos()}", flush=True)

    print(flush=True)
    print("结论:", flush=True)
    print(f"  {'✓' if entered else '✗'} 推到右缘进入捕获", flush=True)
    print(f"  {'✓' if key_ok else '✗'} 抓取层拿到键盘", flush=True)
    print(f"  {'✓' if released else '✗'} 往回推能释放", flush=True)
    print(f"  {'✓' if inset else '✗'} 释放后光标落在边缘内侧", flush=True)
    print(f"  {'✓' if not active else '✗'} 释放后交还焦点", flush=True)

    ok = entered and key_ok and released and inset and not active
    app.win.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
