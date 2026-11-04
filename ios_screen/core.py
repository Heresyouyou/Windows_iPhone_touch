"""边缘模式与面板模式共用的控制核心。

和 Windows 版的结构对照
-----------------------
Windows 版是「主线程消息循环 + HOGP 线程 asyncio」，两者用 `Shared` 加锁交换
坐标和按钮状态。macOS 不需要这层：

  · CoreBluetooth 的 `updateValue:for:onSubscribedCentrals:` 是**同步**的，
    没有 await，也就不需要事件循环；
  · CBPeripheralManager 的回调挂在主队列，事件 tap 挂在主 run loop，
    两者本来就在同一个线程上串行执行。

所以这里把 `Shared` 拆掉，直接由 Controller 持有状态，Sender 只管编码和发送。
少一个线程，就少一整类「报文节拍和坐标更新抢锁」的问题。

有一处必须保留：30ms 的报文节拍。
iOS 按报文到达速度分加速度档，校准曲线是在 30ms 一档上量出来的。改成 5ms
会让整张表失效，表现为「小距离准、大距离冲过头」。
"""
from __future__ import annotations

import time

from . import hid, mac_input
from .calibration import Calibration
from .keymap import (
    KEY_CAPS_LOCK,
    MODIFIER_BIT,
    KeyboardState,
)
from .vpointer import PointerConfig, PushThrough, VirtualPointer

SEND_INTERVAL = 0.03      # 相对模式：iOS 按报文速率分档，30ms 是校准前提
ABS_INTERVAL = 0.01       # 绝对模式：位置是快照，速率不影响落点
HOLD_RESEND = 0.1         # 按住不动时补发按钮状态的周期

# 热键：Ctrl+Option 组合（macOS 的 Option 就是 Windows 的 Alt）
HK_QUIT = 0x0C      # Q
HK_FASTER = 0x18    # =
HK_SLOWER = 0x1B    # -
HOTKEYS = {HK_QUIT: "quit", HK_FASTER: "faster", HK_SLOWER: "slower"}

FLAG_CONTROL = mac_input.FLAG_CONTROL
FLAG_OPTION = mac_input.FLAG_ALTERNATE

# 进入方向 → iPhone 上与之相邻的那条边（也就是"退出边"）
ENTRY_SIDE = {
    "right": "left",
    "left": "right",
    "top": "bottom",
    "bottom": "top",
}


def hotkey_for(keycode: int, flags: int) -> str | None:
    """命中热键返回名字。要求 Control+Option 同时按住，且不带 Command/Shift。"""
    if not (flags & FLAG_CONTROL and flags & FLAG_OPTION):
        return None
    if flags & (mac_input.FLAG_COMMAND | mac_input.FLAG_SHIFT):
        return None
    return HOTKEYS.get(keycode)


