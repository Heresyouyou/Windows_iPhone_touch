# Mac_iPhone_touch

Drive an iPhone from a Mac's mouse and keyboard over Bluetooth LE HID — as if the phone
were one more display sitting just outside the monitor edge.

The Mac advertises a standard BLE HID device the moment it starts. The iPhone pairs with it
through **AssistiveTouch → Devices → Bluetooth Devices**, and from then on receives real
pointer, click, long-press, drag, wheel and keyboard input. Nothing is installed on the
phone and no cable is used.

By default the app also creates a **virtual display** — 393×852 pt, sitting flush against
the right edge of the main screen. That is what makes the pointer genuinely *travel* into
phone space: physical mouse, trackpad and remote-KVM clients (Deskflow) alike just move the
system cursor across the boundary, and the cursor's position inside that display is mapped
1:1 onto the iPhone. `--no-vscreen` falls back to the older behaviour, where you push
against the screen edge and input is captured by an event tap.

Two positioning modes ship together: `absolute` (default, virtual coordinates, unlimited
hand travel) and `relative` (calibration curve, for devices that ignore absolute reports).

This is the macOS port of [Windows_iPhone_touch](https://github.com/Heresyouyou/Windows_iPhone_touch),
which in turn is a Windows rewrite of the macOS original
[iPhone Use](https://github.com/YueChen-C/iPhone-use) approach. The HID layer
(198-byte Report Map, four report channels, absolute-pointer trick) is byte-for-byte
identical to the Windows tree; the input, Bluetooth and packaging layers are new.

---

## What is actually verified here

| Item | Status |
|---|---|
| 114 unit tests (report map fingerprint, encoders, keymap, GATT table, geometry, edge trigger, virtual display, launcher) | ✅ pass |
| Publishing the HID service and starting advertising on macOS 14.8.9 | ✅ verified on real hardware |
| Characters/report IDs/descriptors match the Windows tree | ✅ asserted by tests |
| **iPhone pairs through AssistiveTouch and the pointer appears** | ✅ verified on real hardware |
| Pointer, click, drag, wheel and keyboard all reach the phone | ✅ verified on real hardware |
| Phone space reachable by mouse, trackpad and Deskflow | ✅ verified on real hardware |

Everything in that table was driven by hand on a real iPhone 15 Pro. What is *not*
promised: the virtual display uses `CGVirtualDisplay`, a private API, so a future macOS
may refuse to create it (`--no-vscreen` still works), and a remote-KVM server caches the
desktop size it reported at connect time — it has to reconnect once before its cursor can
cross into the new display.

---

## Three things that cost the most time

### 1. On macOS the service UUID must be published in 128-bit form

**This is the macOS-specific wall, and it is invisible.**

`CBPeripheralManager.addService:` rejects *every* 16-bit SIG UUID with

```
The specified UUID is not allowed for this operation.   (CBErrorUUIDNotAllowed)
```

Measured on macOS 14.8.9 by feeding one UUID at a time to `addService:`:

| UUID passed to `addService:` | Result |
|---|---|
| `1800` (GAP) | ❌ UUID not allowed |
| `1801` (GATT) | ❌ UUID not allowed |
| `1812` (HID) | ❌ UUID not allowed |
| `180A` (Device Info) | ❌ UUID not allowed |
| `180F` (Battery) | ❌ UUID not allowed |
| `6E400001-B5A3-F393-E0A9-E50E24DCCA9E` (custom) | ✅ accepted |
| `00001812-0000-1000-8000-00805F9B34FB` | ✅ **accepted** |
| `0000180A-0000-1000-8000-00805F9B34FB` | ✅ accepted |

So the rule is about *representation*, not about permissions and not about which service
you asked for. Expand the 16-bit UUID onto the Bluetooth SIG base and the check passes —
and per the Core spec the two forms are **the same service**:

```
0x1812  ==  00001812-0000-1000-8000-00805F9B34FB
```

Characteristics and descriptors are *not* affected: a 16-bit `0x2A4D` characteristic
inside a 128-bit service is accepted. So only service UUIDs are expanded
(`gatt_table.ServiceSpec.wire_uuid`), everything else stays 16-bit.

Two consequences worth knowing:

- CoreBluetooth compares `CBUUID`s by bytes, so a 2-byte `1812` and the 16-byte base form
  are **not equal**. Anything that matches against the service (scan filters, our own
  diagnostics) must use the expanded form.
- Windows/WinRT has no such restriction, which is why the Windows tree could publish
  `1812` directly and this one cannot.

### 2. HID characteristics must require encryption

Carried over from the Windows tree, unchanged: `CBAttributePermissionsReadEncryptionRequired`
/ `WriteEncryptionRequired`, **not** `Readable`/`Writeable`.

With plain permissions iOS behaves like this:

- Bluetooth connects; the phone shows "Connected"
- The Mac appears in AssistiveTouch → Devices → Bluetooth Devices
- **All four Report characteristics subscribe successfully**
- Every `updateValue:` call reports success
- **But no pointer appears, keyboard and media keys do nothing, and nothing errors**

HID over GATT requires an encrypted link. Plain gets you a working byte pipe that iOS
refuses to treat as input. With encryption required, iOS initiates bonding by itself and
only then does HID take effect.

The single deliberate exception is Protocol Mode (`0x2A4E`): the host writes it *before*
pairing, so requiring encryption there makes it spin forever. It carries no input data,
so plain is safe. There is a test that pins exactly this exception.

### 3. The program must run as a real `.app` bundle

CoreBluetooth's first use makes TCC look up `NSBluetoothAlwaysUsageDescription` in the
**responsible application's** `Info.plist`. A command-line `python` has no such plist, and
the result is not an error but an immediate `SIGABRT` — exit code 134, no output, no
traceback. The crash report says it plainly:

```
termination.namespace == "TCC"
"This app has crashed because it attempted to access privacy-sensitive data without a
 usage description. The app's Info.plist must contain an NSBluetoothAlwaysUsageDescription key"
```

So `make_app.sh` builds `dist/Mac iPhone Touch.app`, and `ble_hid.bundle_warning()` checks
the plist *before* touching CoreBluetooth so you get a readable message instead of a
silent abort. Checking `__CFBundleIdentifier` is not enough — it is inherited when you
launch from a terminal or IDE.

---

## Other traps

| Symptom | Cause | Fix |
|---|---|---|
| Exit code 134, zero output, when run as `python -m …` | TCC has no usage description | Run the `.app`; see above |
| `open -a App.app --args --edge left` — arguments silently missing | `open --args` delivers **no** argv on this machine (verified with a compiled `.app`: argv had only `argv[0]`; `NSWorkspace.arguments` behaves the same) | `run.sh` writes `launch.args`; the app's `launch` script reads and deletes it |
| Cursor keeps moving during capture | Capture layer is not the key window, or does not cover that display | Use a full-screen transparent window — and **never** swallow events globally, see the warning below |
| Tap dies after a few seconds of silence | `kCGEventTapDisabledByTimeout` | Catch it and call `CGEventTapEnable(tap, True)` again — already handled in `mac_input.EventTap` |
| Typed letters are wrong / scrambled | macOS keycodes are **physical positions** (A/S/D/F = 0x00/0x01/0x02/0x03), not alphabetical | Use `keymap.py`; never assume `ord()`-like ordering |
| Display size is half of what it should be on Retina | `CGDisplayPixelsWide` reports points | Use `CGDisplayCopyDisplayMode` + `CGDisplayModeGetPixelWidth` |
| `CBPeripheralManager` state stays 0, then aborts | No window-server connection (e.g. spawned from a daemon) | Launch through LaunchServices as a real app |
| Services added, but the host sees only one or two | PyObjC dropped the `CBMutableService` — no Python reference after the callback | Keep them in a list (`HidPeripheral._services`) |
| Device visible in AssistiveTouch but spins forever | Advertisement payload over 31 bytes, or a stale iOS GATT cache | Advertise only `0x1812` + name; on the phone use "Forget This Device" and re-pair |
| Click does nothing | Absolute reports position only, no button semantics | Send an extra relative report (`dx=dy=0`) carrying the button bits |
| Long-press does not hold | No report while holding still; iOS treats it as idle | Re-send every 100 ms (`HOLD_RESEND`) |

> **Do not decouple the cursor globally, and do not swallow every event.**
> An earlier version called `CGAssociateMouseAndMouseCursorPosition(False)` and swallowed
> all keyboard events from a tap. When that process died mid-flight it left the machine
> with no working pointer and no working keyboard — the only way out was a reboot. That is
> why capture is now a plain window: it owns focus while it exists, and if the process dies
> the window dies with it, so the system always gets its input back.

### A window on a virtual display receives no mouse events

Measured, not guessed. A full-screen transparent window was placed over both screens, then
the same synthetic click was sent to three points:

| Click location | Window frame covers it | `hitTest` hits | Window gets `mouseDown` | Read-only tap sees it |
|---|---|---|---|---|
| Main screen centre | yes | yes | ✅ | ✅ |
| Virtual display centre | yes | yes | ❌ | ✅ |
| Virtual display corner | yes | yes | ❌ | ✅ |

So on a `CGVirtualDisplay`, windows are simply not part of mouse-event dispatch. The
keyboard is unaffected, because it only cares about the key window — which is why the
symptom looked like "keyboard fine, every click dead". The fix is to cover the *real*
screens only and take clicks and wheel from a non-swallowing tap.

**One more macOS-only fact:** CoreBluetooth does **not** deliver a Mac's own advertisements
to its own central role. Scanning for 8 s in a room with a dozen BLE devices listed all of
them and never the local one. You cannot verify the GATT table on the same machine that
publishes it — use a second machine or the phone.

---

## Quick start

### Dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # pyobjc-core + Cocoa + Quartz + CoreBluetooth
```

### Build the app bundle and run

```bash
./make_app.sh          # -> dist/Mac iPhone Touch.app + run.sh
./make_launcher.sh     # -> dist/启动台.app (optional GUI, see below)
./run.sh               # default: virtual display to the right of the main screen
```

After launch you should see a new 393×852 display appear to the right of your main screen
(Displays in System Settings will list it). The Mac cursor can now cross into it, and while
it is in there the iPhone's pointer follows 1:1. `Ctrl+Option+Q` releases.

`run.sh` passes arguments through a file because `open --args` does not work here:

```bash
./run.sh --no-vscreen              # no virtual display: push against the screen edge
./run.sh --edge left --screen 1    # edge mode, phone on the left, second display
./run.sh --panel                   # window mode
./run.sh --list                    # list displays and exit
tail -f ~/Library/Logs/MacPhoneTouch.log
```

### Permissions (first run, once)

1. **Accessibility** — System Settings → Privacy & Security → Accessibility → tick the app.
   Without it the event tap cannot be created at all.
2. **Bluetooth** — System Settings → Privacy & Security → Bluetooth → allow the app.

Rebuilding with `make_app.sh` re-signs ad-hoc, so macOS may ask for Accessibility again.

### iPhone side (once)

1. Settings → Accessibility → Touch → **AssistiveTouch**, turn it on
2. On the same page → **Devices → Bluetooth Devices** → pick your Mac's name
3. **Confirm the pairing prompt** (it appears because the characteristics require encryption)

> Do not pair from Settings → Bluetooth first. AssistiveTouch needs to own this link; if
> the system Bluetooth layer grabs it, AssistiveTouch spins forever.

### Launcher: both features in one window (recommended)

```bash
./make_app.sh && ./make_launcher.sh     # -> dist/启动台.app
open "dist/启动台.app"
```

Two cards, each with **Start** / **Stop**, plus **Start all** / **Stop all** at the bottom:

| Button | Behaviour |
|---|---|
| Deskflow · Start | `open -a Deskflow`, then wait until it really shows up in the process table |
| Deskflow · Stop | TERM, KILL after 8 s, takes `deskflow-core` with it |
| iPhone access · Start | launches the app bundle through `run.sh`; the virtual display appears |
| iPhone access · Stop | TERMs the app, then clears any leftover `native/vdisplay` |
| Start all / Stop all | both, in parallel |

- A button's enabled state *is* the status: Start greys out while running, Stop greys out
  while stopped. No separate legend to interpret.
- Processes are re-read once a second, so anything killed from outside updates the window.
- **Closing the launcher stops both features.** Deliberate: it means a Deskflow you started
  by hand also gets closed. The window says so at the bottom.
- The launcher installs nothing and does not use launchd. It is an alternative to the
  optional Deskflow guard below — running both makes them fight over the same process.

### Follow Deskflow's lifetime instead (optional, either/or)

```bash
tools/guard_ctl.sh install     # LaunchAgent, takes effect at login
tools/guard_ctl.sh status
tools/guard_ctl.sh uninstall
```

The guard acts **on edges only**: it starts the app when Deskflow goes from "not running"
to "running", and stops it when Deskflow exits. It deliberately does *not* keep the app
alive — otherwise `Ctrl+Option+Q` would be impossible to use, because the app would be
back two seconds later. The predicate is the GUI process
`Deskflow.app/Contents/MacOS/Deskflow`, not `deskflow-core`: the core can linger after the
app quits, and using it would delay or skip the shutdown.

> If `install` reports that launchd refused the registration, the current process has no
> permission to write the user domain (common inside IDE / agent sandboxes). Run the same
> command in your own terminal.

---

## Two modes, pick one

The mode decides *how the pointer is positioned on the phone*. Getting the input in is a
separate question, and by default the answer is the virtual display: whatever moves the
system cursor — mouse, trackpad, a remote-KVM client — is enough, because the cursor's
coordinates inside that display are the answer.

| | `absolute` (default) | `relative` |
|---|---|---|
| Positioning | one absolute report per change; virtual coordinate, unlimited hand travel | calibration curve, one step per 30 ms |
| Latency | none — one report lands where you point | up to ~200 ms to cross the screen |
| Needs calibration | no | yes (`--calibration`) |
| Exit | move the cursor back onto the main screen | push back `--exit-threshold` counts |
| Best for | everyday use | devices that ignore absolute reports |

In edge mode (`--no-vscreen`) the geometry is different: the phone sits to the **right** of
the monitor, so the trigger is the monitor's right edge and the decision is made on **X**
(cursor at `right-1`, still moving right). Entry lands on the iPhone's left edge; pushing
back left across it releases.

Phone below the screen instead (edge mode):

```bash
./run.sh --no-vscreen --edge bottom --zone-start 0.45 --zone-end 0.55
```

## Options

| Option | Default | Meaning |
|---|---|---|
| `--edge` | `right` | `right` / `left` / `top` / `bottom` |
| `--zone-start`, `--zone-end` | `0` … `1` | Restrict the trigger to a segment of the edge |
| `--screen` | `primary` | Display selector: `primary`, index, name fragment, or resolution |
| `--list` | — | List displays and exit |
| `--name` | `Mac iPhone` | BLE advertising name (10 chars keeps the adv packet short) |
| `--mode` | `absolute` | `absolute` / `relative` |
| `--gain` | `3.0` | Absolute mode: iPhone pixels per mouse count |
| `--accel` | `0.0` | Absolute mode: speed gain, `0` = linear |
| `--escape-px` | `6 × gain` | Edge mode: pixels past the exit edge to release |
| `--no-vscreen` | off | Do not create the virtual display; fall back to edge push |
| `--fix-cursor` | — | Re-associate the system cursor with the mouse and exit (first aid) |
| `--sensitivity` | `1.0` | Relative mode: multiplier on the calibration curve |
| `--wheel-step` | `2` | HID detents per wheel notch |
| `--enter-threshold` | `12` | Counts of outward motion needed to capture |
| `--exit-threshold` | `60` | Relative mode: inward counts needed to release |
| `--calibration` | `ios_screen/data/calibration-iphone15pro.json` | 32-step curve |
| `--config` | `user_config.json` | Per-user key remapping |
| `--dry-run` | off | Print reports, do not touch Bluetooth |
| `--home` | — | Send homing reports only |
| `--self-test N` | `0` | Exit after N seconds |
| `-v` | off | Verbose |

During capture: `Ctrl+Option+=` faster, `Ctrl+Option+-` slower, `Ctrl+Option+Q` release
(press again when idle to quit).

---

## Architecture

Everything runs on **one thread, one CFRunLoop**: the Quartz event tap, the CoreBluetooth
delegate callbacks and the send timer are all main-queue. The Windows tree needed two
threads and a lock because WinRT callbacks arrive on thread-pool threads; here the
serialisation is free, and with it the whole class of races disappears.

Two ways in, one way out:

```
default: virtual display
  system cursor (mouse / trackpad / Deskflow)
        │  read position every 30 ms
        ▼
    vscreen ─────▶ Controller ──▶ Sender ──▶ CBPeripheralManager
        ▲              │                       │  Report 1 keyboard
   read-only tap ──────┤ click / wheel         │  Report 2 absolute
   CaptureWindow ──────┘ keyboard              │  Report 3 relative
                                               │  Report 4 consumer

--no-vscreen: edge push
   IOHIDManager (raw counts) ─┐
   CGEventTap  (synthetic)  ──┴─▶ EdgeTrigger ──enter──▶ Controller
```

- `mac_input.py` — event tap, cursor warp/guard, display enumeration, `AXIsProcessTrusted`
- `vscreen.py` — starts `native/vdisplay`, waits for the new display, exposes its frame
- `raw_mouse.py` — `IOHIDManager` via ctypes: real hardware deltas, which a tap cannot see
- `capture_window.py` — the transparent full-screen layer that owns the keyboard; on the
  virtual display a window receives no mouse events at all, so clicks come from the tap
- `safety.py` — watchdog: if the main thread stops beating, the process kills itself rather
  than leaving the Mac without input
- `edge_trigger.py` — edge bands, entry/exit hysteresis (unchanged from Windows)
- `vpointer.py` — virtual absolute position, push-through exit (unchanged)
- `calibration.py` — 32-step curve; `report_interval` must stay 0.03, iOS derives
  acceleration from report arrival rate
- `hid.py` — 198-byte Report Map and report encoders (byte-identical to Windows,
  SHA-256 `b74d9ebb…d80e3`)
- `gatt_table.py` — the GATT database as pure data, plus the 128-bit expansion
- `ble_hid.py` — CoreBluetooth peripheral: serial `addService:`, synchronous `updateValue:`
  with its own retry queue, subscription tracking
- `core.py` — `Sender` / `Controller`: which report to send when, hold-resend, drag routing
- `app.py` — edge mode; `panel.py` — window mode

Rule of thumb for routing:

| Scenario | Channel | Why |
|---|---|---|
| Hover, no button | Absolute, Report 2 | One report, zero latency, no curve |
| Drag | Relative, Report 3 with real `dx/dy` | A drag needs button + motion in one report |
| Hold still | Relative, `dx=dy=0` | Keeps the button held |
| Keyboard | Report 1 | State-based: full 8-byte snapshot per change |

## Layout

```
ios_screen/            # the port itself
  app.py               # entry point, CLI, the 30 ms tick, both input paths
  core.py              # Sender / Controller: which report, when, hold-resend, drags
  ble_hid.py           # CoreBluetooth peripheral
  gatt_table.py        # the GATT database as data + 128-bit expansion
  hid.py               # 198-byte report map and encoders
  mac_input.py         # event tap, cursor, display enumeration
  raw_mouse.py         # IOHIDManager, for deltas a tap cannot see
  vscreen.py           # virtual display lifecycle
  capture_window.py    # the layer that owns the keyboard
  edge_trigger.py      # edge bands + hysteresis
  vpointer.py          # absolute pointer arithmetic
  calibration.py       # relative-mode curve
  keymap.py            # macOS keycode → HID usage
  safety.py            # watchdog
  userconfig.py        # per-user key remapping
  panel.py             # window mode
  geometry.py
  data/                # calibration curves
launcher/              # 启动台: controller.py manages processes, app.py draws the window
native/                # vdisplay.m (compiled by make_app.sh), launcher.icns
make_app.sh            # -> dist/Mac iPhone Touch.app, generates run.sh
make_launcher.sh       # -> dist/启动台.app
run.sh                 # user entry point (writes launch.args, then open)
tools/                 # probes, plus the optional Deskflow guard
tests/                 # 114 tests, no hardware required
```

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

They pin the things that are expensive to rediscover: the Report Map fingerprint, the
`0x2908` descriptor on every report, encryption permissions on every data characteristic,
the Protocol Mode exception, left/right modifiers, 6KRO, no two physical keys mapping to
the same HID usage, edge hysteresis, push-through arithmetic, the 128-bit service
expansion, the virtual-display geometry and the launcher's process matching.

Two of them exist because the corresponding bug already happened: an instance attribute
that shadowed a method (`_on_subscribe`) crashed the CoreBluetooth delegate, and an
`is_at_edge` using `>=` made the phone enterable exactly once.

## Diagnostics

```bash
# 1. What may this Mac publish at all?
./run.sh --module=tools.ble_probe --new

# 2. Publish locally, then run the same command from *another* Mac / inspect the phone
./run.sh --module=tools.ble_probe --new --advertise

# 3. Connect to a HID peripheral and dump its GATT table
./run.sh --module=tools.gatt_selfcheck --new --name=DESKTOP-XXXX

# 4. Just list everything nearby
./run.sh --module=tools.gatt_selfcheck --new --dump
```

## Known limitations

- **The virtual display is a private API.** `CGVirtualDisplay` is what `native/vdisplay.m`
  wraps; Apple may change or remove it. Compilation failure is non-fatal and
  `--no-vscreen` keeps the old behaviour.
- **A remote-KVM server must reconnect once.** Deskflow reports the desktop size to its
  server at connect time; a server that connected before the virtual display existed still
  believes the desktop ends at the old width, and its cursor stops there. Disconnect and
  reconnect (or re-add the Mac) after the display appears.
- **Edge mode alone does not work with Deskflow.** In client mode Deskflow synthesises
  mouse events rather than HID reports, and once the cursor is pinned at the right edge its
  `DeltaX` is zero — the pointer can only slide along the phone's left column. That is
  exactly why the virtual display is the default path.
- **Clicks on the virtual display go to the desktop first.** The capture layer covers only
  the real screens, because a window placed on a `CGVirtualDisplay` receives no mouse
  events at all; the click is taken from a read-only event tap instead. If a click makes
  the Finder flash its focus, that is this.
- The Mac cannot scan its own advertisements, so there is no single-machine end-to-end test.
- Ad-hoc signing means a rebuild may require re-ticking Accessibility.
- One virtual display at a time, matching one phone. The mapping itself is proportional, so
  any phone size works without configuration.
- This tree is macOS only. The Windows implementation lives in
  [Windows_iPhone_touch](https://github.com/Heresyouyou/Windows_iPhone_touch).

## Differences from the Windows tree

| | Windows | macOS (this tree) |
|---|---|---|
| Input capture | Raw Input + low-level hook (300 ms timeout, `ShowCursor` refcount) | cursor position inside a virtual display; read-only tap for clicks; `IOHIDManager` in edge mode |
| Entry gesture | cursor reaches the screen edge | cursor crosses into the virtual display (default), or edge push with `--no-vscreen` |
| HID peripheral | WinRT `GattServiceProvider` | `CBPeripheralManager` |
| Service UUID | 16-bit `0x1812` fine | must be expanded to 128-bit |
| Boot Protocol / Appearance chars | provided | not provided (report protocol only, like iPhone Use) |
| GAP service | must be created explicitly | provided by the system, must **not** be added |
| Threading | two threads + lock | one thread + run loop |
| Notify back-pressure | `notify_value_async` always "succeeds" | `updateValue:` returns `Bool`, needs a retry queue |
| Process form | plain script | `.app` bundle (TCC) |
| Permission | none | Accessibility + Bluetooth |
| Key codes | virtual-key codes | physical-position key codes |

## Related projects

- [iPhone Use](https://github.com/YueChen-C/iPhone-use) — the original macOS approach
- [Windows_iPhone_touch](https://github.com/Heresyouyou/Windows_iPhone_touch) — the Windows tree this was ported from

## License

MIT — see [LICENSE](LICENSE).

---
---

# 中文

把 Mac 的鼠标和键盘通过蓝牙 LE HID 送给 iPhone，就像手机是显示器边缘外的另一块屏。

程序一启动就广播一个标准 BLE HID 设备。iPhone 在**辅助功能 → 触控 → 辅助触控 →
设备 → 蓝牙设备**里配对之后，就能收到真正的指针、点击、长按、拖拽、滚轮和键盘输入。
手机上不用装任何东西，不用插线。

默认还会创建一块**虚拟屏**——393×852 pt，紧贴主屏右侧。这才是"光标真的能走进手机"
的原因：物理鼠标、触控板、远程键鼠（Deskflow）都只是把系统光标移过边界，光标在这块
屏里的位置按比例就是 iPhone 上的位置。`--no-vscreen` 退回旧做法：推屏幕边缘，
由事件 tap 接管输入。

两种定位模式：`absolute`（默认，虚拟坐标，手可以无限推）和 `relative`（校准曲线，
给不吃绝对报表的设备用）。

这是 [Windows_iPhone_touch](https://github.com/Heresyouyou/Windows_iPhone_touch) 的
macOS 移植版；而 Windows 版又是 macOS 原作
[iPhone Use](https://github.com/YueChen-C/iPhone-use) 思路的重写。HID 层（198 字节
Report Map、四路报表、绝对指针技巧）与 Windows 版逐字节一致；输入层、蓝牙层和打包
方式是新写的。

---

## 这里到底验证了什么

| 项目 | 状态 |
|---|---|
| 114 项单测（Report Map 指纹、编码器、键码表、GATT 表、几何、边缘触发、虚拟屏、启动台） | ✅ 全过 |
| 在 macOS 14.8.9 上发布 HID 服务并开始广播 | ✅ 真机验证 |
| 特征、报表 ID、描述符与 Windows 版一致 | ✅ 单测钉死 |
| **iPhone 通过辅助触控配对，指针出现** | ✅ 真机验证 |
| 指针、点击、拖拽、滚轮、键盘都能送到手机 | ✅ 真机验证 |
| 物理鼠标、触控板、Deskflow 三种输入都能进入手机 | ✅ 真机验证 |

表格里每一行都在一台真的 iPhone 15 Pro 上手动操作验证过。这里**不**保证的是：虚拟屏
用的是 `CGVirtualDisplay` 私有 API，将来某版 macOS 可能拒绝创建（此时 `--no-vscreen`
照旧可用）；远程键鼠的服务端在连接时就把桌面尺寸记下来了，必须先重连一次，它的光标
才走得进新加的那块屏。

---

## 最花时间的三件事

### 1. macOS 上服务 UUID 必须用 128 位形式发布

**这是 macOS 特有的一堵墙，而且它完全不出声。**

`CBPeripheralManager.addService:` 会拒收**所有** 16 位 SIG UUID，只回一句

```
The specified UUID is not allowed for this operation.   (CBErrorUUIDNotAllowed)
```

在 macOS 14.8.9 上逐个喂给 `addService:` 的实测结果：

| 传给 `addService:` 的 UUID | 结果 |
|---|---|
| `1800`（GAP） | ❌ UUID not allowed |
| `1801`（GATT） | ❌ UUID not allowed |
| `1812`（HID） | ❌ UUID not allowed |
| `180A`（Device Info） | ❌ UUID not allowed |
| `180F`（Battery） | ❌ UUID not allowed |
| `6E400001-B5A3-F393-E0A9-E50E24DCCA9E`（自定义） | ✅ 通过 |
| `00001812-0000-1000-8000-00805F9B34FB` | ✅ **通过** |
| `0000180A-0000-1000-8000-00805F9B34FB` | ✅ 通过 |

所以这条规则管的是**写法**，跟权限无关、跟你要发布哪个服务也无关。把 16 位 UUID 展开到
Bluetooth SIG 基址上，检查就过了——而按 Core 规范，这两种写法是**同一条服务**：

```
0x1812  ==  00001812-0000-1000-8000-00805F9B34FB
```

特征和描述符不受影响：128 位服务下挂 16 位的 `0x2A4D` 实测可以正常添加。所以只有服务
UUID 需要展开（`gatt_table.ServiceSpec.wire_uuid`），其余保持 16 位。

两个连带后果值得记住：

- CoreBluetooth 的 `CBUUID` 是按字节比的，2 字节的 `1812` 和 16 字节的基址形式
  **不相等**。任何要匹配这个服务的地方（扫描过滤、自家诊断工具）都必须用展开后的写法。
- Windows/WinRT 没有这条限制，所以 Windows 版可以直接发 `1812`，这边不行。

### 2. HID 特征必须要求加密

从 Windows 版原样继承：权限必须是
`CBAttributePermissionsReadEncryptionRequired` / `WriteEncryptionRequired`，
**不能**是 `Readable` / `Writeable`。

用明文权限时 iOS 的表现是这样的：

- 蓝牙连上了，手机上显示「已连接」
- 辅助触控 → 设备 → 蓝牙设备里能看到这台电脑
- **四个 Report 特征全部订阅成功**
- 每次 `updateValue:` 都返回成功
- **但指针不出来，键盘和媒体键没反应，而且任何地方都不报错**

HID over GATT 要求加密链路。明文状态下连接和字节流都正常，但 iOS 不会把这条链路上的
报文当输入。设成要求加密后，iOS 会自己发起配对（bonding），配完 HID 才真正生效。

唯一刻意的例外是 Protocol Mode（`0x2A4E`）：主机会在**配对之前**写它，设成加密反而会让
它一直转圈。它不承载输入数据，明文是安全的。有一条单测专门钉住这个例外。

### 3. 程序必须打成真正的 `.app` 包

CoreBluetooth 第一次使用时，TCC 会去读**责任应用** `Info.plist` 里的
`NSBluetoothAlwaysUsageDescription`。命令行 `python` 没有这份 plist，结果不是报错，
而是立刻 `SIGABRT`——退出码 134，没有输出，没有 traceback。崩溃报告写得很明白：

```
termination.namespace == "TCC"
"This app has crashed because it attempted to access privacy-sensitive data without a
 usage description. The app's Info.plist must contain an NSBluetoothAlwaysUsageDescription key"
```

所以 `make_app.sh` 会生成 `dist/Mac iPhone Touch.app`，而 `ble_hid.bundle_warning()`
在碰 CoreBluetooth **之前**先检查 plist，让你看到一句人话而不是静默 abort。只看
`__CFBundleIdentifier` 不够——从终端或 IDE 启动时它会被继承。

---

## 其他坑

| 现象 | 原因 | 解法 |
|---|---|---|
| `python -m …` 跑，退出码 134、零输出 | TCC 找不到用途说明 | 跑 `.app`，见上 |
| `open -a App.app --args --edge left` 参数**静默丢失** | 本机的 `open --args` 根本不传 argv（拿编译过的 `.app` 验过：argv 里只有 `argv[0]`；`NSWorkspace.arguments` 也一样） | `run.sh` 写 `launch.args`，`.app` 里的 `launch` 读完即删 |
| 捕获时光标还在动 | 抓取层不是 key window，或者没盖住那块屏 | 用全屏透明窗口——**不要**全局吞事件，见下面的警告 |
| tap 安静几秒后自己失效 | `kCGEventTapDisabledByTimeout` | 捕获它并重新 `CGEventTapEnable(tap, True)`，`mac_input.EventTap` 已处理 |
| 打出来的字母是乱的 | macOS keycode 是**物理位置**（A/S/D/F = 0x00/0x01/0x02/0x03），不是字母序 | 用 `keymap.py`，别假设按字母排 |
| Retina 上屏幕尺寸只有一半 | `CGDisplayPixelsWide` 报的是点 | 用 `CGDisplayCopyDisplayMode` + `CGDisplayModeGetPixelWidth` |
| `CBPeripheralManager` 状态一直是 0，然后 abort | 没有窗口服务器连接（比如从守护进程里拉起来） | 走 LaunchServices，以真正的 app 启动 |
| 服务加过了，主机却只看到一两个 | PyObjC 把 `CBMutableService` 回收了——回调结束后 Python 这边没有引用 | 用列表留住（`HidPeripheral._services`） |
| 辅助触控里看得见设备但一直转圈 | 广播包超过 31 字节，或 iOS 的 GATT 缓存过期 | 只广播 `0x1812` + 名字；手机上「忽略此设备」重配 |
| 点击没反应 | 绝对报表只负责定位，不带按键语义 | 额外补一条 `dx=dy=0` 的相对报表带按键位 |
| 长按不生效 | 按住不动时没有报文，iOS 当作空闲 | 每 100ms 重发（`HOLD_RESEND`） |

> **不要全局解耦光标，也不要吞掉所有事件。** 早期版本调用
> `CGAssociateMouseAndMouseCursorPosition(False)`，并在 tap 里吞掉全部键盘事件。
> 进程一旦中途死掉，这台 Mac 就既没有指针也没有键盘，只能重启恢复。现在抓取靠一个
> 普通窗口：它在的时候持有焦点，进程一没窗口就跟着消失，系统总能拿回输入。

### 虚拟屏上的窗口收不到鼠标事件

实测，不是猜的。把全屏透明窗口盖到两块屏上，对三个点各发一次同样的合成点击：

| 点击位置 | 窗口 frame 覆盖 | `hitTest` 命中 | 窗口收到 `mouseDown` | 只读 tap 看到 |
|---|---|---|---|---|
| 主屏中心 | 是 | 命中 | ✅ | ✅ |
| 虚拟屏中心 | 是 | 命中 | ❌ | ✅ |
| 虚拟屏角落 | 是 | 命中 | ❌ | ✅ |

结论：在 `CGVirtualDisplay` 那块屏上，窗口根本不参与鼠标事件派发。键盘不受影响，因为
它只认 key window——这就是「键盘正常、点击全死」的由来。改法是抓取层只盖真实屏，
点击和滚轮改从只读 tap 里取。

**还有一条 macOS 独有的事实：** CoreBluetooth **不会**把本机外设的广播投递给本机的主机
角色。在一个十几台 BLE 设备的房间里扫 8 秒，别人都在，唯独没有自己。所以「自己验自己」
这条路不存在——要么换一台机器，要么直接上手机。

---

## 快速开始

### 依赖

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # pyobjc-core + Cocoa + Quartz + CoreBluetooth
```

### 打包并运行

```bash
./make_app.sh          # 生成 dist/Mac iPhone Touch.app 和 run.sh
./make_launcher.sh     # 生成 dist/启动台.app（可选，见下文）
./run.sh               # 默认：主屏右侧出现一块 393×852 的虚拟屏
```

启动后主屏右侧会多出一块 393×852 的屏（系统设置的「显示器」里能看到）。Mac 的光标
可以走进这块屏，进去之后 iPhone 上的指针就跟着它 1:1 移动。`Ctrl+Option+Q` 释放。

参数走文件，因为本机的 `open --args` 不工作：

```bash
./run.sh --no-vscreen               # 不建虚拟屏，退回「推屏幕边缘」
./run.sh --edge left --screen 1     # 边缘模式，手机在左边，用第二块屏
./run.sh --panel                    # 面板模式
./run.sh --list                     # 列出显示器后退出
tail -f ~/Library/Logs/MacPhoneTouch.log
```

### 权限（首次运行，各一次）

1. **辅助功能** —— 系统设置 → 隐私与安全性 → 辅助功能 → 勾选本应用。
   没有它，事件 tap 根本建不起来。
2. **蓝牙** —— 系统设置 → 隐私与安全性 → 蓝牙 → 允许本应用。

`make_app.sh` 重打包会用 ad-hoc 重新签名，所以辅助功能可能要再勾一次。

### iPhone 侧（只需一次）

1. 设置 → 辅助功能 → 触控 → **辅助触控**，打开
2. 同一页 → **设备 → 蓝牙设备** → 选你的 Mac 名字
3. **确认配对弹窗**（它出现正是因为特征要求加密）

> 不要先在「设置 → 蓝牙」里配对。这条链路必须由辅助触控持有；被系统蓝牙层抢走的话，
> 辅助触控会一直转圈。

### 启动台：两个功能一起管（推荐）

```bash
./make_app.sh          # 本体
./make_launcher.sh     # 启动台 → dist/启动台.app
open "dist/启动台.app"
```

窗口里是两组「启动 / 停止」按钮，底部还有「一键启动」「一键关闭」：

| 按钮 | 行为 |
|---|---|
| Deskflow · 启动 | `open -a Deskflow`，等它真的出现在进程表里 |
| Deskflow · 停止 | 先 TERM，8 秒不退再 KILL，连带 `deskflow-core` |
| iPhone 接入 · 启动 | 经 `run.sh` 拉起本体，虚拟屏出现 |
| iPhone 接入 · 停止 | TERM 本体，再收掉 `native/vdisplay` |
| 一键启动 | 两个功能并行启动 |
| 一键关闭 | 两个功能并行收掉 |

- 按钮的可用状态就是当前能做什么：没跑的时候「停止」是灰的，跑起来之后
  「启动」变灰。不用额外的说明去解释"现在到底开没开"。
- 状态每秒回读进程表。谁在外面把进程杀了，按钮也会自己跟着变。
- **关掉启动台窗口 = 上面两个功能一起关掉。** 这是刻意的语义，代价是 Deskflow
  即使是你手动开的也会被一起关。
- 启动台不常驻、不装 launchd。它和下面那套自动守护是**二选一**——
  两套同时跑会互相打架（守护会把刚被启动台关掉的本体再拉起来）。
  检测到守护已加载时，窗口底部会提示，并给出「解除自动守护」按钮。

### 跟着 Deskflow 启停（可选，与启动台二选一）

如果这台 Mac 是被 Deskflow 当副屏用，又不想每次手动点，可以让本程序完全跟着
它的生命周期走：

```bash
tools/guard_ctl.sh install     # 装成 LaunchAgent，登录即生效
tools/guard_ctl.sh status      # 守护 / 本体 / Deskflow 三方状态
tools/guard_ctl.sh uninstall   # 卸载，并把本体一起收掉
```

装好之后：

- Deskflow 启动（准确说是从「没在跑」变成「在跑」）→ 拉起本程序，虚拟屏随之出现；
- Deskflow 退出 → 收掉本程序，虚拟屏立刻消失，不会给桌面留一块黑洞；
- 你自己按 `Ctrl+Option+Q` 关掉 → **守护不会把你重新拉起来**。

最后一条是刻意的：守护只在状态**变化**的那一刻动手，不做「保证进程在跑」的看门狗。
否则用户想临时关一下就关不掉，比没人守还烦。

判据是 `Deskflow.app/Contents/MacOS/Deskflow` 这个 GUI 进程，不是 `deskflow-core`——
退出应用时 core 可能还残留一小会儿，拿它当判据会晚关甚至不关。

> 如果 `install` 报 `launchd 拒绝了注册`，说明当前进程没有写用户 domain 的权限
> （在 IDE / Agent 的沙箱里很常见）。在你自己的终端里跑同一条命令即可。

---

## 两种模式，任选其一

模式决定的是**指针在手机上怎么定位**。"输入怎么进来"是另一个问题，默认答案就是虚拟屏：
不管是鼠标、触控板还是远程键鼠客户端，只要它能把系统光标移动过去，光标在那块屏里的
坐标就是答案。

| | `absolute`（默认） | `relative` |
|---|---|---|
| 定位 | 每次变化发一条绝对报表；虚拟坐标，手可无限推 | 校准曲线，每 30ms 一步 |
| 延迟 | 无——一条报表直接落到目标位置 | 穿过整屏最多约 200ms |
| 需要校准 | 不需要 | 需要（`--calibration`） |
| 退出 | 把光标移回主屏 | 往回推 `--exit-threshold` 个计数 |
| 适合 | 日常使用 | 不吃绝对报表的设备 |

边缘模式（`--no-vscreen`）的几何不一样：手机在显示器**右侧**，所以触发边是显示器右缘，
判定在 **X** 上（光标停在 `right-1` 仍在往右推）。进入后落点是 iPhone 的左边；
往回推越过该边即释放。

手机在屏幕下方（边缘模式）：

```bash
./run.sh --no-vscreen --edge bottom --zone-start 0.45 --zone-end 0.55
```

## 参数

| 参数 | 默认 | 含义 |
|---|---|---|
| `--edge` | `right` | `right` / `left` / `top` / `bottom` |
| `--zone-start`、`--zone-end` | `0` … `1` | 只让某一段边生效 |
| `--screen` | `primary` | 显示器选择：`primary`、序号、名字片段或分辨率 |
| `--list` | — | 列出显示器后退出 |
| `--name` | `Mac iPhone` | 蓝牙广播名（10 个字符以内，广播包才塞得下） |
| `--mode` | `absolute` | `absolute` / `relative` |
| `--gain` | `3.0` | 绝对模式：一个鼠标计数换多少 iPhone 像素 |
| `--accel` | `0.0` | 绝对模式：速度增益，`0` 为线性 |
| `--escape-px` | `6 × gain` | 边缘模式：越过退出边多少像素释放 |
| `--no-vscreen` | 关 | 不建虚拟屏，退回推屏幕边缘 |
| `--fix-cursor` | — | 重新把系统光标与鼠标关联后退出（急救用） |
| `--sensitivity` | `1.0` | 相对模式：校准曲线倍数 |
| `--wheel-step` | `2` | 每格滚轮发多少 HID 档位 |
| `--enter-threshold` | `12` | 进入捕获所需的朝外计数 |
| `--exit-threshold` | `60` | 相对模式退出所需的朝内计数 |
| `--calibration` | `ios_screen/data/calibration-iphone15pro.json` | 32 档曲线 |
| `--config` | `user_config.json` | 个人按键映射 |
| `--dry-run` | 关 | 只打印报文，不碰蓝牙 |
| `--home` | — | 只发归位报文 |
| `--self-test N` | `0` | N 秒后退出 |
| `-v` | 关 | 详细日志 |

捕获期间：`Ctrl+Option+=` 加快，`Ctrl+Option+-` 放慢，`Ctrl+Option+Q` 释放
（空闲时再按一次结束程序）。

---

## 架构

全部在**一个线程、一个 CFRunLoop** 上：Quartz 事件 tap、CoreBluetooth 的委托回调、
发送定时器都在主队列。Windows 版需要两个线程加锁，是因为 WinRT 回调从线程池线程过来；
这边串行是白送的，顺带整类竞态一起消失。

```
默认：虚拟屏
  系统光标（鼠标 / 触控板 / Deskflow）
        │  每 30ms 读一次位置
        ▼
    vscreen ─────▶ Controller ──▶ Sender ──▶ CBPeripheralManager
        ▲              │                       │  Report 1 键盘
   只读事件 tap ───────┤ 点击 / 滚轮             │  Report 2 绝对指针
   CaptureWindow ──────┘ 键盘                  │  Report 3 相对鼠标
                                              │  Report 4 媒体键

--no-vscreen：边缘模式
   IOHIDManager（原始计数）─┐
   CGEventTap  （合成位移）──┴─▶ EdgeTrigger ──进入──▶ Controller
```

- `mac_input.py` —— 事件 tap、光标 warp/守护、显示器枚举、`AXIsProcessTrusted`
- `vscreen.py` —— 拉起 `native/vdisplay`、等新屏出现、给出它的 frame
- `raw_mouse.py` —— 用 ctypes 调 `IOHIDManager`：tap 看不到的真实硬件位移
- `capture_window.py` —— 抢键盘的那层全屏透明窗口；虚拟屏上的窗口收不到鼠标事件，
  所以点击改由 tap 转发
- `safety.py` —— 看门狗：主线程停止心跳就自杀，绝不把整台 Mac 留在没有输入的状态
- `edge_trigger.py` —— 边缘带、进出迟滞（与 Windows 版一致）
- `vpointer.py` —— 虚拟绝对位置、顶边推出退出（一致）
- `calibration.py` —— 32 档曲线；`report_interval` 必须保持 0.03，iOS 按报文到达
  速度分加速度档
- `hid.py` —— 198 字节 Report Map 与报表编码器（与 Windows 版逐字节相同，
  SHA-256 `b74d9ebb…d80e3`）
- `gatt_table.py` —— GATT 数据库写成纯数据，外加 128 位展开
- `ble_hid.py` —— CoreBluetooth 外设：串行 `addService:`、同步 `updateValue:` 加自己的
  补发队列、订阅跟踪
- `core.py` —— `Sender` / `Controller`：什么时候发哪路报表、按住重发、拖拽路由
- `app.py` —— 入口：参数、30ms 节拍、两条输入路径与自动回退

路由规则：

| 场景 | 走哪路 | 为什么 |
|---|---|---|
| 悬停，无按键 | 绝对，Report 2 | 一条报表，零延迟，不用曲线 |
| 拖拽 | 相对，Report 3 带真实 `dx/dy` | 拖拽要在同一条报表里同时有按键和位移 |
| 按住不动 | 相对，`dx=dy=0` | 保持按住 |
| 键盘 | Report 1 | 状态式：每次变化发完整 8 字节快照 |

## 目录

```
ios_screen/          # 移植本体
  app.py             # 入口：参数、30ms 节拍、两条输入路径
  core.py            # Sender / Controller：发哪路、何时发、按住重发、拖拽
  ble_hid.py         # CoreBluetooth 外设
  gatt_table.py      # GATT 数据库 + 128 位展开
  hid.py             # 198 字节 Report Map 与编码器
  mac_input.py       # 事件 tap、光标、显示器枚举
  raw_mouse.py       # IOHIDManager，取 tap 看不到的位移
  vscreen.py         # 虚拟屏生命周期
  capture_window.py  # 抢键盘的那层窗口
  edge_trigger.py    # 边缘带与迟滞
  vpointer.py        # 绝对指针算术
  calibration.py     # 相对模式曲线
  keymap.py          # macOS 键码 → HID usage
  safety.py          # 看门狗
  userconfig.py      # 个人按键映射
  panel.py           # 面板模式
  geometry.py
  data/              # 校准曲线
launcher/            # 启动台：controller.py 管进程，app.py 管窗口
native/              # vdisplay.m（make_app.sh 现编）、launcher.icns
make_app.sh          # 生成 dist/Mac iPhone Touch.app，同时生成 run.sh
make_launcher.sh     # 生成 dist/启动台.app
run.sh               # 用户入口（写 launch.args，再 open）
tools/               # 探针工具，外加可选的 Deskflow 守护
  ble_probe.py       # 这台机器允许发布哪些服务 UUID？
  gatt_selfcheck.py  # 连上一台 HID 设备，dump 并核对它的 GATT 表
  deskflow_guard.sh  # 可选：跟随 Deskflow 自动启停（与启动台二选一）
tests/               # 114 项，不需要硬件
```

## 单测

```bash
.venv/bin/python -m unittest discover -s tests
```

钉住的都是「重新发现一次很贵」的东西：Report Map 指纹、每个报表上的 `0x2908` 描述符、
所有数据特征的加密权限、Protocol Mode 的例外、左右修饰键、6KRO、两个物理键不能落到同一个
HID usage、边缘迟滞、顶边推出的算术、服务 UUID 的 128 位展开、虚拟屏几何，以及启动台的
进程匹配。

其中两项是因为坑真的踩过才补的：一个实例属性遮住了同名方法（`_on_subscribe`）导致
CoreBluetooth 委托崩溃；`is_at_edge` 用了 `>=`，让 iPhone 只能进去一次。

## 诊断

```bash
# 1. 这台 Mac 到底允许发布什么？
./run.sh --module=tools.ble_probe --new

# 2. 本机发布起来，再从**另一台** Mac 上跑同一条命令看自己
./run.sh --module=tools.ble_probe --new --advertise

# 3. 连上一台 HID 设备，dump 它的 GATT 表
./run.sh --module=tools.gatt_selfcheck --new --name=DESKTOP-XXXX

# 4. 只列附近有什么
./run.sh --module=tools.gatt_selfcheck --new --dump
```

## 已知限制

- **虚拟屏用的是私有 API。** `native/vdisplay.m` 包的是 `CGVirtualDisplay`，苹果随时
  可能改掉。编译失败不影响运行，`--no-vscreen` 保留旧行为。
- **远程键鼠必须重连一次。** Deskflow 在连接时就把桌面尺寸报给服务端了；虚拟屏是在那
  之后才出现的，服务端仍以为桌面到原来的宽度为止，光标走到那里就停住。屏出现后断开重连
  （或重新添加这台 Mac）即可。
- **只靠边缘模式接不了 Deskflow。** client 模式下它产生的是合成鼠标事件而不是 HID 报文，
  光标钉在右缘之后 `DeltaX` 恒为 0，指针只能沿手机左缘上下滑。虚拟屏之所以是默认路径，
  就是为了这个。
- **虚拟屏上的点击会先落给桌面。** 抓取层只盖真实屏——放在 `CGVirtualDisplay` 上的窗口
  根本收不到鼠标事件，点击只能从只读 tap 里取。如果某次点击让 Finder 闪了一下焦点，
  就是这个原因。
- Mac 扫不到自己的广播，所以不存在单机端到端测试。
- ad-hoc 签名意味着重新打包后可能要在辅助功能里重新勾选。
- 一次只建一块虚拟屏，对应一台手机。映射本身是等比的，换机型不用改配置。
- 这棵树只有 macOS。Windows 实现见
  [Windows_iPhone_touch](https://github.com/Heresyouyou/Windows_iPhone_touch)。

## 与 Windows 版的差异

| | Windows | macOS（本树） |
|---|---|---|
| 输入捕获 | Raw Input + 低级钩子（300ms 超时、`ShowCursor` 引用计数） | 读虚拟屏里的光标位置；点击走只读 tap；边缘模式用 `IOHIDManager` |
| 进入方式 | 光标撞到屏幕边缘 | 光标走进虚拟屏（默认），或 `--no-vscreen` 下推边缘 |
| HID 外设 | WinRT `GattServiceProvider` | `CBPeripheralManager` |
| 服务 UUID | 直接发 16 位 `0x1812` | 必须展开成 128 位 |
| Boot Protocol / Appearance 特征 | 提供 | 不提供（只做 Report 协议，同 iPhone Use） |
| GAP 服务 | 必须显式创建 | 系统自带，**不能**加 |
| 线程 | 两线程加锁 | 单线程 + run loop |
| 通知背压 | `notify_value_async` 永远「成功」 | `updateValue:` 返回 `Bool`，需要补发队列 |
| 进程形态 | 普通脚本 | `.app` 包（TCC） |
| 权限 | 无 | 辅助功能 + 蓝牙 |
| 键码 | 虚拟键码 | 物理位置键码 |

## 相关开源项目

- [iPhone Use](https://github.com/YueChen-C/iPhone-use) —— macOS 上的原始思路
- [Windows_iPhone_touch](https://github.com/Heresyouyou/Windows_iPhone_touch) —— 本树的移植来源

## 许可

MIT，见 [LICENSE](LICENSE)。
