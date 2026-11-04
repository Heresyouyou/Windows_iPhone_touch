#!/bin/bash
# 安装自启动 — 双击运行，让 iPhone 副屏守护进程随登录自动运行
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/work.kust.iphonescreen.plist"
LABEL="work.kust.iphonescreen"

if [ ! -x "$DIR/psagent" ]; then
  echo "找不到 psagent，请先在本目录编译：clang -fobjc-arc -framework Cocoa -framework CoreGraphics -framework ApplicationServices -o psagent psagent.m"
  read -r -p "按回车关闭..." _
  exit 1
fi

# 保证 plist 指向当前路径
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array><string>$DIR/psagent</string><string>-v</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
    <key>ThrottleInterval</key><integer>15</integer>
    <key>ProcessType</key><string>Interactive</string>
    <key>StandardOutPath</key><string>$DIR/psagent.log</string>
    <key>StandardErrorPath</key><string>$DIR/psagent.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
# 清掉手动启动的实例，避免两份进程抢锁
pkill -f "$DIR/psagent" 2>/dev/null
sleep 1

if launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then
  echo "✅ 已安装并启动：登录后自动拉起，iPhone Use 打开时自动建副屏"
else
  echo "❌ 安装失败。请手动执行："
  echo "   launchctl bootstrap gui/$(id -u) \"$PLIST\""
fi

sleep 1
launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E "state|pid =" || true
echo
read -r -p "按回车关闭..." _
