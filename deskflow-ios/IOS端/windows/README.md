# Windows 侧 · 可编译的移植底座

这里不是"占位"了。平台无关的部分已经写成 C++17 并跑过单测，Windows 上要做的是
**把外壳接上去**，而不是从零设计。

从哪读起：先 `../docs/06-windows-handoff.md`（交接信 + 落地四步），再回来看这份文件。

## 已经验证过的部分

在本机（macOS 14.8 / clang 15）实际编译运行，42 项断言全绿：

```
$ ./build.sh
[hid] report map / payload
[calibration] parse / lookup / tracking
       收敛用 26 步
[cursor] refcount balance
[geometry] placement matches the verified macOS layout

42 checks, 0 failed
```

| 文件 | 内容 | 验证方式 |
|---|---|---|
| `src/hid_reports.cpp` | 198 字节 HID Report Map、4 类报文编码 | 与上游 `HIDReports.swift` **逐字节比对**（`tests/expected_report_map.h` 由脚本从 Swift 源码生成） |
| `src/calibration.cpp` | 33 档加速度曲线、反查、单步跟踪、归位 | 用真机校准文件跑收敛测试：从 (0,0) 到 (1179,2555) 用 26 步，终点误差 < 半档 |
| `src/geometry.cpp` | 虚拟屏贴右缘、手机区定位 | 复现 macOS 上已验证的 `1680,125 393×852` |
| `src/cursor_guard.cpp` | `ShowCursor` 引用计数配平 | 模拟计数器：进入/重复进入/离开/析构四路都验过 |

**没有验证的部分**（本机无 Windows，无法编译）：

- `src/main_win.cpp` —— Win32 外壳：枚举显示器、`ChangeDisplaySettingsEx` 摆位、
  `WH_MOUSE_LL` 钩子、捕获状态机。逻辑完整，预计只需少量类型/包含修正。
- `src/hogp_winrt.cpp` —— HOGP 发布端，**只有骨架**。它有明确的失败出口，不会假装成功。

## 目录

```
windows/
├── build.sh                  macOS/Linux 上验证核心（不依赖 cmake，作者跑过的命令）
├── CMakeLists.txt            Windows 上用：cmake -S . -B build -A x64
├── include/
│   ├── hid_reports.h         报文与描述符的对外契约
│   ├── calibration.h         曲线：px <-> report 档位
│   ├── geometry.h            像素矩形与摆位
│   ├── cursor_guard.h        ShowCursor 计数配平
│   └── hid_sink.h            HID 出口接口 + DryRunSink
├── src/
│   ├── hid_reports.cpp       ← 已测
│   ├── calibration.cpp       ← 已测
│   ├── geometry.cpp          ← 已测
│   ├── cursor_guard.cpp      ← 已测
│   ├── main_win.cpp          ← 未编译
│   └── hogp_winrt.cpp        ← 骨架
└── tests/
    ├── test_core.cpp         平台无关单测
    └── expected_report_map.h 从上游源码生成的权威字节
```

## 怎么跑

**任意平台验证核心**（30 秒）：

```bash
./build.sh
```

**Windows 构建**：

```powershell
cmake -S . -B build -A x64
cmake --build build --config Release
ctest --test-dir build -C Release
```

**Windows 上的三个自检命令**（按顺序，前一个不通不要做下一个）：

```powershell
.\build\Release\ios_screen.exe --list
.\build\Release\ios_screen.exe --calibration cal.json --home --dry-run
.\build\Release\ios_screen.exe --monitor "PSCCDD0" --calibration cal.json --dry-run
```

第三条跑起来后，把鼠标移进虚拟屏，控制台应开始按 30ms 节奏打印 `report id=3`，
`dx/dy` 随移动方向和幅度变化。这一步通了，说明**几何 + 钩子 + 校准**三点都对，
只剩蓝牙。

## 校准文件从哪来

Windows 自己量不出来（没有画面就没有反馈）。用 Mac 上已经量好的：

```
~/Library/Application Support/iPhoneUse/pointer-calibration/by-screen/<screenID>.json
```

本目录已带一份真机导出的 `calibration-iphone15pro.json`（iPhone 15 Pro，1180×2556），
拷到 Windows 直接 `--calibration` 指定即可。曲线是 iOS 侧的行为，与是哪台电脑无关；
但 **`reportInterval` 必须是 0.03**，改了就等于换了速度段，曲线失效。
