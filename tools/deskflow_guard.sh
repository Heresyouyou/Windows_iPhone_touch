#!/bin/bash
# 跟随 Deskflow 的生命周期：
#   Deskflow 启动 → 把 Mac iPhone Touch 拉起来
#   Deskflow 退出 → 把它收掉（连带虚拟屏一起消失）
#
# 只在状态**变化**的那一刻动手，不持续"保证在跑"——否则用户按
# Ctrl+Option+Q 关掉后会被反复拉起来，等于关不掉。
#
# 由 launchd 常驻（见 guard_ctl.sh install），也可以用 `--once` 手动跑一轮。
#
# 为什么用轮询而不是通知
# ----------------------
# Deskflow 没有"启动时执行命令"的钩子，也没有可订阅的退出通知。进程存在性
# 是唯一可靠的判据，而 pgrep 一次的开销在微秒级，2 秒一轮对系统没有影响。
#
# 为什么盯 GUI 进程而不是 deskflow-core
# -------------------------------------
# 用户说的"关掉 Deskflow"是退出那个应用。core 有可能在 GUI 退出后还残留
# 一小会儿（断开连接、清理），拿它当判据会让本程序晚关甚至不关。
set -u

# 路径从脚本自身位置推，不写死 —— 仓库克隆到哪都能用
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN="$ROOT/run.sh"
LOG="$HOME/Library/Logs/MacPhoneTouch-guard.log"
PIDFILE="$HOME/Library/Application Support/MacPhoneTouch/guard.pid"

# 允许覆盖，方便在没有 Deskflow 的环境里验证"关闭"这一支
DESKFLOW_PAT="${DESKFLOW_PAT:-MacOS/Deskflow$}"
IOS_PAT="${IOS_PAT:-ios_screen\.app}"
POLL="${POLL:-2}"
BOOT_GRACE="${BOOT_GRACE:-6}"

export PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

mkdir -p "$(dirname "$LOG")" "$(dirname "$PIDFILE")"

log() { echo "$(date '+%F %T') $*" >>"$LOG"; }

deskflow_running() { pgrep -f "$DESKFLOW_PAT" >/dev/null 2>&1; }
ios_running()      { pgrep -f "$IOS_PAT" >/dev/null 2>&1; }

start_ios() {
    log "Deskflow 在跑，拉起 Mac iPhone Touch"
    # run.sh 会把参数落到 launch.args 再 open：本机 open --args 不传 argv，
    # 走文件才能既保住 .app 的 TCC 身份（蓝牙只认它）又带得进参数。
    "$RUN" --new >>"$LOG" 2>&1
    # open 是异步的，进程要一两秒才出现。不等的话下一轮会再 open 一次，
    # 于是同时起两个实例，两个都在抢蓝牙。
    sleep "$BOOT_GRACE"
}

stop_ios() {
    log "Deskflow 已退出，收掉 Mac iPhone Touch"
    pkill -TERM -f "$IOS_PAT" 2>/dev/null
    for _ in $(seq 1 12); do
        ios_running || break
        sleep 0.5
    done
    if ios_running; then
        # 走到这里说明主循环卡住了（看门狗本该在 2.5 秒内自己退出）。
        # 强制结束是安全的：所有全局状态都在进程内，进程一没就全没了。
        log "优雅退出超时，强制结束"
        pkill -KILL -f "$IOS_PAT" 2>/dev/null
    fi
    # 万一 vdisplay 因故没跟着父进程退，这里补一刀，免得留下一块黑屏
    pkill -TERM -f "$ROOT/native/vdisplay" 2>/dev/null
    sleep 0.5
}

# ---- 单实例：launchd 的 KeepAlive 在异常退出后会重启，别叠成两个 ----
if [ -f "$PIDFILE" ]; then
    old="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [ -n "$old" ] && [ "$old" != "$$" ] && kill -0 "$old" 2>/dev/null; then
        log "已有守护在跑 pid=$old，本次退出"
        exit 0
    fi
fi
echo $$ >"$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT INT TERM

log "守护启动 pid=$$ deskflow='$DESKFLOW_PAT'"

if [ "${1:-}" = "--once" ]; then
    if deskflow_running; then
        ios_running || start_ios
    else
        ios_running && stop_ios
    fi
    exit 0
fi

# 边沿触发，而不是"持续保证在跑"。
#
# 差别在于谁说了算：
#   · 持续保证：用户按 Ctrl+Option+Q 关掉，2 秒后又被拉起来，关不掉；
#   · 边沿触发：只在 Deskflow 从"没在跑"变成"在跑"的那一刻拉一次，
#     之后用户想关就关，守护不插手。
#
# 启动失败也不会无限重试——日志里有记录，手动跑一次 run.sh 就能定位。
prev="init"
while true; do
    if deskflow_running; then
        if [ "$prev" != "1" ]; then
            ios_running || start_ios
        fi
        prev="1"
    else
        if [ "$prev" = "1" ] || [ "$prev" = "init" ]; then
            ios_running && stop_ios
        fi
        prev="0"
    fi
    sleep "$POLL"
done
