"""主程序：鼠标越界 → 蓝牙 HID → iPhone 指针。

线程模型：
  主线程      Win32 消息循环，收 WM_INPUT（Raw Input），只做位移累加和状态机，
              不做任何 IO —— 消息回调里做重活会把消息队列堵死。
  HOGP 线程   asyncio 事件循环，每 30ms 取走目标位置，查校准表发一条报文。
              30ms 是硬要求：iOS 按报文到达速度分加速度档，改了表就失效。

为什么不是"进入捕获就归位"：
  归位要连发 28 条报文 = 0.84 秒，每次越界都等一次，体验很差。改成启动时归位一次，
  之后靠预测位置连续跟踪；漂得明显了按 Ctrl+Alt+H 手动归位。
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import os
import sys
import threading
import time

from . import hid
from .calibration import Calibration
from .edge_trigger import (
    Change, Edge, EdgeSpec, EdgeTrigger, EdgeTriggerConfig, edge_name, parse_edge,
)
from .geometry import Rect
from .hogp import HogpService
from .keymap import KeyboardState
from .vpointer import PointerConfig, PushThrough, VirtualPointer
from . import winput

# 边缘方向的中文名，只用于启动提示
_EDGE_CN = {Edge.Right: "右", Edge.Left: "左", Edge.Top: "上", Edge.Bottom: "下"}
_SIDE_CN = {"left": "左", "right": "右", "top": "上", "bottom": "下"}

CAL_DEFAULT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data",
    "calibration-iphone15pro.json",
)

SEND_INTERVAL = 0.03      # 相对模式：iOS 按报文速率分加速度档，30ms 是校准前提
ABS_INTERVAL = 0.01       # 绝对模式：位置是"快照"，速率不影响落点，10ms 足够跟手
HOLD_RESEND = 0.1         # 按住不动时补发按钮状态的周期
MOVE = 0x01  # usFlags 的 MOUSE_MOVE_ABSOLUTE 位

# 进入方向 → iPhone 上与之相邻的那条边（也就是"退出边"）
# 从屏幕下方移出 → 落到 iPhone 顶部；想回去就往回推（向上）。
ENTRY_SIDE = {
    Edge.Right: "left",
    Edge.Left: "right",
    Edge.Top: "bottom",
    Edge.Bottom: "top",
}


class Shared:
    """主线程与 HOGP 线程的唯一交点。只放数据，不放逻辑。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.target_x = 0.0
        self.target_y = 0.0
        self.buttons = 0
        self.capturing = False
        self.running = True


