"""窗口模式：用一个可见窗口代表 iPhone 屏幕，鼠标在窗口内移动就控制 iPhone。

这是边缘模式的调试形态，也可以当正式用法——比记屏幕边缘直观。
两层用途分开：
  1. 验证「鼠标位移 → HID 报文 → iPhone 指针」这条链路，排除边缘判定的干扰；
  2. 确认无误后，把触发条件从「窗口内」换成「屏幕边缘」即可，发送逻辑完全一样。

窗口宽度代表 iPhone 宽度，所以 px_per_count = iPhone宽 / 窗口宽，鼠标扫过窗口
就等于指针扫过 iPhone 全宽。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import tkinter as tk

from . import hid
from .app import CAL_DEFAULT, HogpWorker, Shared
from .calibration import Calibration
from .hogp import HogpService
from .keymap import KeyboardState
from .vpointer import PointerConfig, VirtualPointer
from . import winput

PANEL_W, PANEL_H = 420, 900
# 光标离画布中心超过这个距离就把它挪回中心，于是手的行程没有上限
WARP_RADIUS = 110


class PanelApp:
    def __init__(self, args) -> None:
        self.args = args
        self.cal = Calibration.from_file(args.calibration)
        self.shared = Shared()
        self.svc = HogpService()
        self.mode = args.mode
        self.worker = HogpWorker(
            self.svc, self.cal, self.shared,
            dry=args.dry_run, verbose=args.verbose, mode=args.mode,
        )
        # 窗口宽 = iPhone 宽，鼠标横向扫过窗口即指针扫过全屏
        self.px_per_count = self.cal.screen_width / PANEL_W * args.sensitivity
        # 绝对模式的位置来自虚拟坐标，不再取光标在窗口里的位置——
        # 窗口只有 420 像素宽，靠坐标映射的话光标一碰边就到头了。
        self.vp = VirtualPointer(PointerConfig(
            width=self.cal.screen_width,
            height=self.cal.screen_height,
            gain=args.gain,
        ))
        self.cursor = winput.CursorGuard()
        self._capturing = False
        self._last_t = time.monotonic()
        self.cur = (0, 0)
        self.abs_sent = 0
        self.last: tuple[int, int] | None = None
        self.kb = KeyboardState()
        self.kb_sent = 0
        self.click_sent = 0
        self._closed = False
        self._build()

    def _build(self) -> None:
        self.root = tk.Tk()
        self.root.title("iPhone 控制面板")
        self.root.geometry(f"{PANEL_W}x{PANEL_H + 86}+2960+60")
        self.root.configure(bg="#1e1e1e")
        self.root.resizable(False, False)

        self.status = tk.Label(
            self.root, text="启动中…", fg="#cfcfcf", bg="#1e1e1e",
            font=("Consolas", 9), anchor="nw", justify="left",
        )
        self.status.pack(fill="x", padx=10, pady=(8, 4))

        self.canvas = tk.Canvas(
            self.root, width=PANEL_W - 20, height=PANEL_H,
            bg="#2b2b2b", highlightthickness=0,
        )
        self.canvas.pack(padx=10, pady=(0, 8))
        cw = PANEL_W - 20
        self.canvas.create_rectangle(4, 4, cw - 4, PANEL_H - 4, outline="#4a4a4a", width=2)
        self.canvas.create_text(
            cw // 2, PANEL_H // 2,
            text="iPhone 操作区\n\n移进来即捕获，光标钉在中心\n"
                 "手可以一直推，不会撞到窗口边\n\nEsc 释放",
            fill="#7a7a7a", font=("Microsoft YaHei", 11), justify="center",
        )

        self.canvas.bind("<Enter>", self._on_enter)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<Motion>", self._on_motion)

        # 鼠标按键：tkinter 的 num 1=左 2=中 3=右，对应 HID 报文的 bit0/bit2/bit1
        self.canvas.bind("<ButtonPress>", self._on_button_down)
        self.canvas.bind("<ButtonRelease>", self._on_button_up)

        # 键盘：只在面板窗口有焦点时转发，避免抢走别的窗口的输入。
        # tkinter 的 keycode 在 Windows 上就是虚拟键码（VK_*），可以直接查表。
        self.root.bind("<KeyPress>", self._on_key_press)
        self.root.bind("<KeyRelease>", self._on_key_release)
        # 失焦时清空按键状态，否则切走再切回来会留下"卡住不放"的键
        self.root.bind("<FocusOut>", self._on_focus_out)
        # 绝对模式把光标钉在画布里，必须留一个不靠鼠标的出口
        self.root.bind("<Escape>", self._on_escape)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._hold_tick)
        self.root.after(400, self._tick_ui)

    # ---------- 鼠标 ----------

    def _on_enter(self, e) -> None:
        if self.mode == "relative":
            self.last = (e.x, e.y)
            self.cur = (e.x, e.y)
            self.shared.capturing = True
            return
        # 绝对模式：一进来就"抓住"光标，把它钉在画布中心。
        # 之后位置全部由相对位移累积，光标在哪儿不再影响落点，
        # 手可以一直往一个方向推，不必担心撞到窗口边缘。
        self._begin_capture()

    def _begin_capture(self) -> None:
        if self._capturing:
            return
        self._capturing = True
        self.shared.capturing = True
        self.cursor.hide()
        self._last_t = time.monotonic()
        self._warp_center()

    def _end_capture(self) -> None:
        if not self._capturing:
            return
        self._capturing = False
        if self.shared.buttons:
            with self.shared.lock:
                self.shared.buttons = 0
            self.worker.send_absolute_px(0, self.vp.x, self.vp.y, relay_button=True)
        self.shared.capturing = False
        self.cursor.show()

    def _warp_center(self) -> None:
        """把光标挪回画布中心，同时把基准点重置到中心。

        不重置 last 的话，下一次 Motion 会拿"中心"和"边缘"相减，
        算出一大截根本不存在的位移。
        """
        cx = self.canvas.winfo_width() // 2
        cy = self.canvas.winfo_height() // 2
        winput.user32.SetCursorPos(
            self.canvas.winfo_rootx() + cx, self.canvas.winfo_rooty() + cy
        )
        self.last = (cx, cy)
        self.cur = (cx, cy)

    def _on_leave(self, _e) -> None:
        if self.mode == "absolute":
            self._end_capture()
            return
        self.shared.capturing = False
        self.last = None
        # 鼠标移出窗口时若仍按着键，立即放开，否则 iPhone 上会一直按住
        if self.shared.buttons:
            with self.shared.lock:
                self.shared.buttons = 0
            self.worker.send_mouse_now(0)

    def _on_motion(self, e) -> None:
        self.cur = (e.x, e.y)
        if self.mode == "absolute":
            if not self._capturing:
                return
            if self.last is None:
                self.last = (e.x, e.y)
                return
            dx = e.x - self.last[0]
            dy = e.y - self.last[1]
            self.last = (e.x, e.y)
            if dx or dy:
                now = time.monotonic()
                dt = min(0.1, max(1e-4, now - self._last_t))
                self._last_t = now
                with self.shared.lock:
                    buttons = self.shared.buttons
                mdx, mdy = self.vp.move(dx, dy, dt)
                if buttons:
                    # 拖动只能走相对报文，且必须带真实位移
                    if int(round(mdx)) or int(round(mdy)):
                        self.worker.send_mouse_now(buttons, int(round(mdx)), int(round(mdy)))
                else:
                    self.worker.send_absolute_px(0, self.vp.x, self.vp.y)
                    self.abs_sent += 1
            # 行程再居中：离中心超过半径就挪回去，手可以无限推
            cx = self.canvas.winfo_width() // 2
            cy = self.canvas.winfo_height() // 2
            if abs(e.x - cx) > WARP_RADIUS or abs(e.y - cy) > WARP_RADIUS:
                self._warp_center()
            return

        if not self.shared.capturing:
            return
        if self.last is None:
            self.last = (e.x, e.y)
            return
        dx = e.x - self.last[0]
        dy = e.y - self.last[1]
        self.last = (e.x, e.y)
        if dx == 0 and dy == 0:
            return
        with self.shared.lock:
            self.shared.target_x = max(
                0.0, min(float(self.cal.screen_width - 1),
                         self.shared.target_x + dx * self.px_per_count)
            )
            self.shared.target_y = max(
                0.0, min(float(self.cal.screen_height - 1),
                         self.shared.target_y + dy * self.px_per_count)
            )

    # ---------- 鼠标按键 ----------

    _BTN_BITS = {1: 0x01, 2: 0x04, 3: 0x02}  # tkinter num → HID buttons 位

    def _on_button_down(self, e) -> None:
        self.cur = (e.x, e.y)  # 用事件自带坐标，别信上一次 Motion 的残留值
        self.last = (e.x, e.y)
        self._set_button(e.num, True)

    def _on_button_up(self, e) -> None:
        self.cur = (e.x, e.y)
        self._set_button(e.num, False)

    def _set_button(self, num: int, down: bool) -> None:
        bit = self._BTN_BITS.get(num)
        if not bit:
            return
        with self.shared.lock:
            if down:
                self.shared.buttons |= bit
            else:
                self.shared.buttons &= ~bit
            buttons = self.shared.buttons
        # 立即发，不等节流：按下和抬起若落在同一个 30ms 周期会被合并成"没按过"
        if self.mode == "absolute":
            # 绝对报文负责"指针在哪"，但它不带按钮语义（实测点击不生效）；
            # 按下和抬起都要补一条 dx=dy=0 的相对报文，少一条就会卡住不放。
            self.worker.send_absolute_px(buttons, self.vp.x, self.vp.y, relay_button=True)
        else:
            self.worker.send_mouse_now(buttons)
        self.click_sent += 1

    # ---------- 键盘 ----------

    def _on_key_press(self, e) -> None:
        if self.kb.press(e.keycode):
            self.worker.send_report(hid.RID_KEYBOARD, self.kb.report())
            self.kb_sent += 1

    def _on_key_release(self, e) -> None:
        if self.kb.release(e.keycode):
            self.worker.send_report(hid.RID_KEYBOARD, self.kb.report())
            self.kb_sent += 1

    def _release_all(self, _e=None) -> None:
        """失焦/关闭时把所有键放开，否则 iPhone 上会一直按着某个键。"""
        if self.kb.clear():
            self.worker.send_report(hid.RID_KEYBOARD, self.kb.report())

    def _on_focus_out(self, e=None) -> None:
        self._release_all(e)
        # 切走时一定释放捕获，否则光标被钉在画布里、人却回不到桌面
        if self.mode == "absolute":
            self._end_capture()

    def _on_escape(self, _e=None) -> str:
        self._end_capture()
        return "break"

    def _hold_tick(self) -> None:
        """按住不动时每 100ms 重发一次当前状态。

        悬停（无按钮）走绝对快照，按住则发一条 dx=dy=0 的相对报文维持按钮状态。
        注意：这里只维持按钮，不参与拖动——拖动靠 _on_motion 的相对位移。
        """
        if self._closed:
            return
        if self.mode == "absolute" and self._capturing and self.shared.buttons:
            self.worker.send_mouse_now(self.shared.buttons, 0, 0)
        self.root.after(100, self._hold_tick)

    # ---------- UI 刷新 ----------

    def _tick_ui(self) -> None:
        if self._closed:
            return
        w = self.worker
        if self.mode == "absolute":
            pos = f"{self.vp.x:>5.0f},{self.vp.y:>5.0f}"
        else:
            px, py = w.position
            pos = f"{px:>5.0f},{py:>5.0f}"
        txt = (
            f"[{self.mode}] 广播 {self.svc.status}   捕获 "
            f"{'ON' if (self._capturing or self.shared.capturing) else 'OFF'}\n"
            f"订阅 {'YES' if self.svc.subscribed else 'NO'}   "
            f"移动 {w.sent + self.abs_sent}  点 {self.click_sent}  "
            f"键 {self.kb_sent}  失败 {w.send_fail}\n"
            f"位置 {pos}  /  {self.cal.screen_width}x{self.cal.screen_height}"
        )
        self.status.config(text=txt)
        self.root.after(400, self._tick_ui)

    def _on_close(self) -> None:
        self._closed = True
        self._end_capture()
        self.shared.capturing = False
        self.shared.running = False
        self.worker.stop()
        self.root.destroy()

    def run(self) -> int:
        self.worker.start()
        self.worker.ready.wait(timeout=20)
        if self.worker.error:
            print(f"❌ {self.worker.error}")
            return 1
        print(f"面板已启动  [{self.mode}]")
        if self.mode == "absolute":
            print(f"增益 {self.args.gain:.2f} iPhone像素/计数；"
                  f"光标离中心 {WARP_RADIUS}px 自动回中，手可以一直推。")
            print("Esc 或失焦即释放捕获；关窗口退出。")
        else:
            print(f"比例 {self.px_per_count:.3f} iPhone像素/鼠标像素")
        self.root.mainloop()
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ios-screen-panel", description="窗口模式控制 iPhone")
    ap.add_argument("--calibration", default=CAL_DEFAULT)
    ap.add_argument("--sensitivity", type=float, default=1.0)
    ap.add_argument("--mode", choices=["absolute", "relative"], default="absolute",
                    help="absolute=虚拟绝对坐标(默认，手可无限推)；relative=相对累积")
    ap.add_argument("--gain", type=float, default=0.5,
                    help="绝对模式增益：一个鼠标计数换多少 iPhone 像素，默认 0.5")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    if not os.path.isfile(args.calibration):
        print(f"读不到校准文件: {args.calibration}")
        return 2
    return PanelApp(args).run()


if __name__ == "__main__":
    sys.exit(main())
