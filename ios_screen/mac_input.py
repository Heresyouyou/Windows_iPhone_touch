"""macOS 输入原语：CGEventTap、光标、显示器。

为什么这里和 Windows 版长得不一样
----------------------------------
Windows 需要绕一大圈：光标被钳在屏幕边缘后 WM_MOUSEMOVE 的坐标不再变化，
只能用 Raw Input 的 lLastX/lLastY 取设备级计数，再挂一个低级钩子去"吞"事件。

macOS 不需要绕。CGEventTap 拿到的就是事件本身：

    kCGMouseEventDeltaX / DeltaY   设备级位移，与光标是否被钳制、是否被隐藏无关
    回调返回 None                   直接吞掉这条事件，窗口服务器根本收不到

所以在 HID 层（kCGHIDEventTap）挂 tap 并返回 None，光标是真的不动——
这比 Windows 版"吞事件 + 不停把光标 warp 回中心"干净得多。

两个仍然必须做的小事
--------------------
1. **warp 兜底**：如果 tap 只能建在会话层（没有辅助功能权限时我们会退到
   只监听模式），吞掉事件并不能阻止光标移动，这时靠"偏离锚点就 warp 回来"
   纠正。正常路径下这段逻辑一次也不会触发。
2. **warp 事件不算位移**：warp 会生成一条 delta 为 0 的 MouseMoved。
   我们只认 delta，所以天然不会把它当成手在动。
"""
from __future__ import annotations

import ctypes

import Quartz

# 事件 tap 的位置与选项：PyObjC 没有导出这两个选项常量，按 CGEvent.h 定义写死
TAP_LOCATION_HID = 0          # kCGHIDEventTap：事件进入窗口服务器之前
TAP_LOCATION_SESSION = 1      # kCGSessionEventTap：会话层，吞掉时光标已移动过
TAP_HEAD_INSERT = 0           # kCGHeadInsertEventTap
TAP_OPTION_DEFAULT = 0        # kCGEventTapOptionDefault：可修改/可吞
TAP_OPTION_LISTEN_ONLY = 1    # kCGEventTapOptionListenOnly：只观察

# 鼠标按键号 → HID 报文的按钮位（bit0 左、bit1 右、bit2 中）
BUTTON_BITS = {0: 0x01, 1: 0x02, 2: 0x04}

# 修饰键的 CGEventFlags 掩码
FLAG_SHIFT = Quartz.kCGEventFlagMaskShift
FLAG_CONTROL = Quartz.kCGEventFlagMaskControl
FLAG_ALTERNATE = Quartz.kCGEventFlagMaskAlternate
FLAG_COMMAND = Quartz.kCGEventFlagMaskCommand
FLAG_ALPHA_SHIFT = Quartz.kCGEventFlagMaskAlphaShift


# ---------------------------------------------------------------- 辅助功能权限

_hiservices = ctypes.CDLL(
    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
)
_hiservices.AXIsProcessTrusted.restype = ctypes.c_bool


def accessibility_trusted() -> bool:
    """进程是否被授予「辅助功能」。没有它，tap 只能以 listen-only 建立。

    注意这查的是**当前宿主进程**（终端 / iTerm / IDE）的权限，不是本脚本的。
    从不同终端启动会得到不同结果，这不是 bug。
    """
    try:
        return bool(_hiservices.AXIsProcessTrusted())
    except Exception:
        return False


def open_accessibility_settings() -> None:
    import subprocess

    subprocess.run(
        ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
        check=False,
    )


# ---------------------------------------------------------------- 光标


def cursor_pos() -> tuple[float, float]:
    """当前光标位置（点，原点在主屏左上）。"""
    e = Quartz.CGEventCreate(None)
    if e is None:
        return (0.0, 0.0)
    p = Quartz.CGEventGetLocation(e)
    return (float(p.x), float(p.y))


def warp_cursor(x: float, y: float) -> None:
    """把光标挪到指定点（一次性动作，没有常驻状态）。

    只在"退出捕获后放到边缘内侧"的时候用。捕获期间不再需要移动光标：
    位移来自 IOHIDManager 的设备原始报文，与光标在哪毫无关系。
    """
    Quartz.CGWarpMouseCursorPosition((float(x), float(y)))


# 光标是否跟随物理鼠标。曾经在捕获期间被解耦过，现在**永远保持关联**。
_associated = True


