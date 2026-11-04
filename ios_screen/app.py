"""主程序：鼠标越界 → 蓝牙 HID → iPhone 指针。

线程模型
--------
主线程一条 CFRunLoop，跑四件事：

    CFRunLoop
      ├── AppKit 事件泵         抓取层的键盘/点击
      ├── CBPeripheralManager   订阅变化、读写请求
      └── NSTimer               10ms 报文节拍 + 边缘检测

另有一个**只读心跳的看门狗线程**。它不做业务，只在主循环卡死时把进程结束掉，
好让系统把输入还回去（见 safety.py）。

三条不许再犯的错误
------------------
这个版本最重要的事不是功能，是**别再把人家的机器搞瘫**。所以：

1. **不碰光标关联**。`CGAssociateMouseAndMouseCursorPosition(False)` 是系统级
   开关，漏恢复一次，整台机器的鼠标就再也回不到自己屏幕上，只能重启。
   位移现在来自 IOHIDManager 的设备原始报文，与光标在哪毫无关系。
2. **不吞全局键盘事件**。早期版本在捕获时用事件 tap 吞掉所有按键，程序一卡，
   键盘就在全系统范围内失联，连自救快捷键都收不到。现在改用一层透明窗口抢
   焦点，进程一死窗口就没，输入立刻恢复。
3. **任何"常驻的全局状态"都要能被别的线程清掉**。看门狗是最后一道保险。

为什么不用事件 tap 收位移
------------------------
光标贴到屏幕边缘后，WindowServer 会把该方向的增量压成 0（`kCGMouseEventDeltaX`
变 0）。于是到手时 dx 全是 0，iPhone 上表现为"指针只能沿左缘上下滑"。
设备原始报文没有这个问题——它就是鼠标/触控板真正移动了多少。

为什么不是"进入捕获就归位"
--------------------------
归位要连发 28 条报文 = 0.84 秒，每次越界都等一次，体验很差。改成启动时归位一次，
之后靠预测位置连续跟踪；漂得明显了用 --home 重新归位。
"""
from __future__ import annotations

import argparse
import collections
import os
import signal
import sys
import time

import AppKit
from AppKit import NSApplication
from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode
from Foundation import NSDate, NSDefaultRunLoopMode, NSTimer

from . import core, hid, mac_input, userconfig, vscreen
from .ble_hid import HidPeripheral, bundle_warning as bluetooth_bundle_warning
from .calibration import Calibration
from .capture_window import CaptureWindow
from .core import Controller, Sender, hotkey_for
from .edge_trigger import (
    Change,
    Edge,
    EdgeSpec,
    EdgeTrigger,
    EdgeTriggerConfig,
    edge_name,
    is_at_edge,
    parse_edge,
)
from .geometry import Rect
from .raw_mouse import RawMouse
from .safety import Watchdog

_EDGE_CN = {Edge.Right: "右", Edge.Left: "左", Edge.Top: "上", Edge.Bottom: "下"}
_SIDE_CN = {"left": "左", "right": "右", "top": "上", "bottom": "下"}

DEFAULT_CAL = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "calibration-iphone15pro.json"
)
DEFAULT_NAME = "Mac iPhone"

# AppKit 的事件掩码在某些 PyObjC 版本里没导出常量，用字面值兜底
NSEVENT_MASK_ANY = getattr(AppKit, "NSEventMaskAny", 0xFFFFFFFFFFFFFFFF)
NS_ACTIVATION_POLICY_ACCESSORY = 1


def pump_events(timeout: float = 0.02) -> None:
    """跑一次 AppKit 事件泵。

    没有 NSApp.run() 就没法把事件派发到窗口上——自己拿着 CFRunLoopRunInMode
    转圈，窗口能画出来但永远收不到键盘。这里手动取一条事件再交给 NSApp。
    """
    app = NSApplication.sharedApplication()
    event = app.nextEventMatchingMask_untilDate_inMode_dequeue_(
        NSEVENT_MASK_ANY,
        NSDate.dateWithTimeIntervalSinceNow_(timeout),
        NSDefaultRunLoopMode,
        True,
    )
    if event is not None:
        app.sendEvent_(event)


