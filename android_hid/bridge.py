"""HOGP 服务的工作线程：主线程只管把报文丢进来，asyncio 循环负责发。

为什么必须单独一条线程：
  · WinRT 的 GATT 服务是 asyncio 的，而 tkinter 有自己的 mainloop，两者不能同线程；
  · `notify_value_async` 是协程，阻塞式等待会卡住界面；
  · 界面线程里做蓝牙 IO，鼠标一动就掉帧。

发送策略：不排队、不合并。鼠标位移本身就是增量，合并会丢行程；
但会统计"在飞"的发送数，BLE 卡住时不至于无限堆积把内存吃光。
"""
from __future__ import annotations

import asyncio
import threading
import time

from . import hid
from .hogp import HidService

MAX_INFLIGHT = 96          # 在飞发送上限，超过就丢（宁可掉帧，不要堆积）


class HidWorker(threading.Thread):
    def __init__(self, svc: HidService, *, dry: bool = False, verbose: bool = False) -> None:
        super().__init__(daemon=True, name="hogp")
        self.svc = svc
        self.dry = dry
        self.verbose = verbose
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.error: str | None = None
        self.sent = 0
        self.fail = 0
        self.dropped = 0
        self.last_send_t = 0.0
        self._inflight = 0
        self._stopping = False

    # ---------- 生命周期 ----------

    def run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._start())
        except Exception as e:  # noqa: BLE001 - 线程里必须兜住，否则静默退出
            self.error = str(e)
            self.ready.set()
            return
        self.ready.set()
        self.loop.run_forever()
        try:
            self.loop.run_until_complete(self.svc.stop())
        except Exception:
            pass
        try:
            self.loop.close()
        except Exception:
            pass

    async def _start(self) -> None:
        if self.dry:
            return
        await self.svc.start()
        # 广播是异步起来的，给它一点时间再报状态
        for _ in range(20):
            if self.svc.advertising:
                break
            await asyncio.sleep(0.05)

    def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:
            pass

    # ---------- 发送 ----------

    def _submit(self, coro) -> bool:
        if self.dry:
            coro.close()
            return True
        if self._stopping or not self.loop.is_running():
            coro.close()
            return False
        if self._inflight >= MAX_INFLIGHT:
            coro.close()
            self.dropped += 1
            return False
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        except Exception:
            coro.close()
            self.fail += 1
            return False
        self._inflight += 1
        fut.add_done_callback(self._done)
        return True

    def _done(self, fut) -> None:
        self._inflight = max(0, self._inflight - 1)
        try:
            if fut.cancelled() or fut.exception() is not None or not fut.result():
                self.fail += 1
        except Exception:
            self.fail += 1

    def send_mouse(self, buttons: int, dx: int = 0, dy: int = 0, wheel: int = 0,
                   force: bool = False) -> bool:
        """force=True 时即使全零也发一条——按钮抬起就是"全零 + 必须发"，
        少了它手机上会一直按着不放。"""
        dx = int(dx)
        dy = int(dy)
        if not (dx or dy or wheel or buttons) and not force:
            return False
        if self.dry:
            if self.verbose:
                print(f"  mouse btns={buttons:02X} dx={dx:4d} dy={dy:4d} w={wheel:3d}",
                      flush=True)
            self.sent += 1
            return True
        # int8 上限：超了拆成多条没意义（一条报文最大 127），直接钳位
        dx = max(-127, min(127, dx))
        dy = max(-127, min(127, dy))
        wheel = max(-127, min(127, wheel))
        payload = hid.encode_relative_mouse(buttons, dx, dy, wheel)
        self.sent += 1
        return self._submit(self.svc.send_mouse(buttons, dx, dy, wheel))

    def send_keyboard(self, payload: bytes) -> bool:
        if self.dry:
            if self.verbose:
                print(f"  kbd  {' '.join(f'{b:02X}' for b in payload)}", flush=True)
            self.sent += 1
            return True
        self.sent += 1
        return self._submit(self.svc.send_keyboard(bytes(payload)))

    def send_consumer(self, usage: int) -> bool:
        if self.dry:
            if self.verbose:
                print(f"  consumer 0x{usage:04X}", flush=True)
            self.sent += 1
            return True
        self.sent += 1
        return self._submit(self.svc.send_consumer(usage))

    # ---------- 状态 ----------

    @property
    def status(self) -> str:
        return "dry-run" if self.dry else self.svc.status

    @property
    def subscribed(self) -> bool:
        return self.dry or self.svc.subscribed

    @property
    def clients(self) -> int:
        return 0 if self.dry else self.svc.client_count

    def snapshot(self) -> dict:
        return {
            "sent": self.sent,
            "fail": self.fail,
            "dropped": self.dropped,
            "inflight": self._inflight,
            "t": time.monotonic(),
        }
