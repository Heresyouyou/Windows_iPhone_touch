"""验抓取层在**虚拟屏**那片区域上能不能收到点击。

背景
----
虚拟屏模式下，指针位置来自光标坐标，但按钮仍然只能靠抓取层窗口接
`mouseDown:`。如果窗口没有覆盖到虚拟屏、或者事件被系统派发到别处，
表现就是「指针能动、点击全失灵」。

这个探针在虚拟屏中心和主屏中心各合成一次左键，比较两边能不能收到，
一次就能分清是「窗口盖不到」还是「事件没派发」。

用法：

    ./run.sh --module=tools.click_probe --new
    tail -f ~/Library/Logs/MacPhoneTouch.log
"""
from __future__ import annotations

import sys
import time

import Quartz
from AppKit import NSApplication, NSScreen, NSWorkspace
from Quartz import (
    CGEventCreateMouseEvent,
    CGEventPost,
    CGWarpMouseCursorPosition,
    kCGHIDEventTap,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseUp,
    kCGEventMouseMoved,
)

from ios_screen.app import pump_events
from ios_screen.capture_window import CaptureWindow
from ios_screen.mac_input import EventTap


class Probe:
    def __init__(self) -> None:
        self.hits: list[tuple[float, float, int, bool]] = []
        self.tap_hits: list[tuple[float, float]] = []
        self.win = CaptureWindow(
            on_button=self.on_button,
            on_scroll=lambda *a: None,
            on_key=lambda *a: None,
        )
        # 只读 tap：验证「窗口收不到」时，事件在更外层是否还存在
        self.tap = EventTap(
            on_mouse=lambda dx, dy: None,
            on_button=self.on_tap_button,
            on_scroll=lambda *a: False,
            on_key=lambda *a: False,
            should_swallow=lambda: False,
            force_listen_only=True,
        )

    def on_button(self, bit, down):
        self.hits.append((self._last_xy[0], self._last_xy[1], bit, down))
        print(f"  [click] ★ 窗口收到按钮 bit={bit} down={down} at {self._last_xy}", flush=True)

    def on_tap_button(self, bit, down):
        pos = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        self.tap_hits.append((pos.x, pos.y))
        print(
            f"  [tap]   tap 看到按钮 bit={bit} down={down} at "
            f"({pos.x:.0f},{pos.y:.0f})",
            flush=True,
        )
        return False


def wait(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pump_events(0.02)


def post_click(x: float, y: float, down: bool) -> None:
    kind = kCGEventLeftMouseDown if down else kCGEventLeftMouseUp
    ev = CGEventCreateMouseEvent(None, kind, (x, y), 0)
    CGEventPost(kCGHIDEventTap, ev)


def hover(x: float, y: float) -> None:
    """先把光标挪过去，再点。合成点击的坐标是显示坐标（顶部原点）。"""
    CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(None, kCGEventMouseMoved, (x, y), 0))
    CGWarpMouseCursorPosition((x, y))
    wait(0.25)


def probe_point(p: Probe, name: str, x: float, y: float) -> None:
    p._last_xy = (x, y)
    before = len(p.hits)
    print(f"--- {name} ({x:.0f},{y:.0f}) ---", flush=True)
    hover(x, y)

    view = p.win._view
    win = p.win._win
    if view is not None and win is not None:
        # 屏幕坐标(AppKit，左下原点) → 内容视图坐标
        ax = x
        ay = float(NSScreen.screens()[0].frame().size.height) - y
        hit = view.hitTest_((ax - win.frame().origin.x, ay - win.frame().origin.y))
        print(
            f"    窗口 frame={tuple(round(v) for v in (win.frame().origin.x, win.frame().origin.y, win.frame().size.width, win.frame().size.height))}"
            f" 内容区={tuple(round(v) for v in (view.frame().origin.x, view.frame().origin.y, view.frame().size.width, view.frame().size.height))}"
            f" hitTest={'view' if hit is not None else 'nil'}",
            flush=True,
        )

    post_click(x, y, True)
    wait(0.12)
    post_click(x, y, False)
    wait(0.4)
    print(f"    {name}: {'收到' if len(p.hits) > before else '没收到'} "
          f"({len(p.hits) - before} 个事件)", flush=True)


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)

    print("=== 抓取层点击探针 ===", flush=True)
    for s in NSScreen.screens():
        f = s.frame()
        print(
            f"  屏 frame=({f.origin.x:.0f},{f.origin.y:.0f}) "
            f"{f.size.width:.0f}x{f.size.height:.0f}",
            flush=True,
        )

    p = Probe()
    p._last_xy = (0.0, 0.0)
    prev = NSWorkspace.sharedWorkspace().frontmostApplication()
    print(f"  tap 启动: {p.tap.start()}  位置={p.tap.location} "
          f"只监听={p.tap.listen_only}", flush=True)
    p.win.show()
    wait(1.0)

    win = p.win._win
    if win is not None:
        f = win.frame()
        print(
            f"  窗口 frame=({f.origin.x:.0f},{f.origin.y:.0f}) "
            f"{f.size.width:.0f}x{f.size.height:.0f}  key={win.isKeyWindow()} "
            f"level={win.level()}",
            flush=True,
        )
        view = p.win._view
        vf = view.frame()
        print(
            f"  内容区 ({vf.origin.x:.0f},{vf.origin.y:.0f}) "
            f"{vf.size.width:.0f}x{vf.size.height:.0f}",
            flush=True,
        )

    screens = NSScreen.screens()
    main_h = float(screens[0].frame().size.height)

    # 主屏中心（对照）
    probe_point(p, "主屏中心", 840.0, 525.0)

    # 虚拟屏中心
    if len(screens) > 1:
        f = screens[1].frame()
        # CG 坐标（顶部原点）：AppKit y 是从底往上
        cx = float(f.origin.x) + float(f.size.width) / 2.0
        cy = main_h - (float(f.origin.y) + float(f.size.height) / 2.0)
        probe_point(p, "虚拟屏中心", cx, cy)
        # 虚拟屏右下角
        probe_point(p, "虚拟屏右下", float(f.origin.x) + float(f.size.width) - 20,
                    main_h - float(f.origin.y) - 20)
    else:
        print("  只有一块屏：虚拟屏没建起来", flush=True)

    print(f"总计：窗口收到 {len(p.hits)} 个按钮事件，tap 看到 {len(p.tap_hits)} 个", flush=True)
    p.win.close()
    try:
        p.tap.stop()
    except Exception:
        pass
    try:
        if prev is not None and not prev.isTerminated():
            from AppKit import NSApplicationActivateIgnoringOtherApps

            prev.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
