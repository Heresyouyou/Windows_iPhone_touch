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
from ios_screen.vpointer import (  # noqa: E402
    PointerConfig, PushThrough, SIDE_OUTWARD, VirtualPointer,
)

SCREEN = Rect(0, 0, 3440, 1440)
CAL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ios_screen", "data", "calibration-iphone15pro.json",
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
        # macOS 的 CGEventGetLocation 在光标顶满右缘时返回的正是 right(3440)，
        # 不是 Windows 那种 right-1。这一条必须算贴边，否则永远推不进去。
        self.assertTrue(is_at_edge(spec, 3440, 700))    # 顶满右缘
        self.assertTrue(is_at_edge(spec, 3439, 700))    # 压线
        self.assertTrue(is_at_edge(spec, 3437, 700))    # 带宽内
        self.assertFalse(is_at_edge(spec, 3436, 700))   # 带外
        self.assertFalse(is_at_edge(spec, 100, 700))    # 屏幕中间

    def test_at_edge_corner_at_bottom_right(self):
        """右下角同时踩两个边界：x==right 和 y==bottom。"""
        spec = EdgeSpec(Edge.Right, SCREEN)
        self.assertTrue(is_at_edge(spec, 3440, 1440))
        self.assertTrue(is_at_edge(spec, 3440, 1439))
        bottom = EdgeSpec(Edge.Bottom, SCREEN)
        self.assertTrue(is_at_edge(bottom, 1700, 1440))

    def test_at_edge_rejects_outside_screen(self):
        """屏外坐标不算贴边——多显示器时别把邻屏位置误判成本屏边缘。"""
        spec = EdgeSpec(Edge.Right, SCREEN)
        self.assertFalse(is_at_edge(spec, 3441, 700))
        self.assertFalse(is_at_edge(spec, 3439, 1441))

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

    def test_enter_exit_enter_again(self):
        """进得去、出得来、还要能再进去。

        实机 bug 复现：往回推越界释放走的是 Controller.end_capture()，
        它不经过 trigger.update()，所以 active 一直留在 True。此后 update()
        永远走"捕获中"分支（只看位移方向、不再判贴边），再也不返回 Entered——
        现象就是"第一次能推进去，出来之后再怎么推都回不去"。
        """
        # 第一次进入（光标顶满右缘，取到 right 本身）
        self.assertIs(self.t.update(self.spec, 3440, 700, 12, 0), Change.Entered)
        self.assertTrue(self.t.active)

        # 往回推越界：Controller 直接结束捕获，外部负责复位
        self.t.reset()

        # 第二次推到边上，必须还能进
        self.assertIs(self.t.update(self.spec, 3440, 700, 12, 0), Change.Entered)

    def test_stale_active_blocks_reentry(self):
        """不做复位的话，第二次进不去——把这个失败模式钉住，防止有人删掉复位。"""
        self.t.update(self.spec, 3440, 700, 12, 0)
        # 故意不复位，模拟旧代码
        for _ in range(10):
            self.assertIsNot(
                self.t.update(self.spec, 3440, 700, 12, 0), Change.Entered
            )


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


