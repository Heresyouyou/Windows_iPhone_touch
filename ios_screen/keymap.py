"""Windows 虚拟键码 → HID Usage 映射，以及按键状态机。

HID 键盘报文是「状态」而不是「事件」：每条报文描述当前按下的所有键，
而不是"刚按了哪个键"。所以必须自己维护修饰键位掩码 + 最多 6 个普通键，
每次状态变化发一整条报文。这也是为什么按住不放要重复发同样的报文。
"""
from __future__ import annotations

# USB HID Usage ID (Keyboard/Keypad page 0x07)
# 字母 a-z 是 0x04-0x1D，数字 1-9 是 0x1E-0x26，0 是 0x27，其余按规范
_VK_LETTER_BASE = 0x41   # 'A'
_HID_LETTER_BASE = 0x04
_VK_DIGIT_BASE = 0x31    # '1'
_VK_DIGIT0 = 0x30
_HID_DIGIT_BASE = 0x1E
_HID_DIGIT0 = 0x27

VK_TO_HID: dict[int, int] = {}

for i in range(26):
    VK_TO_HID[_VK_LETTER_BASE + i] = _HID_LETTER_BASE + i
for i in range(9):  # '1'..'9'
    VK_TO_HID[_VK_DIGIT_BASE + i] = _HID_DIGIT_BASE + i
VK_TO_HID[_VK_DIGIT0] = _HID_DIGIT0

VK_TO_HID.update({
    0x0D: 0x28,  # Enter
    0x1B: 0x29,  # Esc
    0x08: 0x2A,  # Backspace
    0x09: 0x2B,  # Tab
    0x20: 0x2C,  # Space
    0xBD: 0x2D,  # -
    0xBB: 0x2E,  # =
    0xDB: 0x2F,  # [
    0xDD: 0x30,  # ]
    0xDC: 0x31,  # backslash
    0xBA: 0x33,  # ;
    0xDE: 0x34,  # '
    0xC0: 0x35,  # `
    0xBC: 0x36,  # ,
    0xBE: 0x37,  # .
    0xBF: 0x38,  # /
    0x14: 0x39,  # CapsLock
    0x2D: 0x49,  # Insert
    0x24: 0x4A,  # Home
    0x21: 0x4B,  # PageUp
    0x2E: 0x4C,  # Delete
    0x23: 0x4D,  # End
    0x22: 0x4E,  # PageDown
    0x27: 0x4F,  # Right
    0x25: 0x50,  # Left
    0x28: 0x51,  # Down
    0x26: 0x52,  # Up
    0x90: 0x53,  # NumLock
    0x6F: 0x54,  # Numpad /
    0x6A: 0x55,  # Numpad *
    0x6D: 0x56,  # Numpad -
    0x6B: 0x57,  # Numpad +
    0x0D: 0x28,
    0x5D: 0x65,  # Menu (上下文菜单键)
})

# F1-F12: VK 0x70..0x7B → HID 0x3A..0x45
for i in range(12):
    VK_TO_HID[0x70 + i] = 0x3A + i

# 修饰键 → HID 报文的第一个字节（bit 位）
MODIFIER_BITS: dict[int, int] = {
    0x11: 0x01,  # Ctrl
    0xA2: 0x01,  # LCtrl
    0x10: 0x02,  # Shift
    0xA0: 0x02,  # LShift
    0x12: 0x04,  # Alt
    0xA4: 0x04,  # LAlt
    0x5B: 0x08,  # LWin
    0xA3: 0x10,  # RCtrl
    0xA1: 0x20,  # RShift
    0xA5: 0x40,  # RAlt
    0x5C: 0x80,  # RWin
}


class KeyboardState:
    """维护当前按键集合，产出 8 字节 HID 键盘报文。"""

    def __init__(self) -> None:
        self.modifiers = 0
        self.keys: list[int] = []
        self._mod_stack: dict[int, int] = {}

    def press(self, vk: int) -> bool:
        """返回 True 表示状态变了，需要发报文。"""
        if vk in MODIFIER_BITS:
            bit = MODIFIER_BITS[vk]
            # 左右 Shift 都映射到同一个 bit，用计数避免"松开左 Shift 把右 Shift 也清了"
            prev = self.modifiers
            self._mod_stack[vk] = self._mod_stack.get(vk, 0) + 1
            self.modifiers |= bit
            return self.modifiers != prev
        hid = VK_TO_HID.get(vk)
        if hid is None:
            return False
        if hid in self.keys:
            return False  # 长按重复，不重复添加
        if len(self.keys) >= 6:
            return False  # 6KRO 上限，超出丢弃（HID 规范行为）
        self.keys.append(hid)
        return True

    def release(self, vk: int) -> bool:
        if vk in MODIFIER_BITS:
            bit = MODIFIER_BITS[vk]
            n = self._mod_stack.get(vk, 0) - 1
            if n <= 0:
                self._mod_stack.pop(vk, None)
            else:
                self._mod_stack[vk] = n
            # 只有该 bit 上所有来源的键都松开了，才清位
            still = any(
                MODIFIER_BITS.get(k) == bit and v > 0 for k, v in self._mod_stack.items()
            )
            if still:
                return False
            if self.modifiers & bit:
                self.modifiers &= ~bit
                return True
            return False
        hid = VK_TO_HID.get(vk)
        if hid is None or hid not in self.keys:
            return False
        self.keys.remove(hid)
        return True

    def clear(self) -> bool:
        changed = self.modifiers != 0 or bool(self.keys)
        self.modifiers = 0
        self.keys.clear()
        self._mod_stack.clear()
        return changed

    def report(self) -> bytes:
        keys = list(self.keys[:6]) + [0] * (6 - min(6, len(self.keys)))
        return bytes([self.modifiers & 0xFF, 0] + keys)
