#!/usr/bin/env bash
# 一键后台启动 web 服务（Git Bash / MSYS2）
# 用法：
#   ./start.sh          启动
#   ./start.sh stop     停止
#   ./start.sh status   查看状态（调 API 拿真实运行时信息）
#   ./start.sh restart  重启

set -euo pipefail
cd "$(dirname "$0")"

# 从 config.json 读端口，缺省 8765
PORT=$(python -c "
import json, pathlib
try:
    c = json.loads(pathlib.Path('config.json').read_text('utf-8'))
    print(c.get('web_port', 8765))
except Exception:
    print(8765)
" 2>/dev/null)

case "${1:-start}" in
  start)
    python -m scripts.start_web_detached
    ;;
  stop)
    python -m scripts.stop_web
    ;;
  restart)
    python -m scripts.stop_web || true
    sleep 1
    python -m scripts.start_web_detached
    ;;
  status)
    PID_FILE="web_server.pid"

    # 先检查进程是否活着
    if [ ! -f "$PID_FILE" ]; then
      echo "web_server 没在跑（无 pid 文件）"
      exit 1
    fi
    PID=$(cat "$PID_FILE" 2>/dev/null | tr -d '[:space:]')
    if ! tasklist //FI "PID eq $PID" 2>/dev/null | grep -q "$PID"; then
      echo "web_server 已死（pid=$PID），pid 文件是残留"
      exit 1
    fi

    echo "web_server 运行中 (pid=$PID, port=$PORT)"
    echo "  dashboard: http://127.0.0.1:$PORT/"
    echo ""

    # 调 API 拿真实运行时状态
    API=$(curl -s --max-time 3 "http://127.0.0.1:$PORT/dashboard/status" 2>/dev/null) || true
    if [ -z "$API" ] || echo "$API" | grep -q "Not Found"; then
      echo "  (API 无响应，进程可能还在启动中)"
      exit 0
    fi

    # 解析 JSON（用 python，不依赖 jq）
    python -c "
import json, sys
d = json.loads(sys.stdin.read())

# 会话
total = d.get('sessions_total', 0)
online = d.get('sessions_online', 0)
current = d.get('current', '-')
print(f'  会话: {online}/{total} 在线, 当前={current}')

# 微信
wx = d.get('weixin', {})
wx_connected = wx.get('connected', False)
wx_running = wx.get('running', False)
wx_acct = wx.get('account_id', '')
if wx_running:
    wx_status = '轮询中'
elif wx_connected:
    wx_status = '有 token 但未轮询（可能连到别处或 session 过期）'
else:
    wx_status = '未连接'
print(f'  微信: {wx_status}')
if wx_acct:
    print(f'        account: {wx_acct}')

# Backend
# default backend 从文件读（API 里没返回）
from pathlib import Path
db_file = Path('chat_sessions/_default_backend.txt')
default_be = 'claude_code'
if db_file.exists():
    try:
        default_be = db_file.read_text('utf-8').strip() or default_be
    except Exception:
        pass
print(f'  默认 backend: {default_be}')
print(f'  可用 backends: claude_code, hermes_acp')

# MCP
mcp = d.get('claude', {}).get('mcp_registered', False)
print(f'  MCP cca-msg: {\"已注册\" if mcp else \"未注册\"}')
" <<< "$API"
    ;;
  *)
    echo "用法: $0 {start|stop|restart|status}"
    exit 2
    ;;
esac
