"""HOGP 的 GATT 数据库，写成纯数据。

为什么要把 GATT 表从蓝牙代码里拆出来
------------------------------------
Windows 版把 14 个特征直接写在 `hogp.py` 里，只有插上 iPhone 才能验证对错。
可实际决定成败的只有几条**静态事实**：

  · HID 服务必须是 0x1812，广播类型 Primary；
  · 每个 0x2A4D Report 特征必须挂 0x2908 Report Reference 描述符 [reportId, type]；
  · Report Map 与四个报表的权限必须是「加密可读」——设成明文时 iOS 能连、
    能订阅、能收字节，但不会把它当输入消费，且不报任何错；
  · Report Map 必须和上游逐字节一致。

这些都可以在没有任何蓝牙硬件的情况下断言。所以这里只描述「应该长什么样」，
`ble_hid.py` 负责把这份描述实例化成 CoreBluetooth 对象。改表的人会立刻在
单测里看到自己破坏了哪条不变量。

和 Windows 版的两处刻意差异
---------------------------
1. Windows 版额外提供了 Boot Protocol 三件套（0x2A22/0x2A32/0x2A33）和
   Appearance(0x2A01)。macOS 这边的参照实现是 iPhone Use——它只提供
   Report Protocol 就能稳定工作，说明 iOS 的辅助触控走的是 Report 协议。
   少三个特征就少三处可能出错的地方，所以这里按参照实现来。

2. 服务 UUID 发布时必须展开成 128 位（见下面 sig128），否则
   CBPeripheralManager 一律拒收。特征和描述符不用展开。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import hid

# ---------------------------------------------------------------- 服务

SERVICE_GAP = "1801"       # Generic Attribute：Service Changed 用它做缓存失效通知
SERVICE_HID = "1812"       # HID over GATT
SERVICE_DIS = "180A"       # Device Information
SERVICE_BATTERY = "180F"   # Battery

# ---------------------------------------------------------------- 128 位展开
#
# macOS 的一条硬策略（实测 macOS 14.8.9）：CBPeripheralManager 的 addService:
# **拒收所有 16 位 UUID**，报 CBErrorUUIDNotAllowed
# 「The specified UUID is not allowed for this operation.」。
# 它不看这个 UUID 是什么——0x1800/0x1801 被拒，我们真正要用的 0x1812 一样被拒，
# 换成一个随手的 128 位自定义 UUID 就立刻通过。所以这不是权限，是表示形式。
#
# 而 Bluetooth 规范里 SIG 基址 UUID 与 16 位短 UUID 是**同一条**：
#   0x1812  ==  00001812-0000-1000-8000-00805F9B34FB
# 于是发布时把服务 UUID 展开成 128 位，语义不变，检查能过。
#
# 特征和描述符不受这条限制：16 位的 0x2A4D 挂在 128 位服务下实测可以正常添加。
# 所以只有 ServiceSpec 需要展开，CharSpec 保持 16 位原样（读请求里比对 UUID
# 也是按 16 位做的）。
SIG_BASE = "-0000-1000-8000-00805F9B34FB"


def sig128(short: str) -> str:
    """16 位短 UUID → 128 位 SIG 基址形式；已经是 128 位的原样返回。"""
    s = short.replace("-", "").upper()
    if len(s) == 4:
        return f"0000{s}{SIG_BASE}"
    return short.upper()

# ---------------------------------------------------------------- 特征

C_SERVICE_CHANGED = "2A05"
C_HID_INFO = "2A4A"
C_REPORT_MAP = "2A4B"
C_CONTROL_POINT = "2A4C"
C_REPORT = "2A4D"
C_PROTOCOL_MODE = "2A4E"
C_MANUFACTURER = "2A29"
C_PNP_ID = "2A50"
C_BATTERY_LEVEL = "2A19"

D_REPORT_REFERENCE = "2908"

# 属性
READ = "read"
WRITE = "write"
WRITE_NO_RSP = "writeNr"
NOTIFY = "notify"
INDICATE = "indicate"

# 权限
PERM_READ = "readable"
PERM_WRITE = "writeable"
PERM_READ_ENC = "readEnc"      # CBAttributePermissionsReadEncryptionRequired
PERM_WRITE_ENC = "writeEnc"    # CBAttributePermissionsWriteEncryptionRequired

# HID Information：bcdHID 1.11 / country 0 / NormallyConnectable
HID_INFORMATION = bytes([0x11, 0x01, 0x00, 0x02])

# 各通道的报文长度（读取时返回这么多个 0）
REPORT_LENGTH = {
    hid.RID_KEYBOARD: 8,
    hid.RID_ABSOLUTE: 5,
    hid.RID_RELATIVE: 4,
    hid.RID_CONSUMER: 2,
}

PnP_ID = bytes([0x02, 0xFF, 0xFF, 0x00, 0x01, 0x00, 0x01])
MANUFACTURER_NAME = "Mac iPhone Touch"


@dataclass(frozen=True)
class CharSpec:
    uuid: str
    props: frozenset[str]
    perms: frozenset[str]
    value: bytes | None = None      # None 表示值由 delegate 动态给出
    read_length: int = 0            # value 为 None 时，读请求返回这么多个 0
    report_id: int = 0              # 非 0 表示挂 0x2908 描述符
    report_type: int = 0            # 1=Input 2=Output

    @property
    def needs_report_reference(self) -> bool:
        return self.report_id != 0


@dataclass(frozen=True)
class ServiceSpec:
    uuid: str
    primary: bool
    chars: tuple[CharSpec, ...]
    publish: bool = True           # False = 只作说明，不真的注册给 CoreBluetooth

    @property
    def wire_uuid(self) -> str:
        """真正发给 CoreBluetooth 的服务 UUID。见上面 sig128 的说明。"""
        return sig128(self.uuid)


def _report(rid: int, *, notify: bool = True) -> CharSpec:
    return CharSpec(
        uuid=C_REPORT,
        props=frozenset({READ, NOTIFY} if notify else {READ, WRITE, WRITE_NO_RSP}),
        # 报文特征必须加密读：这是整个项目最贵的一条经验
        perms=frozenset({PERM_READ_ENC} if notify else {PERM_READ_ENC, PERM_WRITE_ENC}),
        value=None,
        read_length=REPORT_LENGTH.get(rid, 1) if notify else 1,
        report_id=rid,
        report_type=1 if notify else 2,
    )


def services() -> tuple[ServiceSpec, ...]:
    """完整服务列表。顺序会进 iOS 的缓存，改动后必须在手机上「忽略此设备」重配。"""
    # GAP(0x1801) 不发布：CoreBluetooth 自己就带着 GATT 服务，手动加是重复的；
    # 而且 Service Changed 由系统维护，应用抢着加只会多一处出错的地方。
    # Windows 那边必须显式建它（WinRT 不代劳），这里反过来。
    gap = ServiceSpec(
        uuid=SERVICE_GAP,
        primary=True,
        publish=False,
        chars=(
            CharSpec(C_SERVICE_CHANGED, frozenset({INDICATE}), frozenset({PERM_READ})),
        ),
    )
    hid_service = ServiceSpec(
        uuid=SERVICE_HID,
        primary=True,
        chars=(
            CharSpec(C_HID_INFO, frozenset({READ}), frozenset({PERM_READ}), HID_INFORMATION),
            # Report Map 必须是加密读：iOS 读取它的时候顺便发起配对（bonding）
            CharSpec(
                C_REPORT_MAP,
                frozenset({READ}),
                frozenset({PERM_READ_ENC}),
                hid.REPORT_MAP,
            ),
            CharSpec(
                C_CONTROL_POINT,
                frozenset({WRITE_NO_RSP}),
                frozenset({PERM_WRITE_ENC}),
            ),
            CharSpec(
                C_PROTOCOL_MODE,
                frozenset({READ, WRITE_NO_RSP}),
                frozenset({PERM_READ, PERM_WRITE}),
                read_length=1,
            ),
            _report(hid.RID_KEYBOARD),
            _report(hid.RID_ABSOLUTE),
            _report(hid.RID_RELATIVE),
            _report(hid.RID_CONSUMER),
            _report(hid.RID_KEYBOARD, notify=False),   # 键盘 LED：输出报表
        ),
    )
    dis = ServiceSpec(
        uuid=SERVICE_DIS,
        primary=True,
        chars=(
            CharSpec(
                C_MANUFACTURER,
                frozenset({READ}),
                frozenset({PERM_READ}),
                MANUFACTURER_NAME.encode(),
            ),
            CharSpec(C_PNP_ID, frozenset({READ}), frozenset({PERM_READ}), PnP_ID),
        ),
    )
    battery = ServiceSpec(
        uuid=SERVICE_BATTERY,
        primary=True,
        chars=(
            CharSpec(
                C_BATTERY_LEVEL,
                frozenset({READ, NOTIFY}),
                frozenset({PERM_READ}),
                read_length=1,
            ),
        ),
    )
    return (gap, hid_service, dis, battery)


def published_services() -> tuple[ServiceSpec, ...]:
    """真正要注册给 CoreBluetooth 的服务。见 ServiceSpec.publish 的说明。"""
    return tuple(s for s in services() if s.publish)


def hid_service() -> ServiceSpec:
    for s in services():
        if s.uuid == SERVICE_HID:
            return s
    raise AssertionError("HID 服务缺失")
