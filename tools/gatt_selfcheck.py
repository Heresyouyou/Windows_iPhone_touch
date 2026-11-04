"""连上一台 BLE HID 设备，把它真正的 GATT 表 dump 出来并逐条核对。

用途
----
iOS 的辅助触控只认某些形状的 HID 表。这个工具把「别人家的合格实现」和
「我们自己的实现」都照一遍，差异一眼可见：

  # 看隔壁跑 Windows 版的那台机器（它正在广播 1812）
  ./run.sh --module=tools.gatt_selfcheck --new --name=DESKTOP-DTEKPKA

  # 先在本机发布起来，再从**另一台** Mac 上跑同一个命令看自己
  ./run.sh --module=tools.gatt_selfcheck --new --publish --name="Mac iPhone"

  # 只列附近有什么，不连接
  ./run.sh --module=tools.gatt_selfcheck --new --dump

为什么不能在本机验自己
----------------------
CoreBluetooth 不把本机外设的广播投递给本机的主机角色——实测扫了 8 秒，
周围十几台设备都在，唯独没有自己的 "Mac iPhone"。这是控制器层面的行为，
不是代码问题。所以「自己验自己」这条路走不通，要么用第二台机器，要么直接上 iPhone。

退出码：0 = 表符合预期；1 = 有断言没过；2 = 蓝牙没就绪；3 = 没找到目标设备。
"""
from __future__ import annotations

import sys
import time

import CoreBluetooth as CB
import objc
from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode
from Foundation import NSObject

from ios_screen import gatt_table as gt
from ios_screen.gatt_table import sig128

# 主机侧匹配服务时也要写 128 位基址形式：CoreBluetooth 的 CBUUID 按字节比，
# 2 字节的 "1812" 与 16 字节的基址形式**不相等**。
HID_WIRE = sig128(gt.SERVICE_HID)


def _pump(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.02, True)


def _uuid(text: str):
    return CB.CBUUID.UUIDWithString_(text)


def _short(uuid) -> str:
    """CBUUID → 便于阅读的形式：SIG 基址的显示 16 位，其它原样。"""
    s = uuid.UUIDString().upper()
    hexs = s.replace("-", "")
    if len(hexs) == 32 and hexs.startswith("0000") and hexs.endswith("00805F9B34FB"):
        return hexs[4:8]
    return s


def _props(c) -> str:
    p = c.properties()
    tags = []
    if p & CB.CBCharacteristicPropertyRead:
        tags.append("r")
    if p & CB.CBCharacteristicPropertyWrite:
        tags.append("w")
    if p & CB.CBCharacteristicPropertyWriteWithoutResponse:
        tags.append("wN")
    if p & CB.CBCharacteristicPropertyNotify:
        tags.append("n")
    if p & CB.CBCharacteristicPropertyIndicate:
        tags.append("i")
    return ",".join(tags) or "-"


class _Inspector(NSObject):
    def init(self):
        self = objc.super(_Inspector, self).init()
        if self is None:
            return None
        self.central = None
        self.target_name = ""
        self.dump = False
        self.peripheral = None
        self.found = False
        self.services = []
        self.failures = []
        self.stage = "idle"
        self.done = False
        return self

    # -------------------------------------------------- 主机侧

    def centralManagerDidUpdateState_(self, central):
        print(f"  [central] state={int(central.state())}", flush=True)
        if central.state() != CB.CBManagerStatePoweredOn:
            return
        self.stage = "scanning"
        central.scanForPeripheralsWithServices_options_(
            None if self.dump else [_uuid(HID_WIRE)],
            {CB.CBCentralManagerScanOptionAllowDuplicatesKey: False},
        )
        print(f"  [central] 扫描 {'全部设备' if self.dump else HID_WIRE}", flush=True)

    def centralManager_didDiscoverPeripheral_advertisementData_RSSI_(
        self, central, peripheral, data, rssi
    ):
        name = data.get(CB.CBAdvertisementDataLocalNameKey) or peripheral.name() or "?"
        uuids = [_short(u) for u in (data.get(CB.CBAdvertisementDataServiceUUIDsKey) or [])]
        print(f"  [central] {name!r}  RSSI={rssi}  服务={uuids or '无'}", flush=True)
        if self.dump or self.peripheral is not None:
            return
        if self.target_name and name != self.target_name:
            return
        self.peripheral = peripheral
        self.found = True
        self.stage = "connecting"
        print(f"  [central] 目标确认，连接 {name!r}", flush=True)
        central.connectPeripheral_options_(peripheral, None)

    def centralManager_didConnectPeripheral_(self, central, peripheral):
        print("  [central] 已连接，发现服务", flush=True)
        peripheral.setDelegate_(self)
        peripheral.discoverServices_(None)

    def centralManager_didFailToConnectPeripheral_error_(self, central, peripheral, error):
        msg = error.localizedDescription() if error else "?"
        self.failures.append(f"连接失败: {msg}")
        self.done = True

    def centralManager_didDisconnectPeripheral_error_(self, central, peripheral, error):
        print("  [central] 已断开", flush=True)

    # -------------------------------------------------- 外设侧回调（发现结果）

    def peripheral_didDiscoverServices_(self, peripheral, error):
        if error is not None:
            self.failures.append(f"发现服务失败: {error.localizedDescription()}")
            self.done = True
            return
        self.services = list(peripheral.services() or [])
        print(f"  [central] 服务 {len(self.services)} 个: "
              f"{[_short(s.UUID()) for s in self.services]}", flush=True)
        if not self.services:
            self.done = True
            return
        self.stage = "chars"
        for s in self.services:
            peripheral.discoverCharacteristics_forService_(None, s)

    def peripheral_didDiscoverCharacteristicsForService_error_(self, peripheral, service, error):
        if error is not None:
            self.failures.append(f"发现特征失败: {error.localizedDescription()}")
            self.done = True
            return
        chars = list(service.characteristics() or [])
        print(f"  [central] └ {_short(service.UUID())}: {len(chars)} 个特征", flush=True)
        for c in chars:
            print(f"             - {_short(c.UUID())}  [{_props(c)}]", flush=True)
        if all(s.characteristics() is not None for s in self.services):
            if self.stage != "descriptors":
                self.stage = "descriptors"
                for s in self.services:
                    for c in s.characteristics() or []:
                        if _short(c.UUID()) == gt.C_REPORT:
                            peripheral.discoverDescriptorsForCharacteristic_(c)

    def peripheral_didDiscoverDescriptorsForCharacteristic_error_(
        self, peripheral, char, error
    ):
        if error is not None:
            return
        names = [_short(d.UUID()) for d in (char.descriptors() or [])]
        print(f"  [central]   报表描述符 {names or '无'}", flush=True)
        if gt.D_REPORT_REFERENCE not in names:
            self.failures.append("有报表缺 0x2908 Report Reference")


