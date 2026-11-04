#!/bin/bash
# start.sh — 手动启动 iPhone 副屏守护进程
#   打开 iPhone Use 时自动建虚拟屏并把窗口摆好；关闭时自动撤掉。
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="work.kust.iphonescreen"

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$(id -u)/$LABEL" && echo "已通过 launchd 启动"
  exit 0
fi

if pgrep -f "$DIR/psagent" >/dev/null 2>&1; then
  echo "已经在运行了"
  exit 0
fi

cd "$DIR" || exit 1
nohup "$DIR/psagent" >>"$DIR/psagent.log" 2>&1 &
echo "已启动 pid=$!（关闭终端不影响）"
