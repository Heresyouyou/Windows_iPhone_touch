"""验抓取层：能不能成为 key window、能不能收到键盘/点击、撤下后焦点有没有还回去。

用法（走 .app，才有 TCC 身份）：

    ./run.sh --module=tools.window_probe --new
    tail -f ~/Library/Logs/MacPhoneTouch.log

事件是合成的（CGEventPost），不需要人动手，所以可以自动跑。
"""
from __future__ import annotations

import sys
import time

from AppKit import NSApplication, NSWorkspace
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventCreateMouseEvent,
    CGEventPost,
    kCGHIDEventTap,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseUp,
)

from ios_screen.app import pump_events
from ios_screen.capture_window import CaptureWindow

KEY_A = 0x00


class Probe:
    def __init__(self) -> None:
        self.buttons = 0
        self.scrolls = 0
        self.keys = []
        self.win = CaptureWindow(
            on_button=self.on_button, on_scroll=self.on_scroll, on_key=self.on_key
        )

    def on_button(self, bit, down):
        self.buttons += 1
        print(f"  [probe] 按钮 bit={bit} down={down}", flush=True)

    def on_scroll(self, delta, continuous):
        self.scrolls += 1
        print(f"  [probe] 滚轮 {delta:+.2f} continuous={continuous}", flush=True)

    def on_key(self, code, down, flags):
        self.keys.append((code, down))
        print(f"  [probe] 键 code=0x{code:02X} down={down}", flush=True)


def post_key(code: int, down: bool) -> None:
    CGEventPost(kCGHIDEventTap, CGEventCreateKeyboardEvent(None, code, down))


def post_click(x: float, y: float, down: bool) -> None:
    kind = kCGEventLeftMouseDown if down else kCGEventLeftMouseUp
    CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(None, kind, (x, y), 0))


def wait(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pump_events(0.02)


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)      # Accessory

    p = Probe()
    prev_app = NSWorkspace.sharedWorkspace().frontmostApplication()
    print(f"抓取层探针  原最前应用: {prev_app}", flush=True)

    p.win.show()
    ok_key = False
    end = time.monotonic() + 1.5
    while time.monotonic() < end:
        pump_events(0.02)
        if p.win.ensure_key():
            ok_key = True
            break
    print(f"  key window: {ok_key}  isActive={app.isActive()}", flush=True)

    # ---- 合成事件：键盘 ----
    post_key(KEY_A, True)
    wait(0.15)
    post_key(KEY_A, False)
    wait(0.15)
    print(f"  键盘收到 {len(p.keys)} 个事件: {p.keys}", flush=True)

    # ---- 合成事件：点击 ----
    post_click(400.0, 400.0, True)
    wait(0.1)
    post_click(400.0, 400.0, False)
    wait(0.15)
    print(f"  按钮收到 {p.buttons} 个事件", flush=True)

    keyboard_ok = any(c == KEY_A for c, _ in p.keys)
    click_ok = p.buttons >= 2

    # ---- 撤下，看焦点有没有还回去 ----
    p.win.hide()
    end = time.monotonic() + 1.2
    while time.monotonic() < end:
        p.win.tick()          # 焦点交还由主循环推进（生产代码里由定时器调用）
        pump_events(0.02)

    now_front = NSWorkspace.sharedWorkspace().frontmostApplication()
    active = app.isActive()
    print(f"  撤下后: isActive={active} isHidden={app.isHidden()} "
          f"窗口可见={p.win._win.isVisible()} 最前应用={now_front}", flush=True)

    same = now_front is not None and prev_app is not None and \
        now_front.processIdentifier() == prev_app.processIdentifier()
    print(f"  焦点已还给原应用: {same}", flush=True)

    # 撤下后再敲一个键：不该再被我们收到
    before = len(p.keys)
    post_key(KEY_A, True)
    wait(0.2)
    post_key(KEY_A, False)
    wait(0.1)
    leftover = len(p.keys) - before
    print(f"  撤下后仍收到 {leftover} 个键盘事件（应为 0）", flush=True)

    print()
    print("结论:", flush=True)
    print(f"  {'✓' if keyboard_ok else '✗'} 抓取层拿得到键盘", flush=True)
    print(f"  {'✓' if click_ok else '✗'} 抓取层拿得到点击", flush=True)
    print(f"  {'✓' if same else '✗'} 撤下后焦点还给原应用", flush=True)
    print(f"  {'✓' if leftover == 0 else '✗'} 撤下后不再拦截键盘", flush=True)

    p.win.close()
    return 0 if (keyboard_ok and click_ok and same and leftover == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
