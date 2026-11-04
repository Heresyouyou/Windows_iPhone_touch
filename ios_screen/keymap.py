"""macOS 虚拟键码 → HID Usage 映射，以及按键状态机。

和 Windows 版的差别
-------------------
Windows 用 VK_* 虚拟键码，它本身已经按键盘布局映射过一遍；macOS 的
kVK_* 是**物理位置**（keycode），和布局无关。所以这里做的是"物理键位 →
US 布局下的 HID usage"。副作用是：如果你把 Mac 设成 AZERTY，按 A 键
位置发出的仍是 HID 的 A。HID 桥接就该这样——iPhone 那边按它自己的
布局解释，不跟着主机布局漂移。

HID 键盘报文是「状态」而不是「事件」：每条报文描述当前按下的所有键。
所以必须自己维护修饰键位掩码 + 最多 6 个普通键，每次状态变化发一整条。
这也是为什么按住不放要重复发同样的报文。

修饰键左右分开算
----------------
Windows 版把左右 Shift 折成同一个 bit，再用计数避免互相清位。macOS 的
flagsChanged 能明确区分左右（keycode 0x38 / 0x3C），HID 也有独立的 bit，
所以这里直接按位映射，不需要栈。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 常量

KEY_CAPS_LOCK = 0x39
KEY_LEFT_SHIFT, KEY_RIGHT_SHIFT = 0x38, 0x3C
KEY_LEFT_CONTROL, KEY_RIGHT_CONTROL = 0x3B, 0x3E
KEY_LEFT_OPTION, KEY_RIGHT_OPTION = 0x3A, 0x3D
KEY_LEFT_COMMAND, KEY_RIGHT_COMMAND = 0x37, 0x36
KEY_FUNCTION = 0x3F

# 修饰键 keycode → HID 报文第一个字节里的 bit
MODIFIER_BIT: dict[int, int] = {
    KEY_LEFT_CONTROL: 0x01,
    KEY_LEFT_SHIFT: 0x02,
    KEY_LEFT_OPTION: 0x04,
    KEY_LEFT_COMMAND: 0x08,
    KEY_RIGHT_CONTROL: 0x10,
    KEY_RIGHT_SHIFT: 0x20,
    KEY_RIGHT_OPTION: 0x40,
    KEY_RIGHT_COMMAND: 0x80,
}

# macOS keycode → USB HID Usage（Keyboard/Keypad page 0x07）
# 字母 A-Z 是 0x04-0x1D，数字 1-9 是 0x1E-0x26，0 是 0x27
KEYCODE_TO_HID: dict[int, int] = {
    # 字母区（注意 macOS 的排列不是字母序，是物理位置）
    0x00: 0x04,  # A
    0x0B: 0x05,  # B
    0x08: 0x06,  # C
    0x02: 0x07,  # D
    0x0E: 0x08,  # E
    0x03: 0x09,  # F
    0x05: 0x0A,  # G
    0x04: 0x0B,  # H
    0x22: 0x0C,  # I
    0x26: 0x0D,  # J
    0x28: 0x0E,  # K
    0x25: 0x0F,  # L
    0x2E: 0x10,  # M
    0x2D: 0x11,  # N
    0x1F: 0x12,  # O
    0x23: 0x13,  # P
    0x0C: 0x14,  # Q
    0x0F: 0x15,  # R
    0x01: 0x16,  # S
    0x11: 0x17,  # T
    0x20: 0x18,  # U
    0x09: 0x19,  # V
    0x0D: 0x1A,  # W
    0x07: 0x1B,  # X
    0x10: 0x1C,  # Y
    0x06: 0x1D,  # Z
    # 数字行
    0x12: 0x1E,  # 1
    0x13: 0x1F,  # 2
    0x14: 0x20,  # 3
    0x15: 0x21,  # 4
    0x17: 0x22,  # 5
    0x16: 0x23,  # 6
    0x1A: 0x24,  # 7
    0x1C: 0x25,  # 8
    0x19: 0x26,  # 9
    0x1D: 0x27,  # 0
    # 符号
    0x18: 0x2E,  # =
    0x1B: 0x2D,  # -
    0x1E: 0x30,  # ]
    0x21: 0x2F,  # [
    0x27: 0x34,  # '
    0x29: 0x33,  # ;
    0x2A: 0x31,  # backslash
    0x2B: 0x36,  # ,
    0x2C: 0x38,  # /
    0x2F: 0x37,  # .
    0x32: 0x35,  # `
    # 编辑键
    0x24: 0x28,  # Return
    0x35: 0x29,  # Escape
    0x33: 0x2A,  # Delete (Backspace)
    0x30: 0x2B,  # Tab
    0x31: 0x2C,  # Space
    0x39: 0x39,  # CapsLock（作为普通键发，见 Controller 里的特殊处理）
    0x75: 0x4C,  # Forward Delete
    0x73: 0x4A,  # Home
    0x77: 0x4D,  # End
    0x74: 0x4B,  # PageUp
    0x79: 0x4E,  # PageDown
    0x72: 0x49,  # Help / Insert
    0x7B: 0x50,  # Left
    0x7C: 0x4F,  # Right
    0x7D: 0x51,  # Down
    0x7E: 0x52,  # Up
    # 小键盘
    0x41: 0x63,  # Keypad .
    0x43: 0x55,  # Keypad *
    0x45: 0x57,  # Keypad +
    0x47: 0x53,  # Keypad Clear
    0x4B: 0x54,  # Keypad /
    0x4C: 0x58,  # Keypad Enter
    0x4E: 0x56,  # Keypad -
    0x51: 0x67,  # Keypad =
    0x52: 0x62,  # Keypad 0
    0x53: 0x59,  # Keypad 1
    0x54: 0x5A,  # Keypad 2
    0x55: 0x5B,  # Keypad 3
    0x56: 0x5C,  # Keypad 4
    0x57: 0x5D,  # Keypad 5
    0x58: 0x5E,  # Keypad 6
    0x59: 0x5F,  # Keypad 7
    0x5B: 0x60,  # Keypad 8
    0x5C: 0x61,  # Keypad 9
    # 功能键（macOS 的 F 键 keycode 同样不连续）
    0x7A: 0x3A,  # F1
    0x78: 0x3B,  # F2
    0x63: 0x3C,  # F3
    0x76: 0x3D,  # F4
    0x60: 0x3E,  # F5
    0x61: 0x3F,  # F6
    0x62: 0x40,  # F7
    0x64: 0x41,  # F8
    0x65: 0x42,  # F9
    0x6D: 0x43,  # F10
    0x67: 0x44,  # F11
    0x6F: 0x45,  # F12
    0x69: 0x68,  # F13
    0x6B: 0x69,  # F14
    0x71: 0x6A,  # F15
    0x6A: 0x6B,  # F16
    0x40: 0x6C,  # F17
    0x4F: 0x6D,  # F18
    0x50: 0x6E,  # F19
    0x5A: 0x6F,  # F20
}

# user_config.json 里能用的可读键名（不区分大小写）
NAME_TO_KEYCODE: dict[str, int] = {
    "tab": 0x30,
    "capslock": KEY_CAPS_LOCK,
    "caps": KEY_CAPS_LOCK,
    "esc": 0x35,
    "escape": 0x35,
    "enter": 0x24,
    "return": 0x24,
    "space": 0x31,
    "backspace": 0x33,
    "delete": 0x75,
    "forwarddelete": 0x75,
    "up": 0x7E,
    "down": 0x7D,
    "left": 0x7B,
    "right": 0x7C,
    "home": 0x73,
    "end": 0x77,
    "pageup": 0x74,
    "pagedown": 0x79,
    "insert": 0x72,
    "help": 0x72,
    "shift": KEY_LEFT_SHIFT,
    "lshift": KEY_LEFT_SHIFT,
    "rshift": KEY_RIGHT_SHIFT,
    "ctrl": KEY_LEFT_CONTROL,
    "control": KEY_LEFT_CONTROL,
    "lctrl": KEY_LEFT_CONTROL,
    "rctrl": KEY_RIGHT_CONTROL,
    "alt": KEY_LEFT_OPTION,
    "option": KEY_LEFT_OPTION,
    "lalt": KEY_LEFT_OPTION,
    "ralt": KEY_RIGHT_OPTION,
    "cmd": KEY_LEFT_COMMAND,
    "command": KEY_LEFT_COMMAND,
    "win": KEY_LEFT_COMMAND,      # 沿用 Windows 版的写法，指同一个键
    "menu": KEY_LEFT_COMMAND,
}
for _name, _code in {
    "a": 0x00, "b": 0x0B, "c": 0x08, "d": 0x02, "e": 0x0E, "f": 0x03, "g": 0x05,
    "h": 0x04, "i": 0x22, "j": 0x26, "k": 0x28, "l": 0x25, "m": 0x2E, "n": 0x2D,
    "o": 0x1F, "p": 0x23, "q": 0x0C, "r": 0x0F, "s": 0x01, "t": 0x11, "u": 0x20,
    "v": 0x09, "w": 0x0D, "x": 0x07, "y": 0x10, "z": 0x06,
    "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "5": 0x17,
    "6": 0x16, "7": 0x1A, "8": 0x1C, "9": 0x19, "0": 0x1D,
}.items():
    NAME_TO_KEYCODE[_name] = _code
for _i, _code in enumerate([0x7A, 0x78, 0x63, 0x76, 0x60, 0x61, 0x62, 0x64, 0x65, 0x6D, 0x67, 0x6F], 1):
    NAME_TO_KEYCODE[f"f{_i}"] = _code


class KeyboardState:
    """维护当前按键集合，产出 8 字节 HID 键盘报文。"""

    def __init__(self) -> None:
        self.modifiers = 0
        self.keys: list[int] = []

    def press(self, keycode: int) -> bool:
        bit = MODIFIER_BIT.get(keycode)
        if bit is not None:
            if self.modifiers & bit:
                return False
            self.modifiers |= bit
            return True
        hid = KEYCODE_TO_HID.get(keycode)
        if hid is None:
            return False
        if hid in self.keys:
            return False          # 长按重复，不重复添加
        if len(self.keys) >= 6:
            return False          # 6KRO 上限，超出丢弃（HID 规范行为）
        self.keys.append(hid)
        return True

    def release(self, keycode: int) -> bool:
        bit = MODIFIER_BIT.get(keycode)
        if bit is not None:
            if not (self.modifiers & bit):
                return False
            self.modifiers &= ~bit
            return True
        hid = KEYCODE_TO_HID.get(keycode)
        if hid is None or hid not in self.keys:
            return False
        self.keys.remove(hid)
        return True

    def clear(self) -> bool:
        changed = self.modifiers != 0 or bool(self.keys)
        self.modifiers = 0
        self.keys.clear()
        return changed

    def report(self) -> bytes:
        keys = list(self.keys[:6]) + [0] * (6 - min(6, len(self.keys)))
        return bytes([self.modifiers & 0xFF, 0] + keys)
