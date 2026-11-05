"""启动台：一个窗口，两个开关，关窗即全部收掉。

设计上的几个取舍
----------------
· 复选框就是开关本身 —— 勾上代表"现在开着"，而不是"下次启动时开"。
  状态每秒从进程表回读，谁在外面把进程杀了，界面也会自己跟上。
· 启停都放到后台线程。停本体最坏要等 10 秒（看门狗 + 虚拟屏），
  在主线程里做会让窗口变成一张不动图片。
· 关窗就退出、退出就收掉两个功能，这是用户要的语义。代价是 Deskflow
  如果本来就在跑也会被一起关掉，所以界面上写清楚了。

为什么要拆成两个类
------------------
pyobjc 会把 NSObject 子类里的每个方法都当成 Objective-C selector，
`_label(self, text, rect, ...)` 这种普通辅助方法会直接抛 BadPrototypeError。
所以界面逻辑放普通类，NSObject 只保留真正需要被 Cocoa 调用的那几个回调。
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time

import objc
from AppKit import (
    NSApplication,
    NSApp,
    NSBackingStoreBuffered,
    NSButton,
    NSButtonTypeSwitch,
    NSColor,
    NSFont,
    NSFontWeightSemibold,
    NSMakeRect,
    NSInsetRect,
    NSMenu,
    NSMenuItem,
    NSBezierPath,
    NSObject,
    NSRunLoop,
    NSRunLoopCommonModes,
    NSTextAlignmentRight,
    NSTextField,
    NSTimer,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskTitled,
    NSApplicationActivationPolicyRegular,
)

from . import controller

W, H = 460.0, 380.0


def _c(name: str):
    return getattr(NSColor, name)()


class CardView(NSView):
    """圆角卡片。自绘而不是给 layer 设背景色 —— layer 只认 CGColor 裸指针，
    系统切换深浅色时不会跟着变，还会抛出 PyObjCPointer 警告。"""

    def drawRect_(self, rect):
        r = NSInsetRect(self.bounds(), 0.5, 0.5)
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 12.0, 12.0)
        _c("controlBackgroundColor").setFill()
        path.fill()
        # 浅色下白卡片贴着浅灰底几乎看不见，一条极淡的分隔线把层次拉出来
        _c("separatorColor").setStroke()
        path.setLineWidth_(1.0)
        path.stroke()


class LauncherUI:
    """窗口、控件与状态同步。不继承 NSObject，方法名不受 pyobjc 约束。"""

    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.busy: str | None = None
        self.note = ""
        self.note_until = 0.0
        self.quit_requested = False
        self.guard_hint = False
        self._last = (None, None)
        self._controls_enabled = None
        self.build()

    # ------------------------------------------------------------ 构建

    def build(self) -> None:
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
        )
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), style, NSBackingStoreBuffered, False
        )
        win.setTitle_("启动台")
        win.setReleasedWhenClosed_(False)
        win.setBackgroundColor_(_c("windowBackgroundColor"))
        win.center()
        self.win = win

        root = win.contentView()
        root.addSubview_(self._label("启动台", (24, 332, 300, 26), 20,
                                     _c("labelColor"), NSFontWeightSemibold))
        root.addSubview_(self._label("一起管 Deskflow 和 iPhone 接入",
                                     (24, 306, 380, 18), 12,
                                     _c("secondaryLabelColor")))

        self.card_deskflow = self._card(root, 204)
        self.card_ios = self._card(root, 108)

        installed = controller.deskflow_installed()
        self.btn_deskflow = self._switch(
            self.card_deskflow, "Deskflow", (16, 44, 210, 22), "toggleDeskflow:"
        )
        if not installed:
            self.btn_deskflow.setEnabled_(False)
        self.card_deskflow.addSubview_(self._label(
            "Windows / Linux 共用一套键盘鼠标",
            (16, 20, 280, 18), 11.5, _c("secondaryLabelColor")))
        self.txt_deskflow = self._label(
            "未安装" if not installed else "已停止",
            (250, 45, 146, 20), 12, _c("tertiaryLabelColor"),
            align=NSTextAlignmentRight,
        )
        self.card_deskflow.addSubview_(self.txt_deskflow)

        self.btn_ios = self._switch(
            self.card_ios, "iPhone 接入", (16, 44, 210, 22), "toggleIos:"
        )
        self.card_ios.addSubview_(self._label(
            "把 iPhone 当成主屏右边的一块屏",
            (16, 20, 280, 18), 11.5, _c("secondaryLabelColor")))
        self.txt_ios = self._label(
            "已停止", (250, 45, 146, 20), 12, _c("tertiaryLabelColor"),
            align=NSTextAlignmentRight,
        )
        self.card_ios.addSubview_(self.txt_ios)

        self.txt_note = self._label("关闭启动台会同时关闭上面两项",
                                    (24, 74, 300, 18), 11.5,
                                    _c("tertiaryLabelColor"))
        root.addSubview_(self.txt_note)

        self.btn_guard = NSButton.alloc().initWithFrame_(NSMakeRect(330, 70, 106, 26))
        self.btn_guard.setTitle_("解除自动守护")
        self.btn_guard.setFont_(NSFont.systemFontOfSize_(11.5))
        self.btn_guard.setBezelStyle_(1)
        self.btn_guard.setTarget_(self.delegate)
        self.btn_guard.setAction_("removeGuard:")
        self.btn_guard.setHidden_(True)
        root.addSubview_(self.btn_guard)

        self.btn_quit = NSButton.alloc().initWithFrame_(NSMakeRect(340, 22, 96, 32))
        self.btn_quit.setTitle_("全部关闭")
        self.btn_quit.setFont_(NSFont.systemFontOfSize_(13))
        self.btn_quit.setBezelStyle_(1)
        self.btn_quit.setTarget_(self.delegate)
        self.btn_quit.setAction_("stopAll:")
        self.btn_quit.setEnabled_(False)
        root.addSubview_(self.btn_quit)

        win.makeKeyAndOrderFront_(None)

        self.tick()
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self.delegate, "tick:", None, True
        )
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        self.timer = timer

    # ------------------------------------------------------------ 小部件

    def _label(self, text, rect, size, color, weight=None, align=0):
        f = NSTextField.alloc().initWithFrame_(NSMakeRect(*rect))
        f.setStringValue_(text)
        f.setBezeled_(False)
        f.setDrawsBackground_(False)
        f.setEditable_(False)
        f.setSelectable_(False)
        f.setAlignment_(align)
        f.setUsesSingleLineMode_(True)
        if weight is None:
            f.setFont_(NSFont.systemFontOfSize_(size))
        else:
            f.setFont_(NSFont.systemFontOfSize_weight_(size, weight))
        f.setTextColor_(color)
        return f

    def _card(self, parent, y):
        v = CardView.alloc().initWithFrame_(NSMakeRect(24, y, W - 48, 86))
        parent.addSubview_(v)
        return v

    def _switch(self, parent, title, rect, action):
        b = NSButton.alloc().initWithFrame_(NSMakeRect(*rect))
        b.setButtonType_(NSButtonTypeSwitch)
        b.setTitle_(title)
        b.setFont_(NSFont.systemFontOfSize_(13.5))
        b.setTarget_(self.delegate)
        b.setAction_(action)
        b.setState_(0)
        parent.addSubview_(b)
        return b

    # ------------------------------------------------------------ 刷新

    def tick(self) -> None:
        if self.quit_requested:
            NSApp.terminate_(None)
            return

        self._sync_guard_hint()

        if self.busy:
            self._refresh_note()
            return

        # 上一次操作把控件禁掉了，这里必须恢复，否则点一次之后整个窗口就点不动了
        self._enable_controls(True)

        d = controller.deskflow_running()
        i = controller.ios_running()

        # 只在变化时写控件：每秒无条件 setState 会让 switch 反复重绘，看着像在闪
        if d != self._last[0]:
            self.btn_deskflow.setState_(1 if d else 0)
            self._last = (d, self._last[1])
        if i != self._last[1]:
            self.btn_ios.setState_(1 if i else 0)
            self._last = (self._last[0], i)

        if not controller.deskflow_installed():
            self._status(self.txt_deskflow, "未安装", _c("systemRedColor"))
        elif d:
            self._status(self.txt_deskflow, "● 运行中", _c("systemGreenColor"))
        else:
            self._status(self.txt_deskflow, "○ 已停止", _c("tertiaryLabelColor"))

        if i:
            self._status(self.txt_ios, "● 运行中", _c("systemGreenColor"))
        else:
            self._status(self.txt_ios, "○ 已停止", _c("tertiaryLabelColor"))

        self.btn_quit.setEnabled_(d or i)
        self._refresh_note()

    def _status(self, field, text, color) -> None:
        if field.stringValue() != text:
            field.setStringValue_(text)
        field.setTextColor_(color)

    def _refresh_note(self) -> None:
        if self.note and time.monotonic() < self.note_until:
            self.txt_note.setStringValue_(self.note)
            self.txt_note.setTextColor_(_c("systemOrangeColor"))
            return
        if self.guard_hint:
            self.txt_note.setStringValue_("检测到自动守护在跑，会和启动台抢方向盘")
            self.txt_note.setTextColor_(_c("systemOrangeColor"))
            return
        self.txt_note.setStringValue_("关闭启动台会同时关闭上面两项")
        self.txt_note.setTextColor_(_c("tertiaryLabelColor"))

    def _sync_guard_hint(self) -> None:
        loaded = controller.guard_loaded()
        if loaded == self.guard_hint:
            return
        self.guard_hint = loaded
        self.btn_guard.setHidden_(not loaded)

    # ------------------------------------------------------------ 开关

    def toggle_deskflow(self, sender) -> None:
        if sender.state() == 1:
            self._begin("正在启动 Deskflow…", controller.start_deskflow)
        else:
            self._begin("正在关闭 Deskflow…", controller.stop_deskflow)

    def toggle_ios(self, sender) -> None:
        if sender.state() == 1:
            self._begin("正在启动 iPhone 接入…", controller.start_ios)
        else:
            self._begin("正在关闭 iPhone 接入…", controller.stop_ios)

    def stop_all(self) -> None:
        if self.busy:
            return
        self._begin("正在全部关闭…", controller.stop_all)

    def remove_guard(self) -> None:
        if self.busy:
            return
        self._begin("正在解除自动守护…", controller.remove_guard)

    # ------------------------------------------------------------ 后台执行

    def _begin(self, note: str, fn) -> None:
        if self.busy:
            return
        self.busy = note
        self.note = note
        self.note_until = time.monotonic() + 60
        self._enable_controls(False)
        threading.Thread(target=self._work, args=(fn,), daemon=True).start()

    def _work(self, fn) -> None:
        ok = False
        msg = ""
        try:
            ok = bool(fn())
        except Exception as e:  # 界面不能因为一个异常就死掉
            msg = f"{type(e).__name__}: {e}"
        finally:
            # 只在这里写、在主线程读，GIL 下足够
            self.busy = None
            if ok:
                self.note = ""
                self.note_until = 0.0
            else:
                self.note = msg or "操作没有成功，看 ~/Library/Logs/MacPhoneTouch.log"
                self.note_until = time.monotonic() + 10

    def _enable_controls(self, enabled: bool) -> None:
        if self._controls_enabled == enabled:
            return
        self._controls_enabled = enabled
        self.btn_deskflow.setEnabled_(enabled and controller.deskflow_installed())
        self.btn_ios.setEnabled_(enabled)
        self.btn_quit.setEnabled_(False)
        self.btn_guard.setEnabled_(enabled)

    # ------------------------------------------------------------ 退出

    def shutdown(self) -> None:
        try:
            self.win.orderOut_(None)
        except Exception:
            pass
        print("启动台退出，收掉 Deskflow 与 iPhone 接入…", flush=True)
        controller.stop_all(timeout=8)
        print("已全部关闭。", flush=True)


# ---------------------------------------------------------------- Cocoa 代理


class AppDelegate(NSObject):
    def init(self):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self.ui = LauncherUI(self)
        return self

    # 下面每个方法都是一个 selector，只做转发
    def tick_(self, timer):
        self.ui.tick()

    def toggleDeskflow_(self, sender):
        self.ui.toggle_deskflow(sender)

    def toggleIos_(self, sender):
        self.ui.toggle_ios(sender)

    def stopAll_(self, sender):
        self.ui.stop_all()

    def removeGuard_(self, sender):
        self.ui.remove_guard()

    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return True

    def applicationWillTerminate_(self, note):
        self.ui.shutdown()


# ---------------------------------------------------------------- 入口


def _install_menu() -> None:
    bar = NSMenu.alloc().init()
    item = NSMenuItem.alloc().init()
    bar.addItem_(item)
    sub = NSMenu.alloc().init()
    item.setSubmenu_(sub)
    sub.addItemWithTitle_action_keyEquivalent_(
        "关于启动台", "orderFrontStandardAboutPanel:", "")
    sub.addItem_(NSMenuItem.separatorItem())
    sub.addItemWithTitle_action_keyEquivalent_("隐藏启动台", "hide:", "h")
    sub.addItem_(NSMenuItem.separatorItem())
    sub.addItemWithTitle_action_keyEquivalent_("退出启动台", "terminate:", "q")
    NSApp.setMainMenu_(bar)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 双击 .app 时 LaunchServices 会塞一个 -psn_0_12345，argparse 会当成非法参数
    argv = [a for a in argv if not a.startswith("-psn_")]

    ap = argparse.ArgumentParser(description="Deskflow 与 iPhone 接入的启动台")
    ap.add_argument("--self-test", type=float, default=0.0,
                    help="N 秒后自动退出（验证退出清理是否生效）")
    args = ap.parse_args(argv)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    _install_menu()

    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)

    # 从终端 kill 时也走一遍清理；SIGKILL 抓不到，那是没办法的事
    def _sig(signum, frame):
        delegate.ui.quit_requested = True

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(s, _sig)
        except (ValueError, OSError):
            pass

    app.activateIgnoringOtherApps_(True)

    if args.self_test > 0:
        def _bye():
            time.sleep(args.self_test)
            delegate.ui.quit_requested = True

        threading.Thread(target=_bye, daemon=True).start()

    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