class HogpWorker(threading.Thread):
    def __init__(self, svc: HogpService, cal: Calibration, shared: Shared, *,
                 dry: bool, verbose: bool, mode: str = "relative"):
        super().__init__(daemon=True, name="hogp")
        self.svc = svc
        self.cal = cal
        self.shared = shared
        self.mode = mode
        self.dry = dry
        self.verbose = verbose
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.error: str | None = None
        self._px = 0.0
        self._py = 0.0
        self._last_dx = 0
        self._last_buttons = 0
        # 绝对模式：上次发出的 0..32767 坐标与上次发出的像素坐标（算拖动位移用）
        self._abs_x = -1
        self._abs_y = -1
        self._abs_fx = 0.0
        self._abs_fy = 0.0
        self._hold_at = 0.0
        self.sent = 0
        self.send_fail = 0
        # 诊断：实际报文速率。BLE 的 notify 是 await 的，真实速率可能远低于
        # 10ms 节拍——速率上不去，加大增益也只会变成"一跳一跳"。
        self.stat_t = time.monotonic()
        self.stat_n = 0
        self.stat_tick = 0
        self.gain = 0.0

    def run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        except Exception as e:  # noqa: BLE001 - 线程里必须兜住，否则静默退出
            self.error = str(e)
            self.ready.set()

    async def _main(self) -> None:
        if not self.dry:
            try:
                await self.svc.start()
            except Exception as e:  # noqa: BLE001
                self.error = f"GATT 服务启动失败: {e}"
                self.ready.set()
                return
        self.ready.set()

        interval = ABS_INTERVAL if self.mode == "absolute" else SEND_INTERVAL
        while self.shared.running:
            await asyncio.sleep(interval)
            await self._tick()
            now = time.monotonic()
            if now - self.stat_t >= 5.0:
                if self.shared.capturing and self.stat_n:
                    dt = now - self.stat_t
                    print(f"  [stat] {self.stat_n / dt:.0f} 报文/秒  "
                          f"节拍 {self.stat_tick / dt:.0f}/秒  增益 {self.gain:.2f}",
                          flush=True)
                self.stat_t = now
                self.stat_n = 0
                self.stat_tick = 0

    async def _tick(self) -> None:
        self.stat_tick += 1
        if not self.shared.capturing:
            return
        if self.mode == "absolute":
            await self._tick_absolute()
        else:
            await self._tick_relative()

    # ----- 相对模式：查校准表，30ms 一步 -----

    async def _tick_relative(self) -> None:
        with self.shared.lock:
            tx, ty = self.shared.target_x, self.shared.target_y
            buttons = self.shared.buttons

        step = self.cal.next_step(self._px, self._py, tx, ty, last_dx=self._last_dx)
        if step.dx == 0 and step.dy == 0 and buttons == self._last_buttons:
            return

        if step.dx or step.dy:
            self._px, self._py = step.x, step.y
            self._last_dx = step.dx
        self._last_buttons = buttons

        payload = hid.encode_relative_mouse(buttons, step.dx, step.dy)
        self.sent += 1
        if self.dry:
            if self.verbose:
                print(f"  report id=3 [{' '.join(f'{b:02X}' for b in payload)}] "
                      f"-> ({self._px:.0f},{self._py:.0f})", flush=True)
        else:
            # 发送失败（蓝牙断开/未订阅）不能把发送线程干掉，否则程序看着在跑但完全没输出
            try:
                ok = await self.svc.send_mouse(buttons, step.dx, step.dy)
                if not ok:
                    self.send_fail += 1
            except Exception:
                self.send_fail += 1

    # ----- 绝对模式：位置一条报文到位，按钮另走相对通道 -----

    async def _tick_absolute(self) -> None:
        with self.shared.lock:
            tx, ty = self.shared.target_x, self.shared.target_y
            buttons = self.shared.buttons

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
                await self._send_relative(buttons, dx, dy)
                self._abs_fx, self._abs_fy = tx, ty
                self._abs_x, self._abs_y = ax, ay
            elif btn_changed or time.monotonic() - self._hold_at >= HOLD_RESEND:
                await self._send_relative(buttons, 0, 0)
                self._hold_at = time.monotonic()
            return

        # 悬停：绝对位置一变就发。松开按钮的那一帧也要发一次，
        # 把指针重新锚回模型位置（拖动期间走的是相对位移，可能有累积误差）。
        if changed or btn_changed:
            await self._send_absolute(0, ax, ay)
            self._abs_x, self._abs_y = ax, ay
        self._abs_fx, self._abs_fy = tx, ty

    def _to_abs(self, x: float, y: float) -> tuple[int, int]:
        w = max(1, self.cal.screen_width - 1)
        h = max(1, self.cal.screen_height - 1)
        ax = int(round(max(0.0, min(float(w), x)) / w * hid.ABSOLUTE_MAX))
        ay = int(round(max(0.0, min(float(h), y)) / h * hid.ABSOLUTE_MAX))
        return ax, ay

    async def _send_absolute(self, buttons: int, ax: int, ay: int) -> None:
        self.sent += 1
        self.stat_n += 1
        if self.dry:
            if self.verbose:
                print(f"  report id=2 [{buttons:02X} {ax:04X} {ay:04X}]", flush=True)
            return
        try:
            ok = await self.svc.send(
                hid.RID_ABSOLUTE, hid.encode_absolute_pointer(buttons, ax, ay)
            )
            if not ok:
                self.send_fail += 1
        except Exception:
            self.send_fail += 1

    async def _send_relative(self, buttons: int, dx: int, dy: int) -> None:
        self.sent += 1
        self.stat_n += 1
        if self.dry:
            if self.verbose:
                print(f"  report id=3 [{buttons:02X} {dx:04d} {dy:04d}]", flush=True)
            return
        try:
            ok = await self.svc.send_mouse(buttons, dx, dy)
            if not ok:
                self.send_fail += 1
        except Exception:
            self.send_fail += 1

    def send_absolute_px(self, buttons: int, x: float, y: float,
                         relay_button: bool = False) -> None:
        """按钮状态变化时立即发，不等 10ms 节拍（按下+抬起可能落在同一拍里）。

        relay_button=True 时再补一条 dx=dy=0 的相对报文：绝对报文不带按钮语义，
        按下和抬起都必须由相对通道表达——只发按下不发抬起，iPhone 上就会一直按着。
        """
        ax, ay = self._to_abs(x, y)
        if self.dry or self.loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._send_absolute(buttons, ax, ay), self.loop)
            if relay_button:
                asyncio.run_coroutine_threadsafe(self._send_relative(buttons, 0, 0), self.loop)
        except Exception:
            pass

    def send_wheel(self, notches: int) -> None:
        """滚轮：Report3 的第 4 字节（int8）。一格 = ±1。

        必须立即发、不走 10ms 节拍——滚轮是离散事件，攒到下一拍就丢了手感。
        """
        if self.dry or self.loop is None:
            return
        n = max(-127, min(127, int(notches)))
        if n == 0:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.svc.send_mouse(0, 0, 0, n), self.loop
            )
        except Exception:
            pass

    def invalidate_absolute(self) -> None:
        """让下一次节拍无条件发一条绝对报文。

        每次重新进入捕获都要调用：否则新落点恰好等于上次发过的坐标时会被
        当成"没变化"跳过，iPhone 上的指针就停在上次的位置不动。
        """
        self._abs_x = -1
        self._abs_y = -1

    def jump_to(self, x: float, y: float) -> None:
        """归位后把预测位置重置到 (0,0)，不跨线程改 _px 的话会立刻又漂回去。"""
        self._px, self._py = x, y
        with self.shared.lock:
            self.shared.target_x = x
            self.shared.target_y = y

    @property
    def position(self) -> tuple[float, float]:
        return self._px, self._py

    def send_report(self, report_id: int, payload: bytes) -> None:
        """从任意线程投递一条报文（键盘事件走这里）。

        鼠标走 _tick 的 30ms 节流，键盘不能等——按键有按下/抬起两个状态，
        延迟 30ms 就会丢掉快速敲击。所以键盘绕过节流直接进 asyncio 循环。
        """
        if self.dry or self.loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.svc.send(report_id, payload), self.loop
            )
        except Exception:
            pass

    def send_mouse_now(self, buttons: int, dx: int = 0, dy: int = 0) -> None:
        """按钮状态变化立即发，不等 30ms 节流。

        点击的按下和抬起经常落在同一个 30ms 周期内，走节流会被合并成"没按过"。
        """
        if self.dry or self.loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.svc.send_mouse(buttons, dx, dy), self.loop
            )
        except Exception:
            pass

    def stop(self) -> None:
        self.shared.running = False
        if not self.dry:
            try:
                self.loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(self.svc.stop())
                )
            except Exception:
                pass


