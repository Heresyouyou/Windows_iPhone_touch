# 06 · Windows 重构交接：从这里开始

> 读者：运行在 Windows 上的 AI 助手或工程师。
> 前提：macOS 版已经跑通并稳定运行（Intel MacBook Air + macOS 14，iPhone 15 Pro，USB 采集 + BLE HID）。
> 本文不是可行性评估——`03-windows-port.md` 是评估，`05-windows-iphone-use.md` 是"iPhone Use 能不能装"的答复。**本文是开工单。**

---

## 0. 一页结论

| 事项 | 状态 |
|---|---|
| 平台无关的核心（报文、校准、几何、光标计数） | **已写好、已在本机跑通 42 项断言**，`windows/` 里可直接用 |
| Win32 外壳（枚举屏、摆位、低级钩子、捕获状态机） | **代码已写全，但本机无 Windows，未编译**。预计只需少量修正 |
| HOGP 发布端（WinRT GATT） | **只有骨架 + 精确参数表**。这是你要填的主要空白 |
| 看见 iPhone 画面 | **做不到**。走盲控。别在这上面浪费时间 |
| 蓝牙适配器支持外设（Peripheral）角色 | **未知，必须先验**。这是全项目唯一的硬门槛 |

一句话：**把 M0 和 M2 当成两个"通/不通"的判定，其余都是体力活。**

---

## 1. 先理解结构差异，再看代码

macOS 版有四层，Windows 版只剩三层，而且位移计算的位置变了：

```
macOS:  鼠标移动 → iPhone Use 窗口自己抓 → iPhone Use 算位移 → 它自己发 BLE
        └ 本程序只负责：建屏、摆窗、藏指针

Windows: 鼠标移动 → 本程序吃掉事件 → 本程序算位移 → 本程序发 BLE
        └ 没有窗口，没有 App，全在自己手里
```

由此产生三个直接结论：

1. **没有窗口，就没有摆窗、没有 AX、没有焦点抢占。** macOS 版里那套"激活预算 / 焦点归还 / `--follow`"逻辑在 Windows 上**整段删掉**，不要移植。
2. **手机画面区 = 整块虚拟屏。** macOS 版要从窗口里抠出 393×852 的画面矩形；Windows 盲控模式下整块屏都是"手机"，越界判定就是"光标是否落在这块屏上"。
3. **没有画面 = 没有反馈 = 不能自测校准。** 必须复用 Mac 上量好的曲线（见第 2 节）。这是 Windows 版最容易被忽略、又最影响手感的地方。

---

## 2. 已经替你做完的部分

`windows/` 下四个文件是平台无关的，已在本机编译并跑过单测。**不要重写，直接用。**

### 2.1 HID 描述符与报文（`hid_reports.cpp`）

198 字节的 Report Map，与上游 iPhone Use 的 `HIDReports.swift` **逐字节一致**。
`tests/expected_report_map.h` 是用脚本从 Swift 源码生成的权威副本，单测逐字节比对。

为什么必须一致：iPhone 会把 HOGP 服务的数据库缓存进配对记录。Report ID、描述符顺序、
报文长度任何一处不同，都可能表现为"配对成功但屏幕上没有指针"。改了这个文件，就要在
iPhone 上"忽略此设备"重新配对。

| Report ID | 用途 | 负载长度 | 布局 |
|---|---|---|---|
| 1 | 键盘 | 8 | 修饰键, 保留, 6×键码 |
| 2 | 绝对指针 | 5 | 按键, X(LE u16), Y(LE u16)，0..32767 |
| 3 | 相对鼠标 | 4 | 按键, dx, dy, 滚轮（后三者 int8） |
| 4 | 媒体键 | 2 | consumer usage (LE u16)，Home = 0x0223 |

> 注意：**通知负载里不带 Report ID 字节**。ID 写在 Report Reference 描述符（0x2908）里。
> 这是最容易写错的一处——带了 ID 会整体错位一个字节。

### 2.2 校准曲线（`calibration.cpp`）

