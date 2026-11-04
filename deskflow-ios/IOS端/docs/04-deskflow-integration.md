# 04 · 与 Deskflow 的集成

## 先看清 Deskflow 现在的模型

截至 2026-11 的 `deskflow/deskflow` master（C++，默认分支 `master`）：

```
src/
├── apps/             server / client / gui 入口
└── lib/
    ├── base/         事件队列、日志等基础设施
    ├── deskflow/     协议与核心对象
    │   ├── Computer.h / IComputer.h
    │   ├── PlatformComputer.h / IPlatformComputer.h
    │   ├── IPrimaryComputer.h / ISecondaryComputer.h
    │   ├── ServerApp.h / ClientApp.h / App.h
    │   └── Clipboard / KeyState / KeyMap ...
    └── platform/     各平台实现
        ├── MSWindowsComputer / MSWindowsDesks / MSWindowsHook / MSWindowsKeyState
        ├── OSXComputer / OSXKeyState / OSXClipboard
        └── EiComputer / EiKeyState / EiEventQueueBuffer
```

一个关键事实：**这个版本里没有 `Screen` / `IPlatformScreen` 这类按屏抽象**。遍历全仓库 866 个文件，只有 `IScreenSaver` 和三个平台的 `*ScreenSaver`，没有任何屏幕类。模型是「**一台机器 = 一个 `PlatformComputer`**」，多显示器由平台层内部消化（Windows 交给 `MSWindowsDesks`，macOS 交给 `OSXComputer`）。

这个事实直接决定了集成方式。

## 为什么 iPhone 不能做成 Deskflow 的 client

Deskflow 的 client 需要跑一个能收发协议、注入本机事件的进程。iOS 是封闭系统：没有后台常驻进程模型，无法装这种客户端。

所以别考虑"给 iPhone 编译一个 deskflow-core"。这条路不通。

## 三种集成粒度

### A. 外部进程（当前实现，零侵入）· 推荐

`psagent` 作为独立程序运行，通过私有 API 造虚拟屏、用 AX 摆窗口、靠 iPhone Use 注入 HID。Deskflow 完全不知道它的存在。

| 优点 | 缺点 |
|---|---|
| 不碰 Deskflow 一行代码 | 两套东西各管一段，语义上"割裂" |
| Deskflow 升级不受影响 | 事件流无法统一调度（例如跨机与跨设备的优先级） |
| 现在就能用 | 用户要装两个东西 |

对这个需求，A 其实已经够了——因为**虚拟屏在操作系统层面就是一块真屏**，Deskflow 的 server 只要把光标移到那台机器的这块屏上，剩下的事自然发生。

### B. 作为同一 Computer 的伪显示器（中等侵入）

不改协议，只在 `src/lib/platform/` 里让平台层"多认一块屏"：

- 显示器枚举时把虚拟屏算进去；
- 光标落入该屏时，把鼠标/键盘事件旁路到一个 `IInputSink` 后端，而不是走本机 `SendInput` / `CGEventPost`。

改动面大致是：

```
src/lib/platform/MSWindowsDesks.cpp     ← 显示器枚举 + 事件分发
src/lib/platform/OSXComputer.mm         ← 同上
src/lib/deskflow/PlatformComputer.h     ← 增加一个可选的输入后端
新增 src/lib/platform/IosHidSink.{h,cpp} ← HOGP 注入实现
```

难点在于这个"旁路"会污染核心事件路径：键盘状态（`KeyState` 的按下/释放表）要和 HID 后端同步，否则会出现"切回来之后 Shift 卡住"这类经典问题。

### C. 新平台后端 `IOSComputer`（语义最正，工作量最大）

把 iPhone 当成一台"机器"，但不是网络对端，而是**本地 HID 设备**。需要实现 `IPlatformComputer` 的全套能力：键盘、鼠标、剪贴板、屏保、显示器信息……

这个方案的吸引力在于模型干净：Deskflow 的配置里就是多了一台机器，屏幕布局图里就是多了一个方块。代价是 iPhone 没有剪贴板通道（除非再实现 HID 之外的私有通道），大部分接口只能是空实现——而"大部分方法是 no-op 的平台后端"通常很难被上游接受。

## 我的建议

**短期用 A，长期争取 B 的最小版本。** 理由：

1. A 已经能交付价值，且不引入维护负担；
2. B 只要能旁路"光标位于虚拟屏时的相对位移 + 键盘事件"，就足以覆盖 95% 的使用场景，改动可控；
3. C 在剪贴板这一环上先天残缺，投入产出比最差。

## 如果要提 PR，建议的提交顺序

先把"能跑的东西 + 文档"作为一个独立目录进来（也就是本目录），不碰 `src/`。这样上游 review 成本最低，也方便别人复现。

```
IOS端/                 ← 第一阶段：只加目录，零侵入
src/lib/platform/...   ← 第二阶段：真正接入事件路径，单独 PR
```

第二阶段之前必须先解决的两个问题，否则 PR 大概率被拒：

| 问题 | 说明 |
|---|---|
| 私有 API | `CGVirtualDisplay` 未公开，苹果随时可能改；上游通常不接受依赖私有 API 的核心路径 |
| 平台不对等 | macOS 有虚拟屏私有 API，Windows 要装签名驱动，Linux 走 Xvfb/wayland 虚拟输出——三套实现无法统一，容易变成"只有 macOS 能用"的功能 |

绕开第一个问题的办法是把虚拟屏做成**可插拔的 provider**：Deskflow 只要求"存在一块位于右侧的显示器"，由用户自行准备（macOS 用本目录的工具，Windows 用 Parsec VDD，Linux 用 xrandr 虚拟输出）。这样 Deskflow 侧不需要私有 API，只需要读显示器布局 + 事件旁路。

## 与 Deskflow 共存的注意事项

- **别抢焦点**：Deskflow 在跨机切换时会 `activate` 前台窗口，本程序如果也在激活 App，两边会互相打断。现方案已限制激活预算，见 `docs/02-macos.md`；
- **别改全局光标位置**：Deskflow 依赖光标位置判断是否越界，本程序只隐藏光标、不 warp，避免把它带偏；
- **虚拟屏要在 Deskflow 启动前就存在**（或至少在 server 重新读取屏幕布局之前），否则 server 侧看到的还是一块屏；
- **配置不要互相覆盖**：Deskflow 的 `~/Library/Deskflow/Deskflow.conf` 与本程序无关，任何时候都不要去改它。
