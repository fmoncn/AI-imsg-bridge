#!/bin/bash
# ==========================================
# claude-imessage-bridge 管理脚本
# ==========================================

set -e

SERVICE_NAME="com.fmon.claude_bridge"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_SRC="$SCRIPT_DIR/com.fmon.claude_bridge.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$SERVICE_NAME.plist"
LOG_DIR="$HOME/.claude_bridge/logs"

_ensure_logs() {
    mkdir -p "$LOG_DIR"
}

_install_plist() {
    if [ ! -f "$PLIST_DST" ]; then
        cp "$PLIST_SRC" "$PLIST_DST"
        echo "📋 plist 已安装到 LaunchAgents"
    fi
}

_check_env() {
    if [ ! -f "$SCRIPT_DIR/.env" ]; then
        echo "⚠️  未找到 .env 文件，请先执行："
        echo "   cp .env.example .env && 编辑 .env"
        exit 1
    fi
}

case "$1" in
    install)
        _check_env
        _ensure_logs
        _install_plist
        echo "✅ 安装完成，运行 ./manage.sh start 启动服务"
        ;;
    start)
        _check_env
        _ensure_logs
        _install_plist
        launchctl load -w "$PLIST_DST" 2>/dev/null || true
        echo "✅ 服务已启动"
        ;;
    stop)
        launchctl unload -w "$PLIST_DST" 2>/dev/null || true
        echo "🛑 服务已停止"
        ;;
    restart)
        "$0" stop
        sleep 1
        "$0" start
        ;;
    status)
        PID=$(launchctl list | awk -v n="$SERVICE_NAME" '$3==n{print $1}')
        if [ -n "$PID" ] && [ "$PID" != "-" ]; then
            echo "🟢 运行中 (PID: $PID)"
        else
            echo "🔴 未运行"
        fi
        ;;
    logs)
        tail -f "$LOG_DIR/bridge.log"
        ;;
    uninstall)
        "$0" stop 2>/dev/null || true
        rm -f "$PLIST_DST"
        echo "🗑️  服务已卸载"
        ;;
    *)
        echo "用法: $0 {install|start|stop|restart|status|logs|uninstall}"
        exit 1
        ;;
esac