class App(Controller):
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

        super().__init__(
            self.cal,
            self.sender,
            mode=args.mode,
            gain=args.gain,
            accel=args.accel,
            escape_px=args.escape_px,
            wheel_step=args.wheel_step,
            sensitivity=args.sensitivity,
            screen_w=self.screen.w,
            screen_h=self.screen.h,
            exit_side=core.ENTRY_SIDE[args.edge.value],
            key_remap=userconfig.key_remap(args.config),
            verbose=args.verbose,
        )
        self.anchor = (
            self.screen.x + self.screen.w / 2.0,
            self.screen.y + self.screen.h / 2.0,
        )

        self.edge = EdgeSpec(
            edge=args.edge,
            screen=self.screen,
            zone_start=args.zone_start,
            zone_end=args.zone_end,
        )
        self.trigger = EdgeTrigger(
            EdgeTriggerConfig(
                enter_threshold=args.enter_threshold,
                exit_threshold=args.exit_threshold,
            )
        )
        self.running = True

        # 捕获期间抢焦点的那层窗口：键盘和点击落到它身上，不再进别的应用。
        # 它没有任何全局副作用，进程一死就消失。
        self.win = CaptureWindow(
            on_button=self.on_button,
            on_scroll=self.on_scroll,
            on_key=self.on_key,
            verbose=args.verbose,
            frame_provider=self._capture_frame,
        )
        self.on_state_change = self._on_capture_state

        self._timer = None
        self._started_at = time.monotonic()
        self.heartbeat = time.monotonic()
        self._watchdog = None

        # 位移的唯一来源：设备原始报文。事件增量会被屏幕边缘钳成 0，靠不住。
        self.raw = RawMouse()
        self.raw_active = False
        self.raw_ready = False

        self.dbg_extra = lambda: (
            f" raw={self.raw.status} 抓取层={'上屏' if self.win.shown else '无'}"
            f" tap(事件={self._tap_events} 超时={self.tap.timeouts})"
            f" 远程={self._remote_n}"
        )

        # [edge] 诊断计数（每秒一行）
        self._edbg_t = time.monotonic()
        self._edbg_n = 0
        self._edbg_dx = 0

        # 光标位移 fallback（Deskflow 这类远程控制没有 HID 报文）
        self._last_cursor = mac_input.cursor_pos()
        self._remote_n = 0

        # 只读事件 tap：Deskflow 的位移是**合成**鼠标事件，IOHIDManager 收不到，
        # 但事件里的 DeltaX/Y 还带着。这个 tap 强制 listen-only，绝不吞任何事件，
        # 因此不会像早期版本那样把用户的键盘/鼠标"锁死"。
        #
        # 虚拟屏模式下它还兼任按钮/滚轮的来源：实测 CGVirtualDisplay 那块屏上的
        # 窗口收不到 mouseDown（frame 盖住、hitTest 命中，事件就是不派发），
        # 而同一位置的点击在 tap 里看得一清二楚。
        self._tap_dx = 0
        self._tap_dy = 0
        self._tap_events = 0
        self._tap_evt_q: collections.deque = collections.deque()
        self.remote_ready = False
        self.tap = mac_input.EventTap(
            on_mouse=self._collect_tap_delta,
            on_button=self._on_tap_button,
            on_scroll=self._on_tap_scroll,
            on_key=lambda *a: False,
            should_swallow=lambda: False,
            force_listen_only=True,
            verbose=args.verbose,
        )

        # 虚拟屏：让 iPhone 在系统层面成为主屏右侧的一块屏。
        # 有它之后就不需要边缘检测——光标走进那块屏就是进了 iPhone，
        # 位置直接 1:1 映射。Deskflow 的光标也因此有地方可走，
        # 不再被钳在 1680 上、DeltaX 归零。
        self.vscreen = None if args.no_vscreen else vscreen.VirtualScreen(
            name=args.name, verbose=args.verbose
        )
        self._vsdbg_t = time.monotonic()
        self._in_timer = False
        # 离开虚拟屏要连续几拍才算数：Deskflow 的位移是成块跳的，
        # 光标可能一瞬间落在边界外，单拍就判退出会让捕获疯狂进出。
        self._leave_n = 0

    # ------------------------------------------------------------ 触发

    def _feed_mouse(self, dx: int, dy: int) -> bool:
        """所有位移的唯一入口。dx/dy 来自 IOHIDManager 的原始计数。"""
        if self.capturing:
            return super().on_mouse(dx, dy)
        # 防御：退出捕获的路径不止一条（往回推越界、Ctrl+Option+Q、面板关窗）。
        # 只要有一条漏了复位，trigger.active 就会留在 True，此后 update() 一直走
        # "捕获中"分支（只看位移方向、不再判贴边），永远不返回 Entered。
        # 表现正是"第一次能推进去，出来之后再怎么推都回不去"。
        if self.trigger.active:
            self.trigger.reset()
        cx, cy = mac_input.cursor_pos()
        change = self.trigger.update(self.edge, int(round(cx)), int(round(cy)), dx, dy)
        if self.args.verbose:
            self._edge_dbg(dx, dy, cx, cy)
        if change is Change.Entered:
            # 触发用的这一下位移不再计入手机端，否则刚进来就离边界半步
            self.begin_capture((cx, cy))
            return True
        if change is Change.Exited:
            self.end_capture()
        return False

    def _edge_dbg(self, dx: int, dy: int, cx: float, cy: float) -> None:
        """空闲时每秒一行，说明"推不进去"卡在哪一环。

        三种失败长得一模一样（推了没反应），但原因完全不同：
          贴边=False        → 光标没到边上，或坐标判定错
          贴边=True dx<=0   → 到边了但这一秒没有朝外位移
          accum 涨到一半    → 阈值没到（enter_threshold）
        """
        self._edbg_n += 1
        self._edbg_dx += dx
        now = time.monotonic()
        if now - self._edbg_t < 1.0:
            return
        r = self.edge.screen
        at = is_at_edge(self.edge, int(round(cx)), int(round(cy)))
        print(
            f"  [edge] 光标=({cx:.0f},{cy:.0f}) 距边={r.right - cx:.0f} "
            f"贴边={at} dx={self._edbg_dx} accum={self.trigger.accum}/"
            f"{self.trigger.cfg.enter_threshold} active={self.trigger.active}",
            flush=True,
        )
        self._edbg_t = now
        self._edbg_n = 0
        self._edbg_dx = 0

    # ------------------------------------------------------- 虚拟屏输入分流

    def _vscreen_mode(self) -> bool:
        return self.vscreen is not None and self.vscreen.active

    def _in_vscreen(self, x: float, y: float) -> bool:
        r = self.vscreen.rect if self.vscreen is not None else None
        if r is None:
            return False
        vx, vy, vw, vh = r
        return vx <= x < vx + vw and vy <= y < vy + vh

    def _capture_frame(self):
        """抓取层该盖哪一块（AppKit 全局坐标，左下原点）。

        虚拟屏模式下**只盖真实屏**。原因不是省事，是那块屏上的窗口收不到
        鼠标事件：frame 明明盖住了、`hitTest:` 也命中，`mouseDown:` 就是不
        被调用；同一位置的点击，只读 tap 却能完整看到。既然盖不住，就别盖，
        让点击穿透到虚拟屏的空桌面（那里没有应用，穿透无害），由 tap 转发。

        键盘不受影响：键盘只认 key window，与窗口盖在哪块屏无关。
        """
        if not self._vscreen_mode() or not self.vscreen.display_id:
            return None
        rects = []
        for s in AppKit.NSScreen.screens():
            try:
                did = int(s.deviceDescription()["NSScreenNumber"])
            except Exception:
                did = -1
            f = s.frame()
            rects.append(
                (
                    did,
                    float(f.origin.x),
                    float(f.origin.y),
                    float(f.size.width),
                    float(f.size.height),
                )
            )
        return vscreen.union_except(rects, self.vscreen.display_id)

    def on_button(self, bit: int, down: bool) -> bool:
        """窗口这条路的按钮。虚拟屏模式下让位给 tap，避免重复发送。"""
        if self._vscreen_mode() and self.tap.tap is not None:
            return False
        return super().on_button(bit, down)

    def on_scroll(self, delta: float, continuous: bool = False) -> bool:
        if self._vscreen_mode() and self.tap.tap is not None:
            return False
        return super().on_scroll(delta, continuous)

    def _on_tap_button(self, bit: int, down: bool) -> bool:
        """tap 看到按钮：只入队，不在这里发报文。

        tap 回调有约 1 秒的超时，超时一次系统就把 tap 摘掉（表现是"用着用着
        突然失灵"）。所以回调里绝不碰蓝牙，真正的发送交给主循环。
        """
        if not self._vscreen_mode() or not self.capturing:
            return False
        x, y = self.tap.last_button_pos
        if not self._in_vscreen(x, y):
            return False
        self._tap_evt_q.append(("btn", bit, down))
        return False

    def _on_tap_scroll(self, delta: float, continuous: bool) -> bool:
        if not self._vscreen_mode() or not self.capturing:
            return False
        x, y = self.tap.last_scroll_pos
        if not self._in_vscreen(x, y):
            return False
        self._tap_evt_q.append(("scroll", delta, continuous))
        return False

    def _drain_tap_events(self) -> None:
        """把 tap 攒下的按钮/滚轮到主循环里落地（每拍最多 10ms 延迟）。"""
        while self._tap_evt_q:
            ev = self._tap_evt_q.popleft()
            if not self.capturing:
                continue
            if self.args.verbose:
                if ev[0] == "btn":
                    print(f"  [tap] 按钮 bit={ev[1]} down={ev[2]} → 转给 iPhone", flush=True)
                else:
                    print(f"  [tap] 滚轮 {ev[1]:+.1f} → 转给 iPhone", flush=True)
            if ev[0] == "btn":
                super().on_button(ev[1], ev[2])
            else:
                super().on_scroll(ev[1], ev[2])

    def on_key(self, keycode: int, down: bool, flags: int) -> bool:
        name = hotkey_for(keycode, flags)
        if name:
            if down:
                if name == "quit":
                    self.force_exit()
                elif name == "faster":
                    self.set_gain(self.vp.cfg.gain * 1.25)
                else:
                    self.set_gain(self.vp.cfg.gain / 1.25)
            # 热键本身不能转发给 iPhone，否则 iPhone 上会突然多出一个 Q
            return True
        return super().on_key(keycode, down, flags)

    def _on_capture_state(self, capturing: bool) -> None:
        # 进出都要把离开计数清零，否则上一次残留的计数会让下一次刚进来就退出
        self._leave_n = 0
        if capturing:
            self.win.show()
        else:
            # 统一出口：不管从哪条路径退出捕获，边缘状态机都要回到"未激活"。
            # 漏掉这一步的后果见 _feed_mouse 的注释——再也推不进去。
            self.trigger.reset()
            self.win.hide()

    def _refresh_geometry(self) -> None:
        """虚拟屏建好之后重新读一次主屏。

        vdisplay 在创建虚拟屏的过程中会把主屏分辨率临时重置（然后恢复），
        所以 __init__ 里读到的那组坐标可能已经过时。
        """
        mon = mac_input.pick_display(self.args.screen)
        if mon is None:
            return
        self.monitor = mon
        self.screen = Rect(mon["x"], mon["y"], mon["w"], mon["h"])
        self.anchor = (
            self.screen.x + self.screen.w / 2.0,
            self.screen.y + self.screen.h / 2.0,
        )
        self.edge = EdgeSpec(
            edge=self.args.edge,
            screen=self.screen,
            zone_start=self.args.zone_start,
            zone_end=self.args.zone_end,
        )

    def edge_fraction(self, cursor_xy: tuple[float, float]) -> tuple[float, float]:
        cx, cy = cursor_xy
        fx = (cx - self.screen.x) / max(1, self.screen.w - 1)
        fy = (cy - self.screen.y) / max(1, self.screen.h - 1)
        return (max(0.0, min(1.0, fx)), max(0.0, min(1.0, fy)))

    def warp_after_exit(self) -> None:
        """放回边缘内侧一点，否则会立刻再次触发进入，形成"进-退-进"的抖动。"""
        if self.vscreen is not None and self.vscreen.active:
            # 虚拟屏模式没有"边缘触发"这回事：光标离开虚拟屏就已经在主屏上了，
            # 再搬一次等于从用户手里把光标抢走。
            return
        l, t, w, h = self.screen.x, self.screen.y, self.screen.w, self.screen.h
        e = self.edge.edge
        if e is Edge.Right:
            self.request_warp(l + w - 120, t + h // 2)
        elif e is Edge.Left:
            self.request_warp(l + 120, t + h // 2)
        elif e is Edge.Top:
            self.request_warp(l + w // 2, t + 120)
        else:
            self.request_warp(l + w // 2, t + h - 120)

    def force_exit(self) -> None:
        """Ctrl+Option+Q：捕获中先退出捕获，再按一次结束程序。"""
        if self.capturing:
            self.end_capture()
        else:
            self.running = False

    # ------------------------------------------------------------ 运行

    def run(self) -> int:
        # AppKit 先起来：窗口、事件泵都要它
        app = NSApplication.sharedApplication()
        try:
            app.setActivationPolicy_(NS_ACTIVATION_POLICY_ACCESSORY)
        except Exception:
            pass

        if not self.args.dry_run:
            warn = bluetooth_bundle_warning()
            if warn:
                print(warn)
                return 2
            self.ble.start()
            if not self._wait_ble(3.0):
                print(f"❌ 蓝牙没有就绪（state={self.ble.state}）")
                return 1

        # 位移只能来自原始报文。没有它就没有任何可用的输入源——
        # 与其给一个"能连上但推不动"的半成品，不如明确报错。
        self.raw_active = self.raw.start()
        if not self.raw_active and not self.args.dry_run:
            print(
                "❌ 读不到设备原始位移。\n"
                f"   {self.raw.last_error}\n"
                "   到「系统设置 → 隐私与安全性 → 输入监控」勾选本应用，然后重启。\n"
                "   没有这项权限，光标一贴边增量就会被系统压成 0，指针根本推不动。"
            )
            self.ble.stop()
            return 1
        print(f"  [raw] 原始位移已启用  {self.raw.status}", flush=True)

        # 只读 tap：远程控制（Deskflow）的合成位移只有这里看得见。
        # 拿不到也不致命——物理设备那条路仍然可用。
        if self.tap.start():
            print(
                f"  [cur] 只读事件 tap 已就绪（位置={self.tap.location} "
                f"只监听={self.tap.listen_only}），可接 Deskflow 等远程光标",
                flush=True,
            )
        else:
            print("  [cur] 只读事件 tap 没装上：远程控制的光标将无法推进 iPhone", flush=True)

        # 虚拟屏：让 iPhone 在系统层面成为主屏右侧一块真实的屏。
        # 这是 Deskflow 场景的唯一解——它的合成位移在光标顶到屏幕边缘后
        # 会被钳成 0，只有让桌面真的变宽，光标才有地方可走。
        if self.vscreen is not None:
            if self.vscreen.start():
                # vdisplay 在创建后还会重排显示排列、恢复主屏分辨率，
                # 这些都要几秒才落定，早读会拿到中间状态
                time.sleep(2.5)
                self._refresh_geometry()
                r = self.vscreen.rect
                if r:
                    print(
                        f"  [vs] 虚拟屏 {r[2]}x{r[3]} 点 at ({r[0]},{r[1]})："
                        f"光标走进这块屏即接管 iPhone（离开即释放）",
                        flush=True,
                    )
                if self.vscreen.error:
                    print(f"      注意：{self.vscreen.error}", flush=True)
            else:
                print(
                    f"  ⚠️ 虚拟屏没建起来（{self.vscreen.error}），"
                    f"退回边缘触发模式",
                    flush=True,
                )
                self.vscreen = None

        self._print_banner()
        self._timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            self.sender.interval, True, self._on_timer
        )

        # 最后一道保险：主循环一旦卡住就结束进程，把输入还给系统
        self.heartbeat = time.monotonic()
        self._watchdog = Watchdog(lambda: self.heartbeat, timeout=2.5)
        self._watchdog.start()

        # 历史版本可能留下过"光标与鼠标解耦"的全局状态，启动时先清一次
        mac_input.force_associate_cursor()

        import atexit

        atexit.register(mac_input.force_associate_cursor)
        deadline = time.monotonic() + self.args.self_test if self.args.self_test > 0 else 0.0
        old_int = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self._on_sigint)
        try:
            signal.signal(signal.SIGTERM, self._on_sigint)
        except Exception:
            pass
        try:
            while self.running:
                pump_events(0.02)
                if deadline and time.monotonic() > deadline:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            signal.signal(signal.SIGINT, old_int)
            self.shutdown()
        return 0

    def _on_sigint(self, signum, frame) -> None:
        self.running = False

    def _on_timer(self, timer=None) -> None:
        # 重入保护。
        #
        # `win.show()` 里的 `activateIgnoringOtherApps_` 会同步跑一段 runloop，
        # NSTimer 因此可能在自己的回调里被再次触发。重入那一刻读到的是**尚未
        # 稳定的中间状态**（窗口刚在改尺寸、光标还没落定），`_tick_vscreen`
        # 会据此判定"已经离开虚拟屏"，于是刚进捕获就退出——而这时按下报文
        # 刚发出去、抬起还没发，iPhone 上等于没点。这就是「点击全部失灵」。
        if self._in_timer:
            return
        self._in_timer = True
        try:
            self._tick_once()
        finally:
            self._in_timer = False

    def _tick_once(self) -> None:
        self.heartbeat = time.monotonic()
        # 注：这里曾经试过把光标从边缘拉回屏幕中央，想让 Deskflow 重新有
        # 地方可走。实测无效——client 维护的是自己的内部坐标，读到 1680 就
        # 钳死在那里，系统光标挪到哪它都只发 DeltaX=0。所以不要在这里做
        # 光标伺服，那只会让它的内部位置和系统光标长期不一致。
        self.maintain_cursor()
        # 撤下抓取层之后要分几步把焦点交还，必须在主循环里推进
        self.win.tick()

        if self.vscreen is not None and self.vscreen.active:
            # 虚拟屏模式：输入只有一个来源——光标在哪，iPhone 指针就在哪。
            # 不需要 raw、不需要边缘判定，物理鼠标和 Deskflow 走的是同一条路。
            self._tick_vscreen()
            # 按钮/滚轮来自只读 tap（窗口在那块屏上收不到），在这里落地。
            # 放在 _tick_vscreen 之后：先进捕获再处理点击，第一下才点得中。
            self._drain_tap_events()
            # 这一路的增量用不上，每拍清空，免得整数无限累积
            self._take_tap_delta()
        elif self.raw_active:
            dx, dy = self.raw.take()
            # tap 的累积每拍都清空：物理鼠标时它和 raw 是同一份位移，
            # 只在 raw 为 0（远程合成）时才有独立价值
            tdx, tdy = self._take_tap_delta()
            if dx or dy:
                if not self.raw_ready:
                    self.raw_ready = True
                    print(
                        f"  [raw] 收到原始位移，已切换 ({self.raw.samples} 样本)",
                        flush=True,
                    )
                # 让光标基准跟上，否则下一拍走 fallback 时会把这期间的光标
                # 移动当成新的位移，重复计算一次
                self._last_cursor = mac_input.cursor_pos()
                self._feed_mouse(dx, dy)
            elif tdx or tdy:
                # 设备没动、事件里有位移 —— 这是 Deskflow 这类远程控制的合成输入
                if not self.remote_ready:
                    self.remote_ready = True
                    print(
                        f"  [cur] 检测到合成鼠标事件（Deskflow 等），已接管"
                        f"  首个位移 ({tdx},{tdy})",
                        flush=True,
                    )
                self._last_cursor = mac_input.cursor_pos()
                self._feed_mouse(tdx, tdy)
            else:
                self._feed_cursor_delta()

        if not self.capturing:
            return
        # 激活是异步的，每拍补一次，直到抓取层真的拿到键盘
        self.win.ensure_key()
        self.sender.tick(self.target_x, self.target_y, self.buttons)

    # ------------------------------------------------------- 虚拟屏模式

    def _tick_vscreen(self) -> None:
        """虚拟屏模式：光标在虚拟屏里的位置，直接就是 iPhone 上的位置。

        这是唯一一条不需要"推过去"这个动作的路径。虚拟屏在系统层面就是一块
        真显示器，所以物理鼠标、触控板、Deskflow 的合成光标全都一样——它们
        都只是把光标移到某个坐标，既不用抓 HID 报文，也不用判边缘。

        映射是纯粹的等比：393 pt 宽 → 1180 像素，1 pt = 3 px，和 Retina 一致。
        """
        r = self.vscreen.rect
        if r is None:
            return
        vx, vy, vw, vh = r
        cx, cy = mac_input.cursor_pos()
        inside = vx <= cx < vx + vw and vy <= cy < vy + vh

        if inside:
            self._leave_n = 0
            if not self.capturing:
                self.begin_capture((cx, cy))
            fx = (cx - vx) / max(1, vw - 1)
            fy = (cy - vy) / max(1, vh - 1)
            tx = max(0.0, min(float(self.cal.screen_width - 1), fx * (self.cal.screen_width - 1)))
            ty = max(0.0, min(float(self.cal.screen_height - 1), fy * (self.cal.screen_height - 1)))
            self.vp.set(tx, ty)
            self.target_x, self.target_y = tx, ty
            if self.args.verbose:
                now = time.monotonic()
                if now - self._vsdbg_t >= 1.0:
                    self._vsdbg_t = now
                    print(
                        f"  [vs] 光标=({cx:.0f},{cy:.0f}) 虚拟屏相对="
                        f"({cx - vx:.0f},{cy - vy:.0f})/{vw}x{vh} "
                        f"→ iPhone=({tx:.0f},{ty:.0f})",
                        flush=True,
                    )
        elif self.capturing:
            # 连续 3 拍（30ms）都在外面才算真的走了。单拍就退出的后果：
            # Deskflow 成块送位移时，光标可能一瞬间落在界外，捕获会疯狂进出，
            # 每次进出都要重发一轮报文，点击因此在半路被掐断。
            self._leave_n += 1
            if self._leave_n == 1 and self.args.verbose:
                print(
                    f"  [vs] 光标离开虚拟屏 ({cx:.0f},{cy:.0f}) "
                    f"rect=({vx},{vy},{vw},{vh}) 开始计时",
                    flush=True,
                )
            if self._leave_n >= 3:
                self._leave_n = 0
                self.end_capture()

    # ------------------------------------------------------- 光标位移（远程控制）

    def _collect_tap_delta(self, dx: int, dy: int) -> bool:
        """只读 tap 的鼠标回调：攒增量，永远返回 False（不吞）。"""
        self._tap_dx += dx
        self._tap_dy += dy
        self._tap_events += 1
        return False

    def _take_tap_delta(self) -> tuple[int, int]:
        dx, dy = self._tap_dx, self._tap_dy
        self._tap_dx = self._tap_dy = 0
        return dx, dy

    def _feed_cursor_delta(self) -> None:
        """本机没有任何 HID 报文时，用光标位置的变化当位移来源。

        为什么需要这条路
        ----------------
        Deskflow / Synergy 这类远程键鼠工具，是把远端的鼠标事件在本地**合成**
        出来的：系统里多了一次光标移动，但物理设备一个报文都没发。所以
        IOHIDManager 拿到的是 0，边缘检测永远不触发——现象就是
        "触控板能推进去，Deskflow 的光标怎么推都进不去"。

        合成长什么样是可以认出来的：**设备没动，光标却动了**。这一拍直接读
        光标位置差，按 1 点 = 1 计数喂给同一条处理链，于是贴边判定、阈值、
        增益全都不用改。

        一次跳超过 MAX_REMOTE_JUMP 点的，是 Deskflow 切换屏幕/程序回中，
        不是人推的，丢掉——否则会凭空在 iPhone 上甩出一大段位移。
        """
        MAX_REMOTE_JUMP = 120.0
        cx, cy = mac_input.cursor_pos()
        lx, ly = self._last_cursor
        self._last_cursor = (cx, cy)
        sdx, sdy = cx - lx, cy - ly
        if abs(sdx) < 0.5 and abs(sdy) < 0.5:
            return
        if abs(sdx) > MAX_REMOTE_JUMP or abs(sdy) > MAX_REMOTE_JUMP:
            if self.args.verbose:
                print(f"  [cur] 丢弃大跳 ({sdx:.0f},{sdy:.0f})", flush=True)
            return
        self._remote_n += 1
        if self.args.verbose and self._remote_n % 100 == 1:
            print(
                f"  [cur] 用光标位移驱动（第 {self._remote_n} 次）"
                f" ({sdx:+.0f},{sdy:+.0f}) 光标=({cx:.0f},{cy:.0f})",
                flush=True,
            )
        self._feed_mouse(int(round(sdx)), int(round(sdy)))

    def _wait_ble(self, timeout: float) -> bool:
        if self.args.dry_run:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ble.powered_on and self.ble.advertising:
                return True
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, True)
        return self.ble.powered_on

    def _print_banner(self) -> None:
        c = self.cal
        print(f"校准 : {c.screen_width}x{c.screen_height} {c.max_count} 档 "
              f"{c.report_interval * 1000:.0f}ms")
        print(f"屏幕 : {self.monitor['name']} {self.screen.w}x{self.screen.h} 点 "
              f"at ({self.screen.x},{self.screen.y})")
        print(f"边缘 : {edge_name(self.edge.edge)}  "
              f"触发区 {self.edge.zone_start * 100:.0f}%-{self.edge.zone_end * 100:.0f}%  "
              f"进入阈值 {self.trigger.cfg.enter_threshold} 计数")
        if self.key_remap:
            print(f"配置 : 已加载 {len(self.key_remap)} 项按键映射（user_config.json）")
        if self.mode == "absolute":
            print(f"模式 : absolute  增益 {self.args.gain:.2f} iPhone像素/计数  "
                  f"顶边 {self.exit_side} 越过 {self.push.threshold:.0f}px 即释放")
        else:
            print(f"模式 : relative  比例 {self.px_per_count:.3f} iPhone像素/鼠标计数"
                  f"（灵敏度 {self.args.sensitivity}）")
        print(f"蓝牙 : {self.ble.status}  名字 {self.args.name!r}  "
              f"订阅 {self.ble.subscription_detail}")
        if self.args.dry_run:
            print("模式 : dry-run，报文只打印不发蓝牙")

        nb = mac_input.has_neighbor(self.monitor, edge_name(self.edge.edge))
        if nb and (self.vscreen is None or not self.vscreen.active):
            print(
                f"⚠️ {edge_name(self.edge.edge)}侧还接着 {nb['name']} "
                f"({nb['w']}x{nb['h']})：光标会先跑到那块屏上，边缘触发不会生效。"
                f"换 --screen 或改用面板模式。"
            )
        if self.vscreen is not None and self.vscreen.active:
            r = self.vscreen.rect or (0, 0, 0, 0)
            print(
                f"\n就绪。右侧那块 {r[2]}x{r[3]} 的虚拟屏就是 iPhone："
                f"光标移进去即接管，移出来即释放。\n"
                f"      位置是绝对值映射（1 pt = 3 px），指针在哪就在哪，"
                f"不需要「推」这个动作。\n"
                f"      物理鼠标、触控板、Deskflow 的远程光标走的是同一条路。\n"
                f"      捕获期间键盘一并转给 iPhone；Ctrl+Option+Q 释放"
                f"（空闲时退出）。\n"
                f"      安全设计：不吞全局事件、不改光标关联。进程若卡死，"
                f"2.5 秒内会被看门狗强制结束，输入自动还给系统。"
            )
            return
        print(
            f"\n就绪。鼠标推向屏幕{_EDGE_CN[self.edge.edge]}缘即接管 iPhone，"
            f"落点在对侧 iPhone 的{_SIDE_CN[self.exit_side]}边；"
            f"往回推越过该边 {self.push.threshold:.0f}px 即释放。\n"
            f"      捕获期间键盘一并转给 iPhone；Ctrl+Option+= 加快 / "
            f"Ctrl+Option+- 放慢 / Ctrl+Option+Q 释放（空闲时退出）。\n"
            f"      安全设计：不吞全局事件、不改光标关联。进程若卡死，"
            f"2.5 秒内会被看门狗强制结束，输入自动还给系统。"
        )

    def shutdown(self) -> None:
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True
        if self._watchdog is not None:
            self._watchdog.stop()
        if self._timer is not None:
            try:
                self._timer.invalidate()
            except Exception:
                pass
        self.capturing = False
        if self.kb.clear():
            self.sender.send_report(hid.RID_KEYBOARD, self.kb.report())
        # 先撤窗口再还光标：窗口一撤，键盘立刻回到系统
        self.win.close()
        self.cursor.show()
        mac_input.force_associate_cursor()
        if self.vscreen is not None:
            self.vscreen.stop()
        try:
            self.tap.stop()
        except Exception:
            pass
        try:
            self.raw.stop()
        except Exception:
            pass
        self.ble.stop()
        print(
            f"\n已退出。发送 {self.sender.sent} 条报文（失败 {self.sender.send_fail}）。"
        )


def run_home(args) -> int:
    """只发归位报文，不碰鼠标。用来单独验证"蓝牙这一路真的能推动 iPhone 指针"。"""
    cal = Calibration.from_file(args.calibration)
    ble = HidPeripheral(local_name=args.name, verbose=args.verbose)
    ble.start()

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not ble.subscribed:
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.2, True)
    if not ble.subscribed and not args.dry_run:
        print("❌ 60 秒内没有客户端订阅。先确认 iPhone 已配对并在辅助触控里选中本机。")
        ble.stop()
        return 1

    reports = cal.homing_reports()
    print(f"发送 {len(reports)} 条归位报文（约 {len(reports) * cal.report_interval:.1f}s）")
    step = cal.report_interval
    for dx, dy in reports:
        payload = hid.encode_relative_mouse(0, dx, dy)
        if args.dry_run:
            print("  [" + " ".join(f"{b:02X}" for b in payload) + "]")
        else:
            ble.send(hid.RID_RELATIVE, payload)
        until = time.monotonic() + step
        while time.monotonic() < until:
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, step / 2, True)
    print("归位完成：iPhone 指针应停在左上角")
    ble.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    # 输出常被重定向到日志：行缓冲能保证崩溃前写下的信息真的落盘
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        prog="ios-screen", description="把 iPhone 当作屏幕边缘外的一块屏（macOS 盲控版）"
    )
    ap.add_argument("--edge", default="right", help="越界方向 right/left/top/bottom，默认 right")
    ap.add_argument("--zone-start", type=float, default=0.0,
                    help="沿边触发区起点(0~1)，如 0.35 表示只让 35%% 之后的边段生效")
    ap.add_argument("--zone-end", type=float, default=1.0, help="沿边触发区终点(0~1)")
    ap.add_argument("--screen", default="primary",
                    help="目标显示器：primary / 序号 / 名字片段 / 分辨率，默认 primary")
    ap.add_argument("--list", action="store_true", help="列出所有显示器后退出")
    ap.add_argument("--name", default=DEFAULT_NAME, help=f"蓝牙广播名，默认 {DEFAULT_NAME!r}")
    ap.add_argument("--config", default=None,
                    help="用户个性化配置 json，默认项目根目录的 user_config.json")
    ap.add_argument("--calibration", default=DEFAULT_CAL, help="校准 json")
    ap.add_argument("--mode", choices=["absolute", "relative"], default="absolute",
                    help="absolute=虚拟绝对坐标(默认，无累积延迟，手可无限推)；"
                         "relative=相对累积(需校准曲线，30ms 一步)")
    ap.add_argument("--gain", type=float, default=3.0,
                    help="绝对模式增益：一个鼠标计数换多少 iPhone 像素，默认 3.0"
                         "（iPhone 是 1180x2556 像素，1:1 会慢到推不动；捕获中可按 "
                         "Ctrl+Option+= 加快 / Ctrl+Option+- 放慢）")
    ap.add_argument("--accel", type=float, default=0.0,
                    help="绝对模式速度增益，0=线性(默认)，0.5~1 快划时放大行程")
    ap.add_argument("--escape-px", type=float, default=None,
                    help="越过退出边多少 iPhone 像素就释放回 Mac，默认 6×增益"
                         "（阈值按 iPhone 像素算，增益放大后必须同步放大，"
                         "否则一两格抖动就会误退出）")
    ap.add_argument("--wheel-step", type=int, default=2,
                    help="每格滚轮发多少 HID 档位（整数），默认 2")
    ap.add_argument("--sensitivity", type=float, default=1.0, help="相对模式灵敏度倍数")
    ap.add_argument("--enter-threshold", type=int, default=12, help="进入捕获所需朝外计数")
    ap.add_argument("--exit-threshold", type=int, default=60, help="相对模式退出所需朝内计数")
    ap.add_argument("--dry-run", action="store_true", help="不发蓝牙，只打印报文")
    ap.add_argument(
        "--no-vscreen",
        action="store_true",
        help="不创建虚拟屏，改用「推屏幕边缘」触发（Deskflow 的光标在这条路上"
             "只能上下移动，因为它的合成位移在边缘会被钳成 0）",
    )
    ap.add_argument("--home", action="store_true", help="只发归位报文")
    ap.add_argument(
        "--fix-cursor",
        action="store_true",
        help="把系统光标重新与鼠标关联后退出（旧版本残留状态时的急救办法）",
    )
    ap.add_argument("--self-test", type=float, default=0.0,
                    help="N 秒后自动退出（自检用，不改变任何行为）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    try:
        args.edge = parse_edge(args.edge)
    except ValueError as e:
        print(e)
        return 2

    # 退出阈值按 iPhone 像素算，和增益挂钩：增益越大，同样的"两格抖动"
    # 换算出来的像素越多，固定阈值会变得过于灵敏。
    # 远程控制（Deskflow）的位移是成块送来的，一次几十到几百，所以留够余量：
    # 10×增益 = 30px，配合进入后 250ms 的宽限期，不会被一次回抽顶出去。
    if args.escape_px is None:
        args.escape_px = 10.0 * max(0.2, args.gain)

    if args.list:
        print(mac_input.describe_displays())
        return 0

    if args.fix_cursor:
        mac_input.force_associate_cursor()
        print("已把光标重新关联到鼠标。")
        return 0

    if not os.path.isfile(args.calibration):
        print(f"读不到校准文件: {args.calibration}")
        return 2

    if args.home:
        return run_home(args)
    return App(args).run()


if __name__ == "__main__":
    sys.exit(main())
