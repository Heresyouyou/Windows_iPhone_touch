"""环境探测：显示器布局、蓝牙硬件、已配对设备、iPhone 连接方式。

只读，不改任何系统状态。Windows 上避免 PowerShell（易静默失败），统一用 ctypes + winreg。
"""
import ctypes
import ctypes.wintypes as wt
import platform
import subprocess
import sys
import winreg

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()


def enum_monitors():
    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.DWORD),
            ("rcMonitor", wt.RECT),
            ("rcWork", wt.RECT),
            ("dwFlags", wt.DWORD),
            ("szDevice", wt.WCHAR * 32),
        ]

    out = []

    def cb(hmon, hdc, lprc, data):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(mi)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            out.append(mi)
        return True

    proto = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)
    user32.EnumDisplayMonitors(None, None, proto(cb), 0)
    return out


def display_config():
    """用 EnumDisplayDevices 读出每块屏的设备名、描述、状态。"""
    class DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD),
            ("DeviceName", wt.WCHAR * 32),
            ("DeviceString", wt.WCHAR * 128),
            ("StateFlags", wt.DWORD),
            ("DeviceID", wt.WCHAR * 128),
            ("DeviceKey", wt.WCHAR * 128),
        ]

    res = []
    i = 0
    while True:
        d = DISPLAY_DEVICEW()
        d.cb = ctypes.sizeof(d)
        if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(d), 0):
            break
        res.append(
            {
                "name": d.DeviceName,
                "desc": d.DeviceString,
                "flags": d.StateFlags,
                "id": d.DeviceID,
                "attached": bool(d.StateFlags & 0x1),
                "primary": bool(d.StateFlags & 0x4),
            }
        )
        i += 1
    return res


def reg_enum(root, path, depth=1, limit=200):
    """递归列注册表子键名，depth 控制深度。"""
    got = []
    try:
        with winreg.OpenKey(root, path) as k:
            n = winreg.QueryInfoKey(k)[0]
            for i in range(min(n, limit)):
                try:
                    name = winreg.EnumKey(k, i)
                except OSError:
                    break
                got.append(name)
                if depth > 1:
                    for sub in reg_enum(root, path + "\\" + name, depth - 1, limit):
                        got.append(name + "\\" + sub)
    except OSError:
        pass
    return got


def reg_device_props(root, path):
    """读设备键里的 FriendlyName / DeviceDesc。"""
    res = {}
    for sub in ("", "\\Device Parameters"):
        try:
            with winreg.OpenKey(root, path + sub) as k:
                for field in ("FriendlyName", "DeviceDesc", "Driver", "PortName"):
                    try:
                        v, _ = winreg.QueryValueEx(k, field)
                        if isinstance(v, str):
                            res[field] = v
                    except OSError:
                        pass
        except OSError:
            pass
    return res


def bluetooth_info():
    info = {}
    # 蓝牙无线电（适配器）
    radios = []
    base = r"SYSTEM\CurrentControlSet\Enum\BTH"
    for name in reg_enum(winreg.HKEY_LOCAL_MACHINE, base, depth=1):
        if "\\" in name:
            props = reg_device_props(winreg.HKEY_LOCAL_MACHINE, base + "\\" + name)
            if props:
                radios.append((name, props))
    info["bth_enum"] = radios[:20]

    # BLE 服务（GATT）—— 说明系统有 BLE 栈
    info["bthledevice"] = reg_enum(
        winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Enum\BTHLE", depth=1
    )[:20]

    # 已配对设备
    paired = []
    pbase = r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, pbase) as k:
            n = winreg.QueryInfoKey(k)[0]
            for i in range(n):
                addr = winreg.EnumKey(k, i)
                entry = {"addr": addr}
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, pbase + "\\" + addr) as dk:
                        for f in ("Name", "Class", "Cod", "AddressType"):
                            try:
                                v, _ = winreg.QueryValueEx(dk, f)
                                entry[f] = v if not isinstance(v, bytes) else v.hex()
                            except OSError:
                                pass
                except OSError:
                    pass
                paired.append(entry)
    except OSError:
        pass
    info["paired"] = paired
    return info


def usb_apple():
    hits = []
    base = r"SYSTEM\CurrentControlSet\Enum\USB"
    for name in reg_enum(winreg.HKEY_LOCAL_MACHINE, base, depth=1):
        low = name.lower()
        if "apple" in low or "iphone" in low or "vid_05ac" in low:
            props = reg_device_props(winreg.HKEY_LOCAL_MACHINE, base + "\\" + name)
            hits.append((name, props))
    return hits


def main():
    print("=" * 70)
    print("系统:", platform.platform(), "|", platform.machine())
    print("Python:", sys.version.split()[0])
    print("=" * 70)

    print("\n[显示器 · EnumDisplayMonitors]")
    mons = enum_monitors()
    for m in mons:
        r, w = m.rcMonitor, m.rcWork
        flag = " [PRIMARY]" if m.dwFlags & 0x1 else ""
        print(
            f"  {m.szDevice}  {r.right-r.left}x{r.bottom-r.top} at ({r.left},{r.top})"
            f"  work={w.right-w.left}x{w.bottom-w.top}{flag}"
        )
    print(f"  共 {len(mons)} 块")

    print("\n[显示适配器 · EnumDisplayDevices]")
    for d in display_config():
        print(f"  {d['name']}  {d['desc']!r}  attached={d['attached']} primary={d['primary']}")
        print(f"      id={d['id']}")

    print("\n[蓝牙 · BTH 枚举]")
    bt = bluetooth_info()
    if not bt["bth_enum"]:
        print("  (空) 没有找到蓝牙设备节点")
    for name, props in bt["bth_enum"]:
        print(f"  {name}")
        for k, v in props.items():
            print(f"      {k} = {v}")

    print("\n[蓝牙 LE 节点 BTHLE]")
    print("  " + (", ".join(bt["bthledevice"]) if bt["bthledevice"] else "(空)"))

    print("\n[已配对蓝牙设备]")
    if not bt["paired"]:
        print("  (空)")
    for p in bt["paired"]:
        print("  " + "  ".join(f"{k}={v}" for k, v in p.items()))

    print("\n[USB · Apple]")
    hits = usb_apple()
    if not hits:
        print("  (空) 未发现 Apple USB 设备")
    for name, props in hits:
        print(f"  {name}")
        for k, v in props.items():
            print(f"      {k} = {v}")

    print("\n[Process 位宽]")
    print("  ", platform.architecture())


if __name__ == "__main__":
    main()
