"""抓自己的 BLE 广播包，确认 0x1812 到底有没有被广播出去。

辅助触控的"蓝牙设备"列表靠广播里的「16-bit Service UUIDs」来识别 HID 设备。
如果 WinRT 只广播了设备名、没带 0x1812，iPhone 就会一直转圈连不上——
系统蓝牙那层能连（它用缓存的服务发现），但辅助触控不认。
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from winrt.windows.devices.bluetooth.advertisement import (  # noqa: E402
    BluetoothLEAdvertisementWatcher,
    BluetoothLEAdvertisementWatcherStatus,
)

from ios_screen.hogp import HogpService  # noqa: E402

TARGET_NAME = "DESKTOP-DTEKPKA"
TARGET_ADDR = 0xE40D36600274

AD_TYPE = {
    0x01: "Flags",
    0x02: "Incomplete 16-bit UUIDs",
    0x03: "Complete 16-bit UUIDs",
    0x08: "Shortened Name",
    0x09: "Complete Name",
    0x16: "Service Data",
    0xFF: "Manufacturer Data",
}

_watcher = None
_hits = 0


def fmt_addr(n: int) -> str:
    return ":".join(f"{(n >> (8 * i)) & 0xFF:02X}" for i in range(5, -1, -1))


def buf_bytes(buf) -> bytes:
    if buf is None:
        return b""
    try:
        return bytes(memoryview(buf))
    except Exception:
        try:
            from winrt.windows.storage.streams import DataReader

            r = DataReader.from_buffer(buf)
            n = r.unconsumed_buffer_length
            out = bytearray(n)
            r.read_bytes(out)
            return bytes(out)
        except Exception:
            return b""


def uuid16_list(raw: bytes) -> list[str]:
    out = []
    for i in range(0, len(raw) - 1, 2):
        out.append(f"0x{raw[i] | (raw[i + 1] << 8):04X}")
    return out


def on_received(_w, args) -> None:
    global _hits
    adv = args.advertisement
    name = adv.local_name or ""
    addr = args.bluetooth_address
    mine = name == TARGET_NAME or addr == TARGET_ADDR
    if not mine:
        return
    _hits += 1
    print(f"\n>>> 命中自己的广播  {fmt_addr(addr)}  name={name!r}  rssi={args.rssi}")
    if not adv.data_sections:
        print("    ⚠ 广播包里没有任何 data section")
    has_1812 = False
    for s in adv.data_sections:
        dt = int(s.data_type)
        raw = buf_bytes(s.data)
        label = AD_TYPE.get(dt, f"0x{dt:02X}")
        print(f"    [{label:<26}] {raw.hex(' ').upper()}")
        if dt in (0x02, 0x03):
            uuids = uuid16_list(raw)
            print(f"       └ 服务 UUID: {uuids}")
            if "0x1812" in uuids:
                has_1812 = True
    print(f"    0x1812 在广播包里: {'✅ 是' if has_1812 else '❌ 否 —— 这就是辅助触控连不上的原因'}")


async def main() -> int:
    global _watcher
    svc = HogpService()
    print("启动 HOGP 广播…")
    await svc.start()
    await asyncio.sleep(1.5)
    print(f"广播状态: {svc.status}")

    _watcher = BluetoothLEAdvertisementWatcher()
    # pywinrt 暴露的是 add_/remove_ 前缀，不是 C# 的 += 事件
    _watcher.add_received(on_received)
    _watcher.start()
    print("扫描 12 秒（同时保持广播）…\n")
    await asyncio.sleep(12)
    _watcher.stop()
    await asyncio.sleep(0.5)

    print(f"\n共收到自己的广播 {_hits} 条")
    if _hits == 0:
        print("⚠ 没扫到自己的广播。Windows 的单适配器常常收不到自发广播，")
        print("  这不等于没在广播——用 iPhone 的辅助触控能否发现才是判据。")
    await svc.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
