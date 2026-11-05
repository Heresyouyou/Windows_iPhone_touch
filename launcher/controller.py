"""启动台背后的两件事：把 Deskflow 和 Mac iPhone Touch 开起来 / 关掉。

这里只碰进程，不 import AppKit —— 界面层可以有 bug，进程层必须能被单独测。

为什么不用 launchd 守护
-----------------------
守护是「Deskflow 在，就把 iPhone 接入拉起来」的常驻逻辑，适合不想看见它的人。
启动台是「我要用的时候手动开、关窗就全收掉」，主动权在人手里。两套同时跑
会互相打架（守护会把刚被启动台关掉的本体再拉起来），所以界面上检测到守护
在跑时会提示解除。
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN_SH = ROOT / "run.sh"
IOS_APP = ROOT / "dist" / "Mac iPhone Touch.app"
DESKFLOW_APP = Path("/Applications/Deskflow.app")

DESKFLOW_BIN = "Deskflow.app/Contents/MacOS/Deskflow"
DESKFLOW_CORE_BIN = "Deskflow.app/Contents/MacOS/deskflow-core"

# pgrep -f 用的是扩展正则，点号要转义，否则会匹配到任意字符
IOS_PATTERN = r"ios_screen\.app"
DESKFLOW_PATTERN = r"Deskflow\.app/Contents/MacOS/Deskflow"
DESKFLOW_CORE_PATTERN = r"Deskflow\.app/Contents/MacOS/deskflow-core"
VDISPLAY_PATTERN = r"native/vdisplay"


# ---------------------------------------------------------------- 进程查询


def pids(pattern: str) -> list[int]:
    """按完整命令行匹配进程。pgrep 找不到时返回 1，这里统一成空列表。"""
    try:
        out = subprocess.run(
            ["/usr/bin/pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            timeout=2.0,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(t) for t in out.split() if t.isdigit()]


def is_running(pattern: str) -> bool:
    return bool(pids(pattern))


def _kill(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def terminate(pattern: str, timeout: float = 6.0) -> bool:
    """先 TERM 再 KILL。返回是否真的清干净了。

    TERM 而不是先发 Apple Event：`tell application "Deskflow" to quit` 会触发
    一次「自动化」授权弹窗，而启动台每次开机都要用，多一个可失败的外部依赖
    不值得。Deskflow 的设置是即时落盘的，SIGTERM 不会丢配置。
    """
    found = pids(pattern)
    if not found:
        return True
    for pid in found:
        _kill(pid, signal.SIGTERM)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pids(pattern):
            return True
        time.sleep(0.1)

    for pid in pids(pattern):
        _kill(pid, signal.SIGKILL)
    time.sleep(0.2)
    return not pids(pattern)


# ---------------------------------------------------------------- 两个功能


def deskflow_installed() -> bool:
    return (DESKFLOW_APP / "Contents" / "MacOS" / "Deskflow").is_file()


def deskflow_running() -> bool:
    return is_running(DESKFLOW_PATTERN)


def ios_running() -> bool:
    return is_running(IOS_PATTERN)


def start_deskflow(wait: float = 15.0) -> bool:
    if deskflow_running():
        return True
    if not deskflow_installed():
        raise RuntimeError("没找到 /Applications/Deskflow.app")

    # open 是异步的：它把启动请求交给 LaunchServices 就返回了，进程可能
    # 要一两秒才出现，所以这里等它真的出现在进程表里再算成功。
    subprocess.run(["open", "-a", str(DESKFLOW_APP)], check=False, timeout=15)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if deskflow_running():
            return True
        time.sleep(0.2)
    return False


def stop_deskflow(timeout: float = 8.0) -> bool:
    ok = terminate(DESKFLOW_PATTERN, timeout)
    # GUI 正常退出会带走 core；异常退出时 core 可能残留，它才是真正占着
    # 端口和剪贴板通道的那个，必须一并收掉。
    terminate(DESKFLOW_CORE_PATTERN, 4.0)
    return ok


def start_ios(wait: float = 20.0) -> bool:
    """经 run.sh 启动本体。

    不能直接 open .app：部分参数（--edge/--panel）要走 launch.args 文件，
    而 run.sh 就是干这个的。默认参数足够启动台使用。
    """
    if ios_running():
        return True
    if not RUN_SH.is_file():
        raise RuntimeError(f"没找到 {RUN_SH}")
    if not IOS_APP.is_dir():
        raise RuntimeError("还没打包，先跑 ./make_app.sh")

    subprocess.run([str(RUN_SH), "--new"], check=False, timeout=20)

    # 首次启动要过 TCC、加载虚拟屏，比 Deskflow 慢，给足 20 秒
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if ios_running():
            return True
        time.sleep(0.2)
    return False


def stop_ios(timeout: float = 10.0) -> bool:
    ok = terminate(IOS_PATTERN, timeout)
    # 万一虚拟屏没跟着父进程退，桌面上会留一块黑屏，补一刀
    terminate(VDISPLAY_PATTERN, 4.0)
    return ok


def stop_all(timeout: float = 10.0) -> None:
    """并行收两个。串行最坏要 18 秒，关窗时等这么久会被以为卡死。"""
    jobs = [
        threading.Thread(target=stop_ios, kwargs={"timeout": timeout}, daemon=True),
        threading.Thread(target=stop_deskflow, daemon=True),
    ]
    for t in jobs:
        t.start()
    for t in jobs:
        t.join(timeout + 5)


def start_all() -> bool:
    """并行起两个。任何一个没起来就返回 False，界面上看得到谁没亮。"""
    result = {"deskflow": False, "ios": False}

    def run(key, fn):
        try:
            result[key] = bool(fn())
        except Exception:
            result[key] = False

    jobs = [
        threading.Thread(target=run, args=("deskflow", start_deskflow), daemon=True),
        threading.Thread(target=run, args=("ios", start_ios), daemon=True),
    ]
    for t in jobs:
        t.start()
    for t in jobs:
        t.join(45)

    return result["deskflow"] and result["ios"]


# ---------------------------------------------------------------- 守护检测


GUARD_LABEL = "work.kust.macphonetouch.guard"


def guard_loaded() -> bool:
    """launchd 里是否装着"跟随 Deskflow"的守护。

    装了的话它会和启动台抢方向盘：启动台把本体关掉，守护下一轮又拉起来。
    """
    try:
        r = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{GUARD_LABEL}"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def guard_script() -> Path:
    return ROOT / "tools" / "guard_ctl.sh"


def remove_guard() -> bool:
    script = guard_script()
    if not script.is_file():
        return False
    r = subprocess.run(["/bin/bash", str(script), "uninstall"],
                       capture_output=True, text=True, timeout=20)
    return r.returncode == 0


# ---------------------------------------------------------------- 状态快照


@dataclass
class Status:
    deskflow: bool
    ios: bool
    vdisplay: bool

    @classmethod
    def read(cls) -> "Status":
        return cls(
            deskflow=deskflow_running(),
            ios=ios_running(),
            vdisplay=is_running(VDISPLAY_PATTERN),
        )
