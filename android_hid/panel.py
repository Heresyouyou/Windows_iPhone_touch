"""窗口模式：把本机键鼠当蓝牙键鼠接到 Android 上（先用窗口验证链路）。

为什么先做窗口模式：
  BLE HID 这条链路上"配对成功但没反应"的坑很多（ProtectionLevel、Report Reference、
  Boot 报表……）。窗口模式把"什么时候算接管"简化成"点一下窗口"，排除了边缘判定、
  多屏、DPI 这些干扰，链路一通就能直接用，之后再做边缘模式只是换触发条件。

交互语义是触控板：
  接管后光标被钉在画布中心并隐藏，手的行程由相对位移累加，可以一直推；
  离中心超过 WARP_RADIUS 就把光标挪回中心，于是没有边界。
  手机端的指针位置由 Android 自己累加，我们只负责发相对位移。

键盘：
  接管期间窗口有焦点，按键走 Report 1（8 字节状态快照）。
  Esc 留给"释放"，不转发；Home/Back/音量/播放走媒体键（Report 3），
  见 Ctrl+Alt+H / B / ↑ / ↓ / P。
"""
from __future__ import annotations

import argparse
import sys
import time
import tkinter as tk
from tkinter import messagebox

# winput 导入时就会声明 DPI 感知，必须早于任何 Tk 窗口
from ios_screen import winput
from ios_screen.keymap import KeyboardState

from . import hid
from .bridge import HidWorker
from .hogp import HidService

PANEL_W, PANEL_H = 720, 540
CANVAS_W, CANVAS_H = 690, 330
# 光标离画布中心超过这个距离就挪回中心；太小会抖，太大会撞到窗口边
WARP_RADIUS = 90
FLUSH_MS = 8          # 位移合流周期：125Hz，比 BLE 实际速率高一档，够跟手

# tkinter 的按键号 → HID 按键位
_BTN_BITS = {1: 0x01, 2: 0x04, 3: 0x02}

BG = "#141518"
PANEL = "#1c1e22"
FG = "#e6e8ea"
DIM = "#8b9096"
ACCENT = "#3ad07a"
WARN = "#f0a24a"
BORDER = "#2e3238"