`distances[i]` = 发一条幅度为 `i` 的 report，iPhone 指针实际走多少物理像素。
真机数据（iPhone 15 Pro，1180×2556）：从 `0.83`（i=1）到 `164.5`（i=32），非线性。

已实现：

- `countForDistance(px)`：反查档位；
- `nextStep(...)`：单步跟踪，返回下一条 report 的 `dx/dy` 和预测落点；
- `homingReports()`：连发最大幅度向左 + 向上，靠 iOS 的边界钳制把指针压到 (0,0)。

单测实测：从 (0,0) 走到 (1179,2555) 用 26 步收敛，终点误差 < 半档（0.41px）。

**Windows 必须用 30ms 的发送间隔**（`calibration.reportInterval`）。iOS 的加速度按速度分档，
改成 5ms 会让整张表失效，表现为"小距离准、大距离冲过头"。

### 2.3 几何（`geometry.cpp`）

复现 macOS 上验证过的坐标：主屏 1680×1050、虚拟屏 509×905 →
`(1680,72)`，去掉 25px 菜单栏后工作区 `(1680,97) 509×880`，画面区 `1680,125 393×852`。

Windows 上虚拟屏不需要 905 高（没有标题栏），但保持同一套函数便于两端对同一份坐标做回归。

### 2.4 光标计数（`cursor_guard.cpp`）

`ShowCursor` 是**引用计数**，返回调用后的计数值，只有 `< 0` 才真的隐藏。单测覆盖了
"从 0 开始"、"计数已被别人抬到 3"、"重复进入"、"析构时仍在隐藏"四种路径。

---

## 3. 落地四步

每步都有明确验收。**前一步不通，不要做下一步。**

### M0 · 环境与虚拟屏（不写代码，约 30 分钟）

1. 装一个**已签名**的虚拟显示驱动。推荐 Parsec VDD（免费、签名、支持自定义分辨率）；
   不要自己写 IddCx——那是内核驱动 + EV 证书的事，与本项目目标无关。
2. 在"显示设置"里把它设为"扩展"，分辨率设成 iPhone 的 3 倍像素（iPhone 15 Pro：1179×2556）。
3. 确认光标能自由移进这块屏，且不会被弹回。

验收：`ios_screen.exe --list` 能列出这块屏，且在非主屏里。

> 这一步失败通常不是代码问题：驱动没签名、屏没启用为"扩展"、或分辨率模式不被接受。

### M1 · 盲控链路（`--dry-run`，约 1 小时）

```powershell
.\build\Release\ios_screen.exe --monitor "PSCCDD0" --calibration cal.json --dry-run -v
```

做三件事：把屏摆到主屏右缘 → 装低级鼠标钩子 → 进入捕获后每 30ms 打印一条 report。

验收：

- 控制台打印 `虚拟屏: ... -> (1920,0) 1179x2556`，且实际位置与期望一致（代码里有回读）；
- 鼠标移进虚拟屏后系统指针**消失**，控制台开始滚 `report id=3`；
- 向右下移动时 `dx/dy` 为正且幅度随速度增长；
- **把鼠标往左推到底能退出捕获**，指针回到主屏（`kExitMarginPx` 那段逻辑）。

这一步已经证明：几何、钩子、校准反查、捕获状态机全部正确。

### M2 · HOGP 发布（最高风险，半天到两天）

这是唯一可能"做不通"的一步。按第 4 节的表填 `hogp_winrt.cpp`，然后：

```powershell
.\build\Release\ios_screen.exe --calibration cal.json --home
```

`--home` 只发归位报文，不碰显示器和钩子——用它单独验证 HID 能不能推动 iPhone 指针。

验收顺序（每一步都要过）：

1. 程序启动后，安卓手机装 nRF Connect **能看到** `0x1812` 服务和 5 个 Report 特征；
2. iPhone 设置→蓝牙里出现一个新设备（名字通常是这台 PC 的名称），能配对；
3. 跑 `--home`，iPhone 上的指针跳到左上角；
4. 全链路：`--monitor ... --calibration ...`（去掉 `--dry-run`），鼠标移进虚拟屏，iPhone 指针跟着动。

