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
from .edge_trigger import Edge, EdgeSpec, EdgeTrigger, EdgeTriggerConfig, edge_name, parse_edge
from .geometry import Rect
from .hogp import HogpService
from . import winput

# 边缘方向的中文名，只用于启动提示
_EDGE_CN = {Edge.Right: "右", Edge.Left: "左", Edge.Top: "上", Edge.Bottom: "下"}

CAL_DEFAULT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data",
    "calibration-iphone15pro.json",
)

SEND_INTERVAL = 0.03
MOVE = 0x01  # usFlags 的 MOUSE_MOVE_ABSOLUTE 位


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
    def __init__(self, svc: HogpService, cal: Calibration, shared: Shared, *, dry: bool, verbose: bool):
        super().__init__(daemon=True, name="hogp")
        self.svc = svc
        self.cal = cal
        self.shared = shared
        self.dry = dry
        self.verbose = verbose
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.error: str | None = None
        self._px = 0.0
        self._py = 0.0
        self._last_dx = 0
        self._last_buttons = 0
        self.sent = 0
        self.send_fail = 0

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

        while self.shared.running:
            await asyncio.sleep(SEND_INTERVAL)
            await self._tick()

    async def _tick(self) -> None:
        if not self.shared.capturing:
            return
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

    def send_absolute(self, buttons: int, x: int, y: int) -> None:
        """绝对坐标：一条报文直接定位，没有累积、没有延迟、不依赖校准曲线。

        x/y 范围 0..32767，对应 iPhone 整屏。这是 iPhone Use 的默认模式。
        """
        if self.dry or self.loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.svc.send(hid.RID_ABSOLUTE, hid.encode_absolute_pointer(buttons, x, y)),
                self.loop,
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
        self.worker = HogpWorker(
            self.svc, self.cal, self.shared, dry=args.dry_run, verbose=args.verbose
        )
        self.cursor = winput.CursorGuard()
        self.swallower = winput.MouseSwallower()
        self.win: winput.RawInputWindow | None = None

        mon = winput.pick_monitor(args.screen)
        if mon is None:
            raise SystemExit(f"找不到显示器 {args.screen!r}，用 --list 看有哪些")
        self.screen = Rect(mon["x"], mon["y"], mon["w"], mon["h"])
        self.monitor_name = mon["device"]
        self.edge = EdgeSpec(edge=args.edge, screen=self.screen)
        self.trigger = EdgeTrigger(
            EdgeTriggerConfig(enterThreshold=args.enter_threshold, exitThreshold=args.exit_threshold)
        )
        # 横向满屏对应 iPhone 横向满屏；纵向因宽高比差异不可能同时精确，用灵敏度补偿
        self.px_per_count = self.cal.screen_width / max(1, self.screen.w) * args.sensitivity

    # ---------- 捕获 ----------

    def begin_capture(self) -> None:
        self.shared.capturing = True
        self.cursor.hide()
        self.swallower.active = True

        # 位置映射：进入点由鼠标在边缘上的位置决定另一个轴。
        # 这样"鼠标在屏幕右缘的上半部"就落在 iPhone 上半部，符合"iPhone 就是右边一块屏"的直觉。
        # 横向从对侧边缘进（右缘进入 → iPhone 左缘），纵向按比例；反之亦然。
        l, t, w, h = self.screen.x, self.screen.y, self.screen.w, self.screen.h
        cx, cy = winput.cursor_pos()
        ipw, iph = self.cal.screen_width, self.cal.screen_height
        fx = (cx - l) / max(1, w - 1)
        fy = (cy - t) / max(1, h - 1)
        if self.edge.edge is Edge.Right:
            tx, ty = 0.0, fy * (iph - 1)
        elif self.edge.edge is Edge.Left:
            tx, ty = float(ipw - 1), fy * (iph - 1)
        elif self.edge.edge is Edge.Top:
            tx, ty = fx * (ipw - 1), 0.0
        else:
            tx, ty = fx * (ipw - 1), float(iph - 1)
        # 只改目标、不改预测位置：让指针从上次位置平滑滑过去，而不是瞬移。
        # 盲控拿不到 iPhone 的真实指针位置，predicted 是唯一依据，硬跳只会让第一次移动失准。
        with self.shared.lock:
            self.shared.target_x = tx
            self.shared.target_y = ty

        # 指针挪到屏幕中央：贴着边缘时下面就是任务栏/窗口，万一漏吞会误点
        winput.user32.SetCursorPos(l + w // 2, t + h // 2)
        if self.args.verbose:
            print(f"  ▶ 进入捕获  edge={edge_name(self.edge.edge)} "
                  f"映射到 ({tx:.0f},{ty:.0f})", flush=True)

    def end_capture(self) -> None:
        self.shared.capturing = False
        self.swallower.active = False
        self.cursor.show()
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

    def on_mouse(self, dx: int, dy: int, btn_flags: int) -> None:
        cx, cy = winput.cursor_pos()
        change = self.trigger.update(self.edge, cx, cy, dx, dy)
        if change is EdgeTrigger.Change.Entered:
            self.begin_capture()
        elif change is EdgeTrigger.Change.Exited:
            self.end_capture()

        if not self.shared.capturing:
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
            # usButtonFlags 的按下/抬起位
            if btn_flags & 0x0001:
                self.shared.buttons |= 0x01
            if btn_flags & 0x0002:
                self.shared.buttons &= ~0x01
            if btn_flags & 0x0004:
                self.shared.buttons |= 0x02
            if btn_flags & 0x0008:
                self.shared.buttons &= ~0x02

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
              f"进入阈值 {self.trigger.cfg.enterThreshold} 退出阈值 {self.trigger.cfg.exitThreshold} 计数")
        print(f"比例 : {self.px_per_count:.3f} iPhone像素/鼠标计数"
              f"（灵敏度 {self.args.sensitivity}）")
        if self.args.dry_run:
            print("模式 : dry-run，报文只打印不发蓝牙")
        else:
            print(f"蓝牙 : 广播中={self.svc.advertising}  订阅={self.svc.subscribed}")

        self.win = winput.RawInputWindow(self.on_mouse)
        self.win.create()
        self.swallower.install()

        print("\n就绪。鼠标推向屏幕%s缘即接管 iPhone；反向推回退出。Ctrl+C 结束。\n"
              % _EDGE_CN[self.edge.edge])

        try:
            while self.shared.running:
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
    ap.add_argument("--screen", default="primary",
                    help="目标显示器：primary / 序号 / 设备名片段，默认 primary")
    ap.add_argument("--list", action="store_true", help="列出所有显示器后退出")
    ap.add_argument("--calibration", default=CAL_DEFAULT, help="校准 json")
    ap.add_argument("--sensitivity", type=float, default=1.0, help="灵敏度倍数，默认 1.0")
    ap.add_argument("--enter-threshold", type=int, default=12, help="进入捕获所需朝外计数")
    ap.add_argument("--exit-threshold", type=int, default=60, help="退出捕获所需朝内计数")
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
