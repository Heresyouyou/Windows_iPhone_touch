"""主程序：鼠标越界 → 蓝牙 HID → iPhone 指针。

线程模型
--------
只有一个线程。事件 tap、CoreBluetooth 回调、定时器全部挂在主 run loop 上：

    CFRunLoop
      ├── CGEventTap 源        鼠标/键盘事件（捕获时吞掉）
      ├── CBPeripheralManager  订阅变化、读写请求
      └── NSTimer              10ms / 30ms 报文节拍

Windows 版必须开第二个线程跑 asyncio，是因为 WinRT 的 notify 是 await 的；
CoreBluetooth 的 updateValue 是同步返回 Bool，不需要事件循环。少一个线程，
就少一类"节拍和坐标更新抢锁"的问题。

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

from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode
from Foundation import NSTimer

from . import core, hid, mac_input, userconfig
from .ble_hid import HidPeripheral, bundle_warning as bluetooth_bundle_warning
from .calibration import Calibration
from .core import Controller, Sender, hotkey_for
from .edge_trigger import (
    Change,
    Edge,
    EdgeSpec,
    EdgeTrigger,
    EdgeTriggerConfig,
    edge_name,
    parse_edge,
)
from .geometry import Rect

_EDGE_CN = {Edge.Right: "右", Edge.Left: "左", Edge.Top: "上", Edge.Bottom: "下"}
_SIDE_CN = {"left": "左", "right": "右", "top": "上", "bottom": "下"}

DEFAULT_CAL = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "calibration-iphone15pro.json"
)
DEFAULT_NAME = "Mac iPhone"


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
        # 锚点取屏幕中央：绝对模式下光标位置不参与计算，这里纯粹是安全位置
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
        self.tap = mac_input.EventTap(
            on_mouse=self.on_mouse,
            on_button=self.on_button,
            on_scroll=self.on_scroll,
            on_key=self.on_key,
            should_swallow=lambda: self.capturing,
            on_tap_disabled=self._on_tap_disabled,
            verbose=args.verbose,
        )
        self._timer = None
        self._started_at = time.monotonic()

    # ------------------------------------------------------------ 触发

    def on_mouse(self, dx: int, dy: int) -> bool:
        if self.capturing:
            return super().on_mouse(dx, dy)
        cx, cy = mac_input.cursor_pos()
        change = self.trigger.update(self.edge, int(round(cx)), int(round(cy)), dx, dy)
        if change is Change.Entered:
            # 触发用的这一下位移不再计入手机端，否则刚进来就离边界半步
            self.begin_capture((cx, cy))
            return True
        if change is Change.Exited:
            self.end_capture()
        return False

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
            mac_input.warp_cursor(l + w - 120, t + h // 2)
        elif e is Edge.Left:
            mac_input.warp_cursor(l + 120, t + h // 2)
        elif e is Edge.Top:
            mac_input.warp_cursor(l + w // 2, t + 120)
        else:
            mac_input.warp_cursor(l + w // 2, t + h - 120)

    def _on_tap_disabled(self, kind: int) -> None:
        print("  ⚠️ 事件 tap 被系统摘掉，已自动重挂（通常是回调里卡太久）", flush=True)

    def force_exit(self) -> None:
        """Ctrl+Option+Q：捕获中先退出捕获，再按一次结束程序。"""
        if self.capturing:
            self.end_capture()
        else:
            self.running = False

    # ------------------------------------------------------------ 运行

    def run(self) -> int:
        if not self.args.dry_run:
            warn = bluetooth_bundle_warning()
            if warn:
                print(warn)
                return 2
            self.ble.start()
            if not self._wait_ble(3.0):
                print(f"❌ 蓝牙没有就绪（state={self.ble.state}）")
                return 1

        if not mac_input.accessibility_trusted() and not self.args.dry_run:
            print(
                "⚠️ 当前应用还没有「辅助功能」权限：事件 tap 建不起来，"
                "既收不到鼠标也吞不掉事件。\n"
                "   到「系统设置 → 隐私与安全性 → 辅助功能」勾选本应用后重新启动。"
            )

        if not self.tap.start():
            print(
                "❌ 建不起事件 tap。请到「系统设置 → 隐私与安全性 → 辅助功能」"
                "里勾选当前终端/应用，然后重启本程序。"
            )
            return 1
        if self.tap.listen_only:
            print(
                "⚠️ 只有监听权限，没有辅助功能权限：捕获时光标不会真的冻住，"
                "靠 warp 兜底；鼠标事件也吞不掉。"
            )

        self._print_banner()
        self._timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            self.sender.interval, True, self._on_timer
        )
        deadline = time.monotonic() + self.args.self_test if self.args.self_test > 0 else 0.0
        old_int = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self._on_sigint)
        try:
            while self.running:
                CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.1, True)
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
        if not self.capturing:
            return
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
            f"Ctrl+Option+- 放慢 / Ctrl+Option+Q 释放（空闲时退出）。"
            f"Ctrl+C 退出。\n"
        )

    def shutdown(self) -> None:
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True
        if self._timer is not None:
            try:
                self._timer.invalidate()
            except Exception:
                pass
        self.capturing = False
        if self.kb.clear():
            self.sender.send_report(hid.RID_KEYBOARD, self.kb.report())
        self.tap.stop()
        self.cursor.show()      # 最后一道保险：无论如何先把系统指针还回去
        self.ble.stop()
        print(
            f"\n已退出。发送 {self.sender.sent} 条报文（失败 {self.sender.send_fail}），"
            f"吞掉 {self.tap.swallowed} 个输入事件。"
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
    ap.add_argument("--gain", type=float, default=1.0,
                    help="绝对模式增益：一个鼠标计数换多少 iPhone 像素，默认 1.0")
    ap.add_argument("--accel", type=float, default=0.0,
                    help="绝对模式速度增益，0=线性(默认)，0.5~1 快划时放大行程")
    ap.add_argument("--escape-px", type=float, default=5.0,
                    help="越过退出边多少像素就释放回 Mac，默认 5")
    ap.add_argument("--wheel-step", type=int, default=2,
                    help="每格滚轮发多少 HID 档位（整数），默认 2")
    ap.add_argument("--sensitivity", type=float, default=1.0, help="相对模式灵敏度倍数")
    ap.add_argument("--enter-threshold", type=int, default=12, help="进入捕获所需朝外计数")
    ap.add_argument("--exit-threshold", type=int, default=60, help="相对模式退出所需朝内计数")
    ap.add_argument("--dry-run", action="store_true", help="不发蓝牙，只打印报文")
    ap.add_argument("--home", action="store_true", help="只发归位报文")
    ap.add_argument("--self-test", type=float, default=0.0,
                    help="N 秒后自动退出（自检用，不改变任何行为）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    try:
        args.edge = parse_edge(args.edge)
    except ValueError as e:
        print(e)
        return 2

    if args.list:
        print(mac_input.describe_displays())
        return 0

    if not os.path.isfile(args.calibration):
        print(f"读不到校准文件: {args.calibration}")
        return 2

    if args.home:
        return run_home(args)
    return App(args).run()


if __name__ == "__main__":
    sys.exit(main())
