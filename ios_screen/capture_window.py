"""捕获期间承接键盘与点击的一层透明窗口。

为什么不用事件 tap 吞事件
-------------------------
这是本项目最贵的一条教训。

把输入"吞掉"最直觉的做法是 `CGEventTap` 回调返回 None。但它有两个致命问题：

1. 本机实测吞不掉（HID / Session 三个位置、可修改 / 只监听两种模式都试过，
   `swallowed` 在涨，光标照走）。既然吞不掉，就没有任何理由去吞。
2. 更要紧的是**安全**：一旦真的吞掉了，程序稍有卡顿，整台机器的键盘就再也
   不响——连自救的快捷键都收不到，用户只能重启。而"记得在退出时恢复"这种
   约定，在崩溃、被强杀、卡在 C 回调里的时候全都靠不住。

所以这里改成"抢焦点"：捕获期间铺一层透明全屏窗口，让它成为 key window。
键盘和点击自然落到这层窗口上，落不到别的应用里去。它没有任何全局副作用：

    进程退出（包括被 watchdog 强杀）→ 窗口随之消失 → 输入立刻恢复。

这是**构造上**安全的，不依赖任何清理代码跑得通。

为什么还需要一层窗口而不是干脆不管
----------------------------------
不管的话，捕获期间点击会打到光标底下的真实应用上（访达、浏览器……），
在 iPhone 上点一下，Mac 上就可能删了个文件。这层窗口把点击挡住并转给 iPhone。
"""
from __future__ import annotations

import time

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivateAllWindows,
    NSApplicationActivateIgnoringOtherApps,
    NSBackingStoreBuffered,
    NSColor,
    NSScreen,
    NSScreenSaverWindowLevel,
    NSView,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWorkspace,
)
from Foundation import NSMakeRect

from .mac_input import BUTTON_BITS


class _OverlayWindow(NSWindow):
    """无边框窗口默认不能成为 key window，必须显式放行。"""

    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return True


