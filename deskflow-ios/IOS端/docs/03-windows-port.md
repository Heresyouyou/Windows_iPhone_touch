# 03 · Windows 移植评估

**结论先说**：架构 100% 可复用，代码基本不可复用。四层里两层能平移（虚拟屏、注入），一层是工作量大但路径清晰（窗口/光标），**一层目前没有开源等价物（采集）**。

先回答那个最实际的问题：如果只要求"鼠标移出右缘就去控制 iPhone"，**不要求在 Windows 上看见 iPhone 画面**，那 Windows 是能做的；如果要连画面一起复刻，Windows 上缺一块拼图。

---

## 逐层对照

| 层 | macOS | Windows 等价 | 难度 |
|---|---|---|---|
| 虚拟屏 | `CGVirtualDisplay` | IddCx 间接显示驱动 | 高（需驱动开发 + 签名） |
| 窗口 | `AXUIElement` | `SetWindowPos` / UIAutomation | 低 |
| 光标 | `CGDisplayHideCursor` | `ShowCursor` + 低级鼠标钩子 | 中 |
| 注入 | CoreBluetooth HOGP | WinRT `GattServiceProvider` | 中 |
| 采集 | AVFoundation + USB | **无** | 见下 |

---

## L2 虚拟屏：IddCx

Windows 没有用户态的"创建显示器"API。要凭空造一块屏，只有一条路：写一个 **IddCx（Indirect Display Driver）** 驱动，向系统注册一个虚拟显示适配器。

三个可选起点：

| 方案 | 性质 | 说明 |
|---|---|---|
| `IddSampleDriver`（微软官方样例） | 用户态 UMDF 驱动 | 结构最简单，改 EDID 就能得到任意分辨率 |
| `VirtualDisplayDriver`（开源） | 社区维护的 IddCx 实现 | 已支持自定义分辨率/刷新率，可参考 |
| `usbmmidd`（旧方案） | WDDM 时代的虚拟适配器 | 老系统可用，新系统不稳，不建议 |

**硬门槛是签名**：

- 开发调试：`bcdedit /set testsigning on`，重启后进测试签名模式（桌面右下角会有水印）；
- 正式使用：驱动需要 EV 代码签名证书 + 微软 attestation 签名，个人开发者成本不低；
- 替代思路：让用户装一个已经签名的第三方虚拟显示驱动（如 Parsec VDD、spacedesk 的驱动），本程序只负责"识别并驱动它"，就不必自己签名。**这是最务实的路径。**

装好之后，几何部分和 macOS 完全同构：虚拟屏出现在 `EnumDisplayMonitors` / `QueryDisplayConfig` 里，把它 `SetDisplayConfig` 摆到主屏右侧，光标就天然能移过去——**不需要任何边缘检测代码**，Windows 原生就允许光标走进任意显示器。

参考：`QueryDisplayConfig` + `SetDisplayConfig(QDC_VIRTUAL_MODE_AWARE)` 设置坐标，坐标体系与 macOS 不同，是**物理像素**且原点在主屏左上，没有"点"的概念，算几何时不需要乘缩放因子。

## L3 窗口

```c
SetWindowPos(hwnd, HWND_TOP, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE);
GetWindowRect(hwnd, &rc);   // 含边框
DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, &rc, sizeof(rc));  // 不含阴影
```

比 macOS 简单的地方：**不需要激活窗口也能设置位置**，所以 macOS 那套"激活预算 / 焦点归还"的逻辑在 Windows 上可以整个删掉。

要注意的地方：

- **DPI 感知必须开 PerMonitorV2**，否则在 150% 缩放的屏上算出来的客户区尺寸是错的；
- 找窗口用 `EnumWindows` + `GetWindowThreadProcessId`，或者 UIAutomation；不需要辅助功能授权；
- 窗口被最小化时 `SetWindowPos` 无效，要先 `ShowWindow(SW_RESTORE)`。

## L4 光标

```c
GetCursorPos(&pt);
while (ShowCursor(FALSE) >= 0);   // 返回值是计数器，必须循环到负数
SetCursorPos(x, y);
ClipCursor(&rc);                  // 可选：把光标锁在虚拟屏内
```

Windows 的坑在于 `ShowCursor` 是**引用计数**而不是布尔开关，配平逻辑要自己维护（记一个 `hiddenCount`，恢复时循环 `while (ShowCursor(TRUE) < 0);`）。

要"接管"指针位移，需要全局低级钩子：

```c
SetWindowsHookEx(WH_MOUSE_LL, proc, GetModuleHandle(NULL), 0);
// 回调里判断 pt 是否落在手机区；是则吞掉事件并交给 HID 层
// 用 (ms->flags & LLMHF_INJECTED) 区分真实输入与注入输入，避免自己吃掉自己的事件
```

