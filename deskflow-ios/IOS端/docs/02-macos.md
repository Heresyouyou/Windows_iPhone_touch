# 02 · macOS 实现

## 文件

| 文件 | 作用 | 单独可用 |
|---|---|---|
| `psagent.m` | 守护进程，串起全部四层 | 是（主力） |
| `vdisplay.m` | 只建虚拟屏，便于调几何 | 是 |
| `phonescreen.m` | `info` / `place` / `bridge` 三个子命令 | 是 |
| `appctl.m` | 启停 iPhone Use（优雅退出 + 状态） | 是 |
| `disps.m` / `axdump.m` | 打印显示器布局 / AX 窗口树 | 是 |
| `memtest.m` | 双释放回归测试（见「崩溃」一节） | 是 |

编译：

```bash
make            # 全部
make psagent    # 单个
```

## 虚拟屏

`CGVirtualDisplay` 是私有 API，头文件需要自己声明（`vdisplay.m` 里有完整的 `@interface` 声明，用 `NSClassFromString` 拿类，避免链接期符号缺失导致启动即崩）。

创建序列有严格的顺序要求，错一步就得到一个 1×1 的废屏：

```objc
CGVirtualDisplayDescriptor *d = [CGVirtualDisplayDescriptor new];
d.name = @"iPhone Virtual";
d.maxPixelsWide = 509 * 2;    // 必须 ≥ 目标模式的像素尺寸
d.maxPixelsHigh = 905 * 2;
d.sizeInMillimeters = CGSizeMake(509 * 0.264, 905 * 0.264);  // 96 DPI 换算
d.queue = dispatch_get_main_queue();

CGVirtualDisplay *vd = [[CGVirtualDisplayClass alloc] initWithDescriptor:d];
CGVirtualDisplaySettings *s = [CGVirtualDisplaySettings new];
s.hiDPI = 0;                                   // 用 1x，点=像素，省得再算缩放
s.modes = @[ mode ];                           // 不给 modes 就是 1×1
[vd applySettings:s];

// 关键：origin 与 mode 必须在同一个配置事务里设置
CGBeginDisplayConfiguration(&cfg);
CGConfigureDisplayOrigin(cfg, vd.displayID, 1680, 72);
CGConfigureDisplayWithDisplayMode(cfg, vd.displayID, mode, NULL);
CGCompleteDisplayConfiguration(cfg, kCGConfigureForSession);
```

### 两个必须处理的副作用

**（1）虚拟屏会抢主屏。** 新屏加入后，系统有可能把它设成主屏（`CGDisplayIsMain` 变真），菜单栏会跳过去。处理办法：安排完等 400ms 再检查一次，若被抢则重新 `CGConfigureDisplayOrigin` 把主屏指回内置屏。

**（2）内置屏会被降级。** 这是最隐蔽的一个：加入虚拟屏后，内置屏模式从 `1680×1050 @2x` 掉到 `1440×900 @1x`，Retina 直接消失。

根因是 `CGDisplayCopyAllDisplayModes()` 默认**不返回** HiDPI 模式，于是"找同尺寸模式"找不到，系统给了个保底的低分辨率。解法是带选项重新枚举：

```objc
NSDictionary *opts = @{ (__bridge NSString *)kCGDisplayShowDuplicateLowResolutionModes: @YES };
CFArrayRef modes = CGDisplayCopyAllDisplayModes(displayID, (__bridge CFDictionaryRef)opts);
```

而且判定要用 `CGDisplayModeGetPixelWidth()`，不能用 `CGDisplayModeGetWidth()`——HiDPI 模式下 `GetWidth` 返回的是**点数**（1680），不是像素（3360）。

另外：`CGDisplayPixelsWide()` 在这台 Intel 机器上对内置屏报的值偏低，别用它判断当前分辨率，用 `CGDisplayCopyDisplayMode` 读真实模式。

## 窗口摆放

```objc
AXUIElementRef app  = AXUIElementCreateApplication(pid);
AXUIElementCopyAttributeValue(app, kAXWindowsAttribute, &wins);  // 非激活时 count=0
AXUIElementSetAttributeValue(win, kAXMinimizedAttribute, kCFBooleanFalse);
AXUIElementSetAttributeValue(win, kAXPositionAttribute, posValue);  // AXValueCreate(kAXValueCGPointType)
AXUIElementSetAttributeValue(win, kAXSizeAttribute, sizeValue);     // AXValueCreate(kAXValueCGSizeType)
```

顺序不能反：先 `position` 后 `size`。反了的话，窗口会因为"新尺寸放不进目标位置的可用空间"而被系统挪位置。

`setWindowFrame` 之后必须回读 `windowFrame()` 校验，不一致再设一次。**不要**假设设置成功。

### 为什么目标 y 是 97 而不是 72

