#!/bin/bash
# stop.sh — 停止守护进程；虚拟屏随之消失，桌面恢复原状
set -u
LABEL="work.kust.iphonescreen"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "已停止 launchd 服务"
pkill -f "tools/phonescreen/psagent" 2>/dev/null && echo "已结束进程"
sleep 1
echo "已停止"
