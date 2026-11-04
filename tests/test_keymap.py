"""键码映射与按键状态机单测。

HID 键盘报文是「状态」不是「事件」，这层出错的表现是"组合键卡住"或
"按住不放变成连击"，只有单测能稳定复现。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ios_screen.keymap import (  # noqa: E402
    KEYCODE_TO_HID,
    KEY_LEFT_COMMAND,
    KEY_LEFT_CONTROL,
    KEY_LEFT_OPTION,
    KEY_LEFT_SHIFT,
    KEY_RIGHT_SHIFT,
    NAME_TO_KEYCODE,
    KeyboardState,
    MODIFIER_BIT,
)


class TestKeycodeTable(unittest.TestCase):
    def test_letters_follow_physical_positions(self):
        # macOS 的 A/S/D/F 是 0x00/0x01/0x02/0x03，不是字母序
        self.assertEqual(KEYCODE_TO_HID[0x00], 0x04)   # A
        self.assertEqual(KEYCODE_TO_HID[0x01], 0x16)   # S
        self.assertEqual(KEYCODE_TO_HID[0x0C], 0x14)   # Q
        self.assertEqual(KEYCODE_TO_HID[0x06], 0x1D)   # Z

    def test_digits(self):
        self.assertEqual(KEYCODE_TO_HID[0x12], 0x1E)   # 1
        self.assertEqual(KEYCODE_TO_HID[0x1D], 0x27)   # 0

    def test_editing_keys(self):
        self.assertEqual(KEYCODE_TO_HID[0x24], 0x28)   # Return
        self.assertEqual(KEYCODE_TO_HID[0x35], 0x29)   # Escape
        self.assertEqual(KEYCODE_TO_HID[0x33], 0x2A)   # Backspace
        self.assertEqual(KEYCODE_TO_HID[0x30], 0x2B)   # Tab

    def test_arrows_are_not_swapped(self):
        self.assertEqual(KEYCODE_TO_HID[0x7B], 0x50)   # Left
        self.assertEqual(KEYCODE_TO_HID[0x7C], 0x4F)   # Right
        self.assertEqual(KEYCODE_TO_HID[0x7D], 0x51)   # Down
        self.assertEqual(KEYCODE_TO_HID[0x7E], 0x52)   # Up

    def test_function_keys(self):
        for i, code in enumerate([0x7A, 0x78, 0x63, 0x76, 0x60, 0x61], 1):
            self.assertEqual(KEYCODE_TO_HID[code], 0x39 + i, f"F{i}")

    def test_no_duplicate_hid_usages(self):
        """两个不同的物理键不能映射到同一个 usage，否则按 A 出 B。"""
        seen = {}
        for code, usage in KEYCODE_TO_HID.items():
            self.assertNotIn(usage, seen, f"{code:#x} 与 {seen.get(usage)} 撞车")
            seen[usage] = code

    def test_modifiers_are_separate_bits(self):
        self.assertEqual(MODIFIER_BIT[KEY_LEFT_CONTROL], 0x01)
        self.assertEqual(MODIFIER_BIT[KEY_LEFT_SHIFT], 0x02)
        self.assertEqual(MODIFIER_BIT[KEY_LEFT_OPTION], 0x04)
        self.assertEqual(MODIFIER_BIT[KEY_LEFT_COMMAND], 0x08)
        self.assertEqual(MODIFIER_BIT[KEY_RIGHT_SHIFT], 0x20)

    def test_modifier_codes_not_in_hid_table(self):
        for code in MODIFIER_BIT:
            self.assertNotIn(code, KEYCODE_TO_HID)


class TestKeyboardState(unittest.TestCase):
    def setUp(self):
        self.kb = KeyboardState()

    def test_press_and_release(self):
        self.assertTrue(self.kb.press(0x00))          # A
        self.assertEqual(self.kb.report()[2], 0x04)
        self.assertTrue(self.kb.release(0x00))
        self.assertEqual(self.kb.report(), bytes(8))

    def test_duplicate_press_is_ignored(self):
        self.kb.press(0x00)
        self.assertFalse(self.kb.press(0x00))         # 长按重复
        self.assertEqual(self.kb.report()[2], 0x04)

    def test_six_key_rollover(self):
        for code in [0x00, 0x0B, 0x08, 0x02, 0x0E, 0x03]:
            self.assertTrue(self.kb.press(code))
        self.assertFalse(self.kb.press(0x05))         # 第 7 个被丢弃
        self.assertEqual(len(self.kb.keys), 6)

    def test_shift_bits_are_independent(self):
        """左右 Shift 各占一位，松开左边不能把右边清掉。"""
        self.kb.press(KEY_LEFT_SHIFT)
        self.kb.press(KEY_RIGHT_SHIFT)
        self.assertEqual(self.kb.report()[0], 0x22)
        self.kb.release(KEY_LEFT_SHIFT)
        self.assertEqual(self.kb.report()[0], 0x20)
        self.kb.release(KEY_RIGHT_SHIFT)
        self.assertEqual(self.kb.report()[0], 0x00)

    def test_command_plus_key(self):
        self.kb.press(KEY_LEFT_COMMAND)
        self.kb.press(0x08)                            # C
        self.assertEqual(self.kb.report(), bytes([0x08, 0, 0x06, 0, 0, 0, 0, 0]))

    def test_clear_releases_everything(self):
        self.kb.press(KEY_LEFT_COMMAND)
        self.kb.press(0x00)
        self.assertTrue(self.kb.clear())
        self.assertEqual(self.kb.report(), bytes(8))
        self.assertFalse(self.kb.clear())

    def test_report_is_always_eight_bytes(self):
        self.assertEqual(len(self.kb.report()), 8)
        for code in [0x00, 0x0B, 0x08]:
            self.kb.press(code)
        self.assertEqual(len(self.kb.report()), 8)

    def test_release_unknown_key_is_noop(self):
        self.assertFalse(self.kb.release(0x00))
        self.assertFalse(self.kb.release(KEY_LEFT_SHIFT))


class TestNameTable(unittest.TestCase):
    def test_common_names(self):
        for name in ("tab", "esc", "enter", "space", "backspace", "capslock",
                     "up", "down", "left", "right", "home", "end",
                     "pageup", "pagedown", "shift", "ctrl", "alt", "cmd"):
            self.assertIn(name, NAME_TO_KEYCODE, name)

    def test_letters_and_digits(self):
        self.assertEqual(NAME_TO_KEYCODE["a"], 0x00)
        self.assertEqual(NAME_TO_KEYCODE["z"], 0x06)
        self.assertEqual(NAME_TO_KEYCODE["0"], 0x1D)
        self.assertEqual(NAME_TO_KEYCODE["9"], 0x19)

    def test_function_keys(self):
        for i in range(1, 13):
            self.assertIn(f"f{i}", NAME_TO_KEYCODE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
