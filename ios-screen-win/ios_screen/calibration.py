"""指针校准曲线 —— 从 windows/src/calibration.cpp 逐逻辑移植。

为什么不能简化成线性比例：iOS 的相对指针有加速度，同样发一条幅度 32 的报文，
在慢速档走 164.5 像素，在快速档走的完全不同。这张表是"发一条幅度 i 的报文，
指针实际走多少物理像素"的实测结果，非线性且与主机无关。

report_interval 必须是 0.03。iOS 按报文到达速度分档，改成 0.005 会让整张表失效，
表现为"小距离准、大距离冲过头"。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Step:
    dx: int = 0
    dy: int = 0
    x: float = 0.0
    y: float = 0.0


class CalibrationError(ValueError):
    pass


class Calibration:
    def __init__(
        self,
        distances: list[float],
        screen_width: int,
        screen_height: int,
        report_interval: float = 0.03,
    ) -> None:
        self.distances = list(distances)
        self.screen_width = int(screen_width)
        self.screen_height = int(screen_height)
        self.report_interval = float(report_interval)

    # ---------- 构造 ----------

    @classmethod
    def from_json(cls, text: str) -> "Calibration":
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as e:
            raise CalibrationError(f"不是合法 JSON: {e}") from e
        if "distances" not in raw:
            raise CalibrationError("缺少 distances")
        c = cls(
            distances=[float(v) for v in raw["distances"]],
            screen_width=raw.get("screenWidth", 0),
            screen_height=raw.get("screenHeight", 0),
            report_interval=float(raw.get("reportInterval", 0.03) or 0.03),
        )
        c.validate()
        return c

    @classmethod
    def from_file(cls, path: str | Path) -> "Calibration":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    # ---------- 校验 ----------

    def validate(self) -> None:
        if not (0 < self.screen_width <= 16384) or not (0 < self.screen_height <= 16384):
            raise CalibrationError("屏幕尺寸不合法")
        if not (0.01 <= self.report_interval <= 0.1):
            raise CalibrationError(f"reportInterval={self.report_interval} 超出 0.01~0.1")
        n = len(self.distances)
        if n < 2 or n > 128:
            raise CalibrationError(f"档位数量 {n} 不合法")
        if self.distances[0] != 0.0:
            raise CalibrationError("distances[0] 必须为 0")
        for i, d in enumerate(self.distances):
            if not math.isfinite(d) or d < 0:
                raise CalibrationError("distances 含非法值")
            if i > 0 and not d > self.distances[i - 1]:
                raise CalibrationError("distances 必须严格递增")
        if self.distances[1] < 0.05:
            raise CalibrationError("最小档位太小")
        last = self.distances[-1]
        if math.ceil(self.screen_width / last) + math.ceil(self.screen_height / last) > 512:
            raise CalibrationError("归位报文数会超过 512")

    # ---------- 查询 ----------

    @property
    def max_count(self) -> int:
        return len(self.distances) - 1

    def count_for_distance(self, px: float) -> int:
        """想移动 px 像素，最接近的报文档位。距离相同时取更小的档（与 C++ 的严格小于一致）。"""
        if len(self.distances) < 2:
            return 0
        want = abs(px)
        best = 1
        best_err = abs(self.distances[1] - want)
        for i in range(2, self.max_count + 1):
            e = abs(self.distances[i] - want)
            if e < best_err:
                best_err = e
                best = i
        return best

    def next_step(
        self,
        x: float,
        y: float,
        target_x: float,
        target_y: float,
        last_dx: int = 0,
        last_dy: int = 0,
    ) -> Step:
        """从 (x,y) 朝目标走一步。返回 dx==dy==0 表示已到位。"""
        if len(self.distances) < 2:
            return Step(0, 0, x, y)

        tx = max(0.0, min(float(self.screen_width - 1), target_x))
        ty = max(0.0, min(float(self.screen_height - 1), target_y))
        remaining = (tx - x, ty - y)
        largest = max(abs(remaining[0]), abs(remaining[1]))
        tolerance = self.distances[1] / 2.0
        if largest <= tolerance:
            return Step(0, 0, x, y)

        counts = [0, 0]
        for axis in (0, 1):
            err = abs(remaining[axis])
            if err <= tolerance:
                continue
            # 不越过目标：单步按剩余比例缩放，避免大步长冲过头
            distance = min(err, self.distances[-1] * err / largest)
            pick = 0
            for i in range(1, self.max_count + 1):
                if self.distances[i] <= distance + tolerance:
                    pick = i
            counts[axis] = pick

        # 上一轮发了 X 就优先发 Y，避免大目标时某一轴一直饿着
        if last_dx != 0:
            preferred = 1
        else:
            preferred = 0 if abs(remaining[0]) >= abs(remaining[1]) else 1

        for k in (0, 1):
            axis = preferred if k == 0 else 1 - preferred
            if counts[axis] <= 0:
                continue
            sign = -1.0 if remaining[axis] < 0 else 1.0
            delta = sign * self.distances[counts[axis]]
            if axis == 0:
                dx = int(sign) * counts[0]
                return Step(dx, 0, max(0.0, min(float(self.screen_width - 1), x + delta)), y)
            dy = int(sign) * counts[1]
            return Step(0, dy, x, max(0.0, min(float(self.screen_height - 1), y + delta)))
        return Step(0, 0, x, y)

    def homing_reports(self) -> list[tuple[int, int]]:
        """连发最大幅度向左、再向上。iOS 在边界会钳制，所以之后位置已知是 (0,0)。"""
        if len(self.distances) < 2:
            return []
        count = self.max_count
        step = self.distances[-1]
        nx = math.ceil(self.screen_width / step) + 2
        ny = math.ceil(self.screen_height / step) + 2
        return [(-count, 0)] * nx + [(0, -count)] * ny
