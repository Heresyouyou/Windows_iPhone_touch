"""把 Mac 发布成一个蓝牙键鼠（HOGP / HID over GATT）。

Windows 版用的是 WinRT 的 GattServiceProvider；macOS 的等价物是
CoreBluetooth 的 CBPeripheralManager。两者模型不一样，有几处必须注意：

1. **服务要一个一个加**。`addService:` 的回调是串行的，一次性 add 多个虽然
   API 允许，但回调里判断"哪一个是最后加完的"很容易错。这里照参照实现的做法：
   加完一个再加下一个，全部加完才广播。

2. **`updateValue:for:onSubscribedCentrals:` 是同步的，返回 Bool**。
   返回 NO 表示发送队列满了，报文必须自己排队，等
   `peripheralManagerIsReadyToUpdateSubscribers:` 再补发。Windows 那边
   `notify_value_async` 是无条件成功的，所以没有这段逻辑——直接照抄会丢报文。

3. **没有订阅者时 updateValue 也返回 YES**（只是没人收）。所以"发送成功"
   不等于"送达"，状态展示必须看订阅表。

4. **权限必须带加密**。见 gatt_table 的说明：明文特征下 iOS 能连、能订阅、
   能收字节，但不当输入用，且不报错。

这一层不需要 asyncio：CoreBluetooth 的回调挂在主队列/主 run loop 上，
和事件 tap、UI 同一个线程，天然串行。这也是 macOS 版比 Windows 版少一个
线程、少一整类竞态的原因。
"""
from __future__ import annotations

import os

import objc
import CoreBluetooth as CB
from Foundation import NSData, NSObject

from . import gatt_table as gt

_PROP = {
    gt.READ: CB.CBCharacteristicPropertyRead,
    gt.WRITE: CB.CBCharacteristicPropertyWrite,
    gt.WRITE_NO_RSP: CB.CBCharacteristicPropertyWriteWithoutResponse,
    gt.NOTIFY: CB.CBCharacteristicPropertyNotify,
    gt.INDICATE: CB.CBCharacteristicPropertyIndicate,
}
_PERM = {
    gt.PERM_READ: CB.CBAttributePermissionsReadable,
    gt.PERM_WRITE: CB.CBAttributePermissionsWriteable,
    gt.PERM_READ_ENC: CB.CBAttributePermissionsReadEncryptionRequired,
    gt.PERM_WRITE_ENC: CB.CBAttributePermissionsWriteEncryptionRequired,
}

STATE_NAMES = {
    0: "unknown",
    1: "resetting",
    2: "unsupported",
    3: "unauthorized",
    4: "poweredOff",
    5: "poweredOn",
}


def _uuid(short: str):
    return CB.CBUUID.UUIDWithString_(short)


def bundle_warning() -> str | None:
    """检查当前进程的「责任应用」有没有蓝牙用途说明。

    这是 macOS 的硬性要求，不是本程序的毛病：CoreBluetooth 首次使用时 TCC 会读
    责任应用的 Info.plist 找 NSBluetoothAlwaysUsageDescription，找不到就直接
    abort（崩溃报告写得很明白）。直接用 `python -m ios_screen.app` 跑，责任应用
    是终端或 IDE，它们通常没有这一项，于是必然崩。

    只看 `__CFBundleIdentifier` 不够：从别的 App（比如 IDE）里启动时它也会被继承。
    所以由 .app 里的启动脚本导出 `MPT_APP_BUNDLE`（它自己的路径），这里直接读
    那份 Info.plist 确认真有这一项。
    """
    path = os.environ.get("MPT_APP_BUNDLE")
    if path:
        plist = os.path.join(path, "Contents", "Info.plist")
        try:
            import plistlib

            with open(plist, "rb") as f:
                info = plistlib.load(f)
            if info.get("NSBluetoothAlwaysUsageDescription"):
                return None
            hint = f"{plist} 里没有 NSBluetoothAlwaysUsageDescription。"
        except Exception as e:  # noqa: BLE001
            hint = f"读不到 {plist}: {e}"
    else:
        bid = os.environ.get("__CFBundleIdentifier")
        hint = (
            f"当前责任应用是 {bid}，不是本程序的 .app，TCC 会用它自己的 Info.plist。"
            if bid
            else "当前进程没有 bundle 身份（由 shell 直接启动）。"
        )
    return (
        "❌ 蓝牙功能必须在 .app 里运行。\n"
        f"   {hint}\n"
        "   macOS 的 TCC 在这种情况会**直接 abort 进程**，不是本程序能捕获的异常。\n"
        "   解决：先运行 ./make_app.sh 生成 dist/Mac iPhone Touch.app，\n"
        "   再用 ./run.sh 启动（不要直接 open --args，本机实测它不传参）；\n"
        "   只想验证报文逻辑可以加 --dry-run（不碰蓝牙）。"
    )


