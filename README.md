# Windows_iPhone_touch

Drive an iPhone from a Windows mouse and keyboard over Bluetooth LE HID — as if the
phone were one more display sitting just outside the monitor edge.

Move the cursor past the screen edge (or into a small panel window) and Windows starts
advertising a standard BLE HID device. The iPhone pairs with it through
**AssistiveTouch → Devices → Bluetooth Devices** and from then on receives real pointer,
click, long-press, drag, wheel and keyboard input. Nothing is installed on the phone,
no USB cable is used, and no virtual display driver is needed — you keep looking at the
physical screen while Windows only supplies the input.

Two positioning modes ship together: `absolute` (default, virtual coordinates, unlimited
hand travel) and `relative` (calibration curve, for devices that ignore absolute reports).

This is a Windows rewrite of the macOS [iPhone Use](https://github.com/YueChen-C/iPhone-use) approach.

---

## The one thing that matters most: HID characteristics must require encryption

**This cost more time than everything else in this project combined.**

`GattLocalCharacteristicParameters.ReadProtectionLevel` / `WriteProtectionLevel`
**must be `GattProtectionLevel.EncryptionRequired`, not `Plain`.**

With `Plain`, iOS behaves like this:

- Bluetooth connects; system settings show "Connected"
- The computer appears in AssistiveTouch → Devices → Bluetooth Devices
- **All four Report characteristics subscribe successfully**
- `notify_value_async` returns success every time
- **But no pointer ever appears, keyboard and media keys do nothing, and there is no error anywhere**

The reason: HID over GATT requires an encrypted link. Over `Plain` the connection and the
byte flow are fine, but iOS will not treat reports on that link as input. With
`EncryptionRequired`, iOS initiates pairing (bonding) by itself, and only after that does
HID actually take effect.

**Correct debugging order** (we went around in circles):

1. Verify the Report Map byte-for-byte against the upstream copy
2. Verify the `0x2908` Report Reference descriptor is attached (read `char.descriptors`)
3. **Then check ProtectionLevel first** — everything else correct but no reaction almost always means this

---

## Other traps

| Symptom | Cause | Fix |
|---|---|---|
| Click does nothing | Absolute reports position only, no button semantics | Send an extra relative report (`dx=dy=0`) carrying the button bits |
| Long-press does not hold | No report while holding still; iOS treats it as idle | Re-send every 100 ms to keep the button held |
| Cannot drag | A relative report with `dx=0` is not a drag | During drag, switch to relative motion with real `dx/dy` |
| Cannot connect while USB is plugged in | USB link preempts BLE HID | Unplug USB while testing |
| Edge detection completely wrong | Process did not declare DPI awareness | Call `SetProcessDpiAwarenessContext(-4)` at startup |

**Interaction routing** (`panel.py`):

| Scenario | Channel | Why |
|---|---|---|
| Hover (no button) | Absolute report, Report 2 | One report to target, zero latency, no calibration curve |
| Drag (button held) | Relative report, Report 3 with real `dx/dy` | A drag needs button + motion in the same report |
| Hold still | Relative report, `dx=dy=0` | Keeps the button held so iOS does not treat it as idle |
| Keyboard | Report 1 | State-based: send a full 8-byte snapshot on every change |

---

## Quick start

### Dependencies

```bash
pip install winrt-runtime winrt-Windows.Devices.Bluetooth \
            winrt-Windows.Devices.Bluetooth.GenericAttributeProfile \
            winrt-Windows.Devices.Bluetooth.Advertisement winrt-Windows.Storage.Streams
```

### iPhone side (once)

1. Settings → Accessibility → Touch → **AssistiveTouch**, turn it on
2. On the same page → **Devices → Bluetooth Devices** → select your computer name
3. **Confirm the pairing prompt** (it only appears because of `EncryptionRequired`)

> Do not pair from Settings → Bluetooth first. AssistiveTouch needs to own this link;
> if the system Bluetooth layer grabs it, AssistiveTouch will spin forever.

### Run

```bash
python -m ios_screen.app                       # edge mode, phone on the right (default)
python -m ios_screen.app --mode relative       # same, using the calibration curve
python -m ios_screen.panel                     # window mode, for debugging
```

### Two modes, pick one

| | `absolute` (default) | `relative` |
|---|---|---|
| Positioning | one absolute report per change; virtual coordinate, unlimited hand travel | calibration curve, one step per 30 ms |
| Latency | none — one report lands where you point | up to ~200 ms to cross the screen |
| Needs calibration | no | yes (`--calibration`) |
| Exit | push back past the entry edge by `--escape-px` | push back `--exit-threshold` counts |
| Best for | everyday use | devices that ignore absolute reports |

Default geometry: the phone sits to the **right** of the monitor, so the trigger is the
monitor's right edge and the decision is made on **X** (cursor at `right-1`, still moving
right). Entry lands on the iPhone's left edge; pushing back left across it releases.

