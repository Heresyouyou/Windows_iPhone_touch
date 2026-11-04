"""Win32 原语：Raw Input、低级鼠标钩子、光标。

为什么用 Raw Input 而不是普通鼠标消息：
  光标撞到屏幕边缘会被内核钳制，WM_MOUSEMOVE 的坐标就不再变化，位移信息丢失。
  Raw Input 的 lLastX/lLastY 是设备级计数，和光标位置无关——FPS 游戏把光标锁在
  屏幕中心还能无限转视角，靠的就是它。这是本方案能不要虚拟屏的根本原因。

为什么低级钩子只负责"吞"不负责"算"：
  WH_MOUSE_LL 回调有 ~300ms 超时，超时系统会静默摘钩，表现为"用着用着突然失灵"。
  所以回调里只置标志、不做任何 IO 和计算，位移一律来自 Raw Input。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

from collections import deque

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _enable_dpi_awareness() -> str:
    """必须在任何窗口/坐标 API 之前调用，否则拿到的是被 DPI 缩放过的逻辑坐标。

    这台机器 125% 缩放：不声明感知时 EnumDisplayMonitors 报 2752x1152，
    实际物理分辨率是 3440x1440。边缘判定会算在 x=2751，而光标真能到 x=3439，
    结果是"推到底也不触发"，且 GetCursorPos 与 Raw Input 的坐标体系对不上。
    """
    try:
        # PER_MONITOR_AWARE_V2 = -4，Win10 1703+
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except Exception:
        pass
    try:
        if user32.SetProcessDPIAware():
            return "system"
    except Exception:
        pass
    return "none"


DPI_MODE = _enable_dpi_awareness()

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

WM_INPUT = 0x00FF
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
WM_HOTKEY = 0x0312

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
VK_Q = 0x51
VK_OEM_PLUS = 0xBB     # 主键盘 =/+，RegisterHotKey 不区分 Shift
VK_OEM_MINUS = 0xBD    # 主键盘 -/_

RID_INPUT = 0x10000003
RIDEV_INPUTSINK = 0x00000100
RIM_TYPEMOUSE = 0

WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
VK_CONTROL = 0x11
VK_MENU = 0x12
WM_MOUSEMOVE = 0x0200
LLMHF_INJECTED = 0x00000001
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wt.USHORT),
        ("usUsage", wt.USHORT),
        ("dwFlags", wt.DWORD),
        ("hwndTarget", wt.HWND),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wt.DWORD),
        ("dwSize", wt.DWORD),
        ("hDevice", wt.HANDLE),
        ("wParam", wt.WPARAM),
    ]


class _BUTTONS(ctypes.Union):
    _fields_ = [
        ("ulButtons", wt.ULONG),
        ("usButtonFlags", wt.USHORT),
        ("usButtonData", wt.USHORT),
    ]


class RAWMOUSE(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("usFlags", wt.USHORT),
        ("u", _BUTTONS),
        ("ulRawButtons", wt.ULONG),
        ("lLastX", wt.LONG),
        ("lLastY", wt.LONG),
        ("ulExtraInformation", wt.ULONG),
    ]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("mouse", RAWMOUSE)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wt.POINT),
        ("mouseData", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)

# ---- 函数签名：不声明 argtypes 在 64 位下会把句柄截断成 32 位 ----

user32.RegisterRawInputDevices.argtypes = [
    ctypes.POINTER(RAWINPUTDEVICE), wt.UINT, wt.UINT
]
user32.RegisterRawInputDevices.restype = wt.BOOL

user32.GetRawInputData.argtypes = [
    wt.HANDLE, wt.UINT, ctypes.c_void_p, ctypes.POINTER(wt.UINT), wt.UINT
]
user32.GetRawInputData.restype = wt.UINT

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = wt.HANDLE
user32.CallNextHookEx.argtypes = [wt.HANDLE, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wt.HANDLE]
user32.UnhookWindowsHookEx.restype = wt.BOOL

user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.RegisterHotKey.restype = wt.BOOL
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wt.BOOL

user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.GetCursorPos.restype = wt.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = wt.SHORT
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wt.BOOL
user32.ShowCursor.argtypes = [wt.BOOL]
user32.ShowCursor.restype = ctypes.c_int

user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
user32.RegisterClassExW.restype = wt.ATOM
user32.CreateWindowExW.restype = wt.HWND
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT),
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
        ("hIconSm", wt.HICON),
    ]


def cursor_pos() -> tuple[int, int]:
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def screen_rect(primary_only: bool = True):
    """返回主屏的 (left, top, width, height)。多屏时取主屏，因为边缘判定是相对主屏的。"""
    SM_CXSCREEN, SM_CYSCREEN = 0, 1
    return (0, 0, user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN))


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", wt.RECT),
        ("rcWork", wt.RECT),
        ("dwFlags", wt.DWORD),
        ("szDevice", wt.WCHAR * 32),
    ]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM
)
user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.c_void_p]
user32.GetMonitorInfoW.restype = wt.BOOL
user32.EnumDisplayMonitors.argtypes = [
    wt.HDC, ctypes.POINTER(wt.RECT), MONITORENUMPROC, wt.LPARAM
]
user32.EnumDisplayMonitors.restype = wt.BOOL


def enum_monitors() -> list[dict]:
    """列出所有 attached 的显示器。szDevice 形如 \\\\.\\DISPLAY1。"""
    out: list[dict] = []

    def cb(hmon, hdc, lprc, _data):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            out.append(
                {
                    "device": mi.szDevice,
                    "x": mi.rcMonitor.left,
                    "y": mi.rcMonitor.top,
                    "w": mi.rcMonitor.right - mi.rcMonitor.left,
                    "h": mi.rcMonitor.bottom - mi.rcMonitor.top,
                    "primary": bool(mi.dwFlags & 0x1),
                }
            )
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    return out


def pick_monitor(selector: str) -> dict | None:
    """按 primary / 序号 / 设备名片段选屏。返回 None 表示没匹配上。"""
    mons = enum_monitors()
    if not mons:
        return None
    s = (selector or "primary").strip().lower()
    if s in ("primary", "0", "main"):
        for m in mons:
            if m["primary"]:
                return m
        return mons[0]
    if s.isdigit():
        i = int(s)
        return mons[i] if 0 <= i < len(mons) else None
    for m in mons:
        if s in m["device"].lower():
            return m
    return None


class CursorGuard:
    """ShowCursor 是引用计数，不是布尔开关：返回调用后的计数，只有 < 0 才真的隐藏。"""

    def __init__(self) -> None:
        self.hidden = False
        self.hide_calls = 0
        self.show_calls = 0

    def hide(self) -> None:
        if self.hidden:
            return
        n = 0
        while True:
            n = user32.ShowCursor(False)
            self.hide_calls += 1
            if n < 0:
                break
            if self.hide_calls > 64:  # 别处把计数抬得离谱时兜底，防止死循环
                break
        self.hidden = True

    def show(self) -> None:
        if not self.hidden:
            return
        while True:
            n = user32.ShowCursor(True)
            self.show_calls += 1
            if n >= 0:
                break
            if self.show_calls > 64:
                break
        self.hidden = False


class RawInputWindow:
    """隐藏窗口，只用来收 WM_INPUT。

    RIDEV_INPUTSINK 让它在不是前台窗口时也能收到输入——否则鼠标一离开本窗口
    就再也收不到位移了。
    """

    CLASS_NAME = "IosScreenRawInput"

    def __init__(self, on_mouse) -> None:
        self._on_mouse = on_mouse  # callable(dx, dy, flags) -> None
        self._wndproc_ref = WNDPROC(self._wndproc)
        self.hwnd = None
        self._msg = wt.MSG()
        self._hotkeys: dict[int, tuple[int, int, object]] = {}

    def create(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = self.CLASS_NAME
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err not in (0, 1410):  # 1410 = 类已注册，重跑时会遇到
                raise ctypes.WinError(err)
        self.hwnd = user32.CreateWindowExW(
            0, self.CLASS_NAME, "ios_screen", 0, 0, 0, 0, 0, None, None, hinst, None
        )
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

        rid = RAWINPUTDEVICE(0x01, 0x02, RIDEV_INPUTSINK, self.hwnd)
        if not user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid)):
            raise ctypes.WinError(ctypes.get_last_error())

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            self._handle_raw(lparam)
            return 0
        if msg == WM_HOTKEY:
            try:
                hk = self._hotkeys.get(int(wparam))
                if hk:
                    hk[2]()
            except Exception:
                pass
            return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def register_hotkey(self, mods: int, vk: int, callback) -> bool:
        """注册全局热键。捕获期间鼠标被吞、光标不动，热键是唯一的兜底出口。"""
        for m, v, _cb in self._hotkeys.values():
            if m == mods and v == vk:
                return True
        kid = 1
        while kid in self._hotkeys:
            kid += 1
        if not user32.RegisterHotKey(self.hwnd, kid, mods, vk):
            return False
        self._hotkeys[kid] = (mods, vk, callback)
        return True

    def unregister_hotkeys(self) -> None:
        for kid in list(self._hotkeys):
            user32.UnregisterHotKey(self.hwnd, kid)
        self._hotkeys.clear()

    def _handle_raw(self, hraw) -> None:
        size = wt.UINT(0)
        hdr_size = ctypes.sizeof(RAWINPUTHEADER)
        if user32.GetRawInputData(hraw, RID_INPUT, None, ctypes.byref(size), hdr_size) == 0xFFFFFFFF:
            return
        if size.value == 0:
            return
        buf = ctypes.create_string_buffer(size.value)
        got = user32.GetRawInputData(hraw, RID_INPUT, buf, ctypes.byref(size), hdr_size)
        if got == 0xFFFFFFFF:
            return
        ri = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
        if ri.header.dwType != RIM_TYPEMOUSE:
            return
        # MOUSE_MOVE_RELATIVE = 0；绝对值模式（触控板/远程桌面）下 lLast 是屏幕坐标，不能用
        if ri.mouse.usFlags & 0x01:
            return
        self._on_mouse(ri.mouse.lLastX, ri.mouse.lLastY, ri.mouse.usButtonFlags)

    def pump_once(self, timeout_ms: int = 50) -> bool:
        """处理一条消息。返回 False 表示收到 WM_QUIT。"""
        r = user32.PeekMessageW(ctypes.byref(self._msg), None, 0, 0, 1)  # PM_REMOVE
        if not r:
            kernel32.Sleep(timeout_ms)
            return True
        if self._msg.message == WM_QUIT:
            return False
        user32.TranslateMessage(ctypes.byref(self._msg))
        user32.DispatchMessageW(ctypes.byref(self._msg))
        return True

    def destroy(self) -> None:
        self.unregister_hotkeys()
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None


class MouseSwallower:
    """捕获期间吞掉鼠标事件，防止指针贴着边缘时点中底下的窗口。

    注意：只在 active 时吞。Idle 状态下必须原样放行，否则用户正常用电脑也会被拦截。
    """

    def __init__(self) -> None:
        self.active = False
        self.hook = None
        self._proc_ref = HOOKPROC(self._proc)
        self.swallowed = 0

    def install(self) -> None:
        self.hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc_ref, None, 0)
        if not self.hook:
            raise ctypes.WinError(ctypes.get_last_error())

    def uninstall(self) -> None:
        if self.hook:
            user32.UnhookWindowsHookEx(self.hook)
            self.hook = None

    def _proc(self, code, wparam, lparam):
        # 回调里绝不能抛异常：任何未捕获异常都会让 Windows 直接把钩子摘掉，
        # 表现是"用一会儿鼠标突然不受控了"，且没有任何报错。
        try:
            if code == HC_ACTION and self.active:
                ms = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                # 自己用 SetCursorPos 触发的事件要放行，否则会把系统自己的定位也吃掉
                if not (ms.flags & LLMHF_INJECTED):
                    self.swallowed += 1
                    return 1
        except Exception:
            pass
        return user32.CallNextHookEx(self.hook, code, wparam, lparam)


class KeyboardForwarder:
    """捕获期间劫持键盘，把按键交给 iPhone。

    和鼠标一样，回调里只做入队——低级钩子有 ~300ms 超时，超时会被系统静默摘钩。
    按键状态机（组合键、6KRO 上限）在主线程里跑，钩子只负责"按下/抬起 + 吞掉"。

    自己的全局热键必须放行，否则 RegisterHotKey 永远收不到（Ctrl+Alt+Q 就失效了）。
    """

    def __init__(self) -> None:
        self.active = False
        self.hook = None
        self._proc_ref = HOOKPROC(self._proc)
        self.events: deque[tuple[int, bool]] = deque(maxlen=128)
        self.swallowed = 0

    def install(self) -> None:
        self.hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc_ref, None, 0)
        if not self.hook:
            raise ctypes.WinError(ctypes.get_last_error())

    def uninstall(self) -> None:
        if self.hook:
            user32.UnhookWindowsHookEx(self.hook)
            self.hook = None

    @staticmethod
    def _is_own_hotkey(vk: int) -> bool:
        def down(key: int) -> bool:
            return bool(user32.GetAsyncKeyState(key) & 0x8000)

        return (down(VK_CONTROL) and down(VK_MENU)
                and vk in (VK_Q, VK_OEM_PLUS, VK_OEM_MINUS))

    def _proc(self, code, wparam, lparam):
        try:
            if code == HC_ACTION and self.active:
                kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if not self._is_own_hotkey(kb.vkCode):
                    down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                    self.events.append((int(kb.vkCode), down))
                    self.swallowed += 1
                    return 1
        except Exception:
            pass
        return user32.CallNextHookEx(self.hook, code, wparam, lparam)
