#!/bin/bash
# 启动 dist/Mac iPhone Touch.app，并把参数带进去。
#
#   ./run.sh                         边缘模式，手机在右侧
#   ./run.sh --edge left --screen 1  手机在左侧、用 1 号屏
#   ./run.sh --panel                 面板模式
#   ./run.sh --new                   强制新开一个实例
#
# 为什么不直接 open --args：本机实测 argv 传不进去（编译过的 app 也一样）。
# 所以参数落到 launch.args，由 .app 里的 launch 读完即删。
set -u
APP="/Users/mac/WorkBuddy/2026-11-01-21-03-27/wtouch/dist/Mac iPhone Touch.app"
ARGFILE="$HOME/Library/Application Support/MacPhoneTouch/launch.args"
mkdir -p "$(dirname "$ARGFILE")"

NEW=0
ARGS=()
for a in "$@"; do
  if [ "$a" = "--new" ]; then NEW=1; else ARGS+=("$a"); fi
done

if [ "${#ARGS[@]}" -gt 0 ]; then
  printf '%s\n' "${ARGS[@]}" > "$ARGFILE"
else
  rm -f "$ARGFILE"
fi

if [ "$NEW" = 1 ]; then
  open -n "$APP"
else
  open "$APP"
fi