Phone below the screen instead:

```bash
python -m ios_screen.app --edge bottom --zone-start 0.45 --zone-end 0.55
```

### Options

| Option | Default | Meaning |
|---|---|---|
| `--mode` | `absolute` | virtual absolute coordinates / relative accumulation |
| `--gain` | `1.0` | absolute mode: iPhone pixels per mouse count |
| `--accel` | `0.0` | absolute mode: speed gain, `0` = linear |
| `--escape-px` | `5` | release back to Windows after pushing this far past the edge |
| `--wheel-step` | `2` | HID detents per wheel notch (integer) |
| `--calibration` | bundled iPhone 15 Pro curve | relative mode only |
| `--sensitivity` | `1.0` | relative-mode movement multiplier |
| `--edge` | `right` | edge-mode direction |
| `--zone-start/--zone-end` | `0..1` | restrict the trigger to a segment of that edge |
| `--screen` | `primary` | which monitor edge mode uses |
| `--list` | — | list all monitors |
| `--dry-run` | — | print reports instead of sending |

### Infinite travel: virtual absolute coordinates

Absolute reports are the responsive path, but taking the coordinate from where the
cursor physically sits caps the hand travel at the size of that window or screen.
This build splits the two apart:

- **Position** — a virtual coordinate accumulated from Raw Input deltas. The cursor
  is hidden and frozen during capture, so the hand can keep pushing forever.
- **Landing** — one absolute report per change, no `30 ms × N` catch-up.
- **Exit** — a signed travel counter: entry point is `0`, moving deeper is positive,
  moving back turns negative; at **-5 px** the pointer is released and the Windows
  cursor returns at the edge it left from.

`--gain 1.0` means roughly 1.2 inches of hand travel (at 1000 DPI) crosses the whole
iPhone. Tune it live with **Ctrl+Alt+=** / **Ctrl+Alt+-**; `Ctrl+Alt+Q` releases
(and quits when idle). `--accel 0.8` adds speed-based gain for fast strokes.

### Personal config (kept out of the repo)

`user_config.json` in the project root is git-ignored. Put machine-specific
preferences there instead of patching the code:

```json
{ "key_remap": { "CapsLock": "Esc" } }
```

`key_remap` applies only while the mouse is captured and only to keys you list;
everything else reaches the iPhone unchanged. Supported names: letters, digits,
`Tab` `CapsLock` `Esc` `Enter` `Space` `Backspace` `Up/Down/Left/Right`
`Home` `End` `PageUp` `PageDown` `Insert` `Delete` `Shift` `Ctrl` `Alt` `Win` `F1..F12`.

---

## Architecture

```
Windows side (three layers, no capture layer)
┌──────────────────────────────────────────────────────────┐
│ Trigger  mouse inside window / screen edge (Raw Input)     │
├──────────────────────────────────────────────────────────┤
│ Encode   absolute Report2 / relative Report3 / keyboard Report1 │
├──────────────────────────────────────────────────────────┤
│ Inject   WinRT GattServiceProvider → HOGP service 0x1812   │
└──────────────────────────────────────────────────────────┘
                        ↓ BLE
                  iPhone (system-level Bluetooth HID)
```

The macOS version has four layers (capture / virtual display / window / inject).
The Windows version drops **capture** and **virtual display**:

- **Capture**: Windows has no official iPhone screen channel, so this is blind control —
  you look at the physical phone.
- **Virtual display**: not needed. Raw Input `lLastX/lLastY` are device-level deltas that
  keep arriving while the cursor is clamped at the screen edge (the same reason FPS games
  can keep turning the camera with a locked cursor). "At the edge and pushing outward" is
  therefore a reliable signal, with no second monitor required.

---

## Layout

