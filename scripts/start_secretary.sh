#!/usr/bin/env bash
# start_secretary.sh - 启动 LAN Mesh Secretary 节点
# 用法: bash scripts/start_secretary.sh [--port 8080] [--name "控制中心"]

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

PORT=45470
NAME=""
CONFIG=""
SHARED=""

# 解析参数
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port|-p)  PORT="$2"; shift 2 ;;
        --name|-n)  NAME="$2"; shift 2 ;;
        --config|-c) CONFIG="$2"; shift 2 ;;
        --shared)   SHARED="$2"; shift 2 ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

# 检查虚拟环境
VENV="$PROJECT_ROOT/.venv"
if [ -x "$VENV/bin/python3" ]; then
    echo "[INFO] 使用虚拟环境 Python..."
    PYTHON="$VENV/bin/python3"
elif command -v python3 &>/dev/null; then
    PYTHON="python3"
else
    echo "[ERROR] 未找到 python3, 请确认已安装 Python 3.11+"
    exit 1
fi

cd "$PROJECT_ROOT"

# 构建启动命令
CMD=($PYTHON main.py secretary --port "$PORT")
[ -n "$NAME" ]   && CMD+=(--name "$NAME")
[ -n "$CONFIG" ] && CMD+=(--config "$CONFIG")
[ -n "$SHARED" ] && CMD+=(--shared "$SHARED")

echo ""
echo "========================================"
echo " LAN Mesh Secretary Node"
echo " Port: $PORT"
[ -n "$NAME" ] && echo " Name: $NAME"
echo "========================================"
echo ""

"${CMD[@]}"