class TestVirtualPointer(unittest.TestCase):
    def make(self, **kw) -> VirtualPointer:
        cfg = PointerConfig(width=1180, height=2556, gain=0.5, **kw)
        return VirtualPointer(cfg)

    def test_linear_gain(self):
        p = self.make()
        p.set(100, 100)
        dx, dy = p.move(10, -10)
        self.assertAlmostEqual(dx, 5.0)
        self.assertAlmostEqual(dy, -5.0)
        self.assertAlmostEqual(p.x, 105.0)
        self.assertAlmostEqual(p.y, 95.0)

    def test_unlimited_travel_is_clamped_not_lost(self):
        """一直朝一个方向推：位置停在边界，但再来一次仍然不会越界。"""
        p = self.make()
        p.set(0, 0)
        for _ in range(100):
            p.move(200, 200)
        self.assertEqual(p.x, float(p.cfg.width - 1))
        self.assertEqual(p.y, float(p.cfg.height - 1))

    def test_returns_applied_delta_after_clamp(self):
        """顶到边界后返回的位移要变小，否则拖动报文会一直推。"""
        p = self.make()
        p.set(float(p.max_x) - 2, 0)
        dx, _ = p.move(100, 0)
        self.assertAlmostEqual(dx, 2.0)

    def test_at_side(self):
        p = self.make()
        p.set(0, 0)
        self.assertTrue(p.at_side("left"))
        self.assertTrue(p.at_side("top"))
        self.assertFalse(p.at_side("right"))
        p.set(p.max_x, p.max_y)
        self.assertTrue(p.at_side("right"))
        self.assertTrue(p.at_side("bottom"))

    def test_accel_off_is_speed_independent(self):
        p = self.make()
        self.assertAlmostEqual(p.gain_for(10, 0, 0.001), 0.5)
        self.assertAlmostEqual(p.gain_for(10, 0, 1.0), 0.5)

    def test_accel_boosts_fast_strokes(self):
        p = self.make(accel=1.0, accel_ref=1000.0, accel_max=3.0)
        slow = p.gain_for(10, 0, 0.1)     # 100 计数/秒
        fast = p.gain_for(10, 0, 0.001)   # 10000 计数/秒
        self.assertGreater(fast, slow)
        self.assertLessEqual(fast, 0.5 * 3.0)

    def test_edge_excess_only_counts_clamped_part(self):
        """没贴边时快速划动，被边界吃掉的量是 0，不能算成"要出去"。"""
        p = self.make()
        p.set(400, 500)
        applied = p.move(100, 0)          # 50px，离右边界还很远
        self.assertEqual(p.edge_excess_px(100, 0, "right", applied), 0.0)

    def test_edge_excess_overflow(self):
        p = self.make()
        p.set(float(p.max_x) - 10, 500)
        applied = p.move(100, 0)          # 请求 50px，只剩 10px 可用
        self.assertAlmostEqual(p.edge_excess_px(100, 0, "right", applied), 40.0)

    def test_edge_excess_wrong_direction(self):
        p = self.make()
        p.set(0.0, 500)
        applied = p.move(100, 0)          # 往内推
        self.assertEqual(p.edge_excess_px(100, 0, "left", applied), 0.0)


class TestPushThrough(unittest.TestCase):
    def test_accumulates_until_threshold(self):
        pt = PushThrough(threshold_px=100.0)
        self.assertFalse(pt.update(True, 40.0))
        self.assertFalse(pt.update(True, 40.0))
        self.assertTrue(pt.update(True, 40.0))

    def test_resets_after_trigger(self):
        pt = PushThrough(threshold_px=100.0)
        pt.update(True, 100.0)
        self.assertEqual(pt.accum, 0)
        self.assertFalse(pt.update(True, 10.0))

    def test_inward_decays(self):
        """贴着边操作（不往外推）不该攒出退出。"""
        pt = PushThrough(threshold_px=100.0)
        pt.update(True, 60.0)
        for _ in range(30):
            pt.update(False, 5.0)
        self.assertEqual(pt.accum, 0)

    def test_zero_counts_while_pushing(self):
        pt = PushThrough(threshold_px=10.0)
        self.assertFalse(pt.update(True, 0.0))
        self.assertFalse(pt.update(True, -5.0))

    def test_side_outward_signs(self):
        self.assertEqual(SIDE_OUTWARD["left"](-7, 0), 7)
        self.assertEqual(SIDE_OUTWARD["right"](7, 0), 7)
        self.assertEqual(SIDE_OUTWARD["top"](0, -7), 7)
        self.assertEqual(SIDE_OUTWARD["bottom"](0, 7), 7)

    def test_release_at_minus_five(self):
        """进入点记 0，越界后为负，负到 -5 就释放。"""
        pt = PushThrough(threshold_px=5.0)
        self.assertFalse(pt.update(True, 3.0))
        self.assertAlmostEqual(pt.signed, -3.0)
        self.assertFalse(pt.update(True, 1.5))
        self.assertTrue(pt.update(True, 0.6))   # 累计 5.1 ≥ 5
        self.assertEqual(pt.accum, 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
