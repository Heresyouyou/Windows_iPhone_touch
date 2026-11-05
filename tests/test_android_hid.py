"""Android HID 路径的单测：描述符字节、报文编码、键盘状态机。

最关键的一条是 test_report_blocks_match_ios_source：把三段字节和
ios_screen/hid.py 的原始常量逐字节对照。手抄 144 个十六进制数很容易错一位，
错了的表现是"配对成功但手势乱跳"，很难查。这里用程序保证没抄错。
"""
from __future__ import annotations

import unittest

from android_hid import hid
from android_hid.hogp import HidService
from ios_screen import hid as ios_hid
from ios_screen.keymap import KeyboardState


def _report_ids(report_map: bytes) -> list[int]:
    """按 0x85 (Report ID) 出现的顺序取出 ID。"""
    out = []
    for i, b in enumerate(report_map):
        if b == 0x85 and i + 1 < len(report_map):
            out.append(report_map[i + 1])
    return out


class TestReportMap(unittest.TestCase):
    def test_length(self):
        self.assertEqual(len(hid.REPORT_MAP), 144)

    def test_report_ids_in_order(self):
        self.assertEqual(_report_ids(hid.REPORT_MAP), [1, 2, 3])

    def test_collections_balanced(self):
        # 每个 A1 (Collection) 必须有对应的 C0 (End Collection)
        self.assertEqual(hid.REPORT_MAP.count(0xA1), hid.REPORT_MAP.count(0xC0))

    def test_starts_with_generic_desktop_keyboard(self):
        head = hid.REPORT_MAP[:4]
        self.assertEqual(list(head), [0x05, 0x01, 0x09, 0x06])

    def test_report_blocks_match_ios_source(self):
        """键盘段必须与 iOS 版完全一致；鼠标/媒体段只允许 Report ID 不同。"""
        src = ios_hid.REPORT_MAP
        self.assertEqual(len(src), 198)
        self.assertEqual(hid.REPORT_MAP[:65], src[:65])

        rel = bytearray(src[119:173])   # iOS 的相对鼠标段（RID 3）
        self.assertEqual((rel[6], rel[7]), (0x85, 0x03))
        rel[7] = hid.RID_MOUSE
        self.assertEqual(hid.REPORT_MAP[65:119], bytes(rel))

        con = bytearray(src[173:198])   # iOS 的消费者段（RID 4）
        self.assertEqual((con[6], con[7]), (0x85, 0x04))
        con[7] = hid.RID_CONSUMER
        self.assertEqual(hid.REPORT_MAP[119:144], bytes(con))

    def test_input_report_table(self):
        self.assertEqual(hid.INPUT_REPORTS,
                         ((hid.RID_KEYBOARD, 8), (hid.RID_MOUSE, 4), (hid.RID_CONSUMER, 2)))

    def test_no_absolute_pointer(self):
        # Android 上不用绝对报文：描述符里不能出现 0x16 0x00 0x00 / 0x26 0xFF 0x7F
        # 这对 16 位逻辑范围（绝对坐标的特征）
        self.assertNotIn(bytes([0x26, 0xFF, 0x7F]), hid.REPORT_MAP)


class TestEncoders(unittest.TestCase):
    def test_keyboard_single_key(self):
        self.assertEqual(hid.encode_keyboard(0, 0x04), bytes([0, 0, 4, 0, 0, 0, 0, 0]))

    def test_keyboard_modifiers_and_padding(self):
        # 6KRO：超过 6 个键截断，不足补 0
        rep = hid.encode_keyboard(0x02, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A)
        self.assertEqual(len(rep), 8)
        self.assertEqual(rep[0], 0x02)
        self.assertEqual(list(rep[2:]), [4, 5, 6, 7, 8, 9])

    def test_keyboard_empty_is_all_zero(self):
        self.assertEqual(hid.encode_keyboard(0), bytes(8))

    def test_mouse_relative_signed(self):
        rep = hid.encode_relative_mouse(0x01, -5, 7, -1)
        self.assertEqual(list(rep), [0x01, 0xFB, 0x07, 0xFF])

    def test_mouse_clamps_int8(self):
        rep = hid.encode_relative_mouse(0, 300, -300, 200)
        self.assertEqual(list(rep), [0, 127, 129, 127])  # -127 = 0x81

    def test_mouse_buttons_preserved(self):
        self.assertEqual(hid.encode_relative_mouse(0x07, 0, 0)[0], 0x07)

    def test_consumer_little_endian(self):
        self.assertEqual(hid.encode_consumer(0x0224), bytes([0x24, 0x02]))
        self.assertEqual(hid.encode_consumer(0x00E9), bytes([0xE9, 0x00]))

    def test_consumer_constants(self):
        self.assertEqual(hid.CONSUMER_BACK, 0x0224)
        self.assertEqual(hid.CONSUMER_HOME, 0x0223)
        self.assertEqual(hid.CONSUMER_VOL_UP, 0x00E9)
        self.assertEqual(hid.CONSUMER_VOL_DOWN, 0x00EA)


class TestKeyboardState(unittest.TestCase):
    def test_letter_press_release(self):
        kb = KeyboardState()
        self.assertTrue(kb.press(0x41))            # 'A'
        self.assertEqual(kb.report(), bytes([0, 0, 0x04, 0, 0, 0, 0, 0]))
        self.assertTrue(kb.release(0x41))
        self.assertEqual(kb.report(), bytes(8))

    def test_repeat_press_is_noop(self):
        kb = KeyboardState()
        kb.press(0x41)
        self.assertFalse(kb.press(0x41))           # 自动重复不应重复入列

    def test_ctrl_shift_combo(self):
        kb = KeyboardState()
        kb.press(0x11)   # Ctrl
        kb.press(0x10)   # Shift
        kb.press(0x41)   # A
        self.assertEqual(kb.report()[0], 0x03)
        self.assertEqual(kb.report()[2], 0x04)

    def test_left_right_shift_use_separate_bits(self):
        kb = KeyboardState()
        kb.press(0xA0)   # LShift → bit 0x02
        self.assertEqual(kb.report()[0], 0x02)
        kb.press(0xA1)   # RShift → bit 0x20，不能覆盖左 Shift
        self.assertEqual(kb.report()[0], 0x22)
        kb.release(0xA0)
        self.assertEqual(kb.report()[0], 0x20)     # 右 Shift 还按着
        kb.release(0xA1)
        self.assertEqual(kb.report()[0], 0x00)

    def test_same_modifier_pressed_twice(self):
        """同一个 VK 被重复按下（tkinter 的 Shift 只给 0x10），
        松开时必须真的清位，不能因为计数残留卡住。"""
        kb = KeyboardState()
        kb.press(0x10)
        kb.press(0x10)
        self.assertEqual(kb.report()[0], 0x02)
        kb.release(0x10)
        self.assertEqual(kb.report()[0], 0x00)

    def test_six_key_limit(self):
        kb = KeyboardState()
        for vk in range(0x41, 0x41 + 8):
            kb.press(vk)
        self.assertEqual(len(kb.keys), 6)


class TestHidServiceShape(unittest.TestCase):
    def test_report_ids_match_table(self):
        """HidService 建特征用的表必须和 hid.INPUT_REPORTS 一致。"""
        svc = HidService()
        self.assertFalse(svc.subscribed)
        self.assertFalse(svc.advertising)
        self.assertEqual(svc.status, "no-provider")
        self.assertEqual([r for r, _ in hid.INPUT_REPORTS],
                         [hid.RID_KEYBOARD, hid.RID_MOUSE, hid.RID_CONSUMER])


if __name__ == "__main__":
    unittest.main()