def set_cursor_associated(on: bool) -> None:
    """【已停用】不要再用它解耦光标。

    为什么停用
    ----------
    `CGAssociateMouseAndMouseCursorPosition(False)` 是**系统级**开关：解耦之后
    整个系统的指针都不再跟随鼠标。早期版本在捕获期间靠它绕开屏幕边缘的增量
    钳制，代价是——程序只要没恢复成功（崩溃、被强杀、卡在某个回调里），
    用户的鼠标就再也回不到自己屏幕上，只能重启。

    这条路径已经被彻底移除：位移改从 IOHIDManager 的设备原始报文取，跟光标
    位置无关，不需要也不应该动这个开关。

    保留函数只是为了显式拒绝：万一有人（或未来的我）又想调它，会看到一行警告
    而不是把别人的机器搞瘫。解除关联的请求会被静默忽略。
    """
    global _associated
    if not on:
        print(
            "  ⚠️ 拒绝解耦光标：CGAssociateMouseAndMouseCursorPosition(False) "
            "是系统级状态，漏恢复会让整台机器的鼠标失灵。已忽略。",
            flush=True,
        )
        return
    Quartz.CGAssociateMouseAndMouseCursorPosition(True)
    _associated = True


def cursor_associated() -> bool:
    return _associated


def force_associate_cursor() -> None:
    """无条件恢复关联，不看进程内记账。

    给 `--fix-cursor` 用：新进程的记账天然是「已关联」，可系统的真实状态可能还是
    上一次进程被强杀时留下的解耦。只能直接调一次系统 API。
    """
    global _associated
    Quartz.CGAssociateMouseAndMouseCursorPosition(True)
    _associated = True


class CursorGuard:
    """CGDisplayHideCursor 是引用计数，不是布尔开关：必须成对调用，否则光标再也回不来。"""

    def __init__(self) -> None:
        self.hidden = False
        self.hide_calls = 0
        self.show_calls = 0
        self._display = Quartz.CGMainDisplayID()

    def hide(self) -> None:
        if self.hidden:
            return
        Quartz.CGDisplayHideCursor(self._display)
        self.hide_calls += 1
        self.hidden = True

    def show(self) -> None:
        if not self.hidden:
            return
        # 只补一次：多补会把别的程序隐藏的光标也显示出来
        Quartz.CGDisplayShowCursor(self._display)
        self.show_calls += 1
        self.hidden = False


# ---------------------------------------------------------------- 显示器


def list_displays() -> list[dict]:
    """列出所有活动显示器。

    坐标是点（不是像素），原点在主屏左上——和 CGEventGetLocation、
    NSScreen 的可见区域用的是同一套，边缘判定直接比大小即可。
    """
    err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
    if err != 0 or not ids:
        return []
    out: list[dict] = []
    for i, did in enumerate(ids[:count]):
        b = Quartz.CGDisplayBounds(did)
        # CGDisplayPixelsWide 在 Retina 上返回的是**点**，不是像素（这台机器上
        # 1680 点 / 3360 像素却报 1680）。要真像素得读当前的 display mode。
        pw = ph = 0
        try:
            mode = Quartz.CGDisplayCopyDisplayMode(did)
            if mode is not None:
                pw, ph = Quartz.CGDisplayModeGetPixelWidth(mode), Quartz.CGDisplayModeGetPixelHeight(mode)
        except Exception:
            pass
        out.append(
            {
                "id": did,
                "index": i,
                "name": f"Display {i + 1}",
                "x": int(round(b.origin.x)),
                "y": int(round(b.origin.y)),
                "w": int(round(b.size.width)),
                "h": int(round(b.size.height)),
                "primary": bool(Quartz.CGDisplayIsMain(did)),
                "pixels_w": int(pw or Quartz.CGDisplayPixelsWide(did)),
                "pixels_h": int(ph or Quartz.CGDisplayPixelsHigh(did)),
            }
        )
    # 让主屏排在第一个，和 Windows 版 enum_monitors 的习惯一致
    out.sort(key=lambda m: (not m["primary"], m["x"], m["y"]))
    for i, m in enumerate(out):
        m["index"] = i
    return out


def pick_display(selector: str) -> dict | None:
    """按 primary / 序号 / 名字片段 / 分辨率选屏。"""
    mons = list_displays()
    if not mons:
        return None
    s = (selector or "primary").strip().lower()
    if s in ("primary", "main", "0"):
        for m in mons:
            if m["primary"]:
                return m
        return mons[0]
    if s.isdigit():
        i = int(s)
        return mons[i] if 0 <= i < len(mons) else None
    for m in mons:
        if s == f"{m['w']}x{m['h']}" or s == f"{m['pixels_w']}x{m['pixels_h']}":
            return m
    for m in mons:
        if s in m["name"].lower():
            return m
    return None


