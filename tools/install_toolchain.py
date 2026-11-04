"""提权安装 VS Build Tools（C++ 工具链 + Windows SDK）。

为什么必须 MSVC：
  HOGP 要走 WinRT 的 GattServiceProvider，C++/WinRT 投影只有 MSVC + Windows SDK 才能编。
  MinGW 能编 Win32 外壳，但对着 WinRT 的 ABI 手写 vtable 是给自己找麻烦。

安装器行为：
  --quiet --wait --norestart 是静默 + 等子进程结束；
  ShellExecuteW("runas") 负责弹 UAC，普通权限启动会直接失败。
"""
import ctypes
import os
import subprocess
import sys
import time

EXE = r"D:\tools\iPhone control\tools\dl\vs_BuildTools.exe"

ARGS = " ".join([
    "--quiet", "--wait", "--norestart", "--nocache",
    "--add", "Microsoft.VisualStudio.Workload.VCTools",
    "--add", "Microsoft.VisualStudio.Component.Windows11SDK.22621",
    "--includeRecommended",
])

VCTOOLS = r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC"
SDK_BIN = r"C:\Program Files (x86)\Windows Kits\10\bin"


def installed():
    return os.path.isdir(VCTOOLS)


def main():
    if not os.path.isfile(EXE):
        print("找不到安装器:", EXE)
        return 1
    if installed():
        print("已安装:", VCTOOLS)
        return 0

    print("启动提权安装，请在 UAC 对话框点『是』…")
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", EXE, ARGS, os.path.dirname(EXE), 1
    )
    if rc <= 32:
        print(f"ShellExecuteW 失败 code={rc}（1223 = 用户取消了 UAC）")
        return 1

    t0 = time.time()
    last = ""
    while time.time() - t0 < 3600:
        time.sleep(20)
        done = installed()
        sdk = os.path.isdir(SDK_BIN)
        # 只在状态变化时打印，避免刷屏
        state = f"MSVC={'OK' if done else '...'} SDK={'OK' if sdk else '...'}  {int(time.time()-t0)}s"
        if state != last:
            print(state, flush=True)
            last = state
        if done and sdk:
            print("安装完成")
            return 0
        # 安装器进程退出且目录仍不全 = 失败
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq vs_BuildTools.exe", "/NH"],
            capture_output=True, text=True, errors="ignore",
        )
        if "vs_BuildTools" not in (r.stdout or "") and time.time() - t0 > 90:
            print("安装器已退出但目标目录不完整，判定失败")
            print((r.stdout or "")[:400])
            return 1
    print("超时（60 分钟）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
