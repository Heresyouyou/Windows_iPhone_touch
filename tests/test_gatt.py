"""GATT 表单测：把"必须加密"这条经验变成断言。

这些断言不需要蓝牙硬件，但它们恰好覆盖了项目里最贵的几个坑：
Report Map 必须逐字节一致、每个 Report 必须挂 0x2908、权限必须是加密。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ios_screen import gatt_table as gt, hid  # noqa: E402


def _chars(spec_uuid: str):
    for s in gt.services():
        if s.uuid == spec_uuid:
            return s.chars
    raise AssertionError(f"没有服务 {spec_uuid}")


class TestServices(unittest.TestCase):
    def test_all_present(self):
        uuids = [s.uuid for s in gt.services()]
        self.assertEqual(uuids, [gt.SERVICE_GAP, gt.SERVICE_HID, gt.SERVICE_DIS, gt.SERVICE_BATTERY])

    def test_all_primary(self):
        for s in gt.services():
            self.assertTrue(s.primary, s.uuid)

    def test_hid_service_uuid(self):
        self.assertEqual(gt.hid_service().uuid, "1812")

    def test_gap_is_not_published(self):
        """GAP 由 CoreBluetooth 自带，应用不该再注册一份。"""
        self.assertNotIn(gt.SERVICE_GAP, [s.uuid for s in gt.published_services()])

    def test_published_services_are_hid_dis_battery(self):
        self.assertEqual(
            [s.uuid for s in gt.published_services()],
            [gt.SERVICE_HID, gt.SERVICE_DIS, gt.SERVICE_BATTERY],
        )


class TestSIGExpansion(unittest.TestCase):
    """服务 UUID 必须展开成 128 位再发布。

    这是 macOS 的硬策略：CBPeripheralManager 的 addService: 拒收所有 16 位 UUID
    （报 CBErrorUUIDNotAllowed「The specified UUID is not allowed for this
    operation.」），跟 UUID 具体是什么无关——0x1812 也一样被拒，换成 128 位
    自定义 UUID 立刻通过。规范里两者等价，所以展开不影响语义。

    这条断言的价值：谁要是把 wire_uuid 改回返回 16 位，蓝牙会一条服务都建不起来，
    而错误信息完全不会指向这里。
    """

    def test_sig128_of_hid(self):
        self.assertEqual(
            gt.sig128("1812"), "00001812-0000-1000-8000-00805F9B34FB"
        )

    def test_sig128_is_case_insensitive(self):
        self.assertEqual(gt.sig128("18a2"), gt.sig128("18A2"))

    def test_sig128_passthrough_for_128_bit(self):
        custom = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
        self.assertEqual(gt.sig128(custom), custom)

    def test_every_published_service_uses_128_bit_wire_uuid(self):
        for s in gt.published_services():
            self.assertEqual(len(s.wire_uuid.replace("-", "")), 32, s.uuid)
            self.assertIn("00805F9B34FB", s.wire_uuid, s.uuid)

    def test_hid_wire_uuid(self):
        self.assertEqual(
            gt.hid_service().wire_uuid, "00001812-0000-1000-8000-00805F9B34FB"
        )

    def test_characteristics_stay_16_bit(self):
        """特征不受这条限制，保持 16 位——读请求里是按 16 位比对的。"""
        for c in gt.hid_service().chars:
            self.assertEqual(len(c.uuid), 4, c.uuid)


class TestHidCharacteristics(unittest.TestCase):
    def setUp(self):
        self.chars = _chars(gt.SERVICE_HID)

    def test_hid_information(self):
        c = next(c for c in self.chars if c.uuid == gt.C_HID_INFO)
        self.assertEqual(c.value, bytes([0x11, 0x01, 0x00, 0x02]))
        self.assertIn(gt.READ, c.props)

    def test_report_map_is_identical_and_encrypted(self):
        c = next(c for c in self.chars if c.uuid == gt.C_REPORT_MAP)
        self.assertEqual(c.value, hid.REPORT_MAP)
        self.assertIn(gt.PERM_READ_ENC, c.perms)
        self.assertNotIn(gt.PERM_READ, c.perms, "明文可读 = iOS 不会把它当输入")

    def test_four_input_reports(self):
        reports = [c for c in self.chars if c.uuid == gt.C_REPORT and c.report_type == 1]
        self.assertEqual([c.report_id for c in reports],
                         [hid.RID_KEYBOARD, hid.RID_ABSOLUTE, hid.RID_RELATIVE, hid.RID_CONSUMER])

    def test_report_lengths(self):
        reports = {c.report_id: c for c in self.chars if c.uuid == gt.C_REPORT and c.report_type == 1}
        self.assertEqual(reports[hid.RID_KEYBOARD].read_length, 8)
        self.assertEqual(reports[hid.RID_ABSOLUTE].read_length, 5)
        self.assertEqual(reports[hid.RID_RELATIVE].read_length, 4)
        self.assertEqual(reports[hid.RID_CONSUMER].read_length, 2)

    def test_every_input_report_is_encrypted_notify(self):
        for c in self.chars:
            if c.uuid == gt.C_REPORT and c.report_type == 1:
                self.assertIn(gt.NOTIFY, c.props, f"R{c.report_id}")
                self.assertIn(gt.PERM_READ_ENC, c.perms, f"R{c.report_id}")

    def test_report_reference_descriptor(self):
        for c in self.chars:
            if c.uuid == gt.C_REPORT:
                self.assertTrue(c.needs_report_reference)
                self.assertIn(c.report_type, (1, 2))

    def test_output_report_is_keyboard_led(self):
        out = [c for c in self.chars if c.uuid == gt.C_REPORT and c.report_type == 2]
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].report_id, hid.RID_KEYBOARD)
        self.assertIn(gt.PERM_WRITE_ENC, out[0].perms)

    def test_control_point_requires_encryption(self):
        c = next(c for c in self.chars if c.uuid == gt.C_CONTROL_POINT)
        self.assertIn(gt.PERM_WRITE_ENC, c.perms)

    def test_data_characteristics_are_encrypted(self):
        """承载键鼠数据的特征必须加密。

        唯一的例外是 Protocol Mode(0x2A4E)：参照实现在配对之前就会写它，
        设成加密反而会让主机在配对前写入失败、一直转圈。它不承载输入数据，
        所以明文可写不构成安全问题。
        """
        for c in self.chars:
            if c.uuid == gt.C_PROTOCOL_MODE:
                continue
            if gt.WRITE in c.props or gt.WRITE_NO_RSP in c.props:
                self.assertIn(gt.PERM_WRITE_ENC, c.perms, f"{c.uuid} {c.report_id}")

    def test_protocol_mode_is_deliberately_plain(self):
        c = next(c for c in self.chars if c.uuid == gt.C_PROTOCOL_MODE)
        self.assertEqual(c.perms, frozenset({gt.PERM_READ, gt.PERM_WRITE}))

    def test_protocol_mode_defaults_to_report(self):
        c = next(c for c in self.chars if c.uuid == gt.C_PROTOCOL_MODE)
        self.assertEqual(c.read_length, 1)
        self.assertIn(gt.WRITE_NO_RSP, c.props)


class TestOtherServices(unittest.TestCase):
    def test_service_changed_can_indicate(self):
        c = _chars(gt.SERVICE_GAP)[0]
        self.assertEqual(c.uuid, gt.C_SERVICE_CHANGED)
        self.assertIn(gt.INDICATE, c.props)

    def test_battery_reports_hundred(self):
        c = _chars(gt.SERVICE_BATTERY)[0]
        self.assertEqual(c.uuid, gt.C_BATTERY_LEVEL)
        self.assertIn(gt.NOTIFY, c.props)

    def test_device_information(self):
        names = [c.uuid for c in _chars(gt.SERVICE_DIS)]
        self.assertIn(gt.C_MANUFACTURER, names)
        self.assertIn(gt.C_PNP_ID, names)


class TestNoShadowedCallbacks(unittest.TestCase):
    """实例属性不能盖住同名方法。

    这不是洁癖：PyObjC 把 Python 异常转成 ObjC 异常，未捕获时整个进程
    被 `terminating due to uncaught exception` 带走，日志里只有一句
    `'NoneType' object is not callable` 和一堆 C 栈，根本看不出是哪一行。
    真实踩过：`self._on_subscribe = None` 盖住了 `_on_subscribe()` 方法，
    iPhone 一订阅就崩。
    """

    def test_instance_attributes_do_not_shadow_methods(self):
        from ios_screen import ble_hid

        obj = ble_hid.HidPeripheral()          # 构造不碰蓝牙，安全
        clashes = [
            name
            for name in vars(obj)
            if callable(getattr(type(obj), name, None))
        ]
        self.assertEqual(clashes, [], f"实例属性盖住了同名方法: {clashes}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