def describe_displays() -> str:
    lines = []
    for m in list_displays():
        tag = "  [primary]" if m["primary"] else ""
        lines.append(
            f"  [{m['index']}] {m['name']:<12} {m['w']}x{m['h']} 点 "
            f"({m['pixels_w']}x{m['pixels_h']} 像素) at ({m['x']},{m['y']}){tag}"
        )
    return "\n".join(lines)


def has_neighbor(mon: dict, edge: str) -> dict | None:
    """所选边的外侧是否还接着另一块屏。

    接着的话，"贴边 + 继续朝外"这个前提就不成立了——光标会先跑到邻屏上，
    边缘触发永远等不到。启动时直接告诉用户，比让他自己发现要好。
    """
    for m in list_displays():
        if m["id"] == mon["id"]:
            continue
        if edge == "right" and m["x"] == mon["x"] + mon["w"] and _overlaps(
            m["y"], m["h"], mon["y"], mon["h"]
        ):
            return m
        if edge == "left" and m["x"] + m["w"] == mon["x"] and _overlaps(
            m["y"], m["h"], mon["y"], mon["h"]
        ):
            return m
        if edge == "top" and m["y"] + m["h"] == mon["y"] and _overlaps(
            m["x"], m["w"], mon["x"], mon["w"]
        ):
            return m
        if edge == "bottom" and m["y"] == mon["y"] + mon["h"] and _overlaps(
            m["x"], m["w"], mon["x"], mon["w"]
        ):
            return m
    return None


