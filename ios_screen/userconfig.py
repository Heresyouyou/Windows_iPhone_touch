"""用户个性化配置：只属于本机的设置，不进版本库。

配置文件是项目根目录的 `user_config.json`（已在 .gitignore 中）。没有这个文件
时一切走默认，程序行为与标准版完全一致。

目前支持：

    {
      "key_remap": { "CapsLock": "Esc", "Tab": "Down" }
    }

含义是"捕获期间，把左边的键当成右边的键发给 iPhone"。左边是物理键，右边是
目标键，都用可读名字（不区分大小写）。重映射只在转发给 iPhone 时生效，
Mac 本机键盘不受影响。

键名表在 `keymap.NAME_TO_KEYCODE`，两边共用一份，避免文档和实现对不上。
"""
from __future__ import annotations

import json
import os

from .keymap import NAME_TO_KEYCODE

# 项目根目录下的本地配置文件（与 ios_screen/ 同级）
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "user_config.json"
)


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
    """把配置里的键名翻译成 {源 keycode: 目标 keycode}。

    名字不认识的一律忽略并打印一行提示——静默丢弃会让人以为配置没生效。
    """
    raw = load(path).get("key_remap") or {}
    out: dict[int, int] = {}
    for src, dst in raw.items():
        sv = NAME_TO_KEYCODE.get(str(src).strip().lower())
        dv = NAME_TO_KEYCODE.get(str(dst).strip().lower())
        if sv is None or dv is None:
            print(f"  [config] 忽略未知按键映射 {src!r} → {dst!r}", flush=True)
            continue
        out[sv] = dv
    return out
