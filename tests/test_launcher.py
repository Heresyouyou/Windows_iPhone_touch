"""启动台的进程层测试。

只测不依赖窗口的部分：真正点按钮的那条路在 launcher/app.py 里，
需要 AppKit，留给手工验证。这里保证"匹配谁、杀谁"不会出错 ——
按命令行匹配最怕的就是误伤，所以要确认模式足够精确。
"""

from __future__ import annotations

import re
import subprocess
import time
import unittest

from launcher import controller

MARK = "launcher-test-marker-9f3a"


class TestPatterns(unittest.TestCase):
    def test_ios_pattern_matches_the_real_command_line(self):
        line = "/Users/x/proj/.venv/bin/python -u -m ios_screen.app -v"
        self.assertTrue(re.search(controller.IOS_PATTERN, line))

    def test_deskflow_pattern_matches_gui_but_not_core(self):
        gui = "/Applications/Deskflow.app/Contents/MacOS/Deskflow"
        core = "/Applications/Deskflow.app/Contents/MacOS/deskflow-core"
        self.assertTrue(re.search(controller.DESKFLOW_PATTERN, gui))
        # 大小写不同，pgrep 也是按字节匹配的，core 不会被当成 GUI
        self.assertIsNone(re.search(controller.DESKFLOW_PATTERN, core))
        self.assertTrue(re.search(controller.DESKFLOW_CORE_PATTERN, core))

    def test_vdisplay_pattern(self):
        line = "/Users/x/proj/native/vdisplay 393 852 1"
        self.assertTrue(re.search(controller.VDISPLAY_PATTERN, line))


class TestProcessControl(unittest.TestCase):
    def setUp(self):
        # exec -a 把 argv[0] 换掉，得到一个只带标记、不碰真实服务的假进程
        self.proc = subprocess.Popen(
            ["/bin/bash", "-c", f'exec -a "{MARK}" sleep 60']
        )
        time.sleep(0.3)

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=3)

    def test_find_by_command_line(self):
        self.assertIn(self.proc.pid, controller.pids(MARK))
        self.assertTrue(controller.is_running(MARK))

    def test_terminate_clears_it(self):
        self.assertTrue(controller.terminate(MARK, timeout=3.0))
        self.assertEqual(controller.pids(MARK), [])

    def test_terminate_is_idempotent(self):
        controller.terminate(MARK, timeout=2.0)
        self.assertTrue(controller.terminate(MARK, timeout=1.0))

    def test_status_snapshot_has_three_flags(self):
        s = controller.Status.read()
        self.assertIsInstance(s.deskflow, bool)
        self.assertIsInstance(s.ios, bool)
        self.assertIsInstance(s.vdisplay, bool)


class TestDesktop(unittest.TestCase):
    def test_install_check_matches_filesystem(self):
        from pathlib import Path
        expected = Path("/Applications/Deskflow.app/Contents/MacOS/Deskflow").is_file()
        self.assertEqual(controller.deskflow_installed(), expected)

    def test_guard_probe_does_not_raise(self):
        self.assertIsInstance(controller.guard_loaded(), bool)


if __name__ == "__main__":
    unittest.main()
