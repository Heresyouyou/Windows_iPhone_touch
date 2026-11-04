"""探针：这台 Mac 到底允许 CBPeripheralManager 发布哪些服务和特征。

为什么需要它
------------
`addService:` 失败时 CoreBluetooth 只给一句
「The specified UUID is not allowed for this operation.」（CBErrorUUIDNotAllowed），
既不说哪个 UUID 违规、也不说为什么。

本机（macOS 14.8.9）实测：**16 位 SIG UUID 一律被拒**，不管它是 0x1800/0x1801
这种系统保留的，还是 0x1812 这种我们真正要用的；换成 128 位自定义 UUID
立刻通过。所以这不是权限问题，是 macOS 对 CBPeripheralManager 的硬策略。

它直接决定项目可行性，所以还要再问一层：把 0x1812 按 Bluetooth SIG 基址展开成
00001812-0000-1000-8000-00805F9B34FB（规范上与 0x1812 等价）能不能过？
特征 UUID 是不是同样的规则？

只能通过 .app 跑
----------------
CoreBluetooth 首次使用要读「责任应用」的 Info.plist，命令行 python 没有，
会直接 abort。所以：

  ./make_app.sh
  ./run.sh --module=tools.ble_probe --new
  tail -f ~/Library/Logs/MacPhoneTouch.log

用法
----
  --s 1812 --s 180A                要测的服务 UUID（16 位短写或完整 128 位）
  --char                           每个服务挂一个 0x2A4D 特征
  --char 00002A4D-0000-1000-8000-00805F9B34FB
                                   挂指定特征
  --advertise                      全部加完后用 1812 广播 3 秒
  --timeout 3.0                    等蓝牙就绪的上限
"""
from __future__ import annotations

import argparse
import sys
import time

import CoreBluetooth as CB
import objc
from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode
from Foundation import NSObject

SIG_BASE = "-0000-1000-8000-00805F9B34FB"


def sig128(short: str) -> str:
    """16 位短 UUID 展开成 Bluetooth SIG 基址的 128 位形式。"""
    return f"0000{short.upper()}{SIG_BASE}"


# 默认候选：两种写法并列，一眼看出差别出在哪
DEFAULT_UUIDS = [
    "1801",                    # GATT（16 位，系统保留）
    "1812",                    # HID（16 位，本项目要用的）
    sig128("1812"),            # HID（128 位基址展开，规范上等价）
    "180A",                    # Device Information（16 位）
    sig128("180A"),            # Device Information（128 位）
    "6E400001-B5A3-F393-E0A9-E50E24DCCA9E",   # 纯自定义 128 位
]

STATE_NAMES = {
    0: "unknown",
    1: "resetting",
    2: "unsupported",
    3: "unauthorized",
    4: "poweredOff",
    5: "poweredOn",
}