```
.
├── ios_screen/
│   ├── data/calibration-iphone15pro.json   measured calibration curve
│   ├── hid.py            198-byte Report Map constant + report encoders
│   ├── calibration.py    32-step acceleration curve, lookup and tracking
│   ├── edge_trigger.py   edge state machine (pure logic, unit tested)
│   ├── geometry.py       Rect
│   ├── keymap.py         VK → HID Usage mapping + key state machine
│   ├── hogp.py           GATT server: 0x1812 + 14 characteristics
│   ├── winput.py         Raw Input / low-level hook / cursor / DPI awareness
│   ├── panel.py          window mode (current main entry)
│   └── app.py            edge mode
├── tests/test_core.py    21 unit tests
└── tools/
    ├── ble_probe.py            BLE peripheral-role probe
    ├── ble_advertise_check.py  advertisement inspection
    ├── hogp_run.py             HOGP bring-up and homing
    └── channel_test.py         per-channel verification
```

## Tests

```bash
python -m unittest discover -s tests -v
```

Covers the edge state machine (enter / exit / false trigger / four directions / off-screen
coordinates), calibration convergence (26 steps, matching the C++ baseline), and rejection
of invalid calibration files.

## Known limitations

| Limitation | Reason |
|---|---|
| No iPhone screen on the PC | Windows has no official USB capture channel (use iPhoneMirror for mirroring and turn off its Bluetooth control) |
| Single-finger only | iOS HID mouse model has no multi-touch injection |
| No clipboard sync | HID carries keyboard and mouse only |
| Re-pair after changing the Report Map | iOS caches the HOGP database |
| Relative mode drifts | Blind control gets no pointer feedback; re-home periodically |

## Related projects