class App:
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
        self.cursor = winput.CursorGuard()
        self.swallower = winput.MouseSwallower()
        self.keyboard = winput.KeyboardForwarder()
        self.kb = KeyboardState()
        self.win: winput.RawInputWindow | None = None

        mon = winput.pick_monitor(args.screen)
        if mon is None:
            raise SystemExit(f"找不到显示器 {args.screen!r}，用 --list 看有哪些")
        self.screen = Rect(mon["x"], mon["y"], mon["w"], mon["h"])
        self.monitor_name = mon["device"]
        self.edge = EdgeSpec(
            edge=args.edge, screen=self.screen,
            zone_start=args.zone_start, zone_end=args.zone_end,
        )
        self.trigger = EdgeTrigger(
            EdgeTriggerConfig(enter_threshold=args.enter_threshold, exit_threshold=args.exit_threshold)
        )
        # 横向满屏对应 iPhone 横向满屏；纵向因宽高比差异不可能同时精确，用灵敏度补偿
        self.px_per_count = self.cal.screen_width / max(1, self.screen.w) * args.sensitivity

        # 绝对模式：位置由虚拟坐标累积，手可以无限推；不再依赖光标位置
        self.vp = VirtualPointer(PointerConfig(
            width=self.cal.screen_width,
            height=self.cal.screen_height,
            gain=args.gain,
            accel=args.accel,
        ))
        self.worker.gain = args.gain
        self.push = PushThrough(args.escape_px)
        # 进入边 → 退出边：从哪边进来，就往哪边推回去
        self.exit_side = ENTRY_SIDE[self.edge.edge]
        self._last_event_t = time.monotonic()
        self._ev_n = 0
        self._ev_t = time.monotonic()
        self._wheel_pending = 0.0
        self._wheel_t = 0.0

    # ---------- 捕获 ----------

    def begin_capture(self) -> None:
        self.shared.capturing = True
        self.cursor.hide()
        self.swallower.active = True
        self.keyboard.active = True
        self.push.reset()
        self._last_event_t = time.monotonic()
        self._wheel_pending = 0.0
        self._wheel_t = 0.0
        self.kb.clear()

        # 落点由"从哪条边进来"决定：入口边就是 iPhone 靠 Windows 的那一侧，
        # 于是往回推就是原路返回。另一个轴按光标在边上的比例映射，符合
        # "iPhone 就是屏幕外面接着的一块"这个直觉。
        l, t, w, h = self.screen.x, self.screen.y, self.screen.w, self.screen.h
        cx, cy = winput.cursor_pos()
        ipw, iph = self.cal.screen_width, self.cal.screen_height
        fx = (cx - l) / max(1, w - 1)
        fy = (cy - t) / max(1, h - 1)
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
        with self.shared.lock:
            self.shared.target_x = tx
            self.shared.target_y = ty
        # 同一落点第二次进入时坐标没变，不等节拍就必须重新锚一次
        self.worker.invalidate_absolute()

        # 指针挪到屏幕中央：贴着边缘时下面就是任务栏/窗口，万一漏吞会误点。
        # 绝对模式下光标位置已经不参与计算，这一步纯粹是安全措施。
        winput.user32.SetCursorPos(l + w // 2, t + h // 2)
        if self.args.verbose:
            print(f"  ▶ 进入捕获  edge={edge_name(self.edge.edge)}  "
                  f"落点 ({tx:.0f},{ty:.0f})  模式 {self.mode}", flush=True)

    def end_capture(self) -> None:
        with self.shared.lock:
            had_buttons = self.shared.buttons
            self.shared.buttons = 0
        # 退出时若还按着键，必须补一条释放报文，否则 iPhone 上会一直按住
        if had_buttons:
            if self.mode == "absolute":
                self.worker.send_absolute_px(0, self.vp.x, self.vp.y, relay_button=True)
            else:
                self.worker.send_mouse_now(0)

        self.shared.capturing = False
        self.swallower.active = False
        self.keyboard.active = False
        # 退出时把键盘也放开，否则 iPhone 上会一直按着某个键
        if self.kb.clear():
            self.worker.send_report(hid.RID_KEYBOARD, self.kb.report())
        self.cursor.show()
        self.trigger.reset()
        # 放回边缘内侧一点，否则会立刻再次触发进入，形成"进-退-进"的抖动
        l, t, w, h = self.screen.x, self.screen.y, self.screen.w, self.screen.h
        if self.edge.edge is Edge.Right:
            winput.user32.SetCursorPos(l + w - 120, t + h // 2)
        elif self.edge.edge is Edge.Left:
            winput.user32.SetCursorPos(l + 120, t + h // 2)
        elif self.edge.edge is Edge.Top:
            winput.user32.SetCursorPos(l + w // 2, t + 120)
        else:
            winput.user32.SetCursorPos(l + w // 2, t + h - 120)
        if self.args.verbose:
            print("  ◀ 退出捕获", flush=True)

    # ---------- 输入 ----------

    def on_mouse(self, dx: int, dy: int, btn_flags: int, wheel: int = 0) -> None:
        self._ev_n += 1
        if not self.shared.capturing:
            cx, cy = winput.cursor_pos()
            if self.trigger.update(self.edge, cx, cy, dx, dy) is Change.Entered:
                # 触发用的这一下位移不再计入手机端，否则刚进来就离边界半步
                self.begin_capture()
            return

        if wheel:
            self._on_wheel(wheel)

        if self.mode == "absolute":
            if not self._move_absolute(dx, dy):
                return
        else:
            # 相对模式仍然靠"反向推回"退出：光标位置在捕获时没有意义，
            # 只看朝内的累计位移
            cx, cy = winput.cursor_pos()
            if self.trigger.update(self.edge, cx, cy, dx, dy) is Change.Exited:
                self.end_capture()
                return
            self._move_relative(dx, dy)

        self._update_buttons(btn_flags)

    # 滚轮平滑发送的最小间隔：一次只发 1 档，靠时间摊开。
    # 取 30ms 而不是更小：BLE 实际投递约 30 报文/秒，发得比这快只会在协议栈里排队，
    # 表现成"手停了画面还在滚"。
    WHEEL_MIN_INTERVAL = 0.03

    def _on_wheel(self, delta: int) -> None:
        """滚轮只攒不发。

        一格原始增量 120，按 --wheel-scale 折算成小数档位；真正发送交给
        _wheel_tick，每 12ms 最多 1 档。这样 1.5 档/格不会变成"一次 1 档、
        一次 2 档"的忽快忽慢，而是均匀地滚。
        """
        step = 120.0 / max(0.1, self.args.wheel_scale)
        # Windows 与 HID 的滚轮符号相反，这里翻一次
        self._wheel_pending += -delta / step

    def _wheel_tick(self) -> None:
        if not self.shared.capturing or abs(self._wheel_pending) < 1.0:
            return
        now = time.monotonic()
        if now - self._wheel_t < self.WHEEL_MIN_INTERVAL:
            return
        self._wheel_t = now
        one = 1 if self._wheel_pending > 0 else -1
        self._wheel_pending -= one
        self.worker.send_wheel(one)

    def _move_absolute(self, dx: int, dy: int) -> bool:
        """虚拟坐标累积 + 顶边退出判定。返回 False 表示已经退出捕获。"""
        now = time.monotonic()
        dt = min(0.1, max(1e-4, now - self._last_event_t))
        self._last_event_t = now

        applied = self.vp.move(dx, dy, dt)
        # 有符号行程：进入点 = 0，向下为 +，向上为 −；负到 escape_px 就释放回 Windows
        excess = self.vp.edge_excess_px(dx, dy, self.exit_side, applied)
        if self.push.update(excess > 0, excess):
            self.end_capture()
            return False

        with self.shared.lock:
            self.shared.target_x = self.vp.x
            self.shared.target_y = self.vp.y
        return True

    def _move_relative(self, dx: int, dy: int) -> None:
        with self.shared.lock:
            self.shared.target_x = max(
                0.0, min(float(self.cal.screen_width - 1),
                         self.shared.target_x + dx * self.px_per_count)
            )
            self.shared.target_y = max(
                0.0, min(float(self.cal.screen_height - 1),
                         self.shared.target_y + dy * self.px_per_count)
            )

    def _update_buttons(self, btn_flags: int) -> None:
        # usButtonFlags 的按下/抬起位
        if not btn_flags:
            return
        with self.shared.lock:
            before = self.shared.buttons
            if btn_flags & 0x0001:
                self.shared.buttons |= 0x01
            if btn_flags & 0x0002:
                self.shared.buttons &= ~0x01
            if btn_flags & 0x0004:
                self.shared.buttons |= 0x02
            if btn_flags & 0x0008:
                self.shared.buttons &= ~0x02
            if btn_flags & 0x0010:
                self.shared.buttons |= 0x04
            if btn_flags & 0x0020:
                self.shared.buttons &= ~0x04
            after = self.shared.buttons
        if after == before:
            return
        # 按钮变化立即发：按下和抬起经常落在同一个 10ms 节拍里，等节拍会被合并成"没按过"
        if self.mode == "absolute":
            self.worker.send_absolute_px(after, self.vp.x, self.vp.y, relay_button=True)
        else:
            self.worker.send_mouse_now(after)

    def _drain_keys(self) -> None:
        """把钩子回调里攒下的按键事件转成 HID 报文。

        钩子只入队、不发送：发送要走 asyncio 线程，在 ~300ms 超时的回调里做不了。
        """
        while self.keyboard.events:
            vk, down = self.keyboard.events.popleft()
            if not self.shared.capturing:
                continue
            changed = self.kb.press(vk) if down else self.kb.release(vk)
            if changed:
                self.worker.send_report(hid.RID_KEYBOARD, self.kb.report())

    def force_exit(self) -> None:
        """Ctrl+Alt+Q：捕获中先退出捕获，再按一次结束程序。"""
        if self.shared.capturing:
            self.end_capture()
        else:
            self.shared.running = False

    def speed_up(self) -> None:
        self._set_gain(self.vp.cfg.gain * 1.25)

    def speed_down(self) -> None:
        self._set_gain(self.vp.cfg.gain / 1.25)

    def _set_gain(self, g: float) -> None:
        new = self.vp.set_gain(g)
        self.worker.gain = new
        print(f"  ⚡ 增益 {new:.2f} iPhone像素/计数", flush=True)

    # ---------- 主循环 ----------

    def run(self) -> int:
        # 三处恢复缺一不可：正常路径 finally、Ctrl+C、以及解释器退出（atexit）。
        # 少一处就会出现"桌面没有鼠标"的状态，只能重启资源管理器。
        atexit.register(self.shutdown)
        self.worker.start()
        self.worker.ready.wait(timeout=20)
        if self.worker.error:
            print(f"❌ {self.worker.error}")
            return 1

        print(f"校准 : {self.cal.screen_width}x{self.cal.screen_height} "
              f"{self.cal.max_count} 档 {self.cal.report_interval * 1000:.0f}ms")
        print(f"屏幕 : {self.monitor_name} {self.screen.w}x{self.screen.h} "
              f"at ({self.screen.x},{self.screen.y})")
        print(f"边缘 : {edge_name(self.edge.edge)}  "
              f"触发区 {self.edge.zone_start * 100:.0f}%-{self.edge.zone_end * 100:.0f}%  "
              f"进入阈值 {self.trigger.cfg.enter_threshold} 计数")
        if self.mode == "absolute":
            print(f"模式 : absolute  增益 {self.args.gain:.2f} iPhone像素/计数  "
                  f"顶边 {self.exit_side} 越过 {self.push.threshold:.0f}px 即释放")
        else:
            print(f"模式 : relative  比例 {self.px_per_count:.3f} iPhone像素/鼠标计数"
                  f"（灵敏度 {self.args.sensitivity}）")
        if self.args.dry_run:
            print("模式 : dry-run，报文只打印不发蓝牙")
        else:
            # 广播是异步起的，直接取会打印成 False，看着像失败
            t0 = time.monotonic()
            while not self.svc.advertising and time.monotonic() - t0 < 2.0:
                time.sleep(0.05)
            print(f"蓝牙 : 广播中={self.svc.advertising}  订阅={self.svc.subscribed}")

        self.win = winput.RawInputWindow(self.on_mouse)
        self.win.create()
        mods = winput.MOD_CONTROL | winput.MOD_ALT | winput.MOD_NOREPEAT
        if not self.win.register_hotkey(mods, winput.VK_Q, self.force_exit):
            print("⚠️ 全局热键 Ctrl+Alt+Q 注册失败（可能被别的程序占用），"
                  "退出捕获请改用反向推出。")
        fast_ok = self.win.register_hotkey(mods, winput.VK_OEM_PLUS, self.speed_up)
        slow_ok = self.win.register_hotkey(mods, winput.VK_OEM_MINUS, self.speed_down)
        if not (fast_ok and slow_ok):
            print("⚠️ Ctrl+Alt+= / Ctrl+Alt+- 调速热键注册失败，只能靠 --gain 启动参数。")
        self.swallower.install()
        self.keyboard.install()

        print("\n就绪。鼠标推向屏幕%s缘即接管 iPhone，落点在对侧 iPhone 的%s边（记为 0）；"
              "往回推越过该边 %.0fpx 即释放回 Windows。\n"
              "      捕获期间键盘一并转给 iPhone；Ctrl+Alt+= 加快 / Ctrl+Alt+- 放慢 / "
              "Ctrl+Alt+Q 释放（空闲时退出）。\n"
              % (_EDGE_CN[self.edge.edge], _SIDE_CN[self.exit_side], self.push.threshold))

        try:
            while self.shared.running:
                self._drain_keys()
                self._wheel_tick()
                now = time.monotonic()
                if now - self._ev_t >= 5.0:
                    if self.shared.capturing:
                        print(f"  [ev]   {self._ev_n / (now - self._ev_t):.0f} 输入事件/秒",
                              flush=True)
                    self._ev_t = now
                    self._ev_n = 0
                if not self.win.pump_once(1):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True
        self.shared.capturing = False
        if self.kb.clear():
            self.worker.send_report(hid.RID_KEYBOARD, self.kb.report())
        self.keyboard.uninstall()
        self.swallower.uninstall()
        self.cursor.show()  # 最后一道保险：无论如何先把系统指针还回去
        self.worker.stop()
        print(f"\n已退出。发送 {self.worker.sent} 条报文"
              f"（失败 {self.worker.send_fail}），吞掉 {self.swallower.swallowed} 个鼠标事件。")


def run_home(args) -> int:
    """只发归位报文，不碰鼠标。用来单独验证"蓝牙这一路真的能推动 iPhone 指针"。"""
    cal = Calibration.from_file(args.calibration)
    svc = HogpService()

    async def go() -> int:
        await svc.start()
        print(f"广播中={svc.advertising}，等 iPhone 订阅…")
        for _ in range(60):
            await asyncio.sleep(1)
            if svc.subscribed:
                break
        else:
            print("❌ 60 秒内没有客户端订阅。先确认 iPhone 已配对并在辅助触控里选中本机。")
            await svc.stop()
            return 1

        reports = cal.homing_reports()
        print(f"发送 {len(reports)} 条归位报文（约 {len(reports) * cal.report_interval:.1f}s）")
        for dx, dy in reports:
            if args.dry_run:
                print(f"  [{' '.join(f'{b:02X}' for b in hid.encode_relative_mouse(0, dx, dy))}]")
            else:
                await svc.send(hid.RID_RELATIVE, hid.encode_relative_mouse(0, dx, dy))
            await asyncio.sleep(cal.report_interval)
        print("归位完成：iPhone 指针应停在左上角")
        await svc.stop()
        return 0

    return asyncio.run(go())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ios-screen", description="把 iPhone 当作屏幕边缘外的一块屏（Windows 盲控版）"
    )
    ap.add_argument("--edge", default="right", help="越界方向 right/left/top/bottom，默认 right")
    ap.add_argument("--zone-start", type=float, default=0.0,
                    help="沿边触发区起点(0~1)，如 0.35 表示只让 35%% 之后的边段生效")
    ap.add_argument("--zone-end", type=float, default=1.0,
                    help="沿边触发区终点(0~1)")
    ap.add_argument("--screen", default="primary",
                    help="目标显示器：primary / 序号 / 设备名片段，默认 primary")
    ap.add_argument("--list", action="store_true", help="列出所有显示器后退出")
    ap.add_argument("--calibration", default=CAL_DEFAULT, help="校准 json")
    ap.add_argument("--mode", choices=["absolute", "relative"], default="absolute",
                    help="absolute=虚拟绝对坐标(默认，无累积延迟，手可无限推)；"
                         "relative=相对累积(需校准曲线，30ms 一步)")
    ap.add_argument("--gain", type=float, default=1.0,
                    help="绝对模式增益：一个鼠标计数换多少 iPhone 像素，默认 1.0")
    ap.add_argument("--accel", type=float, default=0.0,
                    help="绝对模式速度增益，0=线性(默认)，0.5~1 快划时放大行程")
    ap.add_argument("--escape-px", type=float, default=5.0,
                    help="越过退出边多少像素就释放回 Windows，默认 5")
    ap.add_argument("--wheel-scale", type=float, default=1.5,
                    help="滚轮速度：一格滚轮发多少 HID 档位，默认 1.5")
    ap.add_argument("--sensitivity", type=float, default=1.0, help="相对模式灵敏度倍数，默认 1.0")
    ap.add_argument("--enter-threshold", type=int, default=12, help="进入捕获所需朝外计数")
    ap.add_argument("--exit-threshold", type=int, default=60,
                    help="相对模式退出捕获所需朝内计数")
    ap.add_argument("--dry-run", action="store_true", help="不发蓝牙，只打印报文")
    ap.add_argument("--home", action="store_true", help="只发归位报文")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    try:
        args.edge = parse_edge(args.edge)
    except ValueError as e:
        print(e)
        return 2

    if args.list:
        for i, m in enumerate(winput.enum_monitors()):
            tag = "  [primary]" if m["primary"] else ""
            print(f"  [{i}] {m['device']:<14} {m['w']}x{m['h']} "
                  f"at ({m['x']},{m['y']}){tag}")
        return 0

    if not os.path.isfile(args.calibration):
        print(f"读不到校准文件: {args.calibration}")
        return 2

    if args.home:
        return run_home(args)
    return App(args).run()


if __name__ == "__main__":
    sys.exit(main())
