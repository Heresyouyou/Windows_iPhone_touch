"""边缘触发状态机 —— 从 windows/src/edge_trigger.cpp 移植，逻辑逐条对齐。

核心假设只有一条：光标被钳制在屏幕边缘时，Raw Input 的设备级位移还在继续。
所以"贴边 + 继续朝外"就是可靠的越界意图信号，不需要真的存在第二块显示器。

两条阈值必须一紧一松：进入要小（用户一推就进），退出要大（否则在边界上抖）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .geometry import Rect

# 光标离屏边多少像素内算"贴边"。取 2 是为了容忍高 DPI 下 GetCursorPos 的一像素误差。
EDGE_BAND_PX = 2


class Edge(Enum):
    Right = "right"
    Left = "left"
    Top = "top"
    Bottom = "bottom"


def parse_edge(s: str) -> Edge:
    table = {
        "right": Edge.Right, "r": Edge.Right,
        "left": Edge.Left, "l": Edge.Left,
        "top": Edge.Top, "t": Edge.Top,
        "bottom": Edge.Bottom, "b": Edge.Bottom,
    }
    try:
        return table[s.lower()]
    except KeyError:
        raise ValueError(f"未知方向 {s!r}，可选 right/left/top/bottom") from None


def edge_name(e: Edge) -> str:
    return e.value


@dataclass(frozen=True)
class EdgeSpec:
    edge: Edge = Edge.Right
    screen: Rect = field(default_factory=Rect)
    # 沿边的触发区间，0..1 归一化。默认整条边都能触发。
    # 用途：只让屏幕底部「水平 40% 附近」这一小段成为出口，避免整条底边都误触。
    zone_start: float = 0.0
    zone_end: float = 1.0


def is_at_edge(spec: EdgeSpec, x: int, y: int, band_px: int = EDGE_BAND_PX) -> bool:
    r = spec.screen
    # 右/下边界必须用 `>` 而不是 `>=`。
    #
    # Windows 的 GetCursorPos 在光标顶满右下角时最大只到 width-1 / height-1；
    # macOS 的 CGEventGetLocation 不是这样——光标顶住右缘时它返回的正是
    # right 本身（1680），底缘是 bottom 本身（1050）。用 `>=` 判"是否在屏外"，
    # 就会把"正贴着边"误判成"已经跑到屏幕外面"，贴边条件永远不成立。
    # 实测症状：光标 (1680.0, y) 而 is_at_edge 返回 False，于是推不进去。
    if x < r.x or y < r.y or x > r.right or y > r.bottom:
        return False
    # 沿边方向的归一化位置：左右边看 y，上下边看 x。
    # 同样因为能取到 right/bottom，along 会算出 1.0009 这种略大于 1 的值，
    # 不夹紧就会被触发区间的上界挡掉（屏幕最下方那一截右缘失效）。
    if spec.edge in (Edge.Right, Edge.Left):
        along = (y - r.y) / max(1, r.h - 1)
    else:
        along = (x - r.x) / max(1, r.w - 1)
    along = max(0.0, min(1.0, along))
    if not (spec.zone_start <= along <= spec.zone_end):
        return False
    if spec.edge is Edge.Right:
        return x >= r.right - 1 - band_px
    if spec.edge is Edge.Left:
        return x <= r.x + band_px
    if spec.edge is Edge.Top:
        return y <= r.y + band_px
    return y >= r.bottom - 1 - band_px


def outward_component(spec: EdgeSpec, dx: int, dy: int) -> int:
    if spec.edge is Edge.Right:
        return dx
    if spec.edge is Edge.Left:
        return -dx
    if spec.edge is Edge.Top:
        return -dy
    return dy


def inward_component(spec: EdgeSpec, dx: int, dy: int) -> int:
    return -outward_component(spec, dx, dy)


@dataclass
class EdgeTriggerConfig:
    enter_threshold: int = 12
    exit_threshold: int = 60
    decay_per_event: int = 1


class Change(Enum):
    None_ = 0
    Entered = 1
    Exited = 2


class EdgeTrigger:
    def __init__(self, cfg: EdgeTriggerConfig | None = None) -> None:
        self.cfg = cfg or EdgeTriggerConfig()
        self.active = False
        self.accum = 0

    def reset(self) -> None:
        self.active = False
        self.accum = 0

    def update(self, spec: EdgeSpec, cursor_x: int, cursor_y: int, dx: int, dy: int) -> Change:
        out = outward_component(spec, dx, dy)

        if not self.active:
            # 没贴边就不该攒进度：这是"想推出去"和"顺手划到边上"的分界线
            if not is_at_edge(spec, cursor_x, cursor_y):
                self.accum = 0
                return Change.None_
            if out > 0:
                self.accum = min(self.accum + out, self.cfg.enter_threshold * 4)
                if self.accum >= self.cfg.enter_threshold:
                    self.active = True
                    self.accum = 0
                    return Change.Entered
            else:
                self.accum = max(0, self.accum - self.cfg.decay_per_event)
            return Change.None_

        # 捕获中光标位置已不代表意图（可能被我们挪到屏幕中央了），只看位移方向
        if out < 0:
            self.accum += -out
            if self.accum >= self.cfg.exit_threshold:
                self.active = False
                self.accum = 0
                return Change.Exited
        elif out > 0 and self.accum > 0:
            # 又往外推：把退出进度吐回去，避免"进去-出来-进去"攒出误退出
            self.accum = max(0, self.accum - out)
        return Change.None_
