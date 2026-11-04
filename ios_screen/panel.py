"""窗口模式：用一个悬浮面板代表 iPhone 屏幕，点一下面板就接管。

为什么要重写而不是照抄 Windows 的 tkinter 版
------------------------------------------
Windows 版靠 tkinter 的 <Enter>/<Motion> 事件触发，因为只有窗口拿到焦点才收得到
鼠标消息。macOS 不需要这样：事件 tap 是全局的，面板只是一个**视觉锚点**——
告诉人"手机在这一块"，真正的事件仍然来自 tap。

所以这里的触发条件改成「在面板里按下左键」：

  · 移动经过面板不会误捕获（macOS 上窗口密度高，悬停即捕获太容易误触）；
  · 捕获后一切照旧：光标隐藏、事件吞掉、往左推回去或按 Esc 释放。

窗口本身用 AppKit 画，不依赖 tkinter：AppKit 和 CFRunLoop 是同一套事件循环，
和事件 tap、CoreBluetooth 回调共用一个线程，不需要在两个事件循环之间来回 pump。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMakeRect,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSFloatingWindowLevel,
    NSView,
)
from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode
from Foundation import NSTimer, NSString

from . import core, hid, mac_input, userconfig
from .app import DEFAULT_CAL, DEFAULT_NAME
from .ble_hid import HidPeripheral, bundle_warning
from .calibration import Calibration
from .core import Controller, Sender, hotkey_for
from .geometry import Rect

PANEL_W, PANEL_H = 400, 866          # 接近 iPhone 15 Pro 的长宽比（0.462）
KEY_ESCAPE = 0x35


class _PanelView(NSView):
    """只负责画：背景、边框、几行状态字。"""

    def initWithOwner_(self, owner):
        self = super(_PanelView, self).initWithFrame_(NSMakeRect(0, 0, PANEL_W, PANEL_H))
        if self is None:
            return None
        self._owner = owner
        return self

    def isOpaque(self):
        return True

    def drawRect_(self, rect):
        NSColor.colorWithCalibratedWhite_alpha_(0.11, 1.0).set()
        NSBezierPath.fillRect_(rect)

        # 手机轮廓
        NSColor.colorWithCalibratedWhite_alpha_(0.30, 1.0).set()
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(8, 8, PANEL_W - 16, PANEL_H - 16), 18, 18
        )
        path.setLineWidth_(2.0)
        path.stroke()

        # 顶部听筒条（只是让这块区域看起来像手机）
        NSColor.colorWithCalibratedWhite_alpha_(0.35, 1.0).set()
        NSBezierPath.fillRect_(NSMakeRect(PANEL_W / 2 - 40, PANEL_H - 26, 80, 6))

        lines = self._owner.status_lines()
        y = PANEL_H - 70
        for text, size, gray in lines:
            attrs = {
                NSFontAttributeName: NSFont.fontWithName_size_("Menlo", size)
                or NSFont.systemFontOfSize_(size),
                NSForegroundColorAttributeName: NSColor.colorWithCalibratedWhite_alpha_(
                    gray, 1.0
                ),
            }
            NSString.stringWithString_(text).drawAtPoint_withAttributes_((22, y), attrs)
            y -= size + 12


class PanelApp(Controller):
    def __init__(self, args) -> None:
        self.args = args
        self.cal = Calibration.from_file(args.calibration)
        self.ble = HidPeripheral(local_name=args.name, verbose=args.verbose)
        self.sender = Sender(
            self.cal, self.ble, mode=args.mode, dry=args.dry_run, verbose=args.verbose
        )

        mon = mac_input.pick_display(args.screen)
        if mon is None:
            raise SystemExit(f"找不到显示器 {args.screen!r}，用 --list 看有哪些")
        self.monitor = mon
        self.screen = Rect(mon["x"], mon["y"], mon["w"], mon["h"])

        # 面板贴在所选屏幕的右侧，纵向居中
        px = self.screen.x + self.screen.w - PANEL_W - 40
        py = self.screen.y + max(0, (self.screen.h - PANEL_H) // 2)
        self.panel_origin = (px, py)

        super().__init__(
            self.cal,
            self.sender,
            mode=args.mode,
            gain=args.gain,
            accel=args.accel,
            escape_px=args.escape_px,
            wheel_step=args.wheel_step,
            sensitivity=args.sensitivity,
            screen_w=PANEL_W,
            screen_h=PANEL_H,
            exit_side="left",
            key_remap=userconfig.key_remap(args.config),
            verbose=args.verbose,
        )
        self.anchor = (px + PANEL_W / 2.0, py + PANEL_H / 2.0)
        self.region = Rect(px, py, PANEL_W, PANEL_H)
        self.running = True
        self.tap = mac_input.EventTap(
            on_mouse=self.on_mouse,
            on_button=self.on_button,
            on_scroll=self.on_scroll,
            on_key=self.on_key,
            should_swallow=lambda: self.capturing,
            verbose=args.verbose,
        )
        self.window = None
        self.view = None
        self._ui_timer = None
        self._self_test_deadline = 0.0

    # ------------------------------------------------------------ 触发

    def _cursor_inside(self) -> bool:
        cx, cy = mac_input.cursor_pos()
        return (
            self.region.x <= cx < self.region.x + self.region.w
            and self.region.y <= cy < self.region.y + self.region.h
        )

    def on_mouse(self, dx: int, dy: int) -> bool:
        if self.capturing:
            return super().on_mouse(dx, dy)
        return False

    def on_button(self, bit: int, down: bool) -> bool:
        if not self.capturing:
            # 在面板里点一下 = 抓住
            if bit == 0x01 and down and self._cursor_inside():
                self.begin_capture(mac_input.cursor_pos())
                return True
            return False
        return super().on_button(bit, down)

    def on_key(self, keycode: int, down: bool, flags: int) -> bool:
        if self.capturing and keycode == KEY_ESCAPE and down:
            self.end_capture()
            return True
        name = hotkey_for(keycode, flags)
        if name:
            if down:
                if name == "quit":
                    self.running = False
                elif name == "faster":
                    self.set_gain(self.vp.cfg.gain * 1.25)
                else:
                    self.set_gain(self.vp.cfg.gain / 1.25)
            return True
        return super().on_key(keycode, down, flags)

    def edge_fraction(self, cursor_xy: tuple[float, float]) -> tuple[float, float]:
        cx, cy = cursor_xy
        fx = (cx - self.region.x) / max(1, self.region.w - 1)
        fy = (cy - self.region.y) / max(1, self.region.h - 1)
        return (max(0.0, min(1.0, fx)), max(0.0, min(1.0, fy)))

    def warp_after_exit(self) -> None:
        # 放到面板左侧外面，避免下一次点击/移动立刻又抓住
        self.request_warp(self.region.x - 60, self.region.y + self.region.h / 2.0)

    # ------------------------------------------------------------ 窗口

    def _build_window(self) -> None:
        h = self.screen.h
        # AppKit 的窗口坐标原点在主屏左下，Quartz 在左上，这里换算一次
        ns_y = h - (self.panel_origin[1] + PANEL_H)
        rect = NSMakeRect(self.panel_origin[0], ns_y, PANEL_W, PANEL_H)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
        )
        win.setLevel_(NSFloatingWindowLevel)
        win.setOpaque_(True)
        win.setHasShadow_(True)
        win.setReleasedWhenClosed_(False)
        win.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary
        )
        win.setTitle_("iPhone")
        view = _PanelView.alloc().initWithOwner_(self)
        win.setContentView_(view)
        win.makeKeyAndOrderFront_(None)
        self.window, self.view = win, view

    def status_lines(self) -> list[tuple[str, int, float]]:
        w = self.sender
        cap = "抓取中" if self.capturing else "空闲"
        lines = [
            (f"iPhone · {self.args.mode}", 13, 0.92),
            ("", 6, 0.5),
            (f"状态  {cap}", 11, 0.78 if self.capturing else 0.55),
            (f"蓝牙  {self.ble.status}", 11, 0.55),
            (f"订阅  {self.ble.subscription_detail}", 11, 0.55),
        ]
        if self.args.mode == "absolute":
            lines.append((f"位置  {self.vp.x:.0f},{self.vp.y:.0f}", 11, 0.55))
            lines.append((f"增益  {self.vp.cfg.gain:.2f}", 11, 0.55))
        else:
            px, py = w.position
            lines.append((f"位置  {px:.0f},{py:.0f}", 11, 0.55))
        lines += [
            ("", 6, 0.5),
            (f"报文  {w.sent}  失败 {w.send_fail}", 11, 0.45),
            ("", 6, 0.5),
            ("点击面板 = 接管", 11, 0.40),
            ("Esc / 向左推 = 释放", 11, 0.40),
        ]
        return lines

    # ------------------------------------------------------------ 运行

    def run(self) -> int:
        if not self.args.dry_run:
            warn = bundle_warning()
            if warn:
                print(warn)
                return 2
            self.ble.start()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not self.ble.powered_on:
                CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, True)

        if not mac_input.accessibility_trusted() and not self.args.dry_run:
            print(
                "⚠️ 缺少「辅助功能」权限：到「系统设置 → 隐私与安全性 → 辅助功能」"
                "勾选本应用后重新启动。"
            )

        if not self.tap.start():
            print(
                "❌ 建不起事件 tap。请到「系统设置 → 隐私与安全性 → 辅助功能」"
                "里勾选当前终端/应用，然后重启本程序。"
            )
            return 1

        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        self._build_window()
        app.finishLaunching()

        print(f"面板已启动  [{self.args.mode}]  位置 {self.region.x},{self.region.y} "
              f"{PANEL_W}x{PANEL_H}")
        print(f"蓝牙 {self.ble.status}  名字 {self.args.name!r}")
        print("点一下面板即接管；Esc 或向左推回去释放；Ctrl+Option+Q 退出。")

        self._ui_timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            self.sender.interval, True, self._on_timer
        )
        self._ui_timer2 = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            0.4, True, self._on_ui_timer
        )
        if self.args.self_test > 0:
            self._self_test_deadline = time.monotonic() + self.args.self_test

        try:
            while self.running:
                CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.1, True)
                if self._self_test_deadline and time.monotonic() > self._self_test_deadline:
                    print("self-test 结束")
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
        return 0

    def _on_timer(self, timer=None) -> None:
        # 光标动作只能在定时器里落地，事件回调里调系统 API 会被忽略
        self.maintain_cursor()
        if self.capturing:
            self.sender.tick(self.target_x, self.target_y, self.buttons)

    def _on_ui_timer(self, timer=None) -> None:
        if self.view is not None:
            self.view.setNeedsDisplay_(True)

    def shutdown(self) -> None:
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True
        for t in (getattr(self, "_ui_timer", None), getattr(self, "_ui_timer2", None)):
            if t is not None:
                try:
                    t.invalidate()
                except Exception:
                    pass
        self.capturing = False
        if self.kb.clear():
            self.sender.send_report(hid.RID_KEYBOARD, self.kb.report())
        self.tap.stop()
        self.cursor.show()
        self.ble.stop()
        if self.window is not None:
            try:
                self.window.orderOut_(None)
            except Exception:
                pass
        print(f"\n已退出。发送 {self.sender.sent} 条报文（失败 {self.sender.send_fail}）。")


def main(argv: list[str] | None = None) -> int:
    # 输出常被重定向到日志：行缓冲能保证崩溃前写下的信息真的落盘
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="ios-screen-panel", description="面板模式控制 iPhone")
    ap.add_argument("--calibration", default=DEFAULT_CAL)
    ap.add_argument("--screen", default="primary")
    ap.add_argument("--name", default=DEFAULT_NAME)
    ap.add_argument("--config", default=None)
    ap.add_argument("--sensitivity", type=float, default=1.0)
    ap.add_argument("--mode", choices=["absolute", "relative"], default="absolute")
    ap.add_argument("--gain", type=float, default=3.0,
                    help="一个鼠标计数换多少 iPhone 像素，默认 3.0")
    ap.add_argument("--accel", type=float, default=0.0)
    ap.add_argument("--escape-px", type=float, default=5.0)
    ap.add_argument("--wheel-step", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", type=float, default=0.0, help="N 秒后自动退出（自检用）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    if not os.path.isfile(args.calibration):
        print(f"读不到校准文件: {args.calibration}")
        return 2
    return PanelApp(args).run()


if __name__ == "__main__":
    sys.exit(main())
