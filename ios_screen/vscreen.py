"""虚拟显示器：让 iPhone 在系统层面成为主屏右侧的一块屏。

为什么需要它
------------
Deskflow 这类远程键鼠工具，在 client 侧维护的是**自己的**光标坐标，上限就是
它上报给 server 的桌面宽度。桌面只有 1680 宽时，光标顶到 1680 就再也走不动，
它合成的 `DeltaX` 直接变成 0 —— iPhone 上的指针于是只能沿左缘上下滑
（Y 没到边界，所以一直正常）。

右侧多一块 393×852 的虚拟屏之后：

  · 桌面宽度变成 2073，光标能真正走进来，DeltaX 不再归零；
  · 物理鼠标、触控板、Deskflow 三种输入统一成「光标在虚拟屏上的位置」，
    不再需要边缘检测、原始 HID 报文或事件 tap；
  · 393×852 正是 iPhone 15 Pro 的点数，位置天然 1:1 映射到 1180×2556。

代价与边界
----------
· 系统里会多一个显示器（Deskflow 需要重连一次才会重新上报桌面尺寸）；
· 用的是 `CGVirtualDisplay` 私有 API，苹果随时可能改，所以启动失败必须能
  干净地退回边缘模式，而不是让整个程序起不来；
· `vdisplay` 进程盯着主程序的 pid，主程序一没，虚拟屏立刻消失，不会给用户
  留下一块永远黑着的屏。
"""
from __future__ import annotations

import os
import subprocess
import time

from . import mac_input

DEFAULT_W = 393      # iPhone 15 Pro 宽（点）
DEFAULT_H = 852      # iPhone 15 Pro 高（点）


def inside(rect: tuple[float, float, float, float] | None,
           x: float, y: float) -> bool:
    """点是否落在这块矩形里（左上角含、右下角不含，和 CGRectContainsPoint 一致）。"""
    if rect is None:
        return False
    rx, ry, rw, rh = rect
    return rx <= x < rx + rw and ry <= y < ry + rh


def union_except(
    rects: list[tuple[int, float, float, float, float]],
    exclude_id: int | None,
) -> tuple[float, float, float, float] | None:
    """把若干块屏求并集，跳过 exclude_id 那块。

    rects 的每项是 (display_id, x, y, w, h)。抓取层要用它算出「只盖真实屏」
    的范围——虚拟屏上的窗口收不到鼠标事件，盖上去纯属浪费，还会挡住穿透。
    返回 (x, y, w, h)；全都排除了就返回 None，让调用方退回默认行为。
    """
    xs: list[float] = []
    ys: list[float] = []
    xe: list[float] = []
    ye: list[float] = []
    for did, x, y, w, h in rects:
        if exclude_id is not None and did == exclude_id:
            continue
        xs.append(float(x))
        ys.append(float(y))
        xe.append(float(x) + float(w))
        ye.append(float(y) + float(h))
    if not xs:
        return None
    return (min(xs), min(ys), max(xe) - min(xs), max(ye) - min(ys))


def binary_path() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "native", "vdisplay")


class VirtualScreen:
    """vdisplay 子进程 + 它创建出来的那块屏的位置。"""

    def __init__(
        self,
        width: int = DEFAULT_W,
        height: int = DEFAULT_H,
        name: str = "iPhone",
        binary: str | None = None,
        verbose: bool = False,
    ) -> None:
        self.width = int(width)
        self.height = int(height)
        self.name = name
        self.binary = binary or binary_path()
        self.verbose = verbose

        self.proc: subprocess.Popen | None = None
        self.display_id: int | None = None
        self.error = ""

    # ---------------------------------------------------------------- 生命周期

    def start(self, timeout: float = 12.0) -> bool:
        if not os.path.isfile(self.binary):
            self.error = f"找不到 {self.binary}（先跑 ./make_app.sh 编译）"
            return False
        if not os.access(self.binary, os.X_OK):
            self.error = f"{self.binary} 没有执行权限"
            return False

        before = {m["id"] for m in mac_input.list_displays()}

        # --parent-pid：主程序一死就带着虚拟屏一起退出。
        # 否则用户会得到一块永远黑着、还占着桌面宽度的屏。
        cmd = [
            self.binary,
            "--name", self.name,
            "--pt-w", str(self.width),
            "--pt-h", str(self.height),
            "--parent-pid", str(os.getpid()),
        ]
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as e:
            self.error = f"启动 vdisplay 失败: {e}"
            return False

        deadline = time.monotonic() + timeout
        found = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self.error = f"vdisplay 提前退出（code={self.proc.returncode}）"
                return False
            for m in mac_input.list_displays():
                if m["id"] not in before:
                    found = m
                    # 刚创建时系统常把它摆在原点、和主屏重叠；vdisplay 会自己
                    # 用 CGConfigureDisplayOrigin 把它挪到主屏右侧。等到 x>0
                    # 才说明位置真的落定了，否则读到的矩形是错的。
                    if m["x"] > 0:
                        self.display_id = m["id"]
                        if self.verbose:
                            print(
                                f"  [vs] 虚拟屏就绪 id={m['id']} "
                                f"{m['w']}x{m['h']} 点 at ({m['x']},{m['y']})",
                                flush=True,
                            )
                        return True
            time.sleep(0.2)

        if found is not None:
            # 位置没等到，但屏确实建起来了——照样用，只是记一笔
            self.display_id = found["id"]
            self.error = "虚拟屏已创建，但没能确认它被摆到主屏右侧"
            return True
        self.error = "等待虚拟屏出现超时"
        self.stop()
        return False

    def stop(self) -> None:
        proc = self.proc
        self.proc = None
        self.display_id = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ---------------------------------------------------------------- 位置

    @property
    def active(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def rect(self) -> tuple[int, int, int, int] | None:
        """虚拟屏在全局坐标里的 (x, y, w, h)，单位是点。"""
        if self.display_id is None:
            return None
        for m in mac_input.list_displays():
            if m["id"] == self.display_id:
                return (m["x"], m["y"], m["w"], m["h"])
        return None

    def contains(self, x: float, y: float) -> bool:
        return inside(self.rect, x, y)
