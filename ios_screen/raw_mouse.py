"""原始鼠标位移（IOHIDManager）。

为什么非要绕过 CGEvent
----------------------
Windows 版用 Raw Input 拿设备原始计数，理由在 macOS 上同样成立，甚至更硬：

  · 光标一旦贴到屏幕边缘，WindowServer 就把该方向的移动吃掉，
    `kCGMouseEventDeltaX` 直接变成 0；
  · `CGWarpMouseCursorPosition` 只是把光标"画"到别处，并不改变系统内部那个
    累加位置。实测 warps=7、归位后回读确实是 (840,525)，但下一个事件就把它
    弹回 (1679,y)，而这一整秒的 dx 合计还是 ≈0。
  · 事件 tap 也拦不住光标移动（tools/tap_probe.py：三种位置/模式全部吞不掉）。

所以位移只能从 HID 报文本身取。IOHIDManager 给的是设备原始值，和屏幕边缘、
光标钳制、事件抑制统统无关。

权限
----
需要「输入监控」(Input Monitoring)：系统设置 → 隐私与安全性 → 输入监控。
没授权时 `IOHIDManagerOpen` 可能照样成功，但一个值都收不到——所以这里会统计
收到的样本数，调用方可以据此提示用户。

用 ctypes 直连框架
------------------
PyObjC 没有 IOKit 的绑定，只能 dlopen。参数里出现的 CF 类型（run loop、
mode 字符串）也一并从 CoreFoundation 取裸指针，避免 PyObjC 包装层的类型转换问题。
"""
from __future__ import annotations

import ctypes

_IOKIT = "/System/Library/Frameworks/IOKit.framework/IOKit"
_CF = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"

# Generic Desktop 页里的 X / Y
_USAGE_PAGE_GENERIC_DESKTOP = 0x01
_USAGE_X = 0x30
_USAGE_Y = 0x31

_kIOReturnSuccess = 0

# 回调原型：void (*)(void *context, IOReturn result, void *sender, IOHIDValueRef value)
# 必须原样交给 ctypes 当 argtype 用。声明成 c_void_p 的话，Python 函数指针
# 会被当成一个整数地址传过去，系统照收但永远回调不到——现象就是"设备匹配到了、
# 打开也成功，却一个样本都没有"。
VALUE_CALLBACK = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p
)

# IOHIDCheckAccess / IOHIDRequestAccess 的返回码
_ACCESS_NAMES = {0: "已授权", 1: "被拒绝", 2: "未决定"}
_kIOHIDRequestTypeListenEvent = 1