class Sender:
    """把目标位置编码成 HID 报文并发出去。不含任何输入采集逻辑。"""

    def __init__(
        self,
        cal: Calibration,
        ble,
        mode: str = "absolute",
        dry: bool = False,
        verbose: bool = False,
    ) -> None:
        self.cal = cal
        self.ble = ble
        self.mode = mode
        self.dry = dry
        self.verbose = verbose

        self._px = 0.0
        self._py = 0.0
        self._last_dx = 0
        self._last_buttons = 0
        # 绝对模式：上次发出的 0..32767 坐标 + 上次发出的像素坐标（算拖动位移用）
        self._abs_x = -1
        self._abs_y = -1
        self._abs_fx = 0.0
        self._abs_fy = 0.0
        self._hold_at = 0.0

        self.sent = 0
        self.send_fail = 0
        self.stat_t = time.monotonic()
        self.stat_n = 0
        self.stat_tick = 0
        self.gain = 0.0

    # ------------------------------------------------------------ 节拍

    @property
    def interval(self) -> float:
        return ABS_INTERVAL if self.mode == "absolute" else SEND_INTERVAL

    def tick(self, tx: float, ty: float, buttons: int) -> None:
        self.stat_tick += 1
        if self.mode == "absolute":
            self._tick_absolute(tx, ty, buttons)
        else:
            self._tick_relative(tx, ty, buttons)

        now = time.monotonic()
        if now - self.stat_t >= 5.0:
            if self.stat_n:
                dt = now - self.stat_t
                print(
                    f"  [stat] {self.stat_n / dt:.0f} 报文/秒  "
                    f"节拍 {self.stat_tick / dt:.0f}/秒  增益 {self.gain:.2f}",
                    flush=True,
                )
            self.stat_t = now
            self.stat_n = 0
            self.stat_tick = 0

    # ----- 相对模式：查校准表，30ms 一步 -----

    def _tick_relative(self, tx: float, ty: float, buttons: int) -> None:
        step = self.cal.next_step(self._px, self._py, tx, ty, last_dx=self._last_dx)
        if step.dx == 0 and step.dy == 0 and buttons == self._last_buttons:
            return
        if step.dx or step.dy:
            self._px, self._py = step.x, step.y
            self._last_dx = step.dx
        self._last_buttons = buttons

        payload = hid.encode_relative_mouse(buttons, step.dx, step.dy)
        self._emit(hid.RID_RELATIVE, payload, f"id=3 -> ({self._px:.0f},{self._py:.0f})")

    # ----- 绝对模式：一条报文到位，按钮另走相对通道 -----

    def _tick_absolute(self, tx: float, ty: float, buttons: int) -> None:
        ax, ay = self._to_abs(tx, ty)
        changed = (ax != self._abs_x) or (ay != self._abs_y)
        btn_changed = buttons != self._last_buttons
        self._last_buttons = buttons

        if buttons:
            # 拖动只能走相对报文，而且必须带真实位移：
            #   · 绝对报文不带按钮语义（实测按住无效）；
            #   · dx=dy=0 的相对报文只维持按钮，构不成拖动；
            #   · 两条一起发会让 iOS 端重复位移，所以拖动期间只发相对。
            dx = int(round(tx - self._abs_fx))
            dy = int(round(ty - self._abs_fy))
            if dx or dy:
                self._send_relative(buttons, dx, dy)
                self._abs_fx, self._abs_fy = tx, ty
                self._abs_x, self._abs_y = ax, ay
            elif btn_changed or time.monotonic() - self._hold_at >= HOLD_RESEND:
                self._send_relative(buttons, 0, 0)
                self._hold_at = time.monotonic()
            return

        # 悬停：绝对位置一变就发。松开按钮的那一帧也要发一次，
        # 把指针重新锚回模型位置（拖动期间走相对位移，可能有累积误差）。
        if changed or btn_changed:
            self._send_absolute(0, ax, ay)
            self._abs_x, self._abs_y = ax, ay
        self._abs_fx, self._abs_fy = tx, ty

    def _to_abs(self, x: float, y: float) -> tuple[int, int]:
        w = max(1, self.cal.screen_width - 1)
        h = max(1, self.cal.screen_height - 1)
        ax = int(round(max(0.0, min(float(w), x)) / w * hid.ABSOLUTE_MAX))
        ay = int(round(max(0.0, min(float(h), y)) / h * hid.ABSOLUTE_MAX))
        return ax, ay

    # ------------------------------------------------------------ 立即发送

    def _emit(self, report_id: int, payload: bytes, note: str = "") -> bool:
        self.sent += 1
        self.stat_n += 1
        if self.dry:
            if self.verbose:
                hexed = " ".join(f"{b:02X}" for b in payload)
                print(f"  report {note or report_id} [{hexed}]", flush=True)
            return True
        try:
            ok = self.ble.send(report_id, payload)
            if not ok:
                self.send_fail += 1
            return ok
        except Exception:
            self.send_fail += 1
            return False

    def _send_absolute(self, buttons: int, ax: int, ay: int) -> None:
        self._emit(
            hid.RID_ABSOLUTE,
            hid.encode_absolute_pointer(buttons, ax, ay),
            f"id=2 [{buttons:02X} {ax:04X} {ay:04X}]",
        )

    def _send_relative(self, buttons: int, dx: int, dy: int, wheel: int = 0) -> None:
        self._emit(
            hid.RID_RELATIVE,
            hid.encode_relative_mouse(buttons, dx, dy, wheel),
            f"id=3 [{buttons:02X} {dx:04d} {dy:04d}]",
        )

    def send_absolute_px(
        self, buttons: int, x: float, y: float, relay_button: bool = False
    ) -> None:
        """按钮状态变化时立即发，不等节拍（按下+抬起可能落在同一拍里）。

        relay_button=True 时再补一条 dx=dy=0 的相对报文：绝对报文不带按钮语义，
        按下和抬起都必须由相对通道表达——只发按下不发抬起，iPhone 上就会一直按着。
        """
        ax, ay = self._to_abs(x, y)
        self._send_absolute(buttons, ax, ay)
        if relay_button:
            self._send_relative(buttons, 0, 0)

    def send_mouse_now(self, buttons: int, dx: int = 0, dy: int = 0) -> None:
        self._send_relative(buttons, dx, dy)

    def send_wheel(self, notches: int) -> None:
        """滚轮：Report3 的第 4 字节（int8）。一格 = 1。

        必须立即发、不走节拍——滚轮是离散事件，攒到下一拍就丢了手感。
        """
        n = max(-127, min(127, int(notches)))
        if n:
            self._send_relative(0, 0, 0, n)

    def send_report(self, report_id: int, payload: bytes) -> None:
        self._emit(report_id, payload)

    # ------------------------------------------------------------ 状态

    def invalidate_absolute(self) -> None:
        """让下一次节拍无条件发一条绝对报文。

        每次重新进入捕获都要调用：否则新落点恰好等于上次发过的坐标时会被
        当成"没变化"跳过，iPhone 上的指针就停在上次的位置不动。
        """
        self._abs_x = -1
        self._abs_y = -1

    def jump_to(self, x: float, y: float) -> None:
        self._px, self._py = x, y

    @property
    def position(self) -> tuple[float, float]:
        return self._px, self._py


