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
import os
import signal
import sys
import time

import AppKit
from AppKit import NSApplication
from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode
from Foundation import NSDate, NSDefaultRunLoopMode, NSTimer

from . import core, hid, mac_input, userconfig
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
        )

        # [edge] 诊断计数（每秒一行）
        self._edbg_t = time.monotonic()
        self._edbg_n = 0
        self._edbg_dx = 0

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
        if capturing:
            self.win.show()
        else:
            # 统一出口：不管从哪条路径退出捕获，边缘状态机都要回到"未激活"。
            # 漏掉这一步的后果见 _feed_mouse 的注释——再也推不进去。
            self.trigger.reset()
            self.win.hide()

    def edge_fraction(self, cursor_xy: tuple[float, float]) -> tuple[float, float]:
        cx, cy = cursor_xy
        fx = (cx - self.screen.x) / max(1, self.screen.w - 1)
        fy = (cy - self.screen.y) / max(1, self.screen.h - 1)
        return (max(0.0, min(1.0, fx)), max(0.0, min(1.0, fy)))

    def warp_after_exit(self) -> None:
        """放回边缘内侧一点，否则会立刻再次触发进入，形成"进-退-进"的抖动。"""
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
        self.heartbeat = time.monotonic()
        self.maintain_cursor()
        # 撤下抓取层之后要分几步把焦点交还，必须在主循环里推进
        self.win.tick()

        if self.raw_active:
            dx, dy = self.raw.take()
            if dx or dy:
                if not self.raw_ready:
                    self.raw_ready = True
                    print(
                        f"  [raw] 收到原始位移，已切换 ({self.raw.samples} 样本)",
                        flush=True,
                    )
                self._feed_mouse(dx, dy)

        if not self.capturing:
            return
        # 激活是异步的，每拍补一次，直到抓取层真的拿到键盘
        self.win.ensure_key()
        self.sender.tick(self.target_x, self.target_y, self.buttons)

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
        if nb:
            print(
                f"⚠️ {edge_name(self.edge.edge)}侧还接着 {nb['name']} "
                f"({nb['w']}x{nb['h']})：光标会先跑到那块屏上，边缘触发不会生效。"
                f"换 --screen 或改用面板模式。"
            )
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
    # 换算出来的像素越多，固定阈值会变得过于灵敏
    if args.escape_px is None:
        args.escape_px = 6.0 * max(0.2, args.gain)

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
