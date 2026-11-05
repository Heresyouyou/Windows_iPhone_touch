#!/bin/bash
# 生成 dist/Mac iPhone Touch.app —— 一个可双击、也可 open -a 启动的应用包。
#
# 为什么必须打包：CoreBluetooth 第一次使用时，macOS 的 TCC 会读「责任应用」的
# Info.plist 找 NSBluetoothAlwaysUsageDescription。命令行 python 没有这份 plist，
# 结果不是报错而是**直接 abort**（崩溃报告里写得很明白）。
# 用 LaunchServices 启动的 .app 才有 bundle 身份，TCC 才认。
#
# 用法：
#   ./make_app.sh
#   open -a "dist/Mac iPhone Touch.app"
#   open -a "dist/Mac iPhone Touch.app" --args --edge left --screen 1
#   open -a "dist/Mac iPhone Touch.app" --args --panel
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$ROOT/dist/Mac iPhone Touch.app"
PY="$ROOT/.venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "没有找到 $PY"
  echo "先建虚拟环境："
  echo "  python3 -m venv .venv"
  echo "  .venv/bin/pip install -r requirements.txt"
  exit 1
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# 虚拟屏工具：用 CGVirtualDisplay 造一块贴在主屏右侧的显示器，
# 让 iPhone 在系统层面就是"右边多出来的一块屏"。
# 不编译也能跑（会退回边缘触发模式），所以失败只警告、不中断打包。
if [ -f "$ROOT/native/vdisplay.m" ]; then
  if clang -fobjc-arc -O2 -framework Cocoa -framework CoreGraphics \
        -o "$ROOT/native/vdisplay" "$ROOT/native/vdisplay.m" 2>/tmp/vdisplay-build.log; then
    echo "已编译 native/vdisplay"
  else
    echo "⚠️ 虚拟屏工具编译失败，将退回边缘触发模式："
    sed -n '1,6p' /tmp/vdisplay-build.log
    rm -f "$ROOT/native/vdisplay"
  fi
fi

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>work.kust.mac-iphone-touch</string>
  <key>CFBundleName</key><string>Mac iPhone Touch</string>
  <key>CFBundleDisplayName</key><string>Mac iPhone Touch</string>
  <key>CFBundleExecutable</key><string>launch</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><true/>
  <key>NSBluetoothAlwaysUsageDescription</key>
  <string>把 Mac 变成一个蓝牙键鼠，让 iPhone 在「辅助触控 → 设备」里接收指针和键盘输入。</string>
  <key>NSBluetoothPeripheralUsageDescription</key>
  <string>把 Mac 变成一个蓝牙键鼠，供 iPhone 连接。</string>
</dict>
</plist>
PLIST

# 启动脚本：转发参数；--panel 切到面板模式；日志落到 ~/Library/Logs
cat > "$APP/Contents/MacOS/launch" <<LAUNCH
#!/bin/bash
ROOT="$ROOT"
PY="$PY"
LOG="\$HOME/Library/Logs/MacPhoneTouch.log"
cd "\$ROOT" || exit 1

# 把 app 自己的路径告诉 Python，用于确认 Info.plist 里真的有蓝牙用途说明。
# 注意：这里必须用生成期展开的绝对路径。写 \$0 是错的——heredoc 未加引号时
# \$0 会被 make_app.sh 自己展开成「./make_app.sh」，dirname 之后再退两级就飘了。
export MPT_APP_BUNDLE="$APP"

MODULE=ios_screen.app
ARGS=()

# 参数从哪来？
# 本机的 `open --args`（以及 NSWorkspace 的 arguments）实测**完全不传参**：
# 拿一个编译过的 .app 试，argv 里只有 argv[0]；换成脚本可执行也一样。
# 所以参数走文件：run.sh 写一行一个参数，这里读到就删掉。
# 这样既保住 .app 的 LaunchServices 身份（蓝牙 TCC 只认它），又拿得到参数。
ARGFILE="\$HOME/Library/Application Support/MacPhoneTouch/launch.args"

