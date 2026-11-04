# Windows_iPhone_touch · 用 Windows 的鼠标键盘控制 iPhone

鼠标在窗口里移动，iPhone 上的指针跟着动；点击、长按、拖动、键盘输入全部可用。
**不插 USB、不装虚拟显示驱动、iPhone 上不装任何 App、不需要越狱。**

这是 macOS 版 iPhone Use 的 Windows 重写。

---

## 最重要的一条：HID 特征必须加密

**这是整个项目卡了最久的问题，先记在这里。**

`GattLocalCharacteristicParameters` 的 `ReadProtectionLevel` / `WriteProtectionLevel`
**必须设成 `GattProtectionLevel.EncryptionRequired`，不能用 `Plain`。**

用 `Plain` 时 iOS 的表现是：

- 蓝牙能连上（系统蓝牙显示"已连接"）
- 辅助触控的设备列表里能看到这台电脑
- **4 个 Report 特征全部订阅成功**
- `notify_value_async` 全部返回成功
- **但鼠标指针永远不出现，键盘、媒体键全部无效，没有任何报错**

原因：HID over GATT 规范要求加密链路。`Plain` 下虽然能建立连接并收发字节，
但 iOS 不会把这条链路上的报文当输入消费。改成 `EncryptionRequired` 后 iOS 会
主动发起配对（bonding），配对完成后 HID 才真正生效。

**排查这类问题的正确顺序**（我们绕了很多弯路）：
1. 先确认 Report Map 逐字节正确（和 `windows/tests/expected_report_map.h` 比对）
2. 再确认 `0x2908` Report Reference 描述符挂上了（读 `char.descriptors`）
3. **然后直接看 ProtectionLevel** —— 前面都对但没反应，八成是这里

---

## 其他几个坑

| 现象 | 原因 | 做法 |
|---|---|---|
| 点击无效 | 绝对报文只定位、不带按钮语义 | 按钮状态必须额外走一条相对报文（`dx=dy=0`） |
| 长按撑不住 | 按住期间不发报文，iOS 判定静止 | 每 100ms 重发一条维持按钮 |
| 拖不动 | 相对报文 `dx=0` 构不成拖动 | 拖动时切相对位移，带真实 `dx/dy` |
| USB 插着时连不上 | USB 链路抢占 BLE HID | 测试时拔掉 USB |
| 边缘判定完全错位 | 进程没声明 DPI 感知 | 启动即调 `SetProcessDpiAwarenessContext(-4)` |

**交互模式的分工**（`panel.py`）：

| 场景 | 通道 | 理由 |
|---|---|---|
| 悬停（未按键） | 绝对报文 Report2 | 一条到位，零延迟，不依赖校准曲线 |
| 按住拖动 | 相对报文 Report3 + 真实 `dx/dy` | 拖动必须"按钮 + 位移"在同一条报文里 |
| 按住不动 | 相对报文 `dx=dy=0` | 只维持按钮状态，防 iOS 判静止 |
| 键盘 | Report1 | 状态式：每次变化发完整 8 字节快照 |

---

## 快速开始

### 依赖

```bash
pip install winrt-runtime winrt-Windows.Devices.Bluetooth \
            winrt-Windows.Devices.Bluetooth.GenericAttributeProfile \
            winrt-Windows.Devices.Bluetooth.Advertisement winrt-Windows.Storage.Streams
```

### iPhone 侧（一次就好）

1. 设置 → 辅助功能 → 触控 → **辅助触控**，打开
2. 同一页 → **设备 → 蓝牙设备** → 选择 `DESKTOP-xxxx`（你的电脑名）
3. **确认配对弹窗**（设了 EncryptionRequired 后这一步才会出现）

> 不要在「设置 → 蓝牙」里手动配对。辅助触控需要独占这条连接，
> 系统蓝牙先占住的话辅助触控会一直转圈连不上。

### 运行

```bash
python -m ios_screen.panel                    # 窗口模式（推荐，默认绝对坐标）
python -m ios_screen.panel --mode relative    # 相对模式（走校准曲线，移动有追赶感但更稳）
python -m ios_screen.app --edge right         # 边缘模式：鼠标推出屏幕右缘就接管
```

