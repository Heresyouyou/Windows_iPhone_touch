"""虚拟绝对指针：把鼠标的相对位移累积成 iPhone 屏幕上的绝对坐标。

要解决的问题
------------
绝对报文（Report 2）的体验最好：一条报文直接定位，没有"30ms × N 步"的追赶感，
也不依赖校准曲线。但如果坐标直接取自光标在窗口/屏幕上的物理位置，手的行程就被
那块区域锁死了——窗口只有 420 像素宽，光标一碰边就到头，iPhone 屏幕上还剩一大半
够不着；屏幕边缘同理，光标被内核钳在边上，位置不再变化。

做法
----
把两件事拆开：

    · 位置 = 虚拟坐标，由 Raw Input 的相对计数累积而来，理论上可以无限推；
    · 落点 = 绝对报文，一条到位，没有累积延迟。

于是"手的行程"和"指针的位置"彻底解耦：一直朝一个方向推，指针走到 iPhone 边界
就停住，手抬起来回中（clutch）再推，和触控板完全一致。物理光标在捕获期间已经被
低级钩子吞掉、根本不动，所以不存在"撞到屏幕边缘"这回事。

为什么还要 PushThrough
----------------------
位置不再受边界约束之后，"怎么退出"就没有天然的判据了。这里沿用 Deskflow 的
语义：指针顶在靠 Windows 那一侧的边界上，还继续往外推，就是"我要回去"。累计量
超过阈值才退出，避免贴着边缘操作时被手抖带出去。
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass

# 退出边（iPhone 靠 Windows 的那一侧）→ 朝向 Windows 的单位向量
_SIDE_SIGN = {
    "left": (-1.0, 0.0),
    "right": (1.0, 0.0),
    "top": (0.0, -1.0),
    "bottom": (0.0, 1.0),
}

# 退出边（iPhone 靠 Windows 的那一侧）→ 朝向 Windows 的位移分量
SIDE_OUTWARD = {
    "left": lambda dx, dy: -dx,
    "right": lambda dx, dy: dx,
    "top": lambda dx, dy: -dy,
    "bottom": lambda dx, dy: dy,
}


@dataclass(frozen=True)
class PointerConfig:
    """width/height 是 iPhone 的像素尺寸（来自校准文件）。

    gain 是"一个鼠标计数换多少 iPhone 像素"。

    这个值必须跟着 iPhone 的像素密度走。iPhone 15 Pro 是 1180x2556 **像素**，
    而 Mac 屏幕是 1680x1050 **点**——1:1 映射的话，横穿 iPhone 要 1180 个计数，
    是横穿 Mac 屏幕的两倍多，手感就是"怎么推都推不到"。

    默认 3.0：横穿 iPhone 宽度约 393 个计数，和一个手腕动作的量级相当。
    嫌快嫌慢随时用 Ctrl+Option+= / Ctrl+Option+- 调。

    accel 是速度增益：0 表示线性（推荐，位置可预测）；大于 0 时快速划动会按
    speed/accel_ref 放大，最多放大到 accel_max 倍。低速时不受影响，所以精细
    定位仍然准。
    """

    width: int = 1180
    height: int = 2556
    gain: float = 3.0
    accel: float = 0.0
    accel_ref: float = 2500.0
    accel_max: float = 3.0


class VirtualPointer:
    def __init__(self, cfg: PointerConfig) -> None:
        self.cfg = cfg
        self.x = (cfg.width - 1) / 2.0
        self.y = (cfg.height - 1) / 2.0
        self.last_gain = cfg.gain

    # ---------- 边界 ----------

    @property
    def max_x(self) -> float:
        return float(self.cfg.width - 1)

    @property
    def max_y(self) -> float:
        return float(self.cfg.height - 1)

    def clamp_x(self, v: float) -> float:
        return min(self.max_x, max(0.0, v))

    def clamp_y(self, v: float) -> float:
        return min(self.max_y, max(0.0, v))

    def set(self, x: float, y: float) -> None:
        self.x = self.clamp_x(x)
        self.y = self.clamp_y(y)

    def set_gain(self, gain: float) -> float:
        """运行时改增益（热键调速用）。返回实际生效值。"""
        # 上限放到 20：4K 屏 + 高像素密度手机的组合下，8 倍可能还不够
        g = min(20.0, max(0.2, float(gain)))
        self.cfg = dataclasses.replace(self.cfg, gain=g)
        self.last_gain = g
        return g

    def center(self) -> None:
        self.set(self.max_x / 2.0, self.max_y / 2.0)

    # ---------- 位移 ----------

    def gain_for(self, dx: int, dy: int, dt: float) -> float:
        g = self.cfg.gain
        if self.cfg.accel <= 0.0 or dt <= 0.0:
            return g
        speed = math.hypot(dx, dy) / max(dt, 1e-4)  # 计数/秒
        k = min(1.0, speed / max(1.0, self.cfg.accel_ref))
        return g * min(self.cfg.accel_max, 1.0 + self.cfg.accel * k)

    def move(self, dx: int, dy: int, dt: float = 0.0) -> tuple[float, float]:
        """推一格。返回实际生效的像素位移（已被边界截断）。"""
        g = self.gain_for(dx, dy, dt)
        self.last_gain = g
        ox, oy = self.x, self.y
        self.x = self.clamp_x(self.x + dx * g)
        self.y = self.clamp_y(self.y + dy * g)
        return self.x - ox, self.y - oy

    def edge_excess_px(self, dx: int, dy: int, side: str,
                       applied: tuple[float, float]) -> float:
        """越过退出边多少像素（= 有符号行程里低于 0 的那一截）。

        不能把整段位移都算成推出去：手快速划一下时，指针还没碰到边就先走完
        一大截，那段是正常移动，只有被 clamp 截掉的部分才代表"想出去"。
        """
        g = max(1e-6, self.last_gain)
        mdx, mdy = applied
        sx, sy = _SIDE_SIGN[side]
        requested_out = (dx * g) * sx + (dy * g) * sy
        applied_out = mdx * sx + mdy * sy
        return max(0.0, requested_out - applied_out)

    # ---------- 边界状态 ----------

    def at_side(self, side: str, eps: float = 0.5) -> bool:
        if side == "left":
            return self.x <= eps
        if side == "right":
            return self.x >= self.max_x - eps
        if side == "top":
            return self.y <= eps
        return self.y >= self.max_y - eps


class PushThrough:
    """有符号竖直行程：进入点为 0，越过退出边后变负，负到阈值就释放。

    单位是 iPhone 像素，和绝对报文的坐标系一致，所以不受鼠标 DPI 和增益影响。
    不用"位置越界"判定：虚拟坐标早就被截断在 0/上限，位置本身不会再变，
    只有继续推的那一截能表达"我要出去"。
    """

    def __init__(self, threshold_px: float = 5.0, decay: float = 2.0) -> None:
        self.threshold = max(0.5, float(threshold_px))
        self.decay = max(1.0, float(decay))
        self.accum = 0.0

    def reset(self) -> None:
        self.accum = 0.0

    def update(self, pushing: bool, px: float) -> bool:
        """pushing 为真表示"正顶在退出边上且往外推"。返回 True 表示该释放了。"""
        if pushing and px > 0:
            self.accum += px
            if self.accum >= self.threshold:
                self.accum = 0.0
                return True
        elif self.accum:
            # 反向或离边：快速回吐，避免慢慢攒出误退出
            self.accum = max(0.0, self.accum - self.decay * max(1.0, abs(px)))
        return False

    @property
    def signed(self) -> float:
        """对外展示用的有符号坐标：0 = 进入点，负值 = 已越界多少像素。"""
        return -self.accum