class Controller:
    """捕获状态机 + 鼠标/键盘语义。子类只需要提供触发方式（边缘或窗口）。"""

    def __init__(
        self,
        cal: Calibration,
        sender: Sender,
        *,
        mode: str = "absolute",
        gain: float = 3.0,
        accel: float = 0.0,
        escape_px: float = 5.0,
        wheel_step: int = 2,
        sensitivity: float = 1.0,
        screen_w: int = 1680,
        screen_h: int = 1050,
        exit_side: str = "left",
        key_remap: dict[int, int] | None = None,
        verbose: bool = False,
    ) -> None:
        self.cal = cal
        self.sender = sender
        self.mode = mode
        self.wheel_step = wheel_step
        self.sensitivity = sensitivity
        self.exit_side = exit_side
        self.key_remap = key_remap or {}
        self.verbose = verbose

        self.capturing = False
        self.target_x = 0.0
        self.target_y = 0.0
        self.buttons = 0

        self.vp = VirtualPointer(
            PointerConfig(
                width=cal.screen_width,
                height=cal.screen_height,
                gain=gain,
                accel=accel,
            )
        )
        self.sender.gain = gain
        self.push = PushThrough(escape_px)
        self.px_per_count = cal.screen_width / max(1, screen_w) * sensitivity

        self.kb = KeyboardState()
        self.cursor = mac_input.CursorGuard()
        self._wheel_acc = 0.0
        self._last_event_t = time.monotonic()
        self._dbg_t = time.monotonic()
        self._dbg_n = 0
        self._dbg_dx = 0
        self._dbg_dy = 0
        self._caps_on = False
        self.anchor = (float(screen_w) / 2, float(screen_h) / 2)
        self.on_state_change = None      # (capturing: bool) -> None，给 UI 用
        self.dbg_extra = None            # () -> str，诊断用，由入口程序注入

        # 光标动作一律记账，由 maintain_cursor() 在定时器里落地。
        # 只有"退出后挪到内侧"这一种请求，没有任何常驻状态。
        self._pending_warp: tuple[float, float] | None = None
        self._warp_count = 0
        self._warp_after = (0.0, 0.0)     # 最近一次归位的目标点
        self._grace_until = 0.0           # 进入捕获后的退出判定宽限期

    # ------------------------------------------------------------ 子类钩子

    def begin_capture(self, cursor_xy: tuple[float, float]) -> None:
        """进入捕获。落点由「从哪条边进来」决定。"""
        if self.capturing:
            return
        self.capturing = True
        self.push.reset()
        self._grace_until = time.monotonic() + 0.25
        self._last_event_t = time.monotonic()
        self._wheel_acc = 0.0
        self.kb.clear()
        self.cursor.hide()

        ipw, iph = self.cal.screen_width, self.cal.screen_height
        # 另一个轴按光标在边上的比例映射，符合"iPhone 就是屏幕外面接着的一块"这个直觉
        fx, fy = self.edge_fraction(cursor_xy)
        side = self.exit_side
        if side == "left":          # 从屏幕右缘移出 → 落在 iPhone 左缘
            tx, ty = 0.0, fy * (iph - 1)
        elif side == "right":       # 从屏幕左缘移出 → 落在 iPhone 右缘
            tx, ty = float(ipw - 1), fy * (iph - 1)
        elif side == "bottom":      # 从屏幕上缘移出（iPhone 在上方）→ 落在 iPhone 底缘
            tx, ty = fx * (ipw - 1), float(iph - 1)
        else:                       # 从屏幕下缘移出（iPhone 在下方）→ 落在 iPhone 顶缘
            tx, ty = fx * (ipw - 1), 0.0

        self.vp.set(tx, ty)
        self.target_x, self.target_y = tx, ty
        self.sender.jump_to(tx, ty)
        self.sender.invalidate_absolute()
        if self.on_state_change:
            self.on_state_change(True)
        if self.verbose:
            print(
                f"  ▶ 进入捕获  落点 ({tx:.0f},{ty:.0f})  模式 {self.mode}",
                flush=True,
            )

    def end_capture(self) -> None:
        if not self.capturing:
            return
        had_buttons = self.buttons
        self.buttons = 0
        # 退出时若还按着键，必须补一条释放报文，否则 iPhone 上会一直按住
        if had_buttons:
            if self.mode == "absolute":
                self.sender.send_absolute_px(0, self.vp.x, self.vp.y, relay_button=True)
            else:
                self.sender.send_mouse_now(0)

        self.capturing = False
        # 退出时把键盘也放开，否则 iPhone 上会一直按着某个键
        if self.kb.clear():
            self.sender.send_report(hid.RID_KEYBOARD, self.kb.report())
        self.cursor.show()
        self.push.reset()
        # 只挪一次光标（放到不会立刻再次触发的内侧位置）。
        # 绝不改"光标与鼠标的关联"：那是系统级开关，漏恢复一次，
        # 整台机器的指针就不再跟随鼠标，用户只能重启。
        self.warp_after_exit()
        if self.on_state_change:
            self.on_state_change(False)
        if self.verbose:
            print("  ◀ 退出捕获", flush=True)

    def edge_fraction(self, cursor_xy: tuple[float, float]) -> tuple[float, float]:
        """光标在屏幕内的归一化位置。子类可覆盖（面板模式用窗口坐标）。"""
        return (0.5, 0.5)

    def warp_after_exit(self) -> None:
        """退出后把光标放到一个不会立刻再次触发的位置。子类实现。"""

    # ------------------------------------------------------------ 鼠标

    def on_mouse(self, dx: int, dy: int) -> bool:
        if not self.capturing:
            return False
        if self.mode == "absolute":
            self._move_absolute(dx, dy)
        else:
            self._move_relative(dx, dy)
        return self.capturing

    def _move_absolute(self, dx: int, dy: int) -> bool:
        now = time.monotonic()
        dt = min(0.1, max(1e-4, now - self._last_event_t))
        self._last_event_t = now

        # 诊断：确认「事件给到的增量」与「物理光标实际位置」。
        # 光标贴在屏幕边上时 macOS 会把该方向的增量压成 0——这正是
        # 「iPhone 上指针只在左缘上下走」的直接原因，先量出来再改。
        if self.verbose:
            self._dbg_n += 1
            self._dbg_dx += dx
            self._dbg_dy += dy
            if now - self._dbg_t >= 1.0:
                cx, cy = mac_input.cursor_pos()
                extra = self.dbg_extra() if self.dbg_extra else ""
                print(
                    f"  [dbg] {self._dbg_n} 事件 dx={self._dbg_dx} dy={self._dbg_dy} "
                    f"vp=({self.vp.x:.0f},{self.vp.y:.0f}) 光标=({cx:.0f},{cy:.0f}) "
                    f"锚点=({self.anchor[0]:.0f},{self.anchor[1]:.0f}){extra}",
                    flush=True,
                )
                self._dbg_t = now
                self._dbg_n = 0
                self._dbg_dx = 0
                self._dbg_dy = 0

        applied = self.vp.move(dx, dy, dt)
        # 刚进来的这一小段不判退出。
        #
        # 推鼠标到边缘再顶出去，手上总有回抽；远程控制（Deskflow）更明显——
        # 它的位移是成块送来的（实测一次 -192、-31），而退出阈值只有几十像素，
        # 于是一进入就被判成"退回去"，表现是"进去闪一下又回到 Mac"。
        # 250ms 足够盖住这段惯性，又不影响主动退回。
        now2 = time.monotonic()
        if now2 < self._grace_until:
            self.push.reset()
            self.target_x, self.target_y = self.vp.x, self.vp.y
            return True
        # 有符号行程：进入点 = 0，往外为负；负到 escape_px 就释放回 Mac
        excess = self.vp.edge_excess_px(dx, dy, self.exit_side, applied)
        if self.push.update(excess > 0, excess):
            self.end_capture()
            return False
        self.target_x, self.target_y = self.vp.x, self.vp.y
        return True

    def _move_relative(self, dx: int, dy: int) -> None:
        self.target_x = max(
            0.0,
            min(float(self.cal.screen_width - 1), self.target_x + dx * self.px_per_count),
        )
        self.target_y = max(
            0.0,
            min(float(self.cal.screen_height - 1), self.target_y + dy * self.px_per_count),
        )

    # ------------------------------------------------------------ 光标

    def request_warp(self, x: float, y: float) -> None:
        """登记一次光标移动。真正的系统调用在 maintain_cursor() 里做。"""
        self._pending_warp = (float(x), float(y))

    def maintain_cursor(self) -> None:
        """光标状态的唯一落地执行点。必须从 run loop 的定时器调用。

        `CGWarpMouseCursorPosition` 是每次都要显式发起的单次动作，没有常驻状态，
        所以这里只处理"点一下"的请求。**不做伺服，也不碰光标关联**：

        早期版本在捕获期间把光标解耦（CGAssociateMouseAndMouseCursorPosition）
        并每 10ms 把它钉在屏幕中央。那是系统级状态，进程一旦没恢复成功，
        整台机器的鼠标就再也回不到自己屏幕上——必须重启。
        位移现在来自 IOHIDManager 的设备原始报文，跟光标在哪毫无关系，
        因此不需要、也不应该再动光标。
        """
        if self._pending_warp is not None:
            x, y = self._pending_warp
            self._pending_warp = None
            mac_input.warp_cursor(x, y)
            self._warp_count += 1
            self._warp_after = (x, y)
            return

        if self.capturing:
            self.cursor.hide()      # 引用计数，重复调用不会叠加
        else:
            self.cursor.show()

    def on_button(self, bit: int, down: bool) -> bool:
        if not self.capturing:
            return False
        before = self.buttons
        self.buttons = (self.buttons | bit) if down else (self.buttons & ~bit)
        if self.buttons == before:
            return True
        # 按钮变化立即发：按下和抬起经常落在同一个节拍里，等节拍会被合并成"没按过"
        if self.mode == "absolute":
            self.sender.send_absolute_px(
                self.buttons, self.vp.x, self.vp.y, relay_button=True
            )
        else:
            self.sender.send_mouse_now(self.buttons)
        return True

    def on_scroll(self, delta: float, continuous: bool = False) -> bool:
        """滚轮。macOS 的 DeltaAxis1 普通鼠标一格 ±1，触控板是连续小数。

        一格固定发 N 档（N = --wheel-step），不做"1.5 档摊成两条"的平滑：
        那会把一格拆成 1+1，节奏上看就是走两下。
        """
        if not self.capturing:
            return False
        self._wheel_acc += delta * self.wheel_step
        notches = int(self._wheel_acc)
        if notches == 0:
            return True
        self._wheel_acc -= notches
        self.sender.send_wheel(notches)
        return True

    # ------------------------------------------------------------ 键盘

    def on_key(self, keycode: int, down: bool, flags: int) -> bool:
        if not self.capturing:
            return False
        # 修饰键只有 flagsChanged，按下/抬起由标志位决定，不能信 down 参数
        bit = MODIFIER_BIT.get(keycode)
        if bit is not None:
            down = bool(flags & bit)
        elif keycode == KEY_CAPS_LOCK:
            # CapsLock 是锁定键：按一次发"按下+抬起"两个报文，否则 iPhone 上会一直按住
            if down and not self._caps_on:
                self._caps_on = True
                if self.kb.press(KEY_CAPS_LOCK):
                    self.sender.send_report(hid.RID_KEYBOARD, self.kb.report())
                self.kb.release(KEY_CAPS_LOCK)
                self.sender.send_report(hid.RID_KEYBOARD, self.kb.report())
            elif not down:
                self._caps_on = False
            return True

        keycode = self.key_remap.get(keycode, keycode)
        changed = self.kb.press(keycode) if down else self.kb.release(keycode)
        if changed:
            self.sender.send_report(hid.RID_KEYBOARD, self.kb.report())
        return True

    # ------------------------------------------------------------ 增益

    def set_gain(self, g: float) -> float:
        new = self.vp.set_gain(g)
        self.sender.gain = new
        print(f"  ⚡ 增益 {new:.2f} iPhone像素/计数", flush=True)
        return new
