"""把 Windows 发布成一个蓝牙键鼠（HOGP），供 Android 作为 HID 主机连接。

结构和 ios_screen/hogp.py 一致，踩过的坑也一样，**不要改这几条**：

1. 服务 UUID 0x1812，广播类型 Primary；Android 的 HidDeviceService 就是靠
   广播里出现 0x1812 才发起 HID 连接的；
2. 每个 0x2A4D Report 特征都要挂 0x2908 Report Reference 描述符（[reportId, type]）；
3. 通知负载里不带 Report ID 字节，ID 已由描述符声明；
4. ProtectionLevel 必须是 EncryptionRequired —— 用 Plain 时对方能连、能订阅、
   notify 返回成功，但一个输入事件都不会产生，而且没有任何报错。

与 iOS 版的差别：
  · 报表集是键盘 / 相对鼠标 / 媒体键（见 hid.py）；
  · 发送时按"谁真的订阅了"选通道，不做双发——Android 若同时订了 boot 报表和
    report 报表，双发会让指针一次移动两倍。
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Callable

from winrt.windows.devices.bluetooth import BluetoothError
from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattCharacteristicProperties as Props,
    GattLocalCharacteristicParameters,
    GattLocalDescriptorParameters,
    GattProtectionLevel,
    GattServiceProvider,
    GattServiceProviderAdvertisingParameters,
    GattServiceProviderAdvertisementStatus,
)
from winrt.windows.storage.streams import DataWriter

from . import hid

_SIG = "-0000-1000-8000-00805F9B34FB"


def _u(short: str) -> uuid.UUID:
    return uuid.UUID(f"0000{short}{_SIG}")


HID_SERVICE = _u("1812")
C_HID_INFO = _u("2A4A")
C_REPORT_MAP = _u("2A4B")
C_CONTROL_POINT = _u("2A4C")
C_REPORT = _u("2A4D")
C_PROTOCOL_MODE = _u("2A4E")
C_BATTERY = _u("2A19")
C_APPEARANCE = _u("2A01")
D_REPORT_REF = _u("2908")

# Boot Protocol：老主机/部分栈会切到引导模式，只认这三个固定格式的报表
C_BOOT_KB_IN = _u("2A22")
C_BOOT_KB_OUT = _u("2A32")
C_BOOT_MOUSE_IN = _u("2A33")


def _buf(data: bytes):
    w = DataWriter()
    w.write_bytes(data)
    return w.detach_buffer()


class HidService:
    def __init__(self) -> None:
        self._provider = None
        self._reports: dict[int, object] = {}
        self._boot_kb = None
        self._boot_mouse = None
        self._subscribed = False
        self._on_subscribe: Callable[[bool], None] | None = None
        self.protocol_mode = 1  # 1 = Report, 0 = Boot

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        result = await GattServiceProvider.create_async(HID_SERVICE)
        if result.error != BluetoothError.SUCCESS or result.service_provider is None:
            raise RuntimeError(f"创建 0x1812 服务失败: {result.error}")
        self._provider = result.service_provider
        svc = self._provider.service

        # 1. HID Information: bcdHID 1.11 / country 0 / NormallyConnectable
        await self._add(svc, C_HID_INFO, Props.READ, bytes([0x11, 0x01, 0x00, 0x02]))
        # 2. Report Map
        await self._add(svc, C_REPORT_MAP, Props.READ, hid.REPORT_MAP)
        # 3. HID Control Point
        await self._add(svc, C_CONTROL_POINT, Props.WRITE_WITHOUT_RESPONSE, b"\x00",
                        writable=True)

        # 4-6. 输入报表，顺序与 Report Map 里的一一对应
        for rid, size in hid.INPUT_REPORTS:
            char = await self._add(svc, C_REPORT, Props.READ | Props.NOTIFY, bytes(size))
            await self._add_desc(char, D_REPORT_REF, bytes([rid, 0x01]))
            self._reports[rid] = char

        # 7. 键盘 LED 输出报表：Android 会写 CapsLock 状态，不给也不致命，但有更规范
        led = await self._add(svc, C_REPORT, Props.READ | Props.WRITE, b"\x00",
                              writable=True)
        await self._add_desc(led, D_REPORT_REF, bytes([hid.RID_KEYBOARD, 0x02]))

        # 8-10. Boot Protocol 三件套
        self._boot_kb = await self._add(svc, C_BOOT_KB_IN, Props.READ | Props.NOTIFY,
                                        bytes(8))
        await self._add(svc, C_BOOT_KB_OUT, Props.READ | Props.WRITE, b"\x00",
                        writable=True)
        self._boot_mouse = await self._add(svc, C_BOOT_MOUSE_IN, Props.READ | Props.NOTIFY,
                                           bytes(3))

        # 11. Protocol Mode：01 = Report Protocol
        await self._add(svc, C_PROTOCOL_MODE, Props.READ | Props.WRITE_WITHOUT_RESPONSE,
                        b"\x01", writable=True)
        # 12. 电量 100%
        await self._add(svc, C_BATTERY, Props.READ | Props.NOTIFY, b"\x64")
        # 13. Appearance：C2 03 = Mouse。Android 只拿它决定列表里显示什么图标，
        #     不影响功能；这台设备主要当指针用，所以报鼠标。
        await self._add(svc, C_APPEARANCE, Props.READ, bytes([0xC2, 0x03]))

        self._provider.start_advertising(self._adv_params())

    def _adv_params(self) -> GattServiceProviderAdvertisingParameters:
        p = GattServiceProviderAdvertisingParameters()
        p.is_discoverable = True
        p.is_connectable = True
        # 不要手动塞 service_data：StartAdvertising 会自动把 0x1812 写进广播包，
        # 自己填容易超 31 字节，表现是"手机搜不到"。
        return p

    async def stop(self) -> None:
        if self._provider is not None:
            try:
                self._provider.stop_advertising()
            except Exception:
                pass
            self._provider = None

    # ---------- 内部 ----------

    async def _add(self, svc, uuid_, props, value, writable=False):
        p = GattLocalCharacteristicParameters()
        p.characteristic_properties = props
        # 必须是 EncryptionRequired。详见模块 docstring 第 4 条。
        p.read_protection_level = GattProtectionLevel.ENCRYPTION_REQUIRED
        p.write_protection_level = GattProtectionLevel.ENCRYPTION_REQUIRED
        p.static_value = _buf(value)
        res = await svc.create_characteristic_async(uuid_, p)
        if res.error != BluetoothError.SUCCESS:
            raise RuntimeError(f"创建特征 {uuid_} 失败: {res.error}")
        char = res.characteristic
        # 只监听订阅变化；不挂 add_write_requested，避免异步 respond 处理不好
        # 让主机侧写入超时，自己制造"连上就断"。
        char.add_subscribed_clients_changed(lambda _s, _a: self._on_clients_changed())
        return char

    async def _add_desc(self, char, uuid_, value):
        d = GattLocalDescriptorParameters()
        d.read_protection_level = GattProtectionLevel.ENCRYPTION_REQUIRED
        d.write_protection_level = GattProtectionLevel.ENCRYPTION_REQUIRED
        d.static_value = _buf(value)
        res = await char.create_descriptor_async(uuid_, d)
        if res.error != BluetoothError.SUCCESS:
            raise RuntimeError(f"创建描述符 {uuid_} 失败: {res.error}")
        return res.descriptor

    def _on_clients_changed(self) -> None:
        chars = list(self._reports.values()) + [self._boot_kb, self._boot_mouse]
        n = 0
        for c in chars:
            if c is None:
                continue
            try:
                n = max(n, c.subscribed_clients.size)
            except Exception:
                pass
        now = n > 0
        if now != self._subscribed:
            self._subscribed = now
            print(f"  [gatt] >>> 订阅状态 {now}（客户端 {n}）", flush=True)
            if self._on_subscribe:
                self._on_subscribe(now)

    @staticmethod
    def _has_client(char) -> bool:
        if char is None:
            return False
        try:
            return char.subscribed_clients.size > 0
        except Exception:
            return False

    # ---------- 对外 ----------

    def on_subscribe_changed(self, cb: Callable[[bool], None]) -> None:
        self._on_subscribe = cb

    @property
    def subscribed(self) -> bool:
        return self._subscribed

    @property
    def client_count(self) -> int:
        n = 0
        for c in list(self._reports.values()) + [self._boot_kb, self._boot_mouse]:
            if c is None:
                continue
            try:
                n = max(n, c.subscribed_clients.size)
            except Exception:
                pass
        return n

    @property
    def status(self) -> str:
        """原始广播状态。排障必须看这个：ABORTED(3) 与 STOPPED(1) 都会
        让 advertising 变 False，但原因完全不同。"""
        if self._provider is None:
            return "no-provider"
        try:
            s = int(self._provider.advertisement_status)
        except Exception as e:  # noqa: BLE001
            return f"error({e})"
        names = {0: "Created", 1: "Stopped", 2: "Started",
                 3: "Aborted", 4: "StartedWithoutAllAdvData"}
        return f"{names.get(s, '?')}({s})"

    @property
    def advertising(self) -> bool:
        if self._provider is None:
            return False
        return self._provider.advertisement_status in (
            GattServiceProviderAdvertisementStatus.STARTED,
            GattServiceProviderAdvertisementStatus.STARTED_WITHOUT_ALL_ADVERTISEMENT_DATA,
        )

    async def send(self, report_id: int, payload: bytes) -> bool:
        """发一条输入报表。返回 False = 没有订阅者或发送失败。

        注意：WinRT 在**没有订阅者**时 notify_value_async 也返回成功（静默丢弃），
        所以必须先查 subscribed_clients，否则"发送成功"是假的。
        """
        char = self._reports.get(report_id)
        if not self._has_client(char):
            return False
        try:
            await char.notify_value_async(_buf(payload))
            return True
        except Exception:
            return False

    async def send_mouse(self, buttons: int, dx: int, dy: int, wheel: int = 0) -> bool:
        """鼠标位移。按实际订阅情况选通道，不做双发（双发会让位移翻倍）。"""
        report = self._reports.get(hid.RID_MOUSE)
        if self._has_client(report):
            try:
                await report.notify_value_async(
                    _buf(hid.encode_relative_mouse(buttons, dx, dy, wheel))
                )
                return True
            except Exception:
                return False
        if wheel == 0 and self._has_client(self._boot_mouse):
            # Boot 鼠标只有 3 字节，没有滚轮
            try:
                await self._boot_mouse.notify_value_async(
                    _buf(bytes([buttons & 0x07, dx & 0xFF, dy & 0xFF]))
                )
                return True
            except Exception:
                return False
        return False

    async def send_keyboard(self, payload: bytes) -> bool:
        """8 字节键盘快照。Report 协议优先，必要时退回 Boot 报表（格式相同）。"""
        report = self._reports.get(hid.RID_KEYBOARD)
        if self._has_client(report):
            try:
                await report.notify_value_async(_buf(payload))
                return True
            except Exception:
                return False
        if self._has_client(self._boot_kb):
            try:
                await self._boot_kb.notify_value_async(_buf(payload))
                return True
            except Exception:
                return False
        return False

    async def send_consumer(self, usage: int) -> bool:
        return await self.send(hid.RID_CONSUMER, hid.encode_consumer(usage))