class AndroidPanel:
    def __init__(self, args) -> None:
        self.args = args
        self.svc = HidService()
        self.worker = HidWorker(self.svc, dry=args.dry_run, verbose=args.verbose)
        self.kb = KeyboardState()

        self.gain = args.gain
        self.capturing = False
        self.buttons = 0
        self.acc_x = 0.0
        self.acc_y = 0.0
        self.last: tuple[int, int] | None = None
        self._wheel_acc = 0.0
        self._closed = False
        self._stat_t = time.monotonic()
        self._stat_sent = 0
        self._rate = 0.0
        self.pos = [0, 0]   # 本机视角的累计行程，只用于显示

        self._build()

    # ---------- 界面 ----------

    def _build(self) -> None:
        self.root = tk.Tk()
        self.root.title("Android 键鼠桥 · 窗口模式")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(0, (sw - PANEL_W) // 2)
        y = max(0, (sh - PANEL_H) // 3)
        self.root.geometry(f"{PANEL_W}x{PANEL_H}+{x}+{y}")

        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=18, pady=(14, 2))
        tk.Label(head, text="Android 键鼠桥", bg=BG, fg=FG,
                 font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        tk.Label(head, text="窗口模式 · 先验证链路", bg=BG, fg=DIM,
                 font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(10, 0))

        self.status = tk.Label(
            self.root, text="正在启动蓝牙服务…", bg=BG, fg=DIM, anchor="w",
            justify="left", font=("Consolas", 9),
        )
        self.status.pack(fill="x", padx=18, pady=(6, 8))

        self.canvas = tk.Canvas(
            self.root, width=CANVAS_W, height=CANVAS_H, bg=PANEL,
            highlightthickness=0,
        )
        self.canvas.pack(padx=15, pady=(0, 10))
        self._frame = self.canvas.create_rectangle(
            2, 2, CANVAS_W - 2, CANVAS_H - 2, outline=BORDER, width=2
        )
        cx, cy = CANVAS_W // 2, CANVAS_H // 2
        self._cross = (
            self.canvas.create_line(cx - 26, cy, cx + 26, cy, fill=BORDER),
            self.canvas.create_line(cx, cy - 26, cx, cy + 26, fill=BORDER),
        )
        self._title = self.canvas.create_text(
            cx, cy - 74, text="点击此处开始接管", fill=FG,
            font=("Microsoft YaHei UI", 14, "bold"),
        )
        self._hint = self.canvas.create_text(
            cx, cy + 96,
            text="接管后光标钉在中心并隐藏，手可以一直推\n"
                 "键盘在接管期间一并转发 · Esc 释放",
            fill=DIM, font=("Microsoft YaHei UI", 9), justify="center",
        )

        self.canvas.bind("<ButtonPress>", self._on_press)
        self.canvas.bind("<ButtonRelease>", self._on_release)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<MouseWheel>", self._on_wheel)

        self.root.bind("<KeyPress>", self._on_key_down)
        self.root.bind("<KeyRelease>", self._on_key_up)
        self.root.bind("<Escape>", self._on_escape)
        self.root.bind("<FocusOut>", self._on_focus_out)
        self.root.bind("<Control-Alt-h>", lambda e: self._consumer(hid.CONSUMER_HOME))
        self.root.bind("<Control-Alt-b>", lambda e: self._consumer(hid.CONSUMER_BACK))
        self.root.bind("<Control-Alt-p>",
                       lambda e: self._consumer(hid.CONSUMER_PLAY_PAUSE))
        self.root.bind("<Control-Alt-Up>",
                       lambda e: self._consumer(hid.CONSUMER_VOL_UP))
        self.root.bind("<Control-Alt-Down>",
                       lambda e: self._consumer(hid.CONSUMER_VOL_DOWN))
        self.root.bind("<Control-Alt-equal>", lambda e: self._nudge_gain(1.25))
        self.root.bind("<Control-Alt-minus>", lambda e: self._nudge_gain(1 / 1.25))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill="x", padx=18, pady=(0, 12))
        self.btn_start = self._button(foot, "开始接管", self._start, ACCENT)
        self.btn_start.pack(side="left")
        self.btn_stop = self._button(foot, "释放 (Esc)", self._stop_wrapper, None)
        self.btn_stop.pack(side="left", padx=(8, 0))
        self.btn_stop.configure(state="disabled")
        self.gain_label = tk.Label(
            foot, text=f"增益 {self.gain:.2f}", bg=BG, fg=DIM,
            font=("Consolas", 9),
        )
        self.gain_label.pack(side="right")

        self.root.after(FLUSH_MS, self._flush)
        self.root.after(250, self._tick_ui)

    @staticmethod
    def _button(parent, text, cmd, color):
        b = tk.Button(
            parent, text=text, command=cmd, relief="flat", bd=0,
            bg="#26292e" if color is None else color,
            fg=FG if color is None else "#0d1a12",
            activebackground="#31353b" if color is None else color,
            activeforeground=FG if color is None else "#0d1a12",
            font=("Microsoft YaHei UI", 9), padx=16, pady=6, cursor="hand2",
        )
        return b

    # ---------- 接管 / 释放 ----------

    def _start(self) -> None:
        if self.capturing or self._closed:
            return
        if not self.worker.ready.is_set():
            return
        self.capturing = True
        self.buttons = 0
        self.acc_x = self.acc_y = 0.0
        self.pos = [0, 0]
        self.kb.clear()
        self.root.configure(cursor="none")
        self.root.focus_force()
        self._warp_center()
        self.canvas.itemconfigure(self._frame, outline=ACCENT)
        self.canvas.itemconfigure(self._title, text="接管中", fill=ACCENT)
        self.canvas.itemconfigure(
            self._hint,
            text="移动鼠标 = 手机指针 · 左/中/右键 = 点击\n"
                 "Ctrl+Alt+H 主页 / B 返回 / P 播放 / ↑↓ 音量\n"
                 "Esc 释放（Ctrl+Alt+= 或 - 调增益）",
            fill=DIM,
        )
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        print("▶ 接管开始（光标钉在窗口中心，Esc 释放）", flush=True)

    def _stop(self, reason: str = "") -> None:
        if not self.capturing:
            return
        self.capturing = False
        self.root.configure(cursor="")
        # 按键/按钮必须显式放开，否则手机上会一直按着
        if self.buttons:
            self.buttons = 0
            self._send_mouse(0, 0, force=True)
        if self.kb.clear():
            self.worker.send_keyboard(self.kb.report())
        self.acc_x = self.acc_y = 0.0
        self.last = None
        self.canvas.itemconfigure(self._frame, outline=BORDER)
        self.canvas.itemconfigure(self._title, text="点击此处开始接管", fill=FG)
        self.canvas.itemconfigure(
            self._hint,
            text="接管后光标钉在中心并隐藏，手可以一直推\n"
                 "键盘在接管期间一并转发 · Esc 释放",
            fill=DIM,
        )
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        print(f"■ 接管结束{('：' + reason) if reason else ''}", flush=True)

    def _stop_wrapper(self) -> None:
        self._stop("按钮")

    def _on_escape(self, _e=None) -> str:
        if self.capturing:
            self._stop("Esc")
        return "break"

    def _on_focus_out(self, _e=None) -> None:
        if self.capturing:
            self._stop("窗口失焦")

    def _warp_center(self) -> None:
        cx = self.canvas.winfo_width() // 2
        cy = self.canvas.winfo_height() // 2
        winput.user32.SetCursorPos(
            self.canvas.winfo_rootx() + cx, self.canvas.winfo_rooty() + cy
        )
        self.last = (cx, cy)

    # ---------- 鼠标 ----------

    def _on_press(self, e) -> None:
        if not self.capturing:
            if e.num == 1:
                self._start()
            return
        if e.num not in _BTN_BITS:
            return
        self.last = (e.x, e.y)
        self.buttons |= _BTN_BITS[e.num]
        self._send_mouse(0, 0, force=True)

    def _on_release(self, e) -> None:
        if not self.capturing or e.num not in _BTN_BITS:
            return
        bit = _BTN_BITS[e.num]
        # 启动接管的那次点击：按下时还没接管，抬起时这一位本来就没置，
        # 直接忽略，不能凭空发一条"抬起"
        if not (self.buttons & bit):
            return
        self.buttons &= ~bit
        self._send_mouse(0, 0, force=True)

    def _on_motion(self, e) -> None:
        if not self.capturing or self._closed:
            return
        if self.last is None:
            self.last = (e.x, e.y)
            return
        dx = e.x - self.last[0]
        dy = e.y - self.last[1]
        self.last = (e.x, e.y)
        if dx or dy:
            g = self.gain
            if self.args.accel > 0:
                speed = abs(dx) + abs(dy)
                g *= 1.0 + self.args.accel * min(2.0, speed / 30.0)
            self.acc_x += dx * g
            self.acc_y += dy * g
            self.pos[0] += dx
            self.pos[1] += dy
        # 离中心太远就挪回去：手可以无限推，也不会撞到窗口边
        cx = self.canvas.winfo_width() // 2
        cy = self.canvas.winfo_height() // 2
        if abs(e.x - cx) > WARP_RADIUS or abs(e.y - cy) > WARP_RADIUS:
            self._warp_center()

    def _on_wheel(self, e) -> None:
        if not self.capturing:
            return
        # Windows 一格是 ±120；高分辨率滚轮可能一次不到 120，先攒着
        self._wheel_acc += e.delta / 120.0
        notches = int(self._wheel_acc)
        if notches == 0:
            return
        self._wheel_acc -= notches
        self.worker.send_mouse(self.buttons, 0, 0,
                               notches * self.args.wheel_step, force=True)

    def _flush(self) -> None:
        """把攒下的位移合成一条报文发出去。"""
        if self._closed:
            return
        if self.capturing and (abs(self.acc_x) >= 1.0 or abs(self.acc_y) >= 1.0):
            dx = int(round(self.acc_x))
            dy = int(round(self.acc_y))
            if dx or dy:
                self.acc_x -= dx
                self.acc_y -= dy
                self._send_mouse(dx, dy)
        self.root.after(FLUSH_MS, self._flush)

    def _send_mouse(self, dx: int, dy: int, wheel: int = 0,
                    force: bool = False) -> None:
        self.worker.send_mouse(self.buttons, dx, dy, wheel, force=force)

    # ---------- 键盘 ----------

    def _on_key_down(self, e) -> None:
        if not self.capturing:
            return
        if e.keycode == 27:          # Esc 已被绑定为释放
            return
        if self.kb.press(e.keycode):
            self.worker.send_keyboard(self.kb.report())

    def _on_key_up(self, e) -> None:
        if not self.capturing:
            return
        if e.keycode == 27:
            return
        if self.kb.release(e.keycode):
            self.worker.send_keyboard(self.kb.report())

    def _consumer(self, usage: int):
        if self.capturing:
            self.worker.send_consumer(usage)
        return "break"

    def _nudge_gain(self, factor: float):
        if not self.capturing:
            return "break"
        self.gain = max(0.2, min(8.0, self.gain * factor))
        self.gain_label.configure(text=f"增益 {self.gain:.2f}")
        return "break"

    # ---------- 状态栏 ----------

    def _tick_ui(self) -> None:
        if self._closed:
            return
        now = time.monotonic()
        dt = now - self._stat_t
        if dt >= 1.0:
            self._rate = (self.worker.sent - self._stat_sent) / dt
            self._stat_t = now
            self._stat_sent = self.worker.sent
        sub = "DRY" if self.args.dry_run else (
            f"YES({self.worker.clients})" if self.worker.subscribed else "NO"
        )
        line2 = (f"发送 {self.worker.sent:<7d} 失败 {self.worker.fail:<4d} "
                 f"丢弃 {self.worker.dropped:<4d} 在飞 {self.worker.snapshot()['inflight']:<3d} "
                 f"{self._rate:5.0f}/s")
        line3 = (f"行程 {self.pos[0]:+6d},{self.pos[1]:+6d}   "
                 f"增益 {self.gain:.2f}   {'接管中' if self.capturing else '待机'}")
        self.status.configure(
            text=f"广播 {self.worker.status}   订阅 {sub}   服务 0x1812 / 报表 144B\n"
                 f"{line2}\n{line3}",
            fg=FG if (self.worker.subscribed or self.args.dry_run) else WARN,
        )
        self.root.after(250, self._tick_ui)

    # ---------- 生命周期 ----------

    def _on_close(self) -> None:
        self._stop("关闭窗口")
        self._closed = True
        self.worker.stop()
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self) -> int:
        self.worker.start()
        if not self.worker.ready.wait(timeout=20):
            messagebox.showerror("启动失败", "蓝牙服务 20 秒内没有启动完成")
            return 1
        if self.worker.error:
            messagebox.showerror("启动失败", self.worker.error)
            print(f"❌ {self.worker.error}", flush=True)
            return 1

        print(f"蓝牙 : 广播 {self.worker.status}   （服务 0x1812，Report Map 144B）",
              flush=True)
        print("手机 : 设置 → 蓝牙 → 配对新设备 → 选择本机电脑名 → 配对", flush=True)
        print("      首次配对会弹「蓝牙配对请求」，点「配对」；配对后状态栏订阅变 YES。",
              flush=True)
        print("提示 : 点窗口画布开始接管；Esc 释放。\n", flush=True)
        try:
            self.root.mainloop()
        finally:
            self.worker.stop()
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="android-hid-panel",
        description="把本机键鼠当作蓝牙键鼠接到 Android（窗口模式）",
    )
    ap.add_argument("--gain", type=float, default=1.6,
                    help="鼠标增益：一个鼠标计数发多少手机像素，默认 1.6"
                         "（接管中可用 Ctrl+Alt+= / - 调）")
    ap.add_argument("--accel", type=float, default=0.0,
                    help="速度增益，0=线性(默认)；0.5~1 快划时放大行程")
    ap.add_argument("--wheel-step", type=int, default=1,
                    help="每格滚轮发多少 HID 档位，默认 1")
    ap.add_argument("--dry-run", action="store_true", help="不发蓝牙，只打印报文")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    if args.wheel_step < 1:
        args.wheel_step = 1
    return AndroidPanel(args).run()


if __name__ == "__main__":
    sys.exit(main())
