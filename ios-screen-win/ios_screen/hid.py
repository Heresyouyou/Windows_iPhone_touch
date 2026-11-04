"""HID 描述符与报文 —— 单一真相来源是 windows/src/hid_reports.cpp。

为什么要从 C++ 源码解析而不是手抄一份：
  Report Map 一旦有一个字节不同，iPhone 会把缓存里的 HOGP 数据库对不上，
  表现是"配对成功但屏幕上没有指针"。交接材料里这是排第一的坑。
  手抄 198 个字节必然出错，所以这里直接解析那份已经和上游 Swift 逐字节校验过的源码。
"""
from __future__ import annotations

import re
from pathlib import Path

RID_KEYBOARD = 1
RID_ABSOLUTE = 2
RID_RELATIVE = 3
RID_CONSUMER = 4

ABSOLUTE_MAX = 32767

# 描述符在源码里的先后顺序就是 HID 解析顺序，不可调整
_ARRAY_ORDER = ["kKeyboard", "kAbsolutePointer", "kRelativeMouse", "kConsumer"]

_CPP = (
    Path(__file__).resolve().parent.parent.parent
    / "deskflow-ios"
    / "IOS端"
    / "windows"
    / "src"
    / "hid_reports.cpp"
)


def _parse_arrays(source: str) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for name in _ARRAY_ORDER:
        m = re.search(rf"const\s+uint8_t\s+{name}\s*\[\s*\]\s*=\s*\{{(.*?)\}}\s*;", source, re.S)
        if not m:
            raise RuntimeError(f"在 hid_reports.cpp 里找不到数组 {name}")
        # 源码每个字节后面跟着 // 中文注释，注释里还有逗号，必须先整行去掉再切分
        body = re.sub(r"//[^\n]*", "", m.group(1))
        vals = []
        for tok in body.split(","):
            tok = tok.strip()
            if not tok:
                continue
            # 源码里混着十六进制字面量和 kRidXxx 符号
            if tok.startswith("0x") or tok.startswith("0X"):
                vals.append(int(tok, 16))
            elif tok.isdigit():
                vals.append(int(tok))
            elif tok.startswith("kRid"):
                vals.append(
                    {
                        "kRidKeyboard": RID_KEYBOARD,
                        "kRidAbsolutePointer": RID_ABSOLUTE,
                        "kRidRelativeMouse": RID_RELATIVE,
                        "kRidConsumer": RID_CONSUMER,
                    }[tok]
                )
            else:
                raise RuntimeError(f"{name} 里有无法识别的记号: {tok!r}")
        out[name] = bytes(vals)
    return out


def load_report_map(cpp_path: Path | None = None) -> bytes:
    p = cpp_path or _CPP
    if not p.is_file():
        raise FileNotFoundError(f"找不到 {p}")
    arrays = _parse_arrays(p.read_text(encoding="utf-8", errors="ignore"))
    blob = b"".join(arrays[n] for n in _ARRAY_ORDER)
    if len(blob) != 198:
        raise RuntimeError(f"Report Map 应为 198 字节，实际 {len(blob)} 字节——源码改过了？")
    return blob


def encode_keyboard(modifiers: int, usage: int) -> bytes:
    return bytes([modifiers & 0xFF, 0, usage & 0xFF, 0, 0, 0, 0, 0])


def _to_int8(v: int) -> int:
    v = max(-127, min(127, v))
    return v & 0xFF


def encode_relative_mouse(buttons: int, dx: int, dy: int, wheel: int = 0) -> bytes:
    # 负载里不带 Report ID：ID 由 Report Reference 描述符声明，带了会整体错位一字节
    return bytes([buttons & 0xFF, _to_int8(dx), _to_int8(dy), _to_int8(wheel)])


def encode_absolute_pointer(buttons: int, x: int, y: int) -> bytes:
    x = max(0, min(ABSOLUTE_MAX, x))
    y = max(0, min(ABSOLUTE_MAX, y))
    return bytes([buttons & 0xFF, x & 0xFF, x >> 8, y & 0xFF, y >> 8])


def encode_consumer(usage: int) -> bytes:
    return bytes([usage & 0xFF, (usage >> 8) & 0xFF])


if __name__ == "__main__":
    m = load_report_map()
    print(f"Report Map: {len(m)} 字节")
    print("  ".join(f"{b:02X}" for b in m[:32]), "...")
    for name in _ARRAY_ORDER:
        print(f"  {name}: 见源码")
