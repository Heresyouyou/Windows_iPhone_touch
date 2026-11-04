"""探测/激活虚拟显示器。

分两步：
  1. probe  —— 只读，看每块 detached 屏有没有可用的注册表模式（不改变系统状态）
  2. attach —— 尝试把它启用并摆到主屏右侧（会改显示设置，可逆）

用法:
  python tools/vdisplay.py probe
  python tools/vdisplay.py attach --device \\\\.\\DISPLAY5 --width 664 --height 1440 --x 3440 --y 0
  python tools/vdisplay.py detach --device \\\\.\\DISPLAY5
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import sys

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()

ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
ENUM_REGISTRY_SETTINGS = 0xFFFFFFFE

DM_BITSPERPEL = 0x00040000
DM_PELSWIDTH = 0x00080000
DM_PELSHEIGHT = 0x00100000
DM_POSITION = 0x00000020
DM_DISPLAYFREQUENCY = 0x00400000

CDS_UPDATEREGISTRY = 0x00000001
CDS_TEST = 0x00000002
CDS_NORESET = 0x10000000

DISP_CHANGE_SUCCESSFUL = 0
DISP_CHANGE_MAP = {
    1: "DISP_CHANGE_RESTART（需要重启）",
    -1: "DISP_CHANGE_FAILED（驱动不支持该模式）",
    -2: "DISP_CHANGE_BADMODE（模式不被接受）",
    -3: "DISP_CHANGE_NOTUPDATED（写注册表失败）",
    -4: "DISP_CHANGE_BADFLAGS（参数错误）",
    -5: "DISP_CHANGE_BADPARAM（含非法标志）",
    -6: "DISP_CHANGE_BADDUALVIEW（需要双屏支持）",
}


class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", wt.WCHAR * 32),
        ("dmSpecVersion", wt.WORD),
        ("dmDriverVersion", wt.WORD),
        ("dmSize", wt.WORD),
        ("dmDriverExtra", wt.WORD),
        ("dmFields", wt.DWORD),
        ("dmPosition", wt.POINT),
        ("dmDisplayOrientation", wt.DWORD),
        ("dmDisplayFixedOutput", wt.DWORD),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", wt.WCHAR * 32),
        ("dmLogPixels", wt.WORD),
        ("dmBitsPerPel", wt.DWORD),
        ("dmPelsWidth", wt.DWORD),
        ("dmPelsHeight", wt.DWORD),
        ("dmDisplayFlags", wt.DWORD),
        ("dmDisplayFrequency", wt.DWORD),
        ("dmICMMethod", wt.DWORD),
        ("dmICMIntent", wt.DWORD),
        ("dmMediaType", wt.DWORD),
        ("dmDitherType", wt.DWORD),
        ("dmReserved1", wt.DWORD),
        ("dmReserved2", wt.DWORD),
        ("dmPanningWidth", wt.DWORD),
        ("dmPanningHeight", wt.DWORD),
    ]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("DeviceName", wt.WCHAR * 32),
        ("DeviceString", wt.WCHAR * 128),
        ("StateFlags", wt.DWORD),
        ("DeviceID", wt.WCHAR * 128),
        ("DeviceKey", wt.WCHAR * 128),
    ]


def displays():
    out = []
    i = 0
    while True:
        d = DISPLAY_DEVICEW()
        d.cb = ctypes.sizeof(d)
        if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(d), 0):
            break
        out.append(d)
        i += 1
    return out


def query(dev, mode):
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(dm)
    ok = user32.EnumDisplaySettingsExW(dev, mode, ctypes.byref(dm), 0)
    if not ok:
        return None
    return dm


def probe():
    for d in displays():
        state = []
        if d.StateFlags & 0x1:
            state.append("ATTACHED")
        if d.StateFlags & 0x2:
            state.append("MULTIDRIVER")
        if d.StateFlags & 0x4:
            state.append("PRIMARY")
        if d.StateFlags & 0x8:
            state.append("MIRRORING")
        if d.StateFlags & 0x20:
            state.append("VGA")
        print(f"\n{d.DeviceName}  {d.DeviceString!r}")
        print(f"   flags={'|'.join(state) or 'DETACHED'}")
        cur = query(d.DeviceName, ENUM_CURRENT_SETTINGS)
        reg = query(d.DeviceName, ENUM_REGISTRY_SETTINGS)
        for tag, dm in (("current", cur), ("registry", reg)):
            if dm is None:
                print(f"   {tag}: (无)")
            else:
                print(
                    f"   {tag}: {dm.dmPelsWidth}x{dm.dmPelsHeight}"
                    f" @({dm.dmPosition.x},{dm.dmPosition.y})"
                    f" {dm.dmBitsPerPel}bpp {dm.dmDisplayFrequency}Hz"
                )


def attach(dev, w, h, x, y, hz=60, commit=True):
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(dm)
    dm.dmPelsWidth = w
    dm.dmPelsHeight = h
    dm.dmBitsPerPel = 32
    dm.dmDisplayFrequency = hz
    dm.dmPosition.x = x
    dm.dmPosition.y = y
    dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_BITSPERPEL | DM_DISPLAYFREQUENCY | DM_POSITION
    dm.dmDisplayFlags = 0

    # 先 CDS_TEST，不改系统；通过了再真设
    rc = user32.ChangeDisplaySettingsExW(dev, ctypes.byref(dm), None, CDS_TEST, None)
    print(f"  CDS_TEST -> {rc} {DISP_CHANGE_MAP.get(rc, '')}")
    if rc != DISP_CHANGE_SUCCESSFUL:
        return False
    if not commit:
        return True

    rc = user32.ChangeDisplaySettingsExW(dev, ctypes.byref(dm), None, CDS_UPDATEREGISTRY, None)
    print(f"  提交     -> {rc} {DISP_CHANGE_MAP.get(rc, '')}")
    if rc != DISP_CHANGE_SUCCESSFUL:
        return False

    # 回读校验：不要"设完就信"
    now = query(dev, ENUM_CURRENT_SETTINGS)
    if now is None:
        print("  回读失败：显示器仍未激活")
        return False
    print(
        f"  回读: {now.dmPelsWidth}x{now.dmPelsHeight}"
        f" @({now.dmPosition.x},{now.dmPosition.y}) {now.dmDisplayFrequency}Hz"
    )
    return (
        now.dmPelsWidth == w
        and now.dmPelsHeight == h
        and now.dmPosition.x == x
        and now.dmPosition.y == y
    )


def detach(dev):
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(dm)
    dm.dmFields = 0
    dm.dmPelsWidth = 0
    dm.dmPelsHeight = 0
    dm.dmPosition.x = 0
    dm.dmPosition.y = 0
    dm.dmBitsPerPel = 0
    dm.dmDisplayFrequency = 0
    rc = user32.ChangeDisplaySettingsExW(dev, ctypes.byref(dm), None, 0, None)
    print(f"  detach -> {rc} {DISP_CHANGE_MAP.get(rc, '')}")
    return rc == DISP_CHANGE_SUCCESSFUL


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["probe", "attach", "detach"])
    ap.add_argument("--device", default=r"\\.\DISPLAY5")
    ap.add_argument("--width", type=int, default=664)
    ap.add_argument("--height", type=int, default=1440)
    ap.add_argument("--x", type=int, default=3440)
    ap.add_argument("--y", type=int, default=0)
    ap.add_argument("--hz", type=int, default=60)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    if a.cmd == "probe":
        probe()
    elif a.cmd == "attach":
        ok = attach(a.device, a.width, a.height, a.x, a.y, a.hz, commit=not a.dry)
        print("结果:", "成功" if ok else "失败")
        return 0 if ok else 1
    else:
        ok = detach(a.device)
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
