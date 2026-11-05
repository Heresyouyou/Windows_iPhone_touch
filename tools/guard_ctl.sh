#!/bin/bash
# 安装 / 卸载 "跟随 Deskflow 启停" 的守护。
#
#   tools/guard_ctl.sh install    装成 LaunchAgent，登录即生效
#   tools/guard_ctl.sh uninstall  卸载，并把已经拉起的本程序一起收掉
#   tools/guard_ctl.sh status     看守护在不在、本程序在不在
#   tools/guard_ctl.sh restart    改完脚本后重载
#
# 为什么用 LaunchAgent 而不是往 Deskflow 里塞钩子
# -----------------------------------------------
# Deskflow 不提供启动钩子；LaunchAgent + RunAtLoad 是 macOS 上"登录即常驻"
# 的标准做法，而且不依赖任何第三方工具。
set -u

# 路径从脚本自身位置推，不写死 —— 仓库克隆到哪都能用
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GUARD="$ROOT/tools/deskflow_guard.sh"
LABEL="work.kust.macphonetouch.guard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/MacPhoneTouch-guard.log"
UID_NUM="$(id -u)"

usage() {
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
}

write_plist() {
    mkdir -p "$(dirname "$PLIST")"
    cat >"$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$GUARD</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>LimitLoadToSessionType</key>
    <string>Aqua</string>
    <key>ProcessType</key>
    <string>Background</string>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
</dict>
</plist>
PLIST_EOF
    chmod 644 "$PLIST"
}

unload() {
    launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null
    launchctl bootout "gui/$UID_NUM" "$PLIST" 2>/dev/null
    launchctl remove "$LABEL" 2>/dev/null
    return 0
}

load() {
    # 两条路都试：bootstrap 是新接口，load -w 是旧接口。哪条先成都行。
    if launchctl bootstrap "gui/$UID_NUM" "$PLIST" 2>/dev/null; then
        LOAD_ERR=""
    elif launchctl load -w "$PLIST" 2>/dev/null; then
        LOAD_ERR=""
    else
        LOAD_ERR="$(launchctl bootstrap "gui/$UID_NUM" "$PLIST" 2>&1)"
        [ -n "$LOAD_ERR" ] || LOAD_ERR="launchctl load -w 也失败"
    fi
    launchctl enable "gui/$UID_NUM/$LABEL" 2>/dev/null
    launchctl kickstart -k "gui/$UID_NUM/$LABEL" 2>/dev/null
}

case "${1:-}" in
    install|restart)
        [ -x "$GUARD" ] || chmod +x "$GUARD"
        write_plist
        unload
        sleep 0.3
        LOAD_ERR=""
        load
        echo "已写入 $PLIST"
        if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
            echo "已注册到 launchd（$LABEL），登录即生效"
            echo "$(date '+%F %T') 安装/重载守护" >>"$LOG"
        else
            cat <<HINT

⚠️ launchd 拒绝了注册：$LOAD_ERR
   这通常是当前进程没有权限写用户 domain（不是脚本本身的问题）。
   请在你自己的「终端」里执行一次同样的命令：

       $ROOT/tools/guard_ctl.sh install

   终端里跑成功后，登录时会自动常驻；Deskflow 一启动就拉起本程序，
   Deskflow 退出就把本程序收掉。
HINT
            exit 1
        fi
        echo "  log   : $LOG"
        sleep 1
        "$0" status
        ;;
    uninstall)
        unload
        rm -f "$PLIST"
        pkill -TERM -f "$ROOT/tools/deskflow_guard.sh" 2>/dev/null
        # 守护没了就没人收本程序了，顺手一起关，避免留下一块黑屏
        pkill -TERM -f "ios_screen\.app" 2>/dev/null
        pkill -TERM -f "$ROOT/native/vdisplay" 2>/dev/null
        echo "已卸载 $LABEL，本程序也已退出"
        ;;
    status)
        if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
            echo "守护  : 已加载（$LABEL）"
        else
            echo "守护  : 未加载"
        fi
        if pgrep -f "$ROOT/tools/deskflow_guard.sh" >/dev/null 2>&1; then
            echo "循环  : 在跑 pid=$(pgrep -f "$ROOT/tools/deskflow_guard.sh" | head -1)"
        else
            echo "循环  : 没在跑"
        fi
        if pgrep -f "ios_screen\.app" >/dev/null 2>&1; then
            echo "本体  : 在跑 pid=$(pgrep -f "ios_screen\.app" | head -1)"
        else
            echo "本体  : 没在跑"
        fi
        if pgrep -f "MacOS/Deskflow$" >/dev/null 2>&1; then
            echo "Deskflow: 在跑"
        else
            echo "Deskflow: 没在跑（本体会被自动收掉）"
        fi
        ;;
    *)
        usage
        ;;
esac
