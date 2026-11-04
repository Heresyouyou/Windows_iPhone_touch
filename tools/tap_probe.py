"""探针：事件 tap 到底能不能真的吞掉鼠标事件。

为什么非测不可
--------------
`swallowed` 只是「回调返回了 None」的计数，它**不能证明系统真的删掉了这个事件**。
实测遇到过这样的组合：`CGEventTapCreate` 返回了非空 tap、`listen_only` 读到 False、
回调也确实返回 None，但物理光标照样跟着鼠标走、照样被屏幕边缘钳住——
也就是说 tap 只是名义上可写，实际按只监听在工作。

判据
----
用合成事件测，不依赖人手：

  1. 装一个吞鼠标移动的 tap；
  2. 把光标 warp 到 (400,400)；
  3. 往 (900,400) 发一条合成 mouse-moved；
  4. 读光标：还在 (400,400) → 真吞掉了；跑到 (900,400) → 没吞掉。

合成事件走的是和真实事件同一条 CGEventPost(kCGHIDEventTap) 通路，
所以这个判据对真实鼠标同样成立。

跑法
----
  ./run.sh --module=tools.tap_probe --new
  cat ~/Library/Logs/MacPhoneTouch.log
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import Quartz  # noqa: E402
from CoreFoundation import (  # noqa: E402
    CFRunLoopRunInMode,
    kCFRunLoopDefaultMode,
)

from ios_screen import mac_input  # noqa: E402
from ios_screen.mac_input import (  # noqa: E402
    TAP_HEAD_INSERT,
    TAP_LOCATION_HID,
    TAP_LOCATION_SESSION,
    TAP_OPTION_DEFAULT,
    TAP_OPTION_LISTEN_ONLY,
)


def _pump(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.02, True)


def _try(location: int, option: int, label: str) -> tuple[bool, str]:
    """装一个吞鼠标的 tap，用合成事件验证它是否真的生效。"""
    hits = {"n": 0}

    def cb(proxy, type_, event, refcon):
        if type_ == Quartz.kCGEventMouseMoved:
            hits["n"] += 1
            return None          # 要求删掉
        return event

    tap = Quartz.CGEventTapCreate(
        location, TAP_HEAD_INSERT, option,
        Quartz.CGEventMaskBit(Quartz.kCGEventMouseMoved), cb, None,
    )
    if not tap:
        return False, "建不起来"

    src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    from CoreFoundation import CFRunLoopAddSource, CFRunLoopGetCurrent

    CFRunLoopAddSource(CFRunLoopGetCurrent(), src, kCFRunLoopDefaultMode)
    Quartz.CGEventTapEnable(tap, True)
    _pump(0.1)

    # 起点与终点都避开屏幕边缘，免得被边缘钳制干扰判断
    mac_input.warp_cursor(400, 400)
    _pump(0.1)

    ev = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventMouseMoved, (900.0, 400.0), 0
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
    _pump(0.25)

    pos = mac_input.cursor_pos()
    swallowed = abs(pos[0] - 400) < 8
    detail = (
        f"光标 ({pos[0]:.0f},{pos[1]:.0f}) 回调命中 {hits['n']} 次 → "
        + ("真正吞掉了" if swallowed else "没吞掉（事件照常生效）")
    )

    Quartz.CGEventTapEnable(tap, False)
    try:
        from CoreFoundation import CFRunLoopRemoveSource

        CFRunLoopRemoveSource(CFRunLoopGetCurrent(), src, kCFRunLoopDefaultMode)
    except Exception:
        pass
    return swallowed, detail


def _warp_works() -> tuple[bool, str]:
    """warp 一次并回读，判断当前环境下光标能不能被程序摆布。"""
    mac_input.warp_cursor(420, 380)
    _pump(0.12)
    pos = mac_input.cursor_pos()
    ok = abs(pos[0] - 420) < 8 and abs(pos[1] - 380) < 8
    return ok, f"目标 (420,380) 实际 ({pos[0]:.0f},{pos[1]:.0f})"


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    print("事件 tap 探针", flush=True)
    print(f"  辅助功能可信: {mac_input.accessibility_trusted()}", flush=True)
    start = mac_input.cursor_pos()

    # ---- 先看基准：没有 tap 时 warp 是否生效 ----
    ok0, d0 = _warp_works()
    print(f"  {'✓' if ok0 else '✗'} 无 tap 时 warp: {d0}", flush=True)

    # ---- 再装一个什么都不做的 tap，重复同一个测试 ----
    def idle_cb(proxy, type_, event, refcon):
        return event

    idle = Quartz.CGEventTapCreate(
        TAP_LOCATION_HID, TAP_HEAD_INSERT, TAP_OPTION_DEFAULT,
        Quartz.CGEventMaskBit(Quartz.kCGEventMouseMoved), idle_cb, None,
    )
    idle_src = None
    if idle:
        idle_src = Quartz.CFMachPortCreateRunLoopSource(None, idle, 0)
        from CoreFoundation import CFRunLoopAddSource, CFRunLoopGetCurrent

        CFRunLoopAddSource(CFRunLoopGetCurrent(), idle_src, kCFRunLoopDefaultMode)
        Quartz.CGEventTapEnable(idle, True)
        _pump(0.1)
        ok1, d1 = _warp_works()
        print(f"  {'✓' if ok1 else '✗'} 有 tap 时 warp: {d1}", flush=True)
        Quartz.CGEventTapEnable(idle, False)
        try:
            from CoreFoundation import CFRunLoopRemoveSource

            CFRunLoopRemoveSource(CFRunLoopGetCurrent(), idle_src, kCFRunLoopDefaultMode)
        except Exception:
            pass
        _pump(0.1)
    else:
        ok1, d1 = False, "连只透传的 tap 都建不起来"
        print(f"  ✗ 有 tap 时 warp: {d1}", flush=True)

    cases = [
        (TAP_LOCATION_HID, TAP_OPTION_DEFAULT, "HID + 可修改"),
        (TAP_LOCATION_SESSION, TAP_OPTION_DEFAULT, "Session + 可修改"),
        (TAP_LOCATION_HID, TAP_OPTION_LISTEN_ONLY, "HID + 只监听"),
    ]
    results = []
    for loc, opt, label in cases:
        ok, detail = _try(loc, opt, label)
        results.append((label, ok))
        print(f"  {'✓' if ok else '✗'} {label}: {detail}", flush=True)

    mac_input.warp_cursor(*start)
    print(f"\n已把光标放回 ({start[0]:.0f},{start[1]:.0f})", flush=True)

    usable = [label for label, ok in results if ok]
    print()
    if usable:
        print(f"结论: 可用的改写型 tap —— {', '.join(usable)}")
        return 0
    print("结论: 这台机器上没有任何改写型 tap 生效。")
    print("      事件 tap 只能监听，无法阻止光标跟随鼠标。")
    print("      对策：不靠吞事件，改为捕获期间持续把光标伺服回锚点，")
    print("      并让增量只从'光标位移'推导（而不是从事件 delta 读）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
