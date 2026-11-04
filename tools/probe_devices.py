"""用 SetupAPI 枚举设备，替代不可靠的 pnputil / PowerShell。

用法: python tools/probe_devices.py [bluetooth|display|monitor|all]
"""
import ctypes
import ctypes.wintypes as wt
import sys

setupapi = ctypes.WinDLL("setupapi", use_last_error=True)

# 关键：句柄是 64 位，不设 restype 会被截断成 32 位 int，导致 SetupDiGetClassDevs 恒返回 -1
setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
setupapi.SetupDiGetClassDevsW.argtypes = [
    ctypes.POINTER(ctypes.c_byte),
    ctypes.c_wchar_p,
    ctypes.c_void_p,
    wt.DWORD,
]
setupapi.SetupDiEnumDeviceInfo.restype = wt.BOOL
setupapi.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.c_void_p]
setupapi.SetupDiGetDeviceRegistryPropertyW.restype = wt.BOOL
setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    wt.DWORD,
    ctypes.POINTER(wt.DWORD),
    ctypes.c_void_p,
    wt.DWORD,
    ctypes.POINTER(wt.DWORD),
]
setupapi.SetupDiGetDeviceInstanceIdW.restype = wt.BOOL
setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_wchar_p,
    wt.DWORD,
    ctypes.POINTER(wt.DWORD),
]
setupapi.SetupDiDestroyDeviceInfoList.restype = wt.BOOL
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]

DIGCF_PRESENT = 0x02
DIGCF_ALLCLASSES = 0x04

SPDRP_DEVICEDESC = 0x00
SPDRP_HARDWAREID = 0x01
SPDRP_CLASS = 0x07
SPDRP_CLASSGUID = 0x08
SPDRP_FRIENDLYNAME = 0x0C
SPDRP_LOCATION_INFORMATION = 0x0D

CLASSES = {
    "bluetooth": "{e0cbf06c-cd8b-4647-bb8a-263b43f0f974}",
    "display": "{4d36e968-e325-11ce-bfc1-08002be10318}",
    "monitor": "{4d36e96e-e325-11ce-bfc1-08002be10318}",
}


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wt.DWORD),
        ("Data2", wt.WORD),
        ("Data3", wt.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("ClassGuid", GUID),
        ("DevInst", wt.DWORD),
        ("Reserved", ctypes.c_void_p),
    ]


def guid_from_str(s):
    g = GUID()
    ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
    return g


def get_prop(hdev, prop):
    buf = ctypes.create_string_buffer(2048)
    needed = wt.DWORD()
    ok = setupapi.SetupDiGetDeviceRegistryPropertyW(
        hdev, ctypes.byref(hdev._data), prop, None, buf, ctypes.sizeof(buf), ctypes.byref(needed)
    )
    if not ok:
        return None
    if needed.value == 0:
        return ""
    # REG_MULTI_SZ 用 \0 分隔
    raw = buf.raw[: needed.value]
    try:
        txt = raw.decode("utf-16-le").rstrip("\x00")
    except UnicodeDecodeError:
        return None
    return txt.replace("\x00", " | ")


def get_instance_id(hdev):
    buf = ctypes.create_unicode_buffer(1024)
    needed = wt.DWORD()
    ok = setupapi.SetupDiGetDeviceInstanceIdW(
        hdev, ctypes.byref(hdev._data), buf, ctypes.sizeof(buf), ctypes.byref(needed)
    )
    return buf.value if ok else "?"


class DevHandle:
    def __init__(self, ptr):
        self.ptr = ptr
        self._data = SP_DEVINFO_DATA()
        self._data.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)


def enum_class(class_guid, present_only=True):
    flags = DIGCF_PRESENT if present_only else 0
    g = guid_from_str(class_guid)
    gp = ctypes.cast(ctypes.byref(g), ctypes.POINTER(ctypes.c_byte))
    h = setupapi.SetupDiGetClassDevsW(gp, None, None, flags)
    if not h or h == 0xFFFFFFFFFFFFFFFF:
        return []
    out = []
    i = 0
    while True:
        d = SP_DEVINFO_DATA()
        d.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
        if not setupapi.SetupDiEnumDeviceInfo(h, wt.DWORD(i), ctypes.byref(d)):
            break
        dp = ctypes.cast(ctypes.byref(d), ctypes.c_void_p)

        buf = ctypes.create_string_buffer(2048)
        setupapi.SetupDiGetDeviceRegistryPropertyW(
            h, dp, SPDRP_FRIENDLYNAME, None, buf, 2048, None
        )
        friendly = buf.raw.decode("utf-16-le", "ignore").split("\x00")[0]

        buf2 = ctypes.create_string_buffer(2048)
        setupapi.SetupDiGetDeviceRegistryPropertyW(h, dp, SPDRP_DEVICEDESC, None, buf2, 2048, None)
        desc = buf2.raw.decode("utf-16-le", "ignore").split("\x00")[0]

        iid = ctypes.create_unicode_buffer(1024)
        setupapi.SetupDiGetDeviceInstanceIdW(h, dp, iid, 1024, None)

        out.append({"friendly": friendly or desc, "desc": desc, "id": iid.value})
        i += 1
    setupapi.SetupDiDestroyDeviceInfoList(h)
    return out


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    targets = CLASSES.keys() if which == "all" else [which]
    for t in targets:
        print(f"\n=== {t} ===")
        devs = enum_class(CLASSES[t], present_only=True)
        if not devs:
            print("  (当前无在线设备)")
        for d in devs:
            print(f"  {d['friendly']}")
            print(f"      desc={d['desc']}")
            print(f"      id={d['id']}")

    if which in ("all", "bluetooth"):
        print("\n=== 蓝牙无线电（BTHPORT / BTHUSB 服务）===")
        import winreg

        for svc in ("BTHUSB", "BTHPORT", "BthLEEnum", "BthA2dp"):
            try:
                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    rf"SYSTEM\CurrentControlSet\Services\{svc}",
                ) as k:
                    start = winreg.QueryValueEx(k, "Start")[0]
                    print(f"  {svc}: 存在, Start={start}")
            except OSError:
                print(f"  {svc}: 不存在")


if __name__ == "__main__":
    main()
