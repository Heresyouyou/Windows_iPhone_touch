"""矩形与几何。Windows 用物理像素，原点在主屏左上，没有"点"的概念，全程整数。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h
