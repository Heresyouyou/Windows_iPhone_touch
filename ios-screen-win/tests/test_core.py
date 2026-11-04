"""平台无关部分的单测：边缘状态机 + 校准曲线。

这两块是最容易出边界错的地方，也是唯一能在没有真实鼠标/iPhone 的情况下
判定对错的部分。跑法：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ios_screen.calibration import Calibration, CalibrationError  # noqa: E402
from ios_screen.edge_trigger import (  # noqa: E402
    Edge,
    EdgeSpec,
    EdgeTrigger,
    EdgeTriggerConfig,
    Change,
    inward_component,
    is_at_edge,
    outward_component,
    parse_edge,
)
from ios_screen.geometry import Rect  # noqa: E402

SCREEN = Rect(0, 0, 3440, 1440)
CAL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "deskflow-ios", "IOS端", "windows", "calibration-iphone15pro.json",
)


class TestEdgeBasics(unittest.TestCase):
    def test_parse_edge(self):
        self.assertIs(parse_edge("right"), Edge.Right)
        self.assertIs(parse_edge("R"), Edge.Right)
        self.assertIs(parse_edge("bottom"), Edge.Bottom)
        with self.assertRaises(ValueError):
            parse_edge("middle")

    def test_at_edge_right(self):
        spec = EdgeSpec(Edge.Right, SCREEN)
        self.assertTrue(is_at_edge(spec, 3439, 700))    # 压线
        self.assertTrue(is_at_edge(spec, 3437, 700))    # 带宽内
        self.assertFalse(is_at_edge(spec, 3436, 700))   # 带外
        self.assertFalse(is_at_edge(spec, 100, 700))    # 屏幕中间

    def test_at_edge_rejects_outside_screen(self):
        """屏外坐标不算贴边——多显示器时别把邻屏位置误判成本屏边缘。"""
        spec = EdgeSpec(Edge.Right, SCREEN)
        self.assertFalse(is_at_edge(spec, 3440, 700))
        self.assertFalse(is_at_edge(spec, 3439, 1440))

    def test_components_all_edges(self):
        cases = [
            (Edge.Right, (5, 0), 5),
            (Edge.Left, (-5, 0), 5),
            (Edge.Top, (0, -5), 5),
            (Edge.Bottom, (0, 5), 5),
        ]
        for edge, (dx, dy), want in cases:
            spec = EdgeSpec(edge, SCREEN)
            self.assertEqual(outward_component(spec, dx, dy), want, edge)
            self.assertEqual(inward_component(spec, dx, dy), -want, edge)


class TestEdgeTrigger(unittest.TestCase):
    def setUp(self):
        self.spec = EdgeSpec(Edge.Right, SCREEN)
        self.t = EdgeTrigger(EdgeTriggerConfig(enter_threshold=12, exit_threshold=60))

    def test_enter_after_accumulating_outward(self):
        self.assertIs(self.t.update(self.spec, 3439, 700, 5, 0), Change.None_)
        self.assertIs(self.t.update(self.spec, 3439, 700, 5, 0), Change.None_)
        self.assertIs(self.t.update(self.spec, 3439, 700, 5, 0), Change.Entered)
        self.assertTrue(self.t.active)

    def test_no_enter_when_not_at_edge(self):
        """光标在屏幕中间时，往右推多少都不该触发。"""
        for _ in range(50):
            self.assertIs(self.t.update(self.spec, 1000, 700, 60, 0), Change.None_)
        self.assertFalse(self.t.active)

    def test_outward_then_back_decays(self):
        """推一下又收回去，不能攒出误触发。"""
        self.t.update(self.spec, 3439, 700, 6, 0)
        for _ in range(12):
            self.t.update(self.spec, 3439, 700, -1, 0)
        self.assertFalse(self.t.active)
        self.assertEqual(self.t.accum, 0)

    def test_exit_after_inward(self):
        self.t.update(self.spec, 3439, 700, 12, 0)
        self.assertTrue(self.t.active)
        change = Change.None_
        for _ in range(6):
            change = self.t.update(self.spec, 500, 700, -10, 0)
        self.assertIs(change, Change.Exited)
        self.assertFalse(self.t.active)

    def test_outward_while_capturing_cancels_exit_progress(self):
        """捕获中先往内推一点、又往外推，退出进度要吐回去，否则会莫名掉出捕获。"""
        self.t.update(self.spec, 3439, 700, 12, 0)
        self.t.update(self.spec, 500, 700, -40, 0)
        self.assertTrue(self.t.active)
        self.t.update(self.spec, 500, 700, 40, 0)
        self.assertEqual(self.t.accum, 0)
        self.assertTrue(self.t.active)

    def test_exactly_threshold_enters(self):
        self.assertEqual(self.t.update(self.spec, 3439, 700, 12, 0), Change.Entered)

    def test_vertical_edges(self):
        top = EdgeSpec(Edge.Top, SCREEN)
        t = EdgeTrigger(EdgeTriggerConfig(enter_threshold=10, exit_threshold=30))
        self.assertIs(t.update(top, 1000, 1, 0, -10), Change.Entered)
        self.assertIs(t.update(top, 1000, 700, 0, 30), Change.Exited)

    def test_reset(self):
        self.t.update(self.spec, 3439, 700, 12, 0)
        self.t.reset()
        self.assertFalse(self.t.active)
        self.assertEqual(self.t.accum, 0)


class TestCalibration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cal = Calibration.from_file(CAL_PATH)

    def test_shape(self):
        self.assertEqual(self.cal.screen_width, 1180)
        self.assertEqual(self.cal.screen_height, 2556)
        self.assertEqual(self.cal.max_count, 32)
        self.assertAlmostEqual(self.cal.report_interval, 0.03)

    def test_count_for_distance(self):
        self.assertEqual(self.cal.count_for_distance(0), 1)
        self.assertEqual(self.cal.count_for_distance(self.cal.distances[32]), 32)
        # 相邻两档中点偏下应落到低档
        mid = (self.cal.distances[10] + self.cal.distances[11]) / 2
        self.assertIn(self.cal.count_for_distance(mid), (10, 11))

    def test_converges_in_26_steps(self):
        """与 C++ 单测同一组数字：从 (0,0) 到 (1179,2555) 用 26 步，误差 < 半档。"""
        x = y = 0.0
        last = 0
        n = 0
        while n < 500:
            s = self.cal.next_step(x, y, 1179, 2555, last_dx=last)
            if s.dx == 0 and s.dy == 0:
                break
            x, y, last = s.x, s.y, s.dx
            n += 1
        self.assertEqual(n, 26)
        self.assertLess(abs(x - 1179), 0.5)
        self.assertLess(abs(y - 2555), 0.5)

    def test_step_never_overshoots(self):
        """单步不能越过目标，否则贴近目标时会来回抖。"""
        x, y = 500.0, 1200.0
        s = self.cal.next_step(x, y, 520, 1210)
        self.assertLessEqual(s.x, 520.0)
        self.assertLessEqual(s.y, 1210.0)

    def test_target_clamped_to_screen(self):
        s = self.cal.next_step(0, 0, 99999, 99999)
        self.assertGreaterEqual(s.x, 0)
        self.assertLessEqual(s.x, 1179)

    def test_reached_returns_zero(self):
        s = self.cal.next_step(600, 1200, 600, 1200)
        self.assertEqual((s.dx, s.dy), (0, 0))

    def test_homing_count(self):
        rs = self.cal.homing_reports()
        self.assertEqual(len(rs), 28)          # 10 次向左 + 18 次向上
        self.assertTrue(all(dx == -32 and dy == 0 for dx, dy in rs[:10]))
        self.assertTrue(all(dx == 0 and dy == -32 for dx, dy in rs[10:]))

    def test_rejects_bad_calibration(self):
        with self.assertRaises(CalibrationError):
            Calibration.from_json('{"screenWidth":100,"screenHeight":100,"distances":[0,1,1]}')
        with self.assertRaises(CalibrationError):
            Calibration.from_json('{"screenWidth":0,"screenHeight":100,"distances":[0,1]}')
        with self.assertRaises(CalibrationError):
            Calibration.from_json('{"distances":[0,1]}')
        with self.assertRaises(CalibrationError):
            # reportInterval 超出 0.01~0.1，改了这个整张表就失效
            Calibration.from_json(
                '{"screenWidth":100,"screenHeight":100,"reportInterval":0.5,"distances":[0,1]}'
            )

    def test_rejects_unsorted_distances(self):
        with self.assertRaises(CalibrationError):
            Calibration.from_json(
                '{"screenWidth":100,"screenHeight":100,"distances":[0,2,1]}'
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