虚拟屏 bounds 是 `(1680, 72)` 起。但 macOS 在虚拟屏顶部保留了 25px 的菜单栏位置，AX 会把任何 `y=72` 的窗口钳到 `y=97`。

如果代码里硬写 `y=72` 并且拿回读值比较，就会永远判定失败 → 每 4 秒重摆一次 → 日志刷屏 + 窗口抖动。用 `y = screenTop + 25`（或直接读 `visibleFrame` 反算）才是幂等的。

## 光标

```objc
CGEventRef ev = CGEventCreate(NULL);
CGPoint p = CGEventGetLocation(ev);     // 全局坐标，原点在主屏左上
CFRelease(ev);

BOOL inside = CGRectContainsPoint(phoneRect, p) && pointInAppWindow(p, pid);
if (inside && !hidden) { CGDisplayHideCursor(CGMainDisplayID()); hidden = YES; }
if (!inside && hidden) { CGDisplayShowCursor(CGMainDisplayID()); hidden = NO; }
```

`pointInAppWindow` 是必要的：手机画面矩形可能在别的窗口下面（比如你把浏览器拖过去盖住了），此时不该隐藏系统光标。判定用 `CGWindowListCopyWindowInfo` 从顶层往下找第一个包含该点的普通窗口（`kCGWindowLayer == 0`），看它属于谁。

必须跳过 `Window Server`、`Dock`、`Wallpaper`、`SystemUIServer`、`Control Center`、`Notification Center`——否则第一个命中的永远是桌面壁纸，判定恒为"不在 App 里"。

## 崩溃：CoreFoundation 所有权混用

这里出过一次很典型的错，值得单独记：

```objc
CFArrayRef list = CGWindowListCopyWindowInfo(...);   // +1，所有权在我
for (NSDictionary *d in (__bridge_transfer NSArray *)list) { ... }  // 所有权交给 ARC
CFRelease(list);                                     // 又释放一次 ← 双重释放
```

`__bridge_transfer` 已经把这一个引用交给 ARC，函数结束时 ARC 会 release；再手动 `CFRelease` 就是第二次释放。堆被破坏后不会立刻崩，而是在几十万次调用之后随机崩在 `objc_release` 里：

```
EXC_BAD_ACCESS (SIGSEGV), KERN_INVALID_ADDRESS at 0x20
thread 0: objc_release ← pointInAppWindow ← main
```

规则只有一条：**Create/Copy 返回的对象，`__bridge_transfer` 与 `CFRelease` 二选一。**

这个 bug 的杀伤力被 launchd 放大了：`KeepAlive=true` 让它崩溃后 5 秒重启，而每次重启都会重建虚拟屏、重排显示器——用户看到的是"屏幕一直在跳"，很容易误判成和 Deskflow 冲突。

`memtest.m` 就是为此写的回归测试，直接对两个高频函数跑 2500 轮（5000 次调用），崩了就退出码非零：

```bash
make memtest && ./memtest
```

## 指针校准

校准由 iPhone Use 自己做（`POST /calibration/auto`），但它的判定很挑环境：

- **相邻两帧的画面变化必须 ≤ 256 像素**，否则判定"画面在动"直接失败；
- 垂直方向第 16 档固定移动约 1024px，如果那个位置有文字或图标，必然撞上内容变化而失败；
- 结论：**必须在纯色（纯白最好）界面上校准**，且要把光标移开可能的高亮区域。

校准结果按屏幕分别存在：

```
~/Library/Application Support/iPhoneUse/pointer-calibration/by-screen/*.json
```

32 档，每档记录 `(dx, dy) → 实际位移`，曲线在 0.83 → 164.5 px/档之间。手机逻辑分辨率 1180×2556。

一旦校准成功就不要再动，也不要因为它"看起来偏右上"就去改曲线——实测过一次：偏的感觉来自虚拟屏坐标系与 iPhone 坐标系的原点不一致，校准本身是对的。

## 权限

| 权限 | 用途 | 缺失表现 |
|---|---|---|
| 辅助功能 | AX 读写窗口 | `AXIsProcessTrusted()` 为假，窗口列表永远空 |
| 屏幕录制 | 截 iPhone Use 窗口 | 抓出来是纯色（但本项目的键鼠链路**不需要**它） |

## 生命周期

`psagent` 自己不做 daemon 化，交给 launchd：

```xml
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
<key>ThrottleInterval</key><integer>15</integer>
```

`KeepAlive` 只对异常退出生效，且 15 秒节流——即使将来再崩，也不会变成秒级重启风暴把显示器刷来刷去。

安装用 `macos/安装自启动.command`（会写 plist 并 `launchctl bootstrap`）。注意：部分受限环境（含某些沙箱）里 `launchctl bootstrap` 会返回 `EIO`，此时必须由用户在真实终端里执行。