def _overlaps(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 < b0 + b1 and b0 < a0 + a1


# ---------------------------------------------------------------- 事件 tap


class EventTap:
    """全局事件 tap：收鼠标/键盘，捕获期间把事件吞掉。

    回调里只做算术和转发，绝不做 IO——事件 tap 有超时（默认约 1 秒），
    超时后系统会把 tap 关掉（送一条 kCGEventTapDisabledByTimeout）。
    必须重新 enable，否则表现为"用着用着鼠标突然又能动了"。
    """

    def __init__(
        self,
        on_mouse,
        on_button,
        on_scroll,
        on_key,
        should_swallow,
        on_tap_disabled=None,
        verbose=False,
        force_listen_only=False,
    ) -> None:
        self.on_mouse = on_mouse          # (dx, dy) -> None
        self.on_button = on_button        # (bit, down) -> None
        self.on_scroll = on_scroll        # (notches, continuous) -> None
        self.on_key = on_key              # (keycode, down, flags) -> None
        self.should_swallow = should_swallow  # () -> bool
        self.on_tap_disabled = on_tap_disabled
        self.verbose = verbose
        # 只读模式：只观察、绝不吞任何事件。远程键鼠（Deskflow）的位移
        # 只在这里看得见，但绝不能因此改变系统的输入行为。
        self.force_listen_only = bool(force_listen_only)

        self.tap = None
        self.source = None
        self.location = None
        self.listen_only = False
        self.timeouts = 0
        self.swallowed = 0
        # 最近一次按钮/滚轮事件发生的屏幕坐标（顶部原点）。
        # 回调签名保持 (bit, down) 不变——消费方按需读这里，避免破坏既有调用点。
        self.last_button_pos: tuple[float, float] = (0.0, 0.0)
        self.last_scroll_pos: tuple[float, float] = (0.0, 0.0)
        self._cb = self._callback       # 必须持引用，否则回调会被回收
        self._started = False

    # ----- 生命周期 -----

    def start(self) -> bool:
        mask = 0
        for t in (
            Quartz.kCGEventMouseMoved,
            Quartz.kCGEventLeftMouseDragged,
            Quartz.kCGEventRightMouseDragged,
            Quartz.kCGEventOtherMouseDragged,
            Quartz.kCGEventLeftMouseDown,
            Quartz.kCGEventLeftMouseUp,
            Quartz.kCGEventRightMouseDown,
            Quartz.kCGEventRightMouseUp,
            Quartz.kCGEventOtherMouseDown,
            Quartz.kCGEventOtherMouseUp,
            Quartz.kCGEventScrollWheel,
            Quartz.kCGEventKeyDown,
            Quartz.kCGEventKeyUp,
            Quartz.kCGEventFlagsChanged,
        ):
            mask |= 1 << t

        # 先试能吞的 HID tap；没有辅助功能权限时退到只监听，功能降级但不崩
        if self.force_listen_only:
            candidates = (
                (TAP_LOCATION_HID, TAP_OPTION_LISTEN_ONLY),
                (TAP_LOCATION_SESSION, TAP_OPTION_LISTEN_ONLY),
            )
        else:
            candidates = (
                (TAP_LOCATION_HID, TAP_OPTION_DEFAULT),
                (TAP_LOCATION_HID, TAP_OPTION_LISTEN_ONLY),
                (TAP_LOCATION_SESSION, TAP_OPTION_LISTEN_ONLY),
            )
        for location, option in candidates:
            tap = Quartz.CGEventTapCreate(
                location, TAP_HEAD_INSERT, option, mask, self._cb, None
            )
            if tap:
                self.tap, self.location = tap, location
                self.listen_only = option == TAP_OPTION_LISTEN_ONLY
                break
        if not self.tap:
            return False

        self.source = Quartz.CFMachPortCreateRunLoopSource(None, self.tap, 0)
        from CoreFoundation import CFRunLoopAddSource, CFRunLoopGetCurrent, kCFRunLoopDefaultMode

        CFRunLoopAddSource(CFRunLoopGetCurrent(), self.source, kCFRunLoopDefaultMode)
        Quartz.CGEventTapEnable(self.tap, True)
        self._started = True
        return True

    def stop(self) -> None:
        if not self._started:
            return
        try:
            from CoreFoundation import CFRunLoopGetCurrent, CFRunLoopRemoveSource, kCFRunLoopDefaultMode

            if self.source:
                CFRunLoopRemoveSource(CFRunLoopGetCurrent(), self.source, kCFRunLoopDefaultMode)
        except Exception:
            pass
        if self.tap:
            Quartz.CGEventTapEnable(self.tap, False)
        self.tap = None
        self.source = None
        self._started = False

    # ----- 回调 -----

    def _callback(self, proxy, type_, event, refcon):
        # 被系统摘掉时必须重新挂上，否则程序看着还在跑但收不到任何输入
        if type_ in (
            Quartz.kCGEventTapDisabledByTimeout,
            Quartz.kCGEventTapDisabledByUserInput,
        ):
            self.timeouts += 1
            if self.tap:
                Quartz.CGEventTapEnable(self.tap, True)
            if self.on_tap_disabled:
                self.on_tap_disabled(type_)
            return event

        consumed = False
        try:
            if type_ in (
                Quartz.kCGEventMouseMoved,
                Quartz.kCGEventLeftMouseDragged,
                Quartz.kCGEventRightMouseDragged,
                Quartz.kCGEventOtherMouseDragged,
            ):
                dx = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventDeltaX)
                dy = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventDeltaY)
                if dx or dy:
                    consumed = bool(self.on_mouse(int(dx), int(dy)))
            elif type_ in (
                Quartz.kCGEventLeftMouseDown,
                Quartz.kCGEventLeftMouseUp,
                Quartz.kCGEventRightMouseDown,
                Quartz.kCGEventRightMouseUp,
                Quartz.kCGEventOtherMouseDown,
                Quartz.kCGEventOtherMouseUp,
            ):
                num = int(
                    Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventButtonNumber)
                )
                bit = BUTTON_BITS.get(num)
                if bit:
                    down = type_ in (
                        Quartz.kCGEventLeftMouseDown,
                        Quartz.kCGEventRightMouseDown,
                        Quartz.kCGEventOtherMouseDown,
                    )
                    try:
                        p = Quartz.CGEventGetLocation(event)
                        self.last_button_pos = (float(p.x), float(p.y))
                    except Exception:
                        pass
                    consumed = bool(self.on_button(bit, down))
            elif type_ == Quartz.kCGEventScrollWheel:
                # DeltaAxis1 是"行"增量：普通鼠标一格 ±1，触控板是连续小数
                d = Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGScrollWheelEventDeltaAxis1
                )
                continuous = bool(
                    Quartz.CGEventGetIntegerValueField(
                        event, Quartz.kCGScrollWheelEventIsContinuous
                    )
                )
                if d:
                    try:
                        p = Quartz.CGEventGetLocation(event)
                        self.last_scroll_pos = (float(p.x), float(p.y))
                    except Exception:
                        pass
                    consumed = bool(self.on_scroll(float(d), continuous))
            elif type_ in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
                code = int(
                    Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                )
                flags = int(Quartz.CGEventGetFlags(event))
                consumed = bool(self.on_key(code, type_ == Quartz.kCGEventKeyDown, flags))
            elif type_ == Quartz.kCGEventFlagsChanged:
                code = int(
                    Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                )
                flags = int(Quartz.CGEventGetFlags(event))
                consumed = bool(self.on_key(code, True, flags))
        except Exception:
            # 回调里绝不能把异常抛出去：一次异常就可能让 tap 被系统摘掉，
            # 表现为"用一会儿突然失灵"，而且没有任何提示。
            consumed = False

        if (consumed or self.should_swallow()) and not self.listen_only:
            self.swallowed += 1
            return None
        return event
