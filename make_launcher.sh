#!/bin/bash
# 生成 dist/启动台.app —— 双击就能管 Deskflow 和 iPhone 接入。
#
# 为什么要打包：这个窗口要长期停在 Dock 里，也要能被 Cmd+Tab 切到。
# 直接 python 跑出来的窗口没有 bundle 身份，Dock 上是个 Python 图标，
# 双击也没法启动。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$ROOT/dist/启动台.app"
PY="$ROOT/.venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "没有找到 $PY，先建虚拟环境"
  exit 1
fi

if [ ! -f "$ROOT/dist/Mac iPhone Touch.app/Contents/Info.plist" ]; then
  echo "⚠️ 本体还没打包，启动台里的 iPhone 接入会打不开。先跑 ./make_app.sh"
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

if [ -f "$ROOT/native/launcher.icns" ]; then
  cp "$ROOT/native/launcher.icns" "$APP/Contents/Resources/AppIcon.icns"
fi

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>work.kust.macphonetouch.launcher</string>
  <key>CFBundleName</key><string>启动台</string>
  <key>CFBundleDisplayName</key><string>启动台</string>
  <key>CFBundleExecutable</key><string>launch</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# 双击启动时 LaunchServices 会传 -psn_xxx，Python 侧已经过滤掉
cat > "$APP/Contents/MacOS/launch" <<LAUNCH
#!/bin/bash
cd "$ROOT" || exit 1
LOG="\$HOME/Library/Logs/MacPhoneTouch-launcher.log"
mkdir -p "\$(dirname "\$LOG")"
exec "$PY" -u -m launcher.app "\$@" >>"\$LOG" 2>&1
LAUNCH

chmod +x "$APP/Contents/MacOS/launch"

codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || \
  echo "⚠️ ad-hoc 签名失败（不影响本机使用）"

echo "已生成: $APP"
echo "双击打开，或: open \"$APP\""
echo "日志: ~/Library/Logs/MacPhoneTouch-launcher.log"
