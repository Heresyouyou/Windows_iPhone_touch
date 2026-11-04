"""分通道验证：绝对指针 / 相对鼠标 / 媒体键，看到底哪条通道被 iOS 消费。

为什么要分开试：
  订阅成功只证明"连接和特征可见"，不证明"报文被消费"。这三条通道走的是
  Report Map 里三个不同的 Report ID，任何一条有反应就说明 HID 真的在工作，
  区别只在报表格式。全部没反应则说明 iOS 没把这台设备当输入设备用。
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ios_screen import hid  # noqa: E402
from ios_screen.hogp import HogpService  # noqa: E402


async def wait_subscribe(svc: HogpService, seconds: int = 90) -> bool:
    for i in range(seconds):
        await asyncio.sleep(1)
        if svc.subscribed:
            print(f"  已连接（{i}s）", flush=True)
            return True
        if i % 10 == 0:
            print(f"  等待连接… {i}s  订阅={svc.subscribed}", flush=True)
    return False


async def main() -> int:
    svc = HogpService()
    svc.on_subscribe_changed(lambda s: print(f"  >>> 订阅 {s}", flush=True))
    print("启动服务…", flush=True)
    await svc.start()
    print(f"广播 {svc.status}", flush=True)

    if not await wait_subscribe(svc):
        print("❌ 没等到连接")
        await svc.stop()
        return 1

    await asyncio.sleep(1)

    print("\n[1] 绝对指针 → (0,0) 左上角", flush=True)
    ok = await svc.send(hid.RID_ABSOLUTE, hid.encode_absolute_pointer(0, 0, 0))
    print(f"    send={ok}", flush=True)
    await asyncio.sleep(2.5)

    print("[2] 绝对指针 → (32767,32767) 右下角", flush=True)
    ok = await svc.send(hid.RID_ABSOLUTE, hid.encode_absolute_pointer(0, 32767, 32767))
    print(f"    send={ok}", flush=True)
    await asyncio.sleep(2.5)

    print("[3] 绝对指针 → 正中 (16383,16383)", flush=True)
    ok = await svc.send(hid.RID_ABSOLUTE, hid.encode_absolute_pointer(0, 16383, 16383))
    print(f"    send={ok}", flush=True)
    await asyncio.sleep(2.0)

    print("[4] 相对鼠标：向右下 20 步，每步 +30", flush=True)
    for _ in range(20):
        ok = await svc.send_mouse(0, 30, 30)
        await asyncio.sleep(0.03)
    print(f"    最后一条 send={ok}", flush=True)
    await asyncio.sleep(1.5)

    print("[5] 相对鼠标：向左上 40 步，每步 -20（应回到左上）", flush=True)
    for _ in range(40):
        await svc.send_mouse(0, -20, -20)
        await asyncio.sleep(0.03)
    await asyncio.sleep(1.0)

    print("[6] 媒体键 Home（iPhone 应回主屏）", flush=True)
    ok = await svc.send(hid.RID_CONSUMER, hid.encode_consumer(0x0223))
    print(f"    send={ok}", flush=True)
    await asyncio.sleep(1.0)
    await svc.send(hid.RID_CONSUMER, hid.encode_consumer(0x0000))
    await asyncio.sleep(1.0)

    print("\n[7] 键盘：发一次 'a' 的按下+抬起（备忘录里应出现 a）", flush=True)
    await svc.send(hid.RID_KEYBOARD, hid.encode_keyboard(0, 0x04))
    await asyncio.sleep(0.05)
    await svc.send(hid.RID_KEYBOARD, hid.encode_keyboard(0, 0x00))

    print("\n全部发完。保持连接 60 秒，你可以反复观察。", flush=True)
    for i in range(60):
        await asyncio.sleep(1)
        if i % 15 == 0:
            print(f"  ...{60-i}s 订阅={svc.subscribed}", flush=True)

    await svc.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