add_arg() {
  case "\$1" in
    --panel)   MODULE=ios_screen.panel ;;
    --module=*) MODULE="\${1#--module=}" ;;   # 诊断入口：让 .app 跑 tools/ 下的探针
    -psn_*)    : ;;                            # LaunchServices 的历史遗留参数，丢掉
    *)         ARGS+=("\$1") ;;
  esac
}

if [ "\$#" -gt 0 ]; then
  # Terminal 里手动执行 launch 时走这里
  for a in "\$@"; do add_arg "\$a"; done
elif [ -f "\$ARGFILE" ]; then
  while IFS= read -r line; do
    [ -n "\$line" ] && add_arg "\$line"
  done < "\$ARGFILE"
  rm -f "\$ARGFILE"
fi

mkdir -p "\$(dirname "\$LOG")"
{
  echo "===== \$(date '+%F %T') 启动 \$MODULE \${ARGS[*]} ====="
} >>"\$LOG"

exec "\$PY" -u -m "\$MODULE" "\${ARGS[@]}" >>"\$LOG" 2>&1
LAUNCH

chmod +x "$APP/Contents/MacOS/launch"

# 用户层入口：run.sh。它把参数写进文件再交给 LaunchServices，
# 绕开本机失效的 open --args。
cat > "$ROOT/run.sh" <<RUNSH
#!/bin/bash
# 启动 dist/Mac iPhone Touch.app，并把参数带进去。
#
#   ./run.sh                         默认：主屏右侧出现虚拟屏
#   ./run.sh --no-vscreen            不建虚拟屏，退回推屏幕边缘
#   ./run.sh --edge left --screen 1  边缘模式，手机在左侧、用 1 号屏
#   ./run.sh --panel                 面板模式
#   ./run.sh --new                   强制新开一个实例
#
# 为什么不直接 open --args：本机实测 argv 传不进去（编译过的 app 也一样）。
# 所以参数落到 launch.args，由 .app 里的 launch 读完即删。
#
# 路径按脚本自身位置算，不写死 —— 仓库克隆到哪都能用。
set -u
HERE="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
APP="\$HERE/dist/Mac iPhone Touch.app"
ARGFILE="\$HOME/Library/Application Support/MacPhoneTouch/launch.args"
mkdir -p "\$(dirname "\$ARGFILE")"

NEW=0
ARGS=()
for a in "\$@"; do
  if [ "\$a" = "--new" ]; then NEW=1; else ARGS+=("\$a"); fi
done

if [ "\${#ARGS[@]}" -gt 0 ]; then
  printf '%s\n' "\${ARGS[@]}" > "\$ARGFILE"
else
  rm -f "\$ARGFILE"
fi

if [ "\$NEW" = 1 ]; then
  open -n "\$APP"
else
  open "\$APP"
fi
RUNSH
chmod +x "$ROOT/run.sh"

# 临时签名：让 TCC 记住这个应用的授权，重新打包后通常仍然是同一条记录。
# 没有开发者证书时 -s - 就是 ad-hoc 签名，本机使用足够。
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || \
  echo "⚠️ ad-hoc 签名失败（不影响本机使用，但每次重打包可能要在辅助功能里重新勾选）"

echo "已生成: $APP"
echo "        $ROOT/run.sh"
echo
echo "启动（默认边缘模式，手机在右侧）:"
echo "  ./run.sh"
echo "带参数启动:"
echo "  ./run.sh --edge left --screen 1"
echo "面板模式:"
echo "  ./run.sh --panel"
echo "诊断探针（看这台机器允许发布哪些蓝牙服务）:"
echo "  ./run.sh --module=tools.ble_probe --new"
echo "看日志:"
echo "  tail -f ~/Library/Logs/MacPhoneTouch.log"
echo "退出:"
echo "  捕获中按 Ctrl+Option+Q（再按一次结束程序），或 pkill -f ios_screen"
echo
echo "首次运行需要在「系统设置 → 隐私与安全性 → 辅助功能」里勾选本应用。"
