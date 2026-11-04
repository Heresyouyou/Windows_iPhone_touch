# 05 · 给 Windows 端的一封信：iPhone Use 在 Windows 上怎么用

> 读者：运行在 Windows 上的 AI 助手 / 工程师。
> 目的：让你在 10 分钟内搞清楚「能不能用、该怎么用、别在哪些地方浪费时间」。
> 结论先行：**iPhone Use 没有 Windows 版，装不上，也不要试着装。** 但你要做的事有替代路径，本文给到可执行方案。

---

## 一、先确认结论，别急着动手

iPhone Use（上游 `YueChen-C/iPhone-use`）是一个 **macOS 应用**：

| 事实 | 依据 |
|---|---|
| 只发布 macOS universal 包（Apple Silicon 与 Intel 通用） | Releases 里的 `iPhoneUse-v-macOS-universal.zip` |
| 要求 macOS 14 或更高 | 官方快速开始的环境要求表 |
| 用 Swift 6 + AppKit 写的主程序 | 源码从 Swift 6 编译，主程序是桌面窗口 |
| 画面靠 AVFoundation 采集 USB | 相机（CoreMediaIO）权限是必需项 |
| 键鼠靠 CoreBluetooth 提供 BLE HID | 蓝牙权限是必需项，iPhone 侧配对为普通蓝牙键鼠 |

这四件事在 Windows 上**没有一一对应的替代品**：AVFoundation/CoreMediaIO 是苹果私有采集栈，CoreBluetooth 的外设（Peripheral）模式在 Windows 上要靠另一套 API 重写，AppKit 界面更是完全不存在。

所以：`iPhoneUse.app` 在 Windows 上是**无法执行的文件**，不是"缺依赖"。不要尝试 WSL、不要尝试模拟器、不要找 exe —— 它们都不存在。

同类项目里有一个 `semsturgut/open-iphone-use`，README 里写的是 `Windows/Linux: coming soon`，也就是说**截至今天还没有**。可以关注，但不能作为当前方案。

---

## 二、那你到底要选哪条路

先回答一个问题：**你是想"用" iPhone Use，还是想"实现"同样的体验？**

### 路线 A：想「用」——借一台 Mac（现实、省钱、当天可用）

iPhone Use 只跑 Mac，那就让它跑在 Mac 上，Windows 通过三种方式"借用"：

1. **画面**：Mac 那块屏通过 Deskflow / 远程桌面出现在 Windows 桌面上；
2. **控制**：键鼠事件由 Deskflow 送到 Mac，再由 Mac 的 iPhone Use 通过蓝牙发给 iPhone；
3. **脚本 / API**：Windows 上的程序可以 **SSH 端口转发** 直接调 Mac 上那个 `127.0.0.1:7390` 接口。

第 3 条是最容易被忽略但最有用的，具体做法（在 Windows PowerShell 里）：

```powershell
# 把 Mac 上的 7390 映射到本机 7390
ssh -N -L 7390:127.0.0.1:7390 mac@192.168.10.x

# 另开一个窗口，验证
curl.exe -s http://127.0.0.1:7390/status
```

只有 HTTP 会被转发，**HID 输入仍然由 Mac 发出**——Windows 只是"远程下指令"。所以这条路适合"让 Windows 上的自动化脚本操作 iPhone"，不适合"让 Windows 的物理鼠标直接控制 iPhone"。

> 如果你要的正是后者，走路线 B。

### 路线 B：想「实现」——在 Windows 上重写同等体验

可达，但要把四层里的一层换掉。完整评估见 `03-windows-port.md`，这里只给执行顺序：

| 步骤 | 做什么 | 验收标准 |
|---|---|---|
| 1 | 装一个**已签名**的虚拟显示驱动（Parsec VDD / spacedesk） | `QueryDisplayConfig` 能看到第二块屏，鼠标能移进去且不被弹回 |
| 2 | 用 `SetDisplayConfig` 把它摆到主屏右侧 | 光标越过右缘时落点是第二块屏 |
| 3 | 写光标接管：`GetCursorPos` 轮询 + `ShowCursor` 计数配平 | 光标进入该屏时系统指针隐藏，离开时恢复 |
| 4 | 用 WinRT `GattServiceProvider` 发布 HOGP（服务 UUID `0x1812`） | iPhone 设置→蓝牙里出现一个键盘/鼠标设备并能配对 |
| 5 | 把相对位移编码成 HID Report 发出去 | 移动鼠标时 iPhone 屏幕上的指针跟着动 |
| 6 | 画面（可选） | 做不到就做"盲控"，一样能操作 |

