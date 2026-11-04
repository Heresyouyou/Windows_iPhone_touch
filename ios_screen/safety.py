"""看门狗：主循环卡住就把进程干掉，让系统把输入还回去。

为什么需要这么粗暴的手段
------------------------
"用一层窗口抢焦点"本身就是安全的——进程一死窗口就没了。真正难办的是
**进程还活着但主线程卡住**：窗口还挂在屏幕上，键盘照旧被它接走，用户看到的就是
"键盘失灵"。这时候任何依赖主线程去执行的清理代码都跑不到。

所以看门狗必须：

1. 在**另一个线程**里跑，只读一个心跳计数，不碰任何 ObjC 对象；
2. 超时后直接 `os._exit()`，不做优雅退出——进程消失是唯一 100% 可靠的恢复方式；
3. 退出前顺手把光标关联和光标显示恢复一次（万一是历史版本留下的状态）。
"""
from __future__ import annotations

import os
import sys
import threading
import time

from Quartz import CGDisplayShowCursor, CGMainDisplayID

from . import mac_input


class Watchdog(threading.Thread):
    """心跳停了就杀掉自己。daemon 线程，不会阻止进程退出。"""

    def __init__(self, heartbeat, timeout: float = 2.5, verbose: bool = True) -> None:
        super().__init__(name="mpt-watchdog", daemon=True)
        self._heartbeat = heartbeat        # () -> float，返回最近一次心跳的时刻
        self._timeout = float(timeout)
        self.verbose = verbose
        self.fired = False
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        # 先等一个完整的超时窗口，避免启动阶段还没开始心跳就误杀
        if self._stop.wait(self._timeout):
            return
        while not self._stop.wait(0.25):
            try:
                age = time.monotonic() - float(self._heartbeat())
            except Exception:
                age = 0.0
            if age <= self._timeout:
                continue
            self.fired = True
            self._emergency(age)
            return

    def _emergency(self, age: float) -> None:
        try:
            print(
                f"\n❌ 主循环 {age:.1f} 秒没有心跳，判定为卡死。\n"
                f"   立即结束进程，把键盘和鼠标还给系统。",
                flush=True,
            )
        except Exception:
            pass
        # 历史版本可能留下过全局解耦状态，这里无条件恢复一次
        try:
            mac_input.force_associate_cursor()
            CGDisplayShowCursor(CGMainDisplayID())
        except Exception:
            pass
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(1)
