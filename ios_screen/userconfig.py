"""用户个性化配置：只属于本机的设置，不进版本库。

配置文件是项目根目录的 `user_config.json`（已在 .gitignore 中）。没有这个文件
时一切走默认，程序行为与标准版完全一致。

目前支持：

    {
      "key_remap": { "Tab": "Up", "CapsLock": "Down" }
    }

含义是"捕获期间，把左边的键当成右边的键发给 iPhone"。左边是物理键，右边是
目标键，都用可读名字（不区分大小写）。重映射只在转发给 iPhone 时生效，
Windows 本地键盘不受影响。
"""
from __future__ import annotations

import json
import os

# 项目根目录下的本地配置文件（与 ios_screen/ 同级）
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "user_config.json"
)

# 常用键名 → Windows 虚拟键码
_NAME_TO_VK: dict[str, int] = {
    "tab": 0x09,
    "capslock": 0x14,
    "esc": 0x1B,
    "escape": 0x1B,
    "enter": 0x0D,
    "return": 0x0D,
    "space": 0x20,
    "backspace": 0x08,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "insert": 0x2D,
    "delete": 0x2E,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "win": 0x5B,
    "menu": 0x5D,
}
for _c in "abcdefghijklmnopqrstuvwxyz":
    _NAME_TO_VK[_c] = ord(_c.upper())
for _d in "0123456789":
    _NAME_TO_VK[_d] = ord(_d)
for _i in range(1, 13):
    _NAME_TO_VK[f"f{_i}"] = 0x6F + _i


def load(path: str | None = None) -> dict:
    """读取配置。文件不存在或格式不对时返回空字典，绝不因为配置问题起不来。"""
    p = path or CONFIG_PATH
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def key_remap(path: str | None = None) -> dict[int, int]:
    """把配置里的按键重映射翻译成 {源虚拟键码: 目标虚拟键码}。

    名字不认识的一律忽略并打印一行提示——静默丢弃会让人以为配置没生效。
    """
    raw = load(path).get("key_remap") or {}
    out: dict[int, int] = {}
    for src, dst in raw.items():
        sv = _NAME_TO_VK.get(str(src).strip().lower())
        dv = _NAME_TO_VK.get(str(dst).strip().lower())
        if sv is None or dv is None:
            print(f"  [config] 忽略未知按键映射 {src!r} → {dst!r}", flush=True)
            continue
        out[sv] = dv
    return out
