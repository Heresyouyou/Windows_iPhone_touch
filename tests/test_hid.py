"""报文层单测：描述符与编码。

描述符是"改一个字就要重新配对"的东西，所以这里钉死指纹而不是只测长度。
"""
from __future__ import annotations

import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ios_screen import hid  # noqa: E402

# 与上游 iPhone Use 的 HIDReports.swift 逐字节一致的指纹。
# 这个值变了意味着 iPhone 上很可能"配对成功但没有指针"，必须重新配对验证。
REPORT_MAP_SHA256 = "b74d9ebbd9e79d2aacfb0a0cb3c5b86a3eb46c8b3a29964980b56bb50e1d80e3"


class TestReportMap(unittest.TestCase):
    def test_length(self):
        self.assertEqual(len(hid.REPORT_MAP), 198)

    def test_fingerprint(self):
        self.assertEqual(hashlib.sha256(hid.REPORT_MAP).hexdigest(), REPORT_MAP_SHA256)

    def test_starts_with_keyboard_collection(self):
        # 0x05 0x01 0x09 0x06 0xA1 0x01 0x85 0x01 = Generic Desktop / Keyboard / App / ReportID 1
        self.assertEqual(bytes(hid.REPORT_MAP[:8]), bytes([0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x01]))

    def test_four_report_ids_present(self):
        # 每个输入集合都以 0x85 <id> 声明 Report ID
        ids = [hid.REPORT_MAP[i + 1] for i in range(len(hid.REPORT_MAP) - 1) if hid.REPORT_MAP[i] == 0x85]
        self.assertEqual(ids, [1, 2, 3, 4])


class TestEncoders(unittest.TestCase):
    def test_keyboard(self):
        self.assertEqual(hid.encode_keyboard(0x02, 0x04), bytes([0x02, 0, 0x04, 0, 0, 0, 0, 0]))
        self.assertEqual(len(hid.encode_keyboard(0, 0)), 8)

    def test_relative_is_four_bytes_signed(self):
        self.assertEqual(hid.encode_relative_mouse(0x01, 5, -3), bytes([0x01, 5, 0xFD, 0]))
        self.assertEqual(len(hid.encode_relative_mouse(0, 0, 0, 1)), 4)
        self.assertEqual(hid.encode_relative_mouse(0, 0, 0, -1)[3], 0xFF)

    def test_relative_clamps_int8(self):
        self.assertEqual(hid.encode_relative_mouse(0, 999, -999)[1], 127)
        self.assertEqual(hid.encode_relative_mouse(0, 999, -999)[2], 0x81)

    def test_absolute_is_little_endian_u16(self):
        self.assertEqual(hid.encode_absolute_pointer(0, 0x1234, 0x00FF), bytes([0, 0x34, 0x12, 0xFF, 0x00]))
        self.assertEqual(len(hid.encode_absolute_pointer(0, 0, 0)), 5)

    def test_absolute_clamps(self):
        self.assertEqual(hid.encode_absolute_pointer(0, -5, 99999), bytes([0, 0, 0, 0xFF, 0x7F]))
        self.assertEqual(hid.ABSOLUTE_MAX, 32767)

    def test_consumer(self):
        self.assertEqual(hid.encode_consumer(0x0223), bytes([0x23, 0x02]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
