"""链路自检：只起 GATT 服务并等手机订阅，连上后让指针转一圈。

用途：把"配对/订阅"这一步从窗口交互里剥出来。手机上看不到指针时，
先跑这个确认链路，还是不行就别怀疑窗口代码。
"""
from __future__ import annotations

import argparse
import asyncio

from winrt.windows.devices.bluetooth import BluetoothAdapter

from . import hid
from .hogp import HidService

_JIGGLE = [(6, 0), (5, 4), (0, 6), (-5, 4), (-6, 0), (-5, -4), (0, -6), (5, -4)]


async def _run(timeout: float) -> int:
    adapter = await BluetoothAdapter.get_default_async()
    if adapter is None:
        print("❌ 找不到蓝牙适配器")
        return 1
    print(f"适配器: 地址 {adapter.bluetooth_address:012X}  "
          f"LE={adapter.is_low_energy_supported}  "
          f"外设角色={adapter.is_peripheral_role_supported}")
    if not adapter.is_peripheral_role_supported:
        print("❌ 这块蓝牙不支持 LE 外设角色，无法广播 HID 服务")
        return 1

    svc = HidService()
    await svc.start()
    await asyncio.sleep(0.3)
    print(f"广播  : {svc.status}  服务 0x1812  Report Map {len(hid.REPORT_MAP)}B")
    print("等待手机配对并订阅……（手机：设置 → 蓝牙 → 配对新设备 → 选本机）")

    ok = False
    for _ in range(int(timeout * 4)):
        await asyncio.sleep(0.25)
        if svc.subscribed:
            ok = True
            break
    if not ok:
        print(f"❌ {timeout:.0f} 秒内没有订阅。")
        print("   1) 手机是否已经配对（配对新设备里出现本机电脑名）")
        print("   2) 配对后 Android 会在几秒内自动订阅；没订阅就取消配对重来")
        print("   3) 看看广播状态是不是 Aborted(3)——那是适配器被占用/不支持外设角色")
        await svc.stop()
        return 2

    print(f"✅ 已订阅（客户端 {svc.client_count}）。发送一圈位移…")
    # 一条报文 6 个计数，转一圈大概 40 像素，够看清指针在动
    for dx, dy in _JIGGLE:
        await svc.send_mouse(0, dx, dy)
        await asyncio.sleep(0.05)
    print("   手机上应该能看到指针画了一个小方框。")
    print("   键盘：")
    await svc.send_keyboard(hid.encode_keyboard(0, 0x04))  # 'a'
    await asyncio.sleep(0.05)
    await svc.send_keyboard(hid.encode_keyboard(0))
    print("   已发送一次 a 键（若某个输入框有焦点，会打出一个 a）。")

    print("\n保持广播 5 秒供观察，然后退出…")
    await asyncio.sleep(5)
    await svc.stop()
    print("自检结束。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="android-hid-check", description="HOGP 链路自检")
    ap.add_argument("--timeout", type=float, default=60.0, help="等订阅的秒数，默认 60")
    args = ap.parse_args(argv)
    try:
        return asyncio.run(_run(args.timeout))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