如果第 1 步都看不到：`StartAdvertising` 返回 `OtherError`，多半是适配器不支持 Peripheral 角色。
换一个支持双角色的 USB 蓝牙适配器（CSR8510 芯片一类）再试，不要在软件层硬扛。

### M3 · 常驻与恢复（半天）

- 做成托盘程序，或至少一个隐藏窗口的常驻进程；
- `SetConsoleCtrlHandler` + 析构 + `atexit` 三处都要能恢复光标；
- 显示器被拔掉 / 驱动卸载 / 程序崩溃，都不能留下"没有鼠标的桌面"。

---

## 4. GATT 表（照抄，别自己设计）

服务：`00001812-0000-1000-8000-00805F9B34FB`，Primary。

| # | UUID | 名称 | 属性 | 权限 | 值 / 说明 |
|---|---|---|---|---|---|
| 1 | `0x2A4A` | HID Information | Read | Plain | 4 字节：`11 01 00 02`（HID 1.11，country 0，NormallyConnectable） |
| 2 | `0x2A4B` | Report Map | Read | Plain | `kReportMap`，198 字节 |
| 3 | `0x2A4C` | HID Control Point | WriteWithoutResponse | Plain | 1 字节，0=Suspend 1=ExitSuspend |
| 4 | `0x2A4D` | Report · 键盘 | Read + Notify | Plain | Report Ref `[01 01]`，8 字节 |
| 5 | `0x2A4D` | Report · 绝对指针 | Read + Notify | Plain | Report Ref `[02 01]`，5 字节 |
| 6 | `0x2A4D` | Report · 相对鼠标 | Read + Notify | Plain | Report Ref `[03 01]`，4 字节 |
| 7 | `0x2A4D` | Report · 媒体键 | Read + Notify | Plain | Report Ref `[04 01]`，2 字节 |
| 8 | `0x2A4D` | Report · 键盘 LED | Read + Write | Plain | Report Ref `[01 02]`，1 字节（输出报告） |
| 9 | `0x2A4E` | Protocol Mode | Read + WriteWithoutResponse | Plain | 1 字节，`01`=Report |
| 10 | `0x2A19` | Battery Level | Read + Notify | Plain | 1 字节，`64`（100%） |
| 11 | `0x2A01` | Appearance | Read | Plain | `C1 03`（Keyboard），可选，影响 iOS 图标 |

要点：

- **Report Reference（`0x2908`）是必须的**，值 2 字节：`[reportId, reportType]`，
  type 1=Input、2=Output、3=Feature。少了它 iOS 无法把特征和描述符里的报告对上。
- 这批 ID 不是猜的：`/status` 的 `bluetoothHosts[].subscribed` 实测返回
  `["absolutePointer","battery","consumer","keyboard","relativeMouse"]`，正好对应上表 4-7 与 10。
- 广播必须可连接（`IsConnectable = true`），且广播包里要带 `0x1812`。
  `GattServiceProvider.StartAdvertising` 通常会自动带上服务 UUID，但要实测确认。
- Windows 侧设备名来自系统蓝牙名称，`GattServiceProvider` 不能单独设本地名。
  iPhone 里看到的是这台电脑的名字，属正常。

---

## 5. 已知陷阱（都是 macOS 版真踩过的，换到 Windows 只是形式不同）

