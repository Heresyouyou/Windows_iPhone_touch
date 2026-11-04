"""HOGP 联调脚本：把服务挂起来，等 iPhone 配对，然后发归位报文验证指针会动。

用法:
  python tools/hogp_run.py --seconds 180        # 只广播，等配对
  python tools/hogp_run.py --seconds 180 --home # 配对后发归位报文

验收顺序（每一步都要过，过了再下一条）：
  1. 启动后打印"服务已启动 广播=True"
  2. 安卓装 nRF Connect 能扫到 0x1812 和 5 个 Report 特征
  3. iPhone 蓝牙里出现这台电脑的名字，能配对
  4. iPhone 开辅助触控 → 设备 → 蓝牙设备，选中这台电脑
  5. 跑 --home，iPhone 指针跳到左上角
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ios_screen import hid  # noqa: E402
from ios_screen.calibration import Calibration  # noqa: E402
from ios_screen.hogp import HogpService  # noqa: E402

CAL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ios_screen", "data", "calibration-iphone15pro.json",
)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=180)
    ap.add_argument("--home", action="store_true", help="配对成功后发归位报文")
    a = ap.parse_args()

    svc = HogpService()
    svc.on_subscribe_changed(lambda s: print(f"  >>> 订阅状态变为 {s}", flush=True))

    print("构建 GATT 服务…", flush=True)
    try:
        await svc.start()
    except Exception as e:
        print(f"❌ 启动失败: {e}")
        return 1

    print(f"✅ 服务已启动  广播={svc.advertising}", flush=True)
    print("   现在去 iPhone: 设置 → 蓝牙，找这台电脑的名字并配对", flush=True)
    print("   然后: 设置 → 辅助功能 → 触控 → 辅助触控 → 设备 → 蓝牙设备，选中它", flush=True)

    cal = None
    if a.home:
        p = os.path.normpath(CAL_PATH)
        if os.path.isfile(p):
            cal = Calibration.from_file(p)
            print(f"   校准已载入: {cal.screen_width}x{cal.screen_height} "
                  f"{cal.max_count} 档 {cal.report_interval*1000:.0f}ms")
        else:
            print(f"   ⚠ 找不到校准文件 {p}，跳过归位")

    sent_home = False
    last_sub = False
    for i in range(a.seconds):
        await asyncio.sleep(1)
        if i % 10 == 0:
            print(f"  ...{i:3d}s  订阅={svc.subscribed}  广播={svc.advertising}", flush=True)

        # 每次"从未订阅变成已订阅"都发一次归位。
        # 服务重启后 iPhone 不会自动重连，必须在辅助触控里重选；这样用户随便哪一秒
        # 连上都能立刻看到指针跳到左上角，不用赶时间窗口。
        if a.home and cal and svc.subscribed and not last_sub:
            reports = cal.homing_reports()
            print(f"\n  → 检测到连接，发送 {len(reports)} 条归位报文"
                  f"（约 {len(reports)*cal.report_interval:.1f}s）", flush=True)
            ok_n = fail_n = 0
            for dx, dy in reports:
                ok = await svc.send_mouse(0, dx, dy)
                ok_n += 1 if ok else 0
                fail_n += 0 if ok else 1
                await asyncio.sleep(cal.report_interval)
            print(f"  → 归位完成：成功 {ok_n} 条，失败 {fail_n} 条", flush=True)
            if fail_n:
                print("  ⚠ 有报文没发出去。notify_value_async 失败通常意味着"
                      " iOS 没订阅该报表，或连接名存实亡。", flush=True)
            else:
                print("  → 报文全部发出，若 iPhone 仍无指针，问题在 iOS 侧未接管鼠标报表\n",
                      flush=True)
            sent_home = True
        last_sub = svc.subscribed

    await svc.stop()
    print("已停止广播")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()) or 0)
    except KeyboardInterrupt:
        print("\n中断")
        sys.exit(130)
