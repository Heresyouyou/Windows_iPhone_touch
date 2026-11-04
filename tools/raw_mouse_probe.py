"""探针：原始鼠标位移能不能收到。

用法
----
  # 在 .app 里跑（推荐，和主程序同一权限身份）
  ./make_app.sh
  ./run.sh --module=tools.raw_mouse_probe --new
  cat ~/Library/Logs/MacPhoneTouch.log

读法
----
每秒打印一次累积位移。**期间请随手晃一下鼠标**：

  · 数字在动 → 原始位移可用，主程序可以靠它绕开屏幕边缘钳制；
  · 一直是 0 且 status 提示缺权限 → 去「系统设置 → 隐私与安全性 → 输入监控」
    勾选 dist/Mac iPhone Touch.app，然后重启程序。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode  # noqa: E402

from ios_screen.raw_mouse import RawMouse  # noqa: E402


def pump(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.02, True)


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    print("原始鼠标探针 —— 请晃动鼠标", flush=True)
    rm = RawMouse()
    ok = rm.start()
    print(f"  启动: {'成功' if ok else '失败'}", flush=True)
    print(f"  状态: {rm.status}", flush=True)
    print(f"  输入监控: {rm.access_name}  匹配到设备: {rm.device_count} 个", flush=True)
    if not ok:
        print(f"  错误: {rm.last_error}", flush=True)
        return 2

    total = [0, 0]
    for i in range(1, 9):
        pump(1.0)
        dx, dy = rm.take()
        total[0] += dx
        total[1] += dy
        print(
            f"  [{i}s] 本秒 dx={dx:6d} dy={dy:6d}   累计 ({total[0]},{total[1]})   "
            f"样本={rm.samples} 回调={rm.callbacks}",
            flush=True,
        )

    rm.stop()
    print()
    print(f"  回调总次数: {rm.callbacks}")
    print(f"  见过的元素 (page,usage): {sorted(rm.seen)[:20]}")
    print()
    if total[0] or total[1]:
        print("结论: 原始位移已拿到，主程序可以据此计算指针位置。")
        return 0
    print("结论: 一个位移都没收到。")
    print("      九成是缺「输入监控」权限：系统设置 → 隐私与安全性 → 输入监控，")
    print("      勾选 dist/Mac iPhone Touch.app（不是终端），然后重启本程序。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