class RawMouse:
    """鼠标原始位移。每次收发都走 HID 报文，不受光标位置影响。"""

    def __init__(self) -> None:
        self._iokit = None
        self._cf = None
        self._manager = None
        self._cb = None            # 必须持引用，否则回调会被回收
        self._mode = None
        self._runloop = None
        self.dx = 0
        self.dy = 0
        self.samples = 0           # 收到的 X/Y 样本数，用来判断权限是否到位
        self.callbacks = 0         # 回调被调用的总次数（不区分元素）
        self.seen: set[tuple[int, int]] = set()   # 见过的 (usage page, usage)
        self.opened = False
        self.open_status = None
        self.access = None         # IOHIDCheckAccess 的返回码
        self.last_error = ""

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> bool:
        try:
            self._iokit = ctypes.CDLL(_IOKIT)
            self._cf = ctypes.CDLL(_CF)
        except OSError as e:
            self.last_error = f"加载框架失败: {e}"
            return False

        ik = self._iokit
        ik.IOHIDManagerCreate.restype = ctypes.c_void_p
        ik.IOHIDManagerCreate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        ik.IOHIDManagerRegisterInputValueCallback.restype = None
        ik.IOHIDManagerRegisterInputValueCallback.argtypes = [
            ctypes.c_void_p, VALUE_CALLBACK, ctypes.c_void_p,
        ]
        ik.IOHIDManagerScheduleWithRunLoop.restype = None
        ik.IOHIDManagerScheduleWithRunLoop.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]
        ik.IOHIDManagerOpen.restype = ctypes.c_int32
        ik.IOHIDManagerOpen.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        ik.IOHIDValueGetElement.restype = ctypes.c_void_p
        ik.IOHIDValueGetElement.argtypes = [ctypes.c_void_p]
        ik.IOHIDElementGetUsagePage.restype = ctypes.c_uint32
        ik.IOHIDElementGetUsagePage.argtypes = [ctypes.c_void_p]
        ik.IOHIDElementGetUsage.restype = ctypes.c_uint32
        ik.IOHIDElementGetUsage.argtypes = [ctypes.c_void_p]
        ik.IOHIDValueGetIntegerValue.restype = ctypes.c_long
        ik.IOHIDValueGetIntegerValue.argtypes = [ctypes.c_void_p]
        # 指针参数一定要显式声明，否则 ctypes 会按 int 传，64 位指针被截断
        ik.IOHIDManagerUnscheduleFromRunLoop.restype = None
        ik.IOHIDManagerUnscheduleFromRunLoop.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]
        ik.IOHIDManagerClose.restype = ctypes.c_int32
        ik.IOHIDManagerClose.argtypes = [ctypes.c_void_p, ctypes.c_uint32]

        self._cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
        self._cf.CFRunLoopGetCurrent.argtypes = []
        self._cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        self._cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
        ]

        self._runloop = self._cf.CFRunLoopGetCurrent()
        # kCFStringEncodingUTF8 = 0x08000100
        self._mode = self._cf.CFStringCreateWithCString(
            None, b"kCFRunLoopDefaultMode", 0x08000100
        )

        # 先问权限。没有「输入监控」时 IOHIDManagerOpen 往往照样成功，
        # 只是一个值都收不到——不主动问一下，现象会非常难判断。
        ik.IOHIDCheckAccess.restype = ctypes.c_int
        ik.IOHIDCheckAccess.argtypes = [ctypes.c_int]
        ik.IOHIDRequestAccess.restype = ctypes.c_bool
        ik.IOHIDRequestAccess.argtypes = [ctypes.c_int]
        try:
            self.access = ik.IOHIDCheckAccess(_kIOHIDRequestTypeListenEvent)
            if self.access == 2:      # 未决定：主动弹一次系统授权框
                ik.IOHIDRequestAccess(_kIOHIDRequestTypeListenEvent)
                self.access = ik.IOHIDCheckAccess(_kIOHIDRequestTypeListenEvent)
        except Exception:
            self.access = None

        ik.IOHIDManagerCopyDevices.restype = ctypes.c_void_p
        ik.IOHIDManagerCopyDevices.argtypes = [ctypes.c_void_p]
        self._cf.CFSetGetCount.restype = ctypes.c_long
        self._cf.CFSetGetCount.argtypes = [ctypes.c_void_p]

        self._manager = ik.IOHIDManagerCreate(None, 0)
        if not self._manager:
            self.last_error = "IOHIDManagerCreate 返回空"
            return False

        # 匹配所有设备，在回调里按 usage page/usage 过滤。
        # 用 kIOHIDDeviceUsagePageKey/Usage 只匹鼠标会漏掉一部分"指针"设备，
        # 这里宁可多收再筛，反正 X/Y 的 usage 只有 0x30/0x31。
        ik.IOHIDManagerSetDeviceMatching.restype = None
        ik.IOHIDManagerSetDeviceMatching.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        ik.IOHIDManagerSetDeviceMatching(self._manager, None)

        self._cb = VALUE_CALLBACK(self._on_value)
        ik.IOHIDManagerRegisterInputValueCallback(self._manager, self._cb, None)
        ik.IOHIDManagerScheduleWithRunLoop(self._manager, self._runloop, self._mode)

        status = ik.IOHIDManagerOpen(self._manager, 0)
        self.open_status = status
        self.opened = status == _kIOReturnSuccess
        if not self.opened:
            self.last_error = f"IOHIDManagerOpen 返回 0x{status & 0xFFFFFFFF:08X}"
        return self.opened

    def stop(self) -> None:
        if self._manager and self._iokit:
            self._iokit.IOHIDManagerUnscheduleFromRunLoop(
                self._manager, self._runloop, self._mode
            )
            self._iokit.IOHIDManagerClose(self._manager, 0)
        self._manager = None
        self._cb = None

    # ---------------------------------------------------------------- 回调

    def _on_value(self, context, result, sender, value) -> None:
        try:
            self.callbacks += 1
            elem = self._iokit.IOHIDValueGetElement(value)
            if not elem:
                return
            page = self._iokit.IOHIDElementGetUsagePage(elem)
            usage = self._iokit.IOHIDElementGetUsage(elem)
            if len(self.seen) < 40:
                self.seen.add((int(page), int(usage)))
            if page != _USAGE_PAGE_GENERIC_DESKTOP:
                return
            if usage == _USAGE_X:
                self.dx += int(self._iokit.IOHIDValueGetIntegerValue(value))
                self.samples += 1
            elif usage == _USAGE_Y:
                self.dy += int(self._iokit.IOHIDValueGetIntegerValue(value))
                self.samples += 1
        except Exception:
            # HID 回调里绝不能抛异常
            pass

    # ---------------------------------------------------------------- 取用

    def take(self) -> tuple[int, int]:
        """取走并清零累积位移（调用方通常每拍取一次）。"""
        dx, dy = self.dx, self.dy
        self.dx = self.dy = 0
        return dx, dy

    @property
    def access_name(self) -> str:
        if self.access is None:
            return "未知"
        return _ACCESS_NAMES.get(self.access, f"?({self.access})")

    @property
    def device_count(self) -> int:
        if not self._manager or not self._iokit:
            return -1
        try:
            dev = self._iokit.IOHIDManagerCopyDevices(self._manager)
            if not dev:
                return 0
            return int(self._cf.CFSetGetCount(dev))
        except Exception:
            return -1

    @property
    def status(self) -> str:
        if not self.opened:
            return f"打不开({self.last_error})"
        base = f"输入监控={self.access_name} 设备={self.device_count}"
        if self.samples == 0:
            return f"{base} 但没有数据"
        return f"{base} {self.samples} 样本"
