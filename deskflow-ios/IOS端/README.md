# IOS 端 · 把 iPhone 当作一块可越界进入的屏幕

这个目录存放「Mac/PC 键鼠越过屏幕边缘直接控制 iPhone」的实现与设计。

核心目标只有一个：**鼠标从显示器右缘移出，光标落进 iPhone 画面；键盘同时跟随**。不需要在 iPhone 上装任何 App，也不需要把 iPhone 投屏到主显示器上遮挡工作区。

当前状态：

| 平台 | 状态 | 说明 |
|---|---|---|
| macOS | **已跑通** | `macos/` 下的实现，Intel + macOS 14 实测稳定 |
| Windows | **底座已移植** | 平台无关部分已写成 C++17 并通过 42 项单测；Win32 外壳与 HOGP 待实现，见 `docs/06-windows-handoff.md` |
| Linux | 未验证 | 虚拟屏可用 Xvfb/Wayland 虚拟输出，HID 层可移植 |

> Windows 端请直接读 **`docs/06-windows-handoff.md`**：那是开工单，不是评估报告。

---

## 和 Deskflow 是什么关系

**并存，不替代。** Deskflow 负责「跨机器」搬运键鼠，本目录负责「跨设备形态」——把 iPhone 变成主机的一块屏。两者在事件流上首尾相接：

```
物理鼠标 → Deskflow（可选，跨机时介入）→ 主机光标 → 越界 → 本目录 → BLE HID → iPhone
```

需要注意 Deskflow 现行架构的一个事实：master 分支（截至 2026-11）已经**没有** `IPlatformScreen` 这类按屏抽象，866 个源文件里搜不到任何 `*Screen` 类，模型收敛为「一台机器 = 一个 `PlatformComputer`」，多显示器由各平台层（`MSWindowsDesks`、`OSXComputer`）内部处理。

这意味着 iPhone **不能**作为一个 Deskflow client 接入——iOS 上跑不了 Deskflow 客户端。正确的集成位置是**主机侧的虚拟屏幕后端**，详见 `docs/04-deskflow-integration.md`。

---

## 四层架构

整套东西被拆成四层，每层可独立替换，这也是它能跨平台复用的原因：

| 层 | 职责 | macOS | Windows |
|---|---|---|---|
| 采集 | 拿到 iPhone 画面 | iPhone Use（USB + AVFoundation） | **无开源等价物** |
| 虚拟显示 | 造一块屏外屏 | `CGVirtualDisplay`（私有 API） | IddCx 间接显示驱动 |
| 窗口 | 把画面窗口钉在那块屏上 | `AXUIElement` | `SetWindowPos` |
| 注入 | 把事件送进 iPhone | CoreBluetooth HOGP | WinRT `GattServiceProvider` |

**跨平台结论**：架构通用，代码不通。四层里「虚拟显示」和「注入」可以照着重写，只有「采集」在 Windows 上是真障碍。

---

## 目录

```
IOS端/
├── README.md                     本文件
├── docs/
│   ├── 01-architecture.md        四层拆解 + 事件流 + 关键约束
│   ├── 02-macos.md               macOS 实现细节与踩过的坑
│   ├── 03-windows-port.md        Windows 逐层等价实现与硬门槛
│   ├── 04-deskflow-integration.md 如何真正合入 Deskflow
│   ├── 05-windows-iphone-use.md  给 Windows 端的交接：iPhone Use 能不能用、怎么替代
│   └── 06-windows-handoff.md     Windows 重构开工单（读这份动手）
├── macos/                        Objective-C 实现（已跑通）
│   ├── psagent.m                 守护进程：检测 App → 建屏 → 摆窗 → 接管光标
│   ├── vdisplay.m                单独建虚拟屏（调试用）
│   ├── phonescreen.m             单独摆窗口 / 桥接光标（调试用）
│   └── ...
├── windows/                      C++ 移植底座
│   ├── include/                  报文、校准、几何、光标计数的对外契约
│   ├── src/                      四个已测核心 + main_win.cpp + hogp_winrt.cpp
│   ├── tests/                    平台无关单测（42 项断言）
│   └── build.sh                  不依赖 cmake 的验证命令
└── protocol/                     iPhone Use 本地 HTTP 协议与校准格式
```

## 快速开始（macOS）

```bash
cd macos
make                     # 编译全部工具
./start.sh               # 启动守护进程（前台，Ctrl-C 结束）
./安装自启动.command       # 或安装为登录自启
```

前置条件：

1. 安装 [iPhone Use](https://iphoneuse.app)（App Store），用 USB 连上 iPhone，完成 HID 配对与指针校准；
2. 给终端 / 本程序授予「辅助功能」权限（摆窗口要用 AX）；
3. 屏幕布局里给右侧留出空间——虚拟屏会贴在主屏右缘外侧。

几何布局（iPhone 15 Pro）：

```
虚拟屏  509×905   (1680, 72)
窗口    509×880   (1680, 97)   ← 顶部 25px 是被系统菜单栏占掉的
手机画面 393×852  (1680,125)   ← 越界判定就用这个矩形
```

## 已知限制

- `CGVirtualDisplay` 是私有 API，系统升级可能失效；App Store 分发不可用，只能本地编译运行。
- 加虚拟屏会把内置屏模式挤到 1440×900，`ensureBuiltinMode()` 会在 1 秒内纠回 1680×1050，期间可能看到一次闪动。
- iPhone Use 窗口的 `sharingState = 0`，`screencapture` 抓出来是纯色占位——**这不影响键鼠链路**，只是截不到图。
- 指针校准对画面变化敏感：相邻两帧变化超过 256 像素就判定失败，必须在纯色界面上校准。

## 许可

`macos/` 下的代码按仓库根目录的 LICENSE 授权。`CGVirtualDisplay` 等私有 API 的使用风险由使用者自行承担。