- [YueChen-C/iPhone-use](https://github.com/YueChen-C/iPhone-use) — macOS original (MIT); source of the Report Map and calibration data
- [RayrenSX/iPhoneMirror](https://github.com/RayrenSX/iPhoneMirror) — Windows mirroring + BLE reverse control (GPL-3.0); the `EncryptionRequired` clue came from here
- [misterye/deskflow-hid](https://github.com/misterye/deskflow-hid) — same goal, but requires an ESP32-C3 hardware bridge

## License

Report Map and calibration data are from [iPhone Use](https://github.com/YueChen-C/iPhone-use) (MIT).

---
---

# 中文

用 Windows 的鼠标键盘，通过蓝牙 LE HID 直接操作 iPhone——就像手机是显示器旁边
接着的又一块屏。

把光标推出显示器边缘（或移进一个小控制面板），Windows 就开始广播一个标准 BLE HID
设备；iPhone 在「辅助触控 → 设备 → 蓝牙设备」里连上之后，收到的是真正的指针、
点击、长按、拖动、滚轮和键盘输入。手机端不装任何东西、不插 USB 线、不需要虚拟
显示驱动——你看着实体屏幕，Windows 只负责把输入送过去。

两种定位模式同时提供：`absolute`（默认，虚拟坐标，手可以无限推）与 `relative`
（校准曲线，给不认绝对报文的设备用）。

这是 macOS 版 [iPhone Use](https://github.com/YueChen-C/iPhone-use) 方案在 Windows 上的重写。

---

## 最重要的一条：HID 特征必须加密

**这是整个项目卡了最久的问题。**

`GattLocalCharacteristicParameters.ReadProtectionLevel` / `WriteProtectionLevel`
**必须设成 `GattProtectionLevel.EncryptionRequired`，不能用 `Plain`。**

用 `Plain` 时 iOS 的表现是：

- 蓝牙能连上（系统设置显示"已连接"）
- 辅助触控的设备列表里能看到这台电脑
- **4 个 Report 特征全部订阅成功**
- `notify_value_async` 每次都返回成功
- **但鼠标指针永远不出现，键盘和媒体键全部无效，而且没有任何报错**

原因：HID over GATT 规范要求加密链路。`Plain` 下连接和字节流都没问题，但 iOS 不会把
这条链路上的报文当输入消费。改成 `EncryptionRequired` 后，iOS 会主动发起配对
（bonding），配对完成后 HID 才真正生效。

**排查这类问题的正确顺序**（我们绕了很多弯路）：

1. 先确认 Report Map 与上游副本逐字节一致
2. 再确认 `0x2908` Report Reference 描述符挂上了（读 `char.descriptors`）
3. **然后直接看 ProtectionLevel** —— 其他都对但没反应，八成是这里

---

## 其他几个坑

| 现象 | 原因 | 做法 |
|---|---|---|
| 点击无效 | 绝对报文只定位、不带按钮语义 | 额外发一条相对报文（`dx=dy=0`）带按钮位 |
| 长按撑不住 | 按住不动时没有报文，iOS 判定为空闲 | 每 100ms 重发一次维持按钮 |
| 拖不动 | 相对报文 `dx=0` 构不成拖动 | 拖动时改用相对位移，带真实 `dx/dy` |
| USB 插着时连不上 | USB 链路抢占 BLE HID | 测试时拔掉 USB |
| 边缘判定完全错位 | 进程没声明 DPI 感知 | 启动时调 `SetProcessDpiAwarenessContext(-4)` |

**交互通道的分工**（`panel.py`）：

| 场景 | 通道 | 理由 |
|---|---|---|
| 悬停（未按键） | 绝对报文 Report2 | 一条到位，零延迟，不依赖校准曲线 |
| 按住拖动 | 相对报文 Report3 + 真实 `dx/dy` | 拖动需要"按钮 + 位移"在同一条报文里 |
| 按住不动 | 相对报文 `dx=dy=0` | 只维持按钮状态，防止 iOS 判为空闲 |
| 键盘 | Report1 | 状态式：每次变化发完整 8 字节快照 |

---

## 快速开始

### 依赖

```bash
pip install winrt-runtime winrt-Windows.Devices.Bluetooth \
            winrt-Windows.Devices.Bluetooth.GenericAttributeProfile \
            winrt-Windows.Devices.Bluetooth.Advertisement winrt-Windows.Storage.Streams
```

### iPhone 侧（只需一次）

1. 设置 → 辅助功能 → 触控 → **辅助触控**，打开
2. 同一页 → **设备 → 蓝牙设备** → 选择你的电脑名称
3. **确认配对弹窗**（设了 `EncryptionRequired` 后这一步才会出现）

> 不要在「设置 → 蓝牙」里先手动配对。辅助触控需要独占这条连接，
> 系统蓝牙先占住的话辅助触控会一直转圈连不上。

### 运行

```bash
python -m ios_screen.app                       # 边缘模式，手机在右侧（默认）
python -m ios_screen.app --mode relative       # 同上，走校准曲线
python -m ios_screen.panel                     # 窗口模式，调试用
```

### 两种模式，任选其一

| | `absolute`（默认） | `relative` |
|---|---|---|
| 定位方式 | 每次变化发一条绝对报文；虚拟坐标，手可无限推 | 校准曲线，30ms 走一步 |
| 延迟 | 无——一条报文直接落点 | 横穿整屏最多约 200ms |
| 需要校准 | 不需要 | 需要（`--calibration`） |
| 退出 | 往回推越过进入边 `--escape-px` 像素 | 往回推 `--exit-threshold` 个计数 |
| 适用 | 日常使用 | 不认绝对报文的设备 |

默认几何：手机在显示器**右侧**，所以触发边是显示器右缘，判定看 **横坐标**
（光标到 `right-1` 且继续向右）。进入后落在 iPhone 左缘，向左推回去即释放。

想把手机放在屏幕下方：

```bash
python -m ios_screen.app --edge bottom --zone-start 0.45 --zone-end 0.55
```

### 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--mode` | `absolute` | `absolute` 虚拟绝对坐标 / `relative` 相对累积 |
| `--gain` | `1.0` | 绝对模式：一个鼠标计数换多少 iPhone 像素 |
| `--accel` | `0.0` | 绝对模式：速度增益，`0` 为线性 |
| `--escape-px` | `5` | 越过退出边多少像素后释放回 Windows |
| `--wheel-step` | `2` | 每格滚轮发多少 HID 档位（整数） |
| `--calibration` | 自带 iPhone 15 Pro 曲线 | 仅相对模式使用 |
| `--sensitivity` | `1.0` | 相对模式的位移倍率 |
| `--edge` | `right` | 边缘模式的方向 |
| `--zone-start/--zone-end` | `0..1` | 只让该边缘的一段生效 |
| `--screen` | `primary` | 边缘模式使用哪块屏 |
| `--list` | — | 列出所有显示器 |
| `--dry-run` | — | 不发送，只打印报文 |

### 无限行程：虚拟绝对坐标

绝对报文最跟手，但如果坐标取自光标在窗口/屏幕上的物理位置，手的行程就被那块
区域锁死——窗口只有 420 像素宽，光标一碰边就到头。这一版把两件事拆开：

- **位置**：由 Raw Input 的相对位移累积出的虚拟坐标；捕获期间光标被隐藏并冻住，
  所以手可以一直朝一个方向推，没有上限。
- **落点**：一条绝对报文到位，没有 `30ms × N` 的追赶。
- **退出**：有符号行程计数——进入点记 0，往屏幕深处推为正，往回推为负；
  减到 **-5 像素** 即释放，Windows 光标回到原来出去的那条边上。

`--gain 1.0` 大约手走 1.2 英寸（按 1000 DPI）横穿整块 iPhone。运行时用
**Ctrl+Alt+=** / **Ctrl+Alt+-** 实时调速，`Ctrl+Alt+Q` 释放（未捕获时退出程序）。
想要"慢速精细 + 快速跨屏"可以加 `--accel 0.8`。

### 个性化配置（不进版本库）

项目根目录的 `user_config.json` 已被 git 忽略，本机专属的偏好放这里，不要去改代码：

```json
{ "key_remap": { "CapsLock": "Esc" } }
```

`key_remap` 只在捕获期间生效，且只影响你列出的键，其余按键原样转发。
可用键名：字母、数字、`Tab` `CapsLock` `Esc` `Enter` `Space` `Backspace`
`Up/Down/Left/Right` `Home` `End` `PageUp` `PageDown` `Insert` `Delete`
`Shift` `Ctrl` `Alt` `Win` `F1..F12`。

---

## 架构

```
Windows 端（三层，没有采集层）
┌──────────────────────────────────────────────────────────┐
│ 触发   窗口内鼠标 / 屏幕边缘（Raw Input 判定）              │
├──────────────────────────────────────────────────────────┤
│ 编码   绝对 Report2 / 相对 Report3 / 键盘 Report1           │
├──────────────────────────────────────────────────────────┤
│ 注入   WinRT GattServiceProvider → HOGP 服务 0x1812        │
└──────────────────────────────────────────────────────────┘
                        ↓ BLE
                  iPhone（系统级蓝牙 HID）
```

macOS 版有四层（采集 / 虚拟屏 / 窗口 / 注入），Windows 版砍掉了**采集**和**虚拟屏**：

- **采集**：Windows 没有官方 iPhone 画面通道，所以是盲控——眼睛看实体手机屏。
- **虚拟屏**：不需要。Raw Input 的 `lLastX/lLastY` 是设备级位移，光标被钳制在屏幕边缘
  时照样上报（FPS 游戏锁光标还能转视角就是这个原理）。所以"贴边 + 继续朝外"本身就是
  可靠信号，不需要第二块显示器。

---

## 目录

```
.
├── ios_screen/
│   ├── data/calibration-iphone15pro.json   真机校准曲线
│   ├── hid.py            198 字节 Report Map 常量 + 报文编码
│   ├── calibration.py    32 档加速度曲线解析与单步跟踪
│   ├── edge_trigger.py   边缘状态机（纯逻辑，有单测）
│   ├── geometry.py       Rect
│   ├── keymap.py         VK → HID Usage 映射 + 按键状态机
│   ├── hogp.py           GATT 服务端：0x1812 + 14 个特征
│   ├── winput.py         Raw Input / 低级钩子 / 光标计数 / DPI 感知
│   ├── panel.py          窗口模式（当前主力入口）
│   └── app.py            边缘模式
├── tests/test_core.py    21 项单测
└── tools/
    ├── ble_probe.py            蓝牙外设角色探针
    ├── ble_advertise_check.py  广播包检查
    ├── hogp_run.py             HOGP 联调与归位
    └── channel_test.py         分通道验证
```

## 单测

```bash
python -m unittest discover -s tests -v
```

覆盖边缘状态机（进入 / 退出 / 误触发 / 四方向 / 屏外坐标）、校准曲线收敛（26 步，
与 C++ 基线一致）、非法校准文件拒绝。

## 已知限制

| 限制 | 原因 |
|---|---|
| 电脑上看不到 iPhone 画面 | Windows 无官方 USB 采集通道（需要投屏就用 iPhoneMirror，关掉它的蓝牙反控） |
| 只能单指操作 | iOS 的 HID 鼠标模型不支持注入多指手势 |
| 剪贴板不能同步 | HID 只传键鼠 |
| 改了 Report Map 要重新配对 | iOS 会缓存 HOGP 数据库 |
| 相对模式会漂移 | 盲控拿不到指针反馈，需要定期归位 |

## 相关开源项目

- [YueChen-C/iPhone-use](https://github.com/YueChen-C/iPhone-use) — macOS 原版（MIT），本项目 Report Map 与校准数据的来源
- [RayrenSX/iPhoneMirror](https://github.com/RayrenSX/iPhoneMirror) — Windows 投屏 + BLE 反控（GPL-3.0），`EncryptionRequired` 这条线索来自它
- [misterye/deskflow-hid](https://github.com/misterye/deskflow-hid) — 同目标，但依赖 ESP32-C3 硬件桥

## 许可

Report Map 与校准数据来自 [iPhone Use](https://github.com/YueChen-C/iPhone-use)（MIT）。