def _verify(services) -> list[str]:
    """按 iOS 的实际要求核对这张表。"""
    bad = []
    by_uuid = {_short(s.UUID()): s for s in services}
    if gt.SERVICE_HID not in by_uuid:
        bad.append(f"没有 HID 服务：实际 {sorted(by_uuid)}")
        return bad

    chars = {_short(c.UUID()): c for c in (by_uuid[gt.SERVICE_HID].characteristics() or [])}
    for need, why in (
        (gt.C_HID_INFO, "HID Information 0x2A4A"),
        (gt.C_REPORT_MAP, "Report Map 0x2A4B"),
        (gt.C_CONTROL_POINT, "Control Point 0x2A4C"),
        (gt.C_PROTOCOL_MODE, "Protocol Mode 0x2A4E"),
    ):
        if need not in chars:
            bad.append(f"缺 {why}")

    reports = [
        c for c in (by_uuid[gt.SERVICE_HID].characteristics() or [])
        if _short(c.UUID()) == gt.C_REPORT
    ]
    want = len(gt.REPORT_LENGTH) + 1        # 4 个输入 + 1 个输出（键盘 LED）
    if len(reports) != want:
        bad.append(f"报表数 {len(reports)}，期望 {want}")

    notify = [c for c in reports if c.properties() & CB.CBCharacteristicPropertyNotify]
    if len(notify) != len(gt.REPORT_LENGTH):
        bad.append(f"可 notify 的输入报表 {len(notify)} 个，期望 {len(gt.REPORT_LENGTH)}")

    return bad


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    def opt(name, default=None):
        for a in args:
            if a.startswith(f"--{name}="):
                return a.split("=", 1)[1]
        return default

    dump = "--dump" in args
    publish = "--publish" in args
    target = opt("name", "Mac iPhone")
    scan_secs = float(opt("scan-secs", "10"))

    if dump:
        target = ""

    ble = None
    if publish:
        from ios_screen.ble_hid import HidPeripheral

        print("先在本机发布 HID 外设…")
        ble = HidPeripheral(local_name=target or "Mac iPhone", verbose=True)
        ble.start()
        end = time.monotonic() + 6.0
        while time.monotonic() < end and not ble.advertising:
            _pump(0.05)
        print(f"  外设: {ble.status}")
        if not ble.advertising:
            print("❌ 广播没起来，先跑 tools/ble_probe.py")
            return 2

    print(f"检查目标: {target or '（只列举）'}")
    checker = _Inspector.alloc().init()
    checker.target_name = target
    checker.dump = dump
    checker.central = CB.CBCentralManager.alloc().initWithDelegate_queue_(checker, None)

    end = time.monotonic() + scan_secs
    while time.monotonic() < end and not checker.done:
        _pump(0.1)

    if dump:
        return 0

    if checker.peripheral is None:
        print()
        print(f"❌ {scan_secs:.0f} 秒内没找到 {target!r}。")
        print("   注意：本机扫描**看不到自己的广播**，所以 --publish 之后必须换一台机器来看。")
        print("   要在 iPhone 上试：设置 → 辅助功能 → 触控 → 辅助触控 → 设备 → 蓝牙设备")
        return 3

    failures = list(checker.failures) + _verify(checker.services)
    print()
    if failures:
        print(f"❌ {len(failures)} 项不通过:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("✅ 这张 HID 表符合 iOS 的期望形状。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
