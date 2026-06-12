#!/bin/bash
# BME Flask 后端一键启动脚本
# 用法: ./start.sh [start|stop|restart|status]

DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$DIR/.flask.pid"
LOG_FILE="$DIR/log/flask.log"
VENV="$DIR/.venv/bin/activate"

start() {
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "⚠️  后端已在运行 (PID $(cat "$PID_FILE"))"
    return 1
  fi
  mkdir -p "$DIR/log"
  source "$VENV"
  echo "🚀 启动 Flask 后端..."
  nohup python "$DIR/app.py" >> "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  sleep 1
  if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "✅ 后端已启动 (PID $(cat "$PID_FILE")), 日志: $LOG_FILE"
  else
    echo "❌ 启动失败，请检查日志: $LOG_FILE"
    rm -f "$PID_FILE"
    return 1
  fi
}

stop() {
  if [ ! -f "$PID_FILE" ]; then
    echo "⚠️  后端未运行"
    return 1
  fi
  PID="$(cat "$PID_FILE")"
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    echo "🛑 后端已停止 (PID $PID)"
  else
    echo "⚠️  进程已不存在，清理 PID 文件"
  fi
  rm -f "$PID_FILE"
}

status() {
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "🟢 后端运行中 (PID $(cat "$PID_FILE"))"
  else
    echo "⚪ 后端未运行"
    rm -f "$PID_FILE"
  fi
}

case "${1:-start}" in
  start)   start   ;;
  stop)    stop    ;;
  restart) stop; start ;;
  status)  status  ;;
  *)       echo "用法: $0 [start|stop|restart|status]" ;;
esac
