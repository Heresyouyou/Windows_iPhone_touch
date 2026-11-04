#!/bin/bash
# 卸载自启动 — 双击运行，停止守护进程并取消登录自启
set -u
LABEL="work.kust.iphonescreen"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "已停止服务"
pkill -f "tools/phonescreen/psagent" 2>/dev/null && echo "已结束残留进程"

if [ -f "$PLIST" ]; then
  rm -f "$PLIST" && echo "已删除 $PLIST"
fi
echo "完成：下次登录不会再自动运行"
echo
read -r -p "按回车关闭..." _