比 macOS 麻烦的点：macOS 那边"光标进入虚拟屏"之后由 iPhone Use 自己抓位移，本程序只管隐藏指针；Windows 上你得自己把**相对位移**算出来送给 HID 层，因为虚拟屏上没有内容，绝对坐标对 iPhone 没有意义。

## L5 注入：WinRT GATT

iPhone 只接受两种外部键鼠：**MFi 认证的配件** 或 **标准蓝牙 HID（HOGP）**。iPhone Use 走的是后者：把 Mac 伪装成一个 BLE HID 设备。

Windows 10 1809 起提供了 `Windows.Devices.Bluetooth.GenericAttributeProfile.GattServiceProvider`，可以发布自定义 GATT 服务——这就是 HOGP 的实现基础：

```csharp
var provider = await GattServiceProvider.CreateAsync(hidServiceUuid);   // 0x1812
var mouseInput = await provider.Service.CreateCharacteristicAsync(
    hidReportUuid,                                // 0x2A4D
    new GattLocalCharacteristicParameters {
        CharacteristicProperties = GattCharacteristicProperties.Read
                                 | GattCharacteristicProperties.Notify,
        UserDescription = "HID Report"
    });
provider.StartAdvertising(new GattServiceProviderAdvertisingParameters {
    IsConnectable = true,
    IsDiscoverable = true
});
```

需要额外实现的：

- `HID Information`（0x2A4A）、`Report Map`（0x2A4B）、`Protocol Mode`（0x2A4E）、`Boot Keyboard Input Report` 等必选特征；
- `Report Map` 里描述鼠标（相对坐标 + 3 键）和键盘（标准 6KRO 或 consumer control）；
- iPhone 侧配对时会在系统设置里出现一个"键盘/鼠标"设备，和真实蓝牙键鼠一致。

风险点：

- Windows 作为 BLE 外设（Peripheral）要求蓝牙适配器支持**外设角色**，很多台式机/老适配器只支持 Central，`GattServiceProvider.StartAdvertising` 会直接抛异常；
- 需要 `bluetooth` capability（打包成 MSIX/UWP 时），普通 Win32 进程用 WinRT API 需要处理 COM 初始化；
- 与 iPhone 的配对是一次性的，但 iPhone 会缓存绑定信息，换 Report Map 后要在 iPhone 上"忽略此设备"重配。

## L1 采集：这块缺

macOS 上能拿到 iPhone 画面，是因为 iPhone Use 通过 USB 调用了苹果的私有采集通道（CoreMediaIO 层）。Windows 上没有对应物：

| 可能路线 | 现实情况 |
|---|---|
| 苹果官方 API | 不存在。Apple Devices / iTunes 在 Windows 上不提供画面镜像 |
| Phone Link | 屏幕镜像只支持 Android，iPhone 仅限通知/短信/照片 |
| 逆向 USB 私有服务 | ApowerMirror、3uTools 等商业软件走的是这条路，协议未公开，法律与稳定性风险都高 |
| AirPlay 接收端 | iOS 的屏幕镜像走 AirPlay；Windows 上需要自己实现 AirPlay receiver（FairPlay 握手 + H.264 解码），开源实现（如 UxPlay）主要在 macOS/Linux |
| **不采集** | 只做 HID 注入，盲控。看不到画面，但"越界就控制"这件事成立 |

所以 Windows 的落地建议是**分两阶段**：

1. **先做 HID + 虚拟屏**：光标越过右缘即控制 iPhone（盲控），键盘同时生效。做一个常驻托盘程序就能交付，价值明确。
2. **再解决画面**：如果一定要看得见，优先评估把 iPhone 画面用 AirPlay 投到那块虚拟屏上（自研 receiver），而不是继续啃 USB。

---

## 如果真要在 Windows 上动手，第一步做什么

按依赖顺序，前三步都是可独立验证的：

1. 装一个已签名的虚拟显示驱动（Parsec VDD 或 spacedesk），确认能在主屏右侧摆出第二块屏、光标能移进去、`QueryDisplayConfig` 能正确报坐标；
2. 用 `SetWindowsHookEx(WH_MOUSE_LL)` 写一个最小程序，只在光标位于第二块屏时把事件吞掉并打印相对位移；
3. 用 `GattServiceProvider` 发布一个只有鼠标 Report 的 HOGP 服务，用 iPhone 配对，验证能推动指针。

这三步任何一步不通，后面的都不用做。第 3 步最容易失败在蓝牙适配器不支持外设角色上。

## 跨平台可以共享的部分

真正能复用的不是代码，而是**设计决定**：

- 几何闭环（设完必回读校验）；
- 光标隐藏与恢复成对出现；
- 不做自动焦点切换；
- 守护进程只跟随目标 App 的启动/退出，不做全局拦截；
- 虚拟屏被撤掉时必须能干净还原主屏模式。

这几条与操作系统无关，`docs/01-architecture.md` 里的状态机可以直接照搬。