def _data(raw: bytes | None):
    if raw is None:
        return None
    return NSData.dataWithBytes_length_(raw, len(raw))


def _bytes(nsdata) -> bytes:
    if nsdata is None:
        return b""
    try:
        return bytes(nsdata)
    except Exception:
        return b""


class _Delegate(NSObject):
    """CoreBluetooth 回调转发。方法名里的下划线就是 ObjC 选择器的冒号。"""

    def initWithOwner_(self, owner):
        self = objc.super(_Delegate, self).init()
        if self is None:
            return None
        self._owner = owner
        return self

    def peripheralManagerDidUpdateState_(self, manager):
        self._owner._on_state(manager)

    def peripheralManager_didAddService_error_(self, manager, service, error):
        self._owner._on_service_added(service, error)

    def peripheralManagerDidStartAdvertising_error_(self, manager, error):
        self._owner._on_advertising(error)

    def peripheralManager_didReceiveReadRequest_(self, manager, request):
        self._owner._on_read(manager, request)

    def peripheralManager_didReceiveWriteRequests_(self, manager, requests):
        self._owner._on_write(manager, requests)

    def peripheralManager_central_didSubscribeToCharacteristic_(self, manager, central, char):
        self._owner._on_subscribe(char, central, True)

    def peripheralManager_central_didUnsubscribeFromCharacteristic_(self, manager, central, char):
        self._owner._on_subscribe(char, central, False)

    def peripheralManagerIsReadyToUpdateSubscribers_(self, manager):
        self._owner._drain()