def _pump(seconds: float) -> None:
    """在默认 run loop 里转一会儿，让 CoreBluetooth 的回调有机会跑。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.02, True)


def _uuid(text: str):
    return CB.CBUUID.UUIDWithString_(text)


class _Probe(NSObject):
    """逐条 addService，把每个结果记下来。"""

    def initWithUUIDs_charUUID_advUUID_(self, uuids, char_uuid, adv_uuid):
        # 必须用 objc.super：Python 的零参 super() 在 PyObjC 子类里找不到 init
        self = objc.super(_Probe, self).init()
        if self is None:
            return None
        self.uuids = list(uuids)
        self.char_uuid = char_uuid        # None = 不挂特征
        self.adv_uuid = adv_uuid          # None = 不广播
        self.state = 0
        self.results = []
        self.index = 0
        self.manager = None
        self.done = False
        return self

    # ---------------------------------------------------------- 回调

    def peripheralManagerDidUpdateState_(self, manager):
        self.state = int(manager.state())
        if self.state == CB.CBManagerStatePoweredOn:
            self._next()

    def peripheralManager_didAddService_error_(self, manager, service, error):
        uuid = self.uuids[self.index - 1]
        if error is None:
            self.results.append((uuid, True, ""))
            print("     ✓ 允许", flush=True)
        else:
            msg = error.localizedDescription() or f"code={error.code()}"
            self.results.append((uuid, False, msg))
            print(f"     ✗ {msg}", flush=True)
        self._next()

    def peripheralManagerDidStartAdvertising_error_(self, manager, error):
        if error is None:
            print("  广播: 成功", flush=True)
        else:
            print(f"  广播: 失败 {error.localizedDescription()}", flush=True)

    # ---------------------------------------------------------- 流程

    def _next(self):
        if self.index >= len(self.uuids):
            self.done = True
            if self.adv_uuid:
                self.advertise()
            return
        uuid = self.uuids[self.index]
        self.index += 1
        service = CB.CBMutableService.alloc().initWithType_primary_(_uuid(uuid), True)
        if self.char_uuid:
            char = (
                CB.CBMutableCharacteristic.alloc()
                .initWithType_properties_value_permissions_(
                    _uuid(self.char_uuid),
                    CB.CBCharacteristicPropertyRead | CB.CBCharacteristicPropertyNotify,
                    None,
                    CB.CBAttributePermissionsReadable,
                )
            )
            service.setCharacteristics_([char])
        print(f"  → {uuid}   特征={self.char_uuid or '无'}", flush=True)
        self.manager.addService_(service)

    def advertise(self):
        self.manager.startAdvertising_(
            {
                CB.CBAdvertisementDataLocalNameKey: "MPT-Probe",
                CB.CBAdvertisementDataServiceUUIDsKey: [_uuid(self.adv_uuid)],
            }
        )


def _summary(results) -> int:
    ok = [r for r in results if r[1]]
    bad = [r for r in results if not r[1]]
    print()
    print(f"允许 {len(ok)} 个" + ("" if not ok else ": " + " ".join(r[0] for r in ok)))
    print(f"拒绝 {len(bad)} 个")
    for uuid, _, err in bad:
        print(f"  {uuid:<40} {err}")
    print()

    def passed(*prefixes):
        return any(r[1] and r[0].upper().startswith(prefixes) for r in results)

    if passed("00001812", "1812"):
        print("结论: HID 服务可以发布 —— HOGP 这条路在本机可行。")
        return 0
    print(
        "结论: HID 服务(0x1812)两种写法都被拒 —— 这台 Mac 不允许应用发布 HID 服务。\n"
        "      这不是参数写错，是系统策略；Windows 版能跑是因为 WinRT 没有这条限制。"
    )
    return 1


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="CBPeripheralManager 服务发布探针")
    ap.add_argument("--s", action="append", default=[], help="要测的服务 UUID，可重复")
    ap.add_argument(
        "--char",
        nargs="?",
        const="2A4D",
        default=None,
        help="给每个服务挂一个特征，默认 0x2A4D",
    )
    ap.add_argument(
        "--advertise",
        nargs="?",
        const="1812",
        default=None,
        help="全部加完后广播 3 秒，默认带 0x1812",
    )
    ap.add_argument("--timeout", type=float, default=4.0, help="等蓝牙就绪的上限秒数")
    args = ap.parse_args(argv)

    uuids = args.s or DEFAULT_UUIDS
    print(f"探针: 逐个 addService，共 {len(uuids)} 个")
    print(f"      特征={args.char or '不挂'}  广播={args.advertise or '不广播'}")

    probe = _Probe.alloc().initWithUUIDs_charUUID_advUUID_(uuids, args.char, args.advertise)
    probe.manager = CB.CBPeripheralManager.alloc().initWithDelegate_queue_(probe, None)

    end = time.monotonic() + args.timeout
    while time.monotonic() < end and probe.state == 0:
        _pump(0.05)

    state = STATE_NAMES.get(probe.state, f"?({probe.state})")
    print(f"蓝牙状态: {state}")
    if probe.state != CB.CBManagerStatePoweredOn:
        if probe.state == CB.CBManagerStateUnauthorized:
            print("  授权被拒：系统设置 → 隐私与安全性 → 蓝牙，允许后重试")
        return 2

    end = time.monotonic() + args.timeout + 4.0
    while time.monotonic() < end and not probe.done:
        _pump(0.05)
    if not probe.done:
        print("⚠️ 超时：还没测完就卡住了，多半是某个 addService 没回调")
        return 3

    _pump(3.0 if args.advertise else 0.3)
    return _summary(probe.results)


if __name__ == "__main__":
    raise SystemExit(main())
