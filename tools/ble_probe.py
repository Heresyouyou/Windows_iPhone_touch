"""蓝牙外设角色探针 —— 全项目唯一的硬门槛。

交接材料说得很清楚：Windows 10 1809+ 才有 GattServiceProvider，但能不能真的
广播，取决于适配器支不支持 Peripheral 角色。很多台式机/笔记本内置蓝牙只支持
Central（当主机连别人），一广播就 OtherError。

这个脚本只做一件事：把答案问出来。不做任何持久化改动。
"""
import asyncio
import sys
import traceback
import uuid

from winrt.windows.devices.bluetooth import BluetoothAdapter, BluetoothError
from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattServiceProvider,
    GattServiceProviderAdvertisingParameters,
    GattServiceProviderAdvertisementStatus,
)

HID_SERVICE = uuid.UUID("00001812-0000-1000-8000-00805F9B34FB")


def err_name(e):
    try:
        return str(e).split(".")[-1]
    except Exception:
        return str(e)


async def main():
    print("=" * 66)
    print("蓝牙外设角色探针")
    print("=" * 66)

    print("\n[1] 取默认适配器")
    try:
        adapter = await BluetoothAdapter.get_default_async()
    except Exception as e:
        print("  抛异常:", err_name(e))
        return 2
    if adapter is None:
        print("  没有蓝牙适配器（系统里没有可用无线电）")
        return 2

    addr = adapter.bluetooth_address
    print(f"  地址: {':'.join(f'{(addr >> (8*i)) & 0xFF:02X}' for i in range(5, -1, -1))}")
    print(f"  LowEnergy 支持      : {adapter.is_low_energy_supported}")
    print(f"  Central(主机) 角色  : {adapter.is_central_role_supported}")
    print(f"  Peripheral(外设)角色: {adapter.is_peripheral_role_supported}   <-- 关键")
    try:
        print(f"  广告卸载支持        : {adapter.is_advertisement_offload_supported}")
    except Exception:
        pass

    if not adapter.is_peripheral_role_supported:
        print("\n结论: ❌ 适配器不支持 Peripheral 角色")
        print("      这条路走不通。需要换一个支持双角色的 USB 蓝牙适配器（CSR8510 一类）。")
        return 1

    print("\n[2] 创建 0x1812 HID 服务提供者")
    try:
        result = await GattServiceProvider.create_async(HID_SERVICE)
    except Exception as e:
        print("  抛异常:", err_name(e))
        traceback.print_exc()
        return 1

    print(f"  Error: {result.error}")
    if result.error != BluetoothError.SUCCESS:
        print("\n结论: ❌ 创建服务失败")
        return 1
    provider = result.service_provider
    if provider is None:
        print("  service_provider 为空")
        return 1
    print(f"  服务 UUID: {provider.service.uuid}")
    print(f"  当前广播状态: {provider.advertisement_status}")

    print("\n[3] 启动广播（可连接 + 可发现）")
    params = GattServiceProviderAdvertisingParameters()
    params.is_discoverable = True
    params.is_connectable = True
    try:
        provider.start_advertising(params)
    except Exception as e:
        print("  ❌ 抛异常:", err_name(e))
        traceback.print_exc()
        print("\n结论: ❌ 广播启动失败 —— 适配器大概率不支持 Peripheral 角色")
        return 1

    await asyncio.sleep(1.5)
    st = provider.advertisement_status
    print(f"  广播状态: {st}")

    ok = st in (
        GattServiceProviderAdvertisementStatus.STARTED,
        GattServiceProviderAdvertisementStatus.STARTED_WITHOUT_ALL_ADVERTISEMENT_DATA,
    )
    if ok:
        print("\n结论: ✅ 广播已启动 —— 这条链路成立")
        print("      现在用手机蓝牙搜索，应能看到这台电脑的名字。")
    else:
        print(f"\n结论: ❌ 广播未进入 STARTED（状态 {st}）")

    print("\n[4] 保持广播 60 秒，方便你用手机搜索（Ctrl+C 提前结束）")
    try:
        for i in range(60, 0, -1):
            await asyncio.sleep(1)
            if i % 10 == 0:
                print(f"  ...{i}s  状态={provider.advertisement_status}", flush=True)
    except asyncio.CancelledError:
        pass

    print("\n[5] 停止广播并释放")
    try:
        provider.stop_advertising()
        print("  已停止")
    except Exception as e:
        print("  停止时异常:", err_name(e))

    return 0 if ok else 1


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc or 0)
