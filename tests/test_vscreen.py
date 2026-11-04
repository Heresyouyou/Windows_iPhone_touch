"""虚拟屏的两块纯逻辑：点在不在屏内、抓取层该盖哪。

为什么这两条值得单测
--------------------
`CGVirtualDisplay` 那块屏上的窗口**收不到鼠标事件**（实测：frame 盖住、
`hitTest:` 命中，`mouseDown:` 就是不派发；同一位置的点击只读 tap 看得一清二楚）。
于是输入必须分流：

    · 虚拟屏里的点击 → 只读 tap 转发；
    · 真实屏上的点击 → 抓取层窗口挡住，不转给 iPhone。

分流的第一道判据就是「这个坐标在不在虚拟屏里」。判错一个像素的后果是
点击重复发送（两边都转）或者完全丢失，而且只在真机上才看得出来。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ios_screen.vscreen import inside, union_except  # noqa: E402

# 本机实测的布局：主屏 1680x1050 在原点，虚拟屏 393x852 贴在右侧、垂直居中
MAIN = (0, 0, 0, 1680, 1050)
VIRT = (4128835, 1680, 99, 393, 852)


class InsideTest(unittest.TestCase):
    def test_center(self):
        self.assertTrue(inside((1680, 99, 393, 852), 1876, 525))

    def test_left_top_inclusive(self):
        self.assertTrue(inside((1680, 99, 393, 852), 1680, 99))

    def test_right_bottom_exclusive(self):
        # 和 CGRectContainsPoint 一致：右/下边界的那个像素属于下一块屏
        self.assertFalse(inside((1680, 99, 393, 852), 2073, 525))
        self.assertFalse(inside((1680, 99, 393, 852), 1876, 951))

    def test_just_inside(self):
        self.assertTrue(inside((1680, 99, 393, 852), 2072, 950))

    def test_outside_left(self):
        self.assertFalse(inside((1680, 99, 393, 852), 1679, 525))

    def test_none_rect(self):
        self.assertFalse(inside(None, 0, 0))


class UnionExceptTest(unittest.TestCase):
    def test_excludes_virtual(self):
        got = union_except([MAIN, VIRT], VIRT[0])
        self.assertEqual(got, (0.0, 0.0, 1680.0, 1050.0))

    def test_without_exclude_is_full_desktop(self):
        got = union_except([MAIN, VIRT], None)
        self.assertEqual(got, (0.0, 0.0, 2073.0, 1050.0))

    def test_all_excluded_returns_none(self):
        # 只剩虚拟屏时不能返回一块空矩形——调用方要能判断"没有可盖的屏"
        self.assertIsNone(union_except([VIRT], VIRT[0]))

    def test_two_real_screens(self):
        second = (99, -1920, 0, 1920, 1080)   # 左侧一块 1920x1080
        got = union_except([MAIN, second, VIRT], VIRT[0])
        self.assertEqual(got, (-1920.0, 0.0, 3600.0, 1080.0))

    def test_gap_between_screens_is_covered(self):
        # 两块真实屏之间有负空间时，窗口用并集覆盖，不按"只盖有屏的地方"
        right = (98, 1680, 0, 1680, 1050)
        got = union_except([MAIN, right], None)
        self.assertEqual(got, (0.0, 0.0, 3360.0, 1050.0))


if __name__ == "__main__":
    unittest.main()
