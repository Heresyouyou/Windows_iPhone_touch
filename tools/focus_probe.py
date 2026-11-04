"""焦点归位的四种做法，逐个试，看哪种真能把激活状态交还给上一个应用。

撤下抓取层时如果只是 orderOut，我们的进程会继续占着"最前面"却没有窗口，
键盘就发进了一个不存在的响应者——用户看到的就是"键盘失灵"。必须找到一条
真能把焦点交回去的调用序列。

    ./run.sh --module=tools.focus_probe --new
"""
from __future__ import annotations

import sys
import time

from AppKit import NSApplication, NSWorkspace

from ios_screen.app import pump_events


def frontmost() -> str:
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return "(nil)"
    return f"{app.localizedName()}({app.processIdentifier()})"


def wait(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pump_events(0.02)


def take_focus(app) -> None:
    app.activateIgnoringOtherApps_(True)
    wait(0.4)


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)
    target = NSWorkspace.sharedWorkspace().frontmostApplication()
    print(f"目标（要还回去的应用）: {target}", flush=True)
    print(f"起始最前: {frontmost()}", flush=True)

    results = []

    def check(name: str) -> None:
        ok = NSWorkspace.sharedWorkspace().frontmostApplication().processIdentifier() == target.processIdentifier()
        results.append((name, ok))
        print(f"  → {name}: isActive={app.isActive()} 最前={frontmost()}  {'✓' if ok else '✗'}", flush=True)

    # A: hide
    take_focus(app)
    app.hide_(None)
    wait(0.8)
    check("A hide")

    # B: deactivate
    take_focus(app)
    app.deactivate()
    wait(0.8)
    check("B deactivate")

    # C: 直接激活对方
    take_focus(app)
    ret = target.activateWithOptions_(1 | 2)
    wait(0.8)
    check(f"C activate(others) ret={ret}")

    # D: hide 再激活对方
    take_focus(app)
    app.hide_(None)
    wait(0.2)
    ret = target.activateWithOptions_(1 | 2)
    wait(0.8)
    check(f"D hide+activate ret={ret}")

    # E: 激活对方再 hide
    take_focus(app)
    ret = target.activateWithOptions_(1 | 2)
    wait(0.2)
    app.hide_(None)
    wait(0.8)
    check(f"E activate+hide ret={ret}")

    print("\n汇总:", flush=True)
    for name, ok in results:
        print(f"  {'✓' if ok else '✗'} {name}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