**第 5 步是分水岭**。第 1–4 步任何一步失败，后面都不用做；第 4 步最容易失败在蓝牙适配器不支持**外设角色（Peripheral）**——台式机内置的很多只支持 Central。

判断方法：直接调 `GattServiceProvider.CreateAsync` + `StartAdvertising`，抛异常就是不支持，换一个支持双角色的 USB 蓝牙适配器（CSR8510 一类）。

### 路线 C：只想"看见 iPhone 画面 + 简单点按"——用现成商业镜像软件

ApowerMirror、Vysor 这类能镜像画面并做有限交互。它们**不是**本项目的目标形态：

- 不能做到"鼠标越界即进入"，必须在软件窗口里操作；
- 键鼠控制能力受 iOS 限制，很多场景只能点按；
- 闭源、收费、协议不透明。

如果你只是偶尔看一眼手机，这条最快；如果要"像第二块屏一样用"，回到路线 B。

---

## 三、Windows 端的现实能力边界

| 能力 | Windows 能不能 | 说明 |
|---|---|---|
| 看 iPhone 画面 | 部分 | 无官方 USB 采集；要么商业软件，要么自研 AirPlay 接收端 |
| 用物理键鼠控制 iPhone | 能 | BLE HID（HOGP）+ 虚拟屏，纯软件可实现 |
| 让鼠标"越界"进入 iPhone | 能 | 依赖一块真实存在的虚拟显示器 |
| 在 iPhone 上看见鼠标指针 | 能 | iPhone 会把 BLE 鼠标当真实外设，指针是系统级 |
| 剪贴板双向同步 | 不能 | HID 没有剪贴板通道，需要另建数据通道 |

最后一条经常被忽略：**HID 只能传键鼠**。想要剪贴板，得在 iPhone 上跑一个 App 建 socket，而 iOS 不允许后台常驻——这条路基本走不通。

---

## 四、别做的事（省时间）

1. **不要找 iPhone Use 的 Windows 安装包**——不存在，任何声称是的都是假的；
2. **不要用 WSL 跑它**——WSL 里没有 CoreBluetooth / AVFoundation，也没有 AppKit；
3. **不要指望 Phone Link 镜像 iPhone**——微软的屏幕镜像只支持 Android，iPhone 仅限通知/短信/照片；
4. **不要指望"苹果官方 Windows 方案"**——Apple Devices 只做备份同步，不做镜像；
5. **不要为了虚拟屏去写内核驱动**——先用 Parsec VDD 验证整条链路，能跑通再考虑自研。

---

## 五、macOS 侧已经验证过的结论，可以直接抄

这些与操作系统无关，是踩坑换来的，Windows 实现时照做能省掉大部分返工：

- **几何必须闭环**：设置完窗口/显示器位置，一定要回读校验，不一致就重试一次；不要"设完就信"。
- **光标隐藏与恢复必须成对**：程序崩溃、被杀、显示器被撤，都要能恢复系统指针，否则用户会看到一个没有鼠标的桌面。
- **不要自动切换焦点**：为了摆窗口去激活 App 是必要的，但激活完要立刻把焦点还回去，且限制次数——否则用户会遇到"键盘一直被抢"。
- **守护进程只跟随目标 App 的启动/退出**，不要做全局拦截。
- **CF/CoreFoundation 对象的所有权二选一**（macOS 教训，Windows 上是 COM 的 `AddRef`/`Release` 同理）：引用计数错一次，会在运行几十万次之后随机崩溃，且被自动重启放大成"屏幕乱跳"这种难以归因的故障。

---

## 六、给你的第一步（可立即执行）

如果你现在就要动手，按这个顺序，**每一步都能独立验证**：

```powershell
# 1. 确认当前显示器布局（PowerShell 读取）
Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID |
  Select-Object InstanceName, ManufacturerName

# 2. 装 Parsec VDD 后，确认多了一块屏
#    设置 → 系统 → 显示，看是否有"显示 2"

# 3. 确认蓝牙适配器支持 BLE
Get-PnpDevice -Class Bluetooth | Select-Object FriendlyName, Status
```

第 3 步只能确认"有 BLE"，不能确认"支持外设角色"——那个必须用代码试 `GattServiceProvider.StartAdvertising`。

---

## 参考

- 上游：`github.com/YueChen-C/iPhone-use`（macOS 独占）
- 同类（未发布 Windows 版）：`github.com/semsturgut/open-iphone-use`
- 本目录：`03-windows-port.md`（逐层技术方案）、`04-deskflow-integration.md`（与 Deskflow 的关系）