### 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--mode` | `absolute` | `absolute` 绝对坐标 / `relative` 相对累积 |
| `--calibration` | 自带 iPhone 15 Pro 曲线 | 仅相对模式用 |
| `--sensitivity` | `1.0` | 灵敏度倍数 |
| `--edge` | `right` | 边缘模式的方向 |
| `--screen` | `primary` | 边缘模式用哪块屏 |
| `--list` | — | 列出所有显示器 |
| `--dry-run` | — | 不发蓝牙，只打印报文 |

---

## 架构

```
Windows 端（三层，没有采集层）
┌──────────────────────────────────────────────────┐
│ 触发   窗口内鼠标 / 屏幕边缘（Raw Input 边缘判定）  │
├──────────────────────────────────────────────────┤
│ 编码   绝对 Report2 / 相对 Report3 / 键盘 Report1    │
├──────────────────────────────────────────────────┤
│ 注入   WinRT GattServiceProvider → HOGP 服务 0x1812 │
└──────────────────────────────────────────────────┘
                       ↓ BLE
                 iPhone（系统级蓝牙键鼠）
```

macOS 版有四层（采集 / 虚拟屏 / 窗口 / 注入），Windows 版砍掉了**采集**和**虚拟屏**：

- **采集**：Windows 没有官方 iPhone 画面通道，走盲控（眼睛看实体手机屏）
- **虚拟屏**：不需要。Raw Input 的 `lLastX/lLastY` 是设备级位移，光标被钳制在
  屏幕边缘时照样上报（FPS 游戏锁光标还能转视角就是这个原理），所以"贴边 + 继续
  朝外"本身就是可靠的越界信号，不需要第二块显示器

---

## 目录

```
.
├── ios_screen/
│   ├── data/calibration-iphone15pro.json   真机校准曲线（相对模式用）
│   ├── hid.py            198 字节 Report Map 常量 + 报文编码
│   ├── calibration.py    32 档加速度曲线解析与单步跟踪
│   ├── edge_trigger.py   边缘状态机（纯逻辑，单测覆盖）
│   ├── geometry.py       Rect
│   ├── keymap.py         VK → HID Usage 映射 + 按键状态机
│   ├── hogp.py           GATT 服务端：0x1812 + 14 个特征
│   ├── winput.py         Raw Input / 低级钩子 / 光标计数 / DPI 感知
│   ├── panel.py          窗口模式主程序（当前主力）
│   └── app.py            边缘模式主程序
├── tests/test_core.py    21 项单测
└── tools/
    ├── ble_probe.py            蓝牙外设角色探针
    ├── ble_advertise_check.py  广播包检查
    ├── hogp_run.py             HOGP 联调 + 归位
    └── channel_test.py         分通道验证（绝对/相对/键盘/媒体键）
```

## 单测

```bash
python -m unittest discover -s tests -v
```

覆盖边缘状态机（进入/退出/误触发/四方向/屏外坐标）、校准曲线收敛（26 步，
与 C++ 基线一致）、非法校准拒绝。

## 已知限制

| 限制 | 原因 |
|---|---|
| 看不到 iPhone 画面 | Windows 无官方 USB 采集通道（需要就用 iPhoneMirror 投屏，关掉它的蓝牙反控） |
| 只能单指操作 | iOS 的 HID 鼠标模型，不支持多指手势 |
| 剪贴板不能同步 | HID 只传键鼠 |
| 改了 Report Map 要重配 | iOS 缓存 HOGP 数据库 |
| 相对模式会漂移 | 盲控拿不到指针反馈，长时间使用需归位 |

## 相关开源项目

- [YueChen-C/iPhone-use](https://github.com/YueChen-C/iPhone-use) — macOS 原版（MIT），本项目的 Report Map 与校准曲线来源
- [RayrenSX/iPhoneMirror](https://github.com/RayrenSX/iPhoneMirror) — Windows 投屏 + BLE 反控（GPL-3.0），`EncryptionRequired` 这条关键线索来自它
- [misterye/deskflow-hid](https://github.com/misterye/deskflow-hid) — 同目标，但依赖 ESP32-C3 硬件桥

## 许可

Report Map 与校准曲线数据来自 [iPhone Use](https://github.com/YueChen-C/iPhone-use)（MIT）。
