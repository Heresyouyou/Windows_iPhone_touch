"""探针：.app 进程里到底能不能挪光标 / 解耦光标。

为什么需要单独测
----------------
有两条路径会让「程序以为做了，系统其实没做」：

  · `CGEventTapCreate` 在权限不足时可能给回一个 tap，但实际按只监听工作；
  · `CGWarpMouseCursorPosition` / `CGAssociateMouseAndMouseCursorPosition`
    失败时**不抛异常、不返回错误**，就是悄悄不生效。

这两件事在外部读日志看不出来，只能在同一进程里做一次「调用 → 回读」。
判据很简单：warp 之后立刻读光标位置，动没动一目了然。

跑法
----
  ./run.sh --module=tools.cursor_probe --new
  cat ~/Library/Logs/MacPhoneTouch.log
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ios_screen import mac_input  # noqa: E402


def _check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✓' if ok else '✗'} {name}{'  ' + detail if detail else ''}", flush=True)
    return ok


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    print("光标探针（在 .app 进程里跑）", flush=True)
    print(f"  责任应用: {os.environ.get('__CFBundleIdentifier')}", flush=True)
    print(f"  辅助功能可信: {mac_input.accessibility_trusted()}", flush=True)

    start = mac_input.cursor_pos()
    print(f"  起始位置: ({start[0]:.0f},{start[1]:.0f})", flush=True)

    ok = True

    # ---------- 1. 能不能挪光标 ----------
    print("\n[1] CGWarpMouseCursorPosition", flush=True)
    target = (300.0, 300.0)
    mac_input.warp_cursor(*target)
    time.sleep(0.15)
    pos = mac_input.cursor_pos()
    moved = abs(pos[0] - target[0]) < 4 and abs(pos[1] - target[1]) < 4
    ok &= _check("warp 到 (300,300)", moved, f"实际 ({pos[0]:.0f},{pos[1]:.0f})")

    # ---------- 2. 能不能解耦 ----------
    print("\n[2] CGAssociateMouseAndMouseCursorPosition(False)", flush=True)
    mac_input.set_cursor_associated(False)
    time.sleep(0.15)
    # 解耦后挪一次，再读；解耦本身没有返回值，只能靠组合行为判断
    mac_input.warp_cursor(500.0, 400.0)
    time.sleep(0.15)
    pos2 = mac_input.cursor_pos()
    ok &= _check(
        "解耦后仍可定位", abs(pos2[0] - 500) < 4 and abs(pos2[1] - 400) < 4,
        f"实际 ({pos2[0]:.0f},{pos2[1]:.0f})",
    )

    # ---------- 3. 恢复 ----------
    mac_input.force_associate_cursor()
    mac_input.warp_cursor(*start)
    print(f"\n已恢复光标到 ({start[0]:.0f},{start[1]:.0f})", flush=True)

    print()
    if ok:
        print("结论: 这个进程有完整的光标控制能力。")
        print("      若主程序里光标仍不动，问题在调用时机（例如在 tap 回调里调），不在权限。")
        return 0
    print("结论: 这个进程**不能**控制光标。")
    print("      事件 tap 能建、能数 swallowed，但系统没真的执行——典型的权限没到位：")
    print("      系统设置 → 隐私与安全性 → 辅助功能 里要勾选的是 dist/ 下那个 .app 本身。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