| 现象 | 原因 | 做法 |
|---|---|---|
| 隐藏光标没生效 | `ShowCursor` 是引用计数，返回新计数 | 循环到 `< 0`；恢复时循环到 `>= 0`（`cursor_guard.cpp` 已实现） |
| 指针"漂"或"冲过头" | 用线性比例换算位移，iOS 实际是非线性加速度 | 必须查 32 档表，不能 `dx = 像素 / 常量` |
| 小距离准、大距离飞 | 发送间隔变了，加速度落在另一档 | 固定 30ms，与校准时的 `reportInterval` 一致 |
| 配对成功但没有指针 | Report Map / Report Reference 错，或负载里多带了 ID 字节 | 用 `tests/expected_report_map.h` 逐字节比对；负载不带 ID |
| 鼠标被困在虚拟屏里 | 吞掉了全部事件，没有出口 | 保留 `kExitMarginPx` 越界退出；再留一个热键兜底 |
| 鼠标自己乱飘 | 低级钩子把自己注入的事件又吃了一遍 | 用 `LLMHF_INJECTED` 过滤 |
| 钩子突然失效 | `WH_MOUSE_LL` 回调超时（系统默认 ~300ms）会被摘钩 | 回调里只做累加与标记，发送交给定时器 |
| 崩溃后桌面没有鼠标 | 恢复逻辑只写在正常退出路径 | 析构 + `SetConsoleCtrlHandler` + `atexit` 三处都放 |
| 加显示器后主屏分辨率变了 | 驱动或系统重置了模式 | 记录原始模式，改完**回读校验**，不一致就恢复（macOS 版靠这个活下来的） |
| 抢走用户键盘焦点 | 为了摆窗口反复 `Activate`（macOS 版每 0.6s 一次） | Windows 不需要激活窗口，整段逻辑不要移植 |
| 随机崩溃，几十万次调用后 | CF/COM 引用计数双重释放（macOS 版真崩过，被自动重启放大成"屏幕乱跳"） | 所有权二选一：`__bridge_transfer` 就不再 `CFRelease`；COM 同理只 `Release` 一次 |

---

## 6. 验收清单

- [ ] `./build.sh`（或 `ctest`）42 项全绿
- [ ] `--list` 能看到虚拟屏，且不占主屏位置
- [ ] `--home` 让 iPhone 指针跳到左上角
- [ ] `--dry-run` 下移进虚拟屏，系统指针消失、报文按 30ms 打印、方向正确
- [ ] 左推到底能退出捕获，指针回到主屏
- [ ] iPhone 蓝牙里能看到并配对成功，配对后没有"需要校准"提示
- [ ] 全链路：鼠标越界 → iPhone 指针跟随，回到主屏 → iPhone 指针停下
- [ ] Ctrl+C / 强杀 / 拔掉虚拟屏驱动，桌面指针都能恢复

---

## 7. 做不到的部分（别硬啃）

| 想要 | 现实 |
|---|---|
| 在 Windows 上看见 iPhone 画面 | 没有官方通道。商业软件靠逆向，协议不公开。AirPlay 接收端要自研 FairPlay + H.264 |
| 剪贴板双向同步 | HID 只传键鼠，没有数据通道。iOS 又不允许后台常驻 socket |
| iPhone 当 Deskflow 的 client | iOS 跑不了 Deskflow 客户端进程。只能做主机侧的屏幕后端 |
| 100% 精确的绝对定位 | 盲控只能靠"归位 + 相对位移"推算，误差约半档（<1px），但依赖曲线准确 |

如果一定要画面，最省力的降级是：**Mac 负责采集画面并通过 Deskflow/VNC 暴露给 Windows，
Windows 只负责越界与 HID**。这样 Windows 端不用碰采集，全部工作量落在第 3 节。

---

## 8. 文件地图

```
IOS端/
├── docs/
│   ├── 01-architecture.md        四层拆解、事件流、状态机（与平台无关，仍适用）
│   ├── 02-macos.md               macOS 实现细节与全部踩坑
│   ├── 03-windows-port.md        逐层可行性评估
│   ├── 04-deskflow-integration.md 与 Deskflow 的关系与集成粒度
│   ├── 05-windows-iphone-use.md  iPhone Use 在 Windows 上能不能用
│   └── 06-windows-handoff.md     本文
├── macos/                        macOS 实现（已跑通，可对照阅读）
├── windows/                      ← 你的工作区
│   ├── include/                  五个头文件（契约）
│   ├── src/                      四个已测核心 + 两个 Windows 专属
│   ├── tests/                    平台无关单测 + 权威描述符
│   └── build.sh                  验证核心的那条命令
└── protocol/                     iPhone Use 的 HTTP 接口与校准格式（仅 macOS 有）
```

开工前建议先读 `01-architecture.md`：里面的状态机、几何闭环原则、光标成对恢复原则
与操作系统无关，能省掉你一半的试错。