class HidPeripheral:
    def __init__(self, local_name: str = "Mac iPhone", verbose: bool = False) -> None:
        self.local_name = local_name
        self.verbose = verbose

        self._manager = None
        self._delegate = None
        self._queue = list(gt.published_services())   # 待逐个添加
        self._adding = None
        self._published = False
        self._failed_services: list[str] = []

        self._chars: dict[int, object] = {}     # report id → CBMutableCharacteristic
        self._services: list = []              # 强引用：PyObjC 不持有就会被回收
        self._output_char = None
        self._control_point = None
        self._service_changed = None
        self._char_report_id: dict[int, int] = {}   # id(char) → report id（含输出报表）
        self._subs: dict[int, set] = {}             # id(char) → {central id}
        self._pending: list[tuple[int, bytes]] = []

        self._state = 0
        self._advertising = False
        self._advertised_error = None
        self._subscribed = False
        # 注意别叫 self._on_subscribe：那会遮盖同名方法 _on_subscribe()，
        # 订阅回调触发时就变成「调用 None」，整个进程被 ObjC 异常带走。
        self._subscribe_cb = None
        self._sub_count = 0

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        self._delegate = _Delegate.alloc().initWithOwner_(self)
        self._manager = CB.CBPeripheralManager.alloc().initWithDelegate_queue_(
            self._delegate, None
        )

    def stop(self) -> None:
        if self._manager is not None:
            try:
                self._manager.stopAdvertising()
            except Exception:
                pass
            try:
                self._manager.removeAllServices()
            except Exception:
                pass
            try:
                self._manager.setDelegate_(None)
            except Exception:
                pass
        self._manager = None
        self._published = False
        self._advertising = False
        self._state = 0
        self._chars.clear()
        self._char_report_id.clear()
        self._services.clear()
        self._subs.clear()
        self._pending.clear()

    # ------------------------------------------------------------ 状态

    @property
    def state(self) -> str:
        return STATE_NAMES.get(self._state, f"?({self._state})")

    @property
    def powered_on(self) -> bool:
        return self._state == CB.CBManagerStatePoweredOn

    @property
    def advertising(self) -> bool:
        return self._advertising

    @property
    def subscribed(self) -> bool:
        return self._subscribed

    @property
    def status(self) -> str:
        if self._manager is None:
            return "no-provider"
        if not self.powered_on:
            return self.state
        if not self._published:
            return "publishing"
        if self._advertised_error:
            return f"adv-error({self._advertised_error})"
        return "advertising" if self._advertising else "not-advertising"

    @property
    def subscription_detail(self) -> str:
        parts = []
        for rid, char in sorted(self._chars.items()):
            n = len(self._subs.get(id(char), ()))
            if n:
                parts.append(f"R{rid}×{n}")
        return " ".join(parts) or "无"

    def on_subscribe_changed(self, cb) -> None:
        self._subscribe_cb = cb

    # ------------------------------------------------------------ 发布流程

    def _on_state(self, manager) -> None:
        self._state = int(manager.state())
        if self.verbose:
            print(f"  [ble] state={self.state}", flush=True)
        if self._state == CB.CBManagerStatePoweredOn:
            # 复位后旧数据库失效，必须从头建，不能沿用缓存的对象
            self._chars.clear()
            self._char_report_id.clear()
            self._services.clear()
            self._subs.clear()
            self._pending.clear()
            self._published = False
            self._advertising = False
            self._failed_services = []
            self._queue = list(gt.published_services())
            self._add_next()
        elif self._state == CB.CBManagerStateUnauthorized:
            print(
                "  [ble] 蓝牙权限被拒绝：系统设置 → 隐私与安全性 → 蓝牙，"
                "允许当前终端/应用后重启本程序",
                flush=True,
            )
        elif self._state == CB.CBManagerStateUnsupported:
            print("  [ble] 这台机器不支持 BLE 外设角色", flush=True)

    def _add_next(self) -> None:
        if not self.powered_on:
            return
        if not self._queue:
            self._published = True
            if gt.SERVICE_HID in self._failed_services:
                print(
                    "  [ble] ❌ HID 服务(0x1812)被系统拒绝，iPhone 端不可能看到这个设备。\n"
                    "       广播仍然会开，但没有可用的输入通道。",
                    flush=True,
                )
            self._advertise()
            return
        spec = self._queue.pop(0)
        service = self._build_service(spec)
        # 必须留住强引用。PyObjC 只是把对象交给 ObjC，Python 这边不持有的话
        # 回调一结束就可能被回收，表现是「服务加过了但主机只看到其中一两个」。
        self._services.append(service)
        self._adding = service
        self._manager.addService_(service)
        if self.verbose:
            print(f"  [ble] 添加服务 {spec.uuid}", flush=True)

    def _build_service(self, spec: gt.ServiceSpec):
        chars = []
        for c in spec.chars:
            props = 0
            for p in c.props:
                props |= _PROP[p]
            perms = 0
            for p in c.perms:
                perms |= _PERM[p]
            ch = CB.CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
                _uuid(c.uuid), props, _data(c.value), perms
            )
            if c.needs_report_reference:
                desc = CB.CBMutableDescriptor.alloc().initWithType_value_(
                    _uuid(gt.D_REPORT_REFERENCE),
                    _data(bytes([c.report_id, c.report_type])),
                )
                ch.setDescriptors_([desc])
            chars.append(ch)
            self._remember(c, ch)
        service = CB.CBMutableService.alloc().initWithType_primary_(
            _uuid(spec.wire_uuid), spec.primary
        )
        service.setCharacteristics_(chars)
        return service

    def _remember(self, spec: gt.CharSpec, char) -> None:
        if spec.uuid == gt.C_REPORT and spec.report_type == 1:
            self._chars[spec.report_id] = char
            self._char_report_id[id(char)] = spec.report_id
        elif spec.uuid == gt.C_REPORT:
            self._output_char = char
            self._char_report_id[id(char)] = spec.report_id
        elif spec.uuid == gt.C_CONTROL_POINT:
            self._control_point = char
        elif spec.uuid == gt.C_SERVICE_CHANGED:
            self._service_changed = char

    def _on_service_added(self, service, error) -> None:
        if error is not None:
            uuid = "?"
            try:
                uuid = service.UUID().UUIDString()
            except Exception:
                pass
            print(
                f"  [ble] 添加服务 {uuid} 失败: {error.localizedDescription()}",
                flush=True,
            )
            self._failed_services.append(uuid)
            self._adding = None
            # 关键：不能因为一个可选服务被拒就停在这里，否则永远走不到广播。
            # 真正的致命项（HID 服务）会在下面单独检查。
            self._add_next()
            return
        self._adding = None
        self._add_next()

    def _advertise(self) -> None:
        if not self.powered_on or self._manager.isAdvertising():
            return
        # 只带 0x1812 和名字。塞太多会把广播包顶到 31 字节上限，
        # 表现为「设备能扫到但辅助触控里一直转圈」。
        self._advertised_error = None
        self._manager.startAdvertising_(
            {
                CB.CBAdvertisementDataLocalNameKey: self.local_name,
                CB.CBAdvertisementDataServiceUUIDsKey: [_uuid(gt.SERVICE_HID)],
            }
        )

    def _on_advertising(self, error) -> None:
        if error is not None:
            # 已经广播中会报 alreadyAdvertising，这不算失败
            self._advertised_error = str(error.localizedDescription())
            self._advertising = self._manager.isAdvertising()
        else:
            self._advertised_error = None
            self._advertising = True
        if self.verbose:
            print(f"  [ble] 广播={self._advertising} 错误={self._advertised_error}", flush=True)

    # ------------------------------------------------------------ GATT 读写

    def _on_read(self, manager, request) -> None:
        char = request.characteristic()
        if self.verbose:
            # 主机连上来第一件事就是读 HID Information / Report Map，
            # 所以这条日志能区分「根本没连上」和「连上了但没订阅」。
            print(
                f"  [ble] ← 读 {char.UUID().UUIDString()[:8]} "
                f"from {request.central().identifier().UUIDString()[:8]}",
                flush=True,
            )
        value = self._read_value(char)
        if value is None:
            manager.respondToRequest_withResult_(request, CB.CBATTErrorAttributeNotFound)
            return
        offset = int(request.offset())
        if offset > len(value):
            manager.respondToRequest_withResult_(request, CB.CBATTErrorInvalidOffset)
            return
        request.setValue_(_data(value[offset:]))
        manager.respondToRequest_withResult_(request, CB.CBATTErrorSuccess)

    def _read_value(self, char) -> bytes | None:
        uuid = char.UUID().UUIDString()
        if uuid == gt.C_PROTOCOL_MODE:
            return b"\x01"                       # 1 = Report Protocol
        if uuid == gt.C_BATTERY_LEVEL:
            return b"\x64"                       # 100%
        rid = self._char_report_id.get(id(char))
        if rid is not None:
            if char is self._output_char:
                return b"\x00"
            return bytes(gt.REPORT_LENGTH.get(rid, 0))
        return _bytes(char.value())

    def _on_write(self, manager, requests) -> None:
        first = None
        for req in requests:
            if first is None:
                first = req
            char = req.characteristic()
            if self.verbose:
                print(
                    f"  [ble] → 写 {char.UUID().UUIDString()[:8]} "
                    f"{len(_bytes(req.value()))}B "
                    f"from {req.central().identifier().UUIDString()[:8]}",
                    flush=True,
                )
            if char is self._control_point:
                # 控制点写入但没有任何输入订阅 = 主机拿着旧的 GATT 缓存。
                # 用 Service Changed 让它重新发现服务，否则它会一直转圈。
                self._maybe_service_changed(req.central())
        if first is not None:
            manager.respondToRequest_withResult_(first, CB.CBATTErrorSuccess)

    def _maybe_service_changed(self, central) -> None:
        if not self._has_input_subscriber() and self._service_changed is not None:
            self._queue_report(self._service_changed, bytes([0x10, 0x00, 0xFF, 0xFF]), 1)

    # ------------------------------------------------------------ 订阅

    def _on_subscribe(self, char, central, subscribed: bool) -> None:
        key = id(char)
        holders = self._subs.setdefault(key, set())
        cid = central.identifier().UUIDString()
        if subscribed:
            holders.add(cid)
        else:
            holders.discard(cid)
        if self.verbose:
            rid = self._char_report_id.get(key)
            tag = f"R{rid}" if rid is not None else char.UUID().UUIDString()
            print(f"  [ble] {'订阅' if subscribed else '退订'} {tag}", flush=True)
        now = self._has_input_subscriber()
        if now != self._subscribed:
            self._subscribed = now
            print(f"  [ble] >>> 总订阅状态 {now}", flush=True)
            if self._subscribe_cb:
                self._subscribe_cb(now)

    def _has_input_subscriber(self) -> bool:
        """有没有主机在监听输入报表。只有输入报表算数——订阅电量不代表能用。"""
        return any(self._subs.get(id(c)) for c in self._chars.values())

    # ------------------------------------------------------------ 发送

    def send(self, report_id: int, payload: bytes) -> bool:
        """发一条输入报表。返回 False 表示没有可用的报文通道。"""
        char = self._chars.get(report_id)
        if char is None:
            return False
        return self._queue_report(char, payload, len(payload))

    def send_mouse(self, buttons: int, dx: int, dy: int, wheel: int = 0) -> bool:
        from . import hid

        return self.send(hid.RID_RELATIVE, hid.encode_relative_mouse(buttons, dx, dy, wheel))

    def _queue_report(self, char, payload: bytes, size: int) -> bool:
        if self._manager is None or not self.powered_on or not self._published:
            return False
        ok = self._manager.updateValue_forCharacteristic_onSubscribedCentrals_(
            _data(payload), char, None
        )
        if not ok:
            # 队列满：留着，等 isReadyToUpdateSubscribers 再补。
            # 只保留最近 16 条——指针报文过期就没意义了，补发旧的反而会抖。
            self._pending.append((id(char), payload))
            if len(self._pending) > 16:
                del self._pending[:-16]
        return True

    def _drain(self) -> None:
        if self._manager is None or not self._pending:
            return
        by_id = {id(c): c for c in self._chars.values()}
        if self._output_char is not None:
            by_id[id(self._output_char)] = self._output_char
        if self._service_changed is not None:
            by_id[id(self._service_changed)] = self._service_changed
        left = []
        for key, payload in self._pending:
            char = by_id.get(key)
            if char is None:
                continue
            if not self._manager.updateValue_forCharacteristic_onSubscribedCentrals_(
                _data(payload), char, None
            ):
                left.append((key, payload))
        self._pending = left
