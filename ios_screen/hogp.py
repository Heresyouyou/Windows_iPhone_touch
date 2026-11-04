"""把 Windows 发布成一个蓝牙键鼠（HOGP）。

关键的四个约束，每一条踩错都表现为"配对成功但 iPhone 上没有指针"：

1. 服务 UUID 必须是 0x1812，广播类型 Primary；
2. 每个 0x2A4D Report 特征都必须挂 0x2908 Report Reference 描述符，
   值是 [reportId, reportType]，type 1=Input 2=Output；
3. 通知负载里**不带** Report ID 字节——ID 已经由描述符声明了，多带一个字节
   会让 iOS 把整条报文错位解析；
4. HID Information 必须是 11 01 00 02（HID 1.11 / country 0 / NormallyConnectable）。

另一个实际前提（交接材料没写，iPhoneMirror 的文档里才有）：
iPhone 侧必须打开「设置 → 辅助功能 → 触控 → 辅助触控 → 设备 → 蓝牙设备」并配对，
否则 HOGP 连上了也不给指针。这是 iOS 的鼠标模型决定的，不是我们的 bug。
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

# Boot Protocol 报表。标准 HOGP 里这三个是给 BIOS/老系统用的，按说 iOS 不需要，
# 但 iPhoneMirror 的更新日志专门提到「Boot Protocol compatibility for more iOS devices」，
# 说明部分 iOS 的辅助触控会切到 Boot 模式，只给 Report Protocol 它就一直转圈连不上。
# 注意：Boot 报表不带 Report Reference 描述符，格式是固定的。
C_BOOT_KB_IN = _u("2A22")
C_BOOT_KB_OUT = _u("2A32")
C_BOOT_MOUSE_IN = _u("2A33")


def _buf(data: bytes):
    w = DataWriter()
    w.write_bytes(data)
    return w.detach_buffer()


def _read_buf(buf) -> bytes:
    """把 IBuffer 读成 bytes。两种取法都试一遍，pywinrt 不同版本行为不一致。"""
    if buf is None:
        return b""
    try:
        return bytes(memoryview(buf))
    except Exception:
        pass
    try:
        from winrt.windows.storage.streams import DataReader

        n = buf.length
        out = bytearray(n)
        DataReader.from_buffer(buf).read_bytes(out)
        return bytes(out)
    except Exception:
        return b""


class HogpService:
    def __init__(self) -> None:
        self._provider = None
        self._reports: dict[int, object] = {}
        self._boot_mouse = None
        self._subscribed = False
        self._on_subscribe: Callable[[bool], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.protocol_mode = 1  # 1 = Report, 0 = Boot

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        result = await GattServiceProvider.create_async(HID_SERVICE)
        if result.error != BluetoothError.SUCCESS or result.service_provider is None:
            raise RuntimeError(f"创建 0x1812 服务失败: {result.error}")
        self._provider = result.service_provider
        svc = self._provider.service

        # 1. HID Information
        await self._add(svc, C_HID_INFO, Props.READ, bytes([0x11, 0x01, 0x00, 0x02]),
                        name="HIDInfo")
        # 2. Report Map —— 198 字节，从 C++ 源码解析，保证与上游逐字节一致
        await self._add(svc, C_REPORT_MAP, Props.READ, hid.REPORT_MAP,
                        name="ReportMap")
        # 3. HID Control Point
        await self._add(
            svc, C_CONTROL_POINT, Props.WRITE_WITHOUT_RESPONSE, b"\x00",
            writable=True, name="ControlPoint",
        )

        # 4-7. 四个输入报表，顺序与 Report Map 里的一一对应
        for rid, size in (
            (hid.RID_KEYBOARD, 8),
            (hid.RID_ABSOLUTE, 5),
            (hid.RID_RELATIVE, 4),
            (hid.RID_CONSUMER, 2),
        ):
            char = await self._add(
                svc, C_REPORT, Props.READ | Props.NOTIFY, bytes(size), name=f"Report{rid}"
            )
            await self._add_desc(char, D_REPORT_REF, bytes([rid, 0x01]))
            self._reports[rid] = char

        # 8. 键盘 LED（输出报表）：可以有，iOS 写不写无所谓，但不给会让某些栈抱怨
        led = await self._add(
            svc, C_REPORT, Props.READ | Props.WRITE, b"\x00", writable=True, name="LedReport"
        )
        await self._add_desc(led, D_REPORT_REF, bytes([hid.RID_KEYBOARD, 0x02]))

        # 9-11. Boot Protocol 三件套：键盘输入(8B)、键盘输出(1B)、鼠标输入(3B)
        await self._add(svc, C_BOOT_KB_IN, Props.READ | Props.NOTIFY, bytes(8),
                        name="BootKbIn")
        await self._add(svc, C_BOOT_KB_OUT, Props.READ | Props.WRITE, b"\x00",
                        writable=True, name="BootKbOut")
        # iOS 的辅助触控接管后大概率切到 Boot Protocol，订阅的就是这个 3 字节报表
        self._boot_mouse = await self._add(
            svc, C_BOOT_MOUSE_IN, Props.READ | Props.NOTIFY, bytes(3), name="BootMouseIn"
        )

        # 12. Protocol Mode：01 = Report Protocol（另一个值是 00 引导协议）
        await self._add(
            svc, C_PROTOCOL_MODE, Props.READ | Props.WRITE_WITHOUT_RESPONSE, b"\x01",
            writable=True, name="ProtocolMode",
        )
        # 13. 电量：100%
        await self._add(svc, C_BATTERY, Props.READ | Props.NOTIFY, b"\x64", name="Battery")
        # 14. Appearance：C1 03 = Keyboard，影响 iOS 里显示的图标
        await self._add(svc, C_APPEARANCE, Props.READ, bytes([0xC1, 0x03]), name="Appearance")

        self._provider.start_advertising(self._adv_params())

    def _adv_params(self) -> GattServiceProviderAdvertisingParameters:
        p = GattServiceProviderAdvertisingParameters()
        p.is_discoverable = True
        p.is_connectable = True
        # 不手动塞 service_data：StartAdvertising 会把服务的 0x1812 自动带进广播包，
        # 自己填反而容易把广播包塞超 31 字节，表现为"搜不到设备"。
        return p

    async def stop(self) -> None:
        if self._provider is not None:
            try:
                self._provider.stop_advertising()
            except Exception:
                pass
            self._provider = None

    # ---------- 内部 ----------

    async def _add(self, svc, uuid_, props, value, writable=False, name=""):
        p = GattLocalCharacteristicParameters()
        p.characteristic_properties = props
        # 必须用 EncryptionRequired，不能用 Plain。
        # HID over GATT 规范要求加密链路；Plain 下 iOS 能连、能订阅、能收字节，
        # 但不会把报文当输入消费——表现是"订阅成功、发送成功、指针永不出现"。
        # 设为加密后 iOS 会主动发起配对（bonding），配对完成 HID 才真正生效。
        p.read_protection_level = GattProtectionLevel.ENCRYPTION_REQUIRED
        p.write_protection_level = GattProtectionLevel.ENCRYPTION_REQUIRED
        p.static_value = _buf(value)
        res = await svc.create_characteristic_async(uuid_, p)
        if res.error != BluetoothError.SUCCESS:
            raise RuntimeError(f"创建特征 {uuid_} 失败: {res.error}")
        char = res.characteristic
        # 只监听订阅变化。不挂 add_write_requested：那里的异步请求必须在回调里
        # respond()，在同步上下文里取值不安全，处理不好会让 iOS 的写入超时——
        # 那会自己制造一个"连上就断"的假故障。
        if name:
            char.add_subscribed_clients_changed(
                lambda _s, _a, n=name: self._on_clients_changed(n)
            )
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

    def _on_clients_changed(self, name: str = "") -> None:
        try:
            n = 0
            for c in self._reports.values():
                n = max(n, c.subscribed_clients.size)
            # Boot 报表也要算：辅助触控接管后只订 0x2A33，漏了它就会一直误报"未订阅"
            if self._boot_mouse is not None:
                n = max(n, self._boot_mouse.subscribed_clients.size)
            now = n > 0
        except Exception:
            now = self._subscribed
        if name:
            print(f"  [gatt] 订阅变化: {name}", flush=True)
        if now != self._subscribed:
            self._subscribed = now
            print(f"  [gatt] >>> 总订阅状态 {now}", flush=True)
            if self._on_subscribe:
                self._on_subscribe(now)

    # ---------- 对外 ----------

    def on_subscribe_changed(self, cb: Callable[[bool], None]) -> None:
        self._on_subscribe = cb

    @property
    def subscribed(self) -> bool:
        return self._subscribed

    @property
    def status(self) -> str:
        """原始广播状态。排障时必须看这个，别只看 advertising 的布尔值——
        ABORTED(3) 和 STOPPED(1) 都会让 advertising=False，但原因完全不同。"""
        if self._provider is None:
            return "no-provider"
        try:
            s = self._provider.advertisement_status
        except Exception as e:  # noqa: BLE001
            return f"error({e})"
        names = {
            0: "Created", 1: "Stopped", 2: "Started",
            3: "Aborted", 4: "StartedWithoutAllAdvData",
        }
        return f"{names.get(int(s), '?')}({int(s)})"

    @property
    def advertising(self) -> bool:
        if self._provider is None:
            return False
        return self._provider.advertisement_status in (
            GattServiceProviderAdvertisementStatus.STARTED,
            GattServiceProviderAdvertisementStatus.STARTED_WITHOUT_ALL_ADVERTISEMENT_DATA,
        )

    async def send(self, report_id: int, payload: bytes) -> bool:
        char = self._reports.get(report_id)
        if char is None:
            return False
        try:
            await char.notify_value_async(_buf(payload))
            return True
        except Exception:
            return False

    async def send_mouse(self, buttons: int, dx: int, dy: int, wheel: int = 0) -> bool:
        """鼠标位移同时按两种编码发出，但只发往真有订阅者的通道。

        关键：WinRT 的 notify_value_async 在没有订阅者时**不报错**，只是静默丢弃。
        所以"发送成功"不等于"报文送达"，必须自己查 subscribed_clients。

        Boot 报表只有 3 字节，没有滚轮字段；带滚轮时只能走 Report 协议。
        """
        ok = False
        if wheel == 0 and self._boot_mouse is not None and self._has_client(self._boot_mouse):
            # Boot 鼠标格式：buttons(3 bit) + dx(int8) + dy(int8)
            try:
                await self._boot_mouse.notify_value_async(
                    _buf(bytes([buttons & 0x07, dx & 0xFF, dy & 0xFF]))
                )
                ok = True
            except Exception:
                pass

        char = self._reports.get(hid.RID_RELATIVE)
        if char is not None and self._has_client(char):
            try:
                await char.notify_value_async(
                    _buf(hid.encode_relative_mouse(buttons, dx, dy, wheel))
                )
                ok = True
            except Exception:
                pass
        return ok

    @staticmethod
    def _has_client(char) -> bool:
        try:
            return char.subscribed_clients.size > 0
        except Exception:
            return False