class _OverlayView(NSView):
    """把鼠标键、滚轮、键盘全部转给 owner。"""

    def initWithFrame_owner_(self, frame, owner):
        self = objc.super(_OverlayView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._owner = owner
        return self

    def acceptsFirstResponder(self):
        return True

    def isOpaque(self):
        return False

    # ----- 鼠标按键 -----

    def mouseDown_(self, event):
        self._owner.button(event, 0, True)

    def mouseUp_(self, event):
        self._owner.button(event, 0, False)

    def rightMouseDown_(self, event):
        self._owner.button(event, 1, True)

    def rightMouseUp_(self, event):
        self._owner.button(event, 1, False)

    def otherMouseDown_(self, event):
        self._owner.button(event, 2, True)

    def otherMouseUp_(self, event):
        self._owner.button(event, 2, False)

    # 拖动期间系统会送 dragged 而不是 moved；这里不需要位移（位移走原始 HID），
    # 但必须实现，否则默认实现会把事件丢给下一响应者。
    def mouseDragged_(self, event):
        pass

    def rightMouseDragged_(self, event):
        pass

    def otherMouseDragged_(self, event):
        pass

    def mouseMoved_(self, event):
        pass

    def scrollWheel_(self, event):
        self._owner.scroll(event)

    # ----- 键盘 -----

    def keyDown_(self, event):
        self._owner.key(event, True)

    def keyUp_(self, event):
        self._owner.key(event, False)

    def flagsChanged_(self, event):
        # 修饰键只有 flagsChanged，按下/抬起由 flags 决定，交给 core 判断
        self._owner.key(event, True)

    def performKeyEquivalent_(self, event):
        """Cmd 组合键也要转给 iPhone，不能让它们走菜单/系统默认行为。"""
        self._owner.key(event, True)
        return True


class CaptureWindow:
    """全屏透明抓取层。show/hide 幂等。"""

    def __init__(
        self,
        on_button,
        on_scroll,
        on_key,
        verbose=False,
        frame_provider=None,
    ) -> None:
        self.on_button = on_button      # (bit, down) -> None
        self.on_scroll = on_scroll      # (delta, continuous) -> None
        self.on_key = on_key            # (keycode, down, flags) -> None
        self.verbose = verbose
        # () -> (x, y, w, h) | None。返回 None 时用所有屏幕的并集。
        #
        # 虚拟屏模式下必须绕开虚拟屏：实测 CGVirtualDisplay 那块屏上的窗口
        # **收不到鼠标事件**（窗口 frame 盖住了、hitTest 也命中，但 mouseDown
        # 就是不派发；同一位置的点击，只读 tap 却看得一清二楚）。
        # 既然盖不住，就别盖——让它穿透到虚拟屏的空桌面，点击由 tap 转发。
        self.frame_provider = frame_provider

        self._win = None
        self._view = None
        self._prev_app = None
        self.shown = False
        self._restore_at = None
        self._restore_tries = 0

    # ------------------------------------------------------------ 构建

    def _target_rect(self):
        if self.frame_provider is not None:
            try:
                r = self.frame_provider()
                if r is not None:
                    return NSMakeRect(r[0], r[1], r[2], r[3])
            except Exception:
                pass

        # 覆盖所有屏幕的并集：只盖主屏的话，光标跑到第二块屏上点击就漏出去了
        x0 = y0 = float("inf")
        x1 = y1 = float("-inf")
        for screen in NSScreen.screens():
            f = screen.frame()
            x0 = min(x0, float(f.origin.x))
            y0 = min(y0, float(f.origin.y))
            x1 = max(x1, float(f.origin.x) + float(f.size.width))
            y1 = max(y1, float(f.origin.y) + float(f.size.height))
        if x1 <= x0 or y1 <= y0:
            x0, y0, x1, y1 = 0.0, 0.0, 1920.0, 1080.0
        return NSMakeRect(x0, y0, x1 - x0, y1 - y0)

    def _ensure(self) -> None:
        if self._win is not None:
            return

        union = self._target_rect()

        win = _OverlayWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            union, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
        )
        win.setOpaque_(False)
        win.setBackgroundColor_(NSColor.clearColor())
        win.setLevel_(NSScreenSaverWindowLevel)
        win.setHasShadow_(False)
        win.setIgnoresMouseEvents_(False)
        win.setAcceptsMouseMovedEvents_(True)
        win.setReleasedWhenClosed_(False)
        win.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorIgnoresCycle
        )

        view = _OverlayView.alloc().initWithFrame_owner_(union, self)
        win.setContentView_(view)

        self._win = win
        self._view = view

    # ------------------------------------------------------------ 显示

    def _sync_frame(self) -> None:
        """让窗口跟上 frame_provider 的当前答案。

        虚拟屏是运行中才建起来的，窗口可能先用并集建过一次。不跟着改的话，
        窗口会一直盖着那块屏——盖了也没用（那上面收不到鼠标事件），
        还会挡住 tap 之外的正常穿透。
        """
        if self.frame_provider is None or self._win is None:
            return
        want = self._target_rect()
        f = self._win.frame()
        if (
            abs(f.origin.x - want.origin.x) < 0.5
            and abs(f.origin.y - want.origin.y) < 0.5
            and abs(f.size.width - want.size.width) < 0.5
            and abs(f.size.height - want.size.height) < 0.5
        ):
            return
        self._win.setFrame_display_(want, True)
        if self._view is not None:
            self._view.setFrame_(
                NSMakeRect(0.0, 0.0, want.size.width, want.size.height)
            )
        if self.verbose:
            print(
                f"  [win] 抓取层改盖 ({want.origin.x:.0f},{want.origin.y:.0f}) "
                f"{want.size.width:.0f}x{want.size.height:.0f}",
                flush=True,
            )

    def show(self) -> None:
        if self.shown:
            return
        self._ensure()
        self._sync_frame()
        # 记下原来最前面的应用，退出时还给它——捕获期间焦点被我们抢走了
        try:
            self._prev_app = NSWorkspace.sharedWorkspace().frontmostApplication()
        except Exception:
            self._prev_app = None

        app = NSApplication.sharedApplication()
        try:
            app.unhide_(None)
        except Exception:
            pass
        app.activateIgnoringOtherApps_(True)
        self._win.makeKeyAndOrderFront_(None)
        self._win.makeFirstResponder_(self._view)
        self.shown = True
        if self.verbose:
            print("  [win] 抓取层已上屏（键盘/点击转给 iPhone）", flush=True)

    def ensure_key(self) -> bool:
        """确保抓取层真的是 key window。

        为什么要每拍重试：`activateIgnoringOtherApps:` 是**异步**的，调用返回时
        应用还没激活，此刻 makeKeyAndOrderFront 只是把窗口摆上去，它并不会成为
        key window，于是键盘照旧发给别人。定时器每 10ms 补一次，激活一完成就抢到。

        这里不能在函数里自己 pump 事件——本函数由定时器调用，pump 会把定时器
        再打进来一次，递归。
        """
        if not self.shown or self._win is None:
            return False
        app = NSApplication.sharedApplication()
        if self._win.isKeyWindow() and app.isActive():
            return True
        if not app.isActive():
            try:
                app.activateIgnoringOtherApps_(True)
            except Exception:
                pass
        self._win.makeKeyAndOrderFront_(None)
        self._win.makeFirstResponder_(self._view)
        return bool(self._win.isKeyWindow())

    def hide(self) -> None:
        if not self.shown:
            return
        self.shown = False
        try:
            self._win.orderOut_(None)
        except Exception:
            pass
        # 焦点交还必须延后：orderOut 要等 run loop 处理完一轮，这一拍调 hide_
        # 会被直接忽略（实测 isActive 仍是 True）。交给 tick() 分几步做完。
        self._restore_at = time.monotonic()
        self._restore_tries = 0
        if self.verbose:
            print("  [win] 抓取层已撤下", flush=True)

    def tick(self) -> None:
        """主循环每拍调用：把激活状态还给原来的应用。

        为什么要分步：`orderOut:` 是异步入队的，紧接着调 `hide_` 时窗口还是
        key window，系统直接把这次 hide 丢掉，应用继续占着"最前面"却没有窗口——
        键盘就发给了一个不存在的响应者，用户看到的就是"键盘失灵"。

        所以每隔 50ms 重试一次，直到 `isActive` 变 False，最多四次。
        """
        if self._restore_at is None:
            return
        now = time.monotonic()
        if now - self._restore_at < 0.05:
            return
        self._restore_at = now
        self._restore_tries += 1

        app = NSApplication.sharedApplication()
        prev = self._prev_app
        try:
            app.hide_(None)
            if (
                prev is not None
                and not prev.isTerminated()
                and prev.processIdentifier() != app.processIdentifier()
            ):
                prev.activateWithOptions_(
                    NSApplicationActivateIgnoringOtherApps | NSApplicationActivateAllWindows
                )
        except Exception:
            pass

        if not app.isActive() or self._restore_tries >= 4:
            self._restore_at = None
            self._prev_app = None
            if self.verbose:
                print(
                    f"  [win] 焦点已交还（{'是' if not app.isActive() else '仍未'}，"
                    f"试了 {self._restore_tries} 次）",
                    flush=True,
                )

    def close(self) -> None:
        self.hide()
        if self._win is not None:
            try:
                self._win.setContentView_(None)
                self._win.close()
            except Exception:
                pass
            self._win = None
            self._view = None

    # ------------------------------------------------------------ 事件转换

    def button(self, event, num: int, down: bool) -> None:
        bit = BUTTON_BITS.get(num)
        if bit is None:
            return
        try:
            self.on_button(bit, down)
        except Exception:
            pass

    def scroll(self, event) -> None:
        try:
            dy = float(event.scrollingDeltaY())
            if dy == 0.0:
                return
            if event.hasPreciseScrollingDeltas():
                # 触控板给的是像素，换算成"行"的量级，好和鼠标共用同一条曲线
                dy /= 8.0
            self.on_scroll(dy, bool(event.hasPreciseScrollingDeltas()))
        except Exception:
            pass

    def key(self, event, down: bool) -> None:
        try:
            self.on_key(int(event.keyCode()), down, int(event.modifierFlags()))
        except Exception:
            pass
