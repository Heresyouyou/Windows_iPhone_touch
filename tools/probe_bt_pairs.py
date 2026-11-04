"""查蓝牙配对设备的真实名字。

Windows 把 Name 存成 REG_BINARY 的 UTF-16LE（带结尾 \\0），直接读会看到一堆十六进制。
另外 BTHLE 节点下会写 Vid/Pid，能反推厂商。
"""
import ctypes
import ctypes.wintypes as wt
import winreg

setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
setupapi.SetupDiGetClassDevsW.argtypes = [
    ctypes.POINTER(ctypes.c_byte), ctypes.c_wchar_p, ctypes.c_void_p, wt.DWORD
]
setupapi.SetupDiEnumDeviceInfo.restype = wt.BOOL
setupapi.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.c_void_p]
setupapi.SetupDiGetDeviceRegistryPropertyW.restype = wt.BOOL
setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
    ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
]
setupapi.SetupDiGetDeviceInstanceIdW.restype = wt.BOOL
setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, wt.DWORD, ctypes.POINTER(wt.DWORD)
]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]


def decode_name(raw):
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, bytes):
        return str(raw)
    for enc in ("utf-16-le", "utf-8", "latin-1"):
        try:
            s = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        s = s.split("\x00")[0]
        if s and all(32 <= ord(c) < 0x10000 for c in s):
            return s
    return raw.hex()


def paired_devices():
    base = r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices"
    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                addr = winreg.EnumKey(k, i)
                e = {"addr": ":".join(addr[i:i+2] for i in range(0, len(addr), 2)).upper()}
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{base}\\{addr}") as dk:
                        for f in ("Name", "Class", "Cod", "AddressType", "LastSeen"):
                            try:
                                v, _ = winreg.QueryValueEx(dk, f)
                                e[f] = decode_name(v) if f == "Name" else v
                            except OSError:
                                pass
                except OSError:
                    pass
                out.append(e)
    except OSError as ex:
        print("读不到 BTHPORT（可能需要管理员）:", ex)
    return out


def bthle_nodes():
    base = r"SYSTEM\CurrentControlSet\Enum\BTHLE"
    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                dev = winreg.EnumKey(k, i)
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{base}\\{dev}") as dk:
                        for j in range(winreg.QueryInfoKey(dk)[1]):
                            name, val, _ = winreg.EnumValue(dk, j)
                            if name in ("FriendlyName", "DeviceDesc", "ContainerID", "Service"):
                                out.append((dev, name, val))
                except OSError:
                    pass
    except OSError:
        pass
    return out


if __name__ == "__main__":
    print("=== 已配对蓝牙设备 ===")
    for d in paired_devices():
        cls = d.get("Cod", 0)
        # CoD 高位：0x02 = 电话，0x04 = 音频/视频，0x05 = 外设，0x01 = 电脑
        major = (cls >> 8) & 0x1F if isinstance(cls, int) else 0
        major_name = {
            1: "电脑", 2: "手机", 3: "LAN/网络", 4: "音频/视频",
            5: "外设(键鼠)", 6: "成像", 7: "可穿戴", 8: "玩具", 0: "杂项",
        }.get(major, f"?({major})")
        print(f"  {d['addr']}  {d.get('Name','?'):<28} type={major_name}  Cod={hex(cls) if isinstance(cls,int) else cls}")

    print("\n=== BTHLE 节点属性 ===")
    for dev, name, val in bthle_nodes():
        print(f"  {dev}  {name} = {val}")
