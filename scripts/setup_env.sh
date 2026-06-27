#!/usr/bin/env bash
# setup_env.sh - 初始化项目环境 (Linux/Mac)
# 用法: bash scripts/setup_env.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "========================================"
echo " LAN Mesh 环境初始化"
echo "========================================"
echo ""

# 1. 创建虚拟环境
VENV="$PROJECT_ROOT/.venv"
if [ ! -d "$VENV" ] || [ ! -x "$VENV/bin/python3" ]; then
    echo "[1/3] 创建虚拟环境..."
    python3 -m venv .venv
    echo "      已创建 .venv/"
else
    echo "[1/3] 虚拟环境已存在, 跳过"
fi

# 2. 安装依赖
echo "[2/3] 安装 Python 依赖..."
"$VENV/bin/pip" install -r requirements.txt -q
echo "      依赖安装完成"

# 3. 复制模型池模板
POOL_SRC="$PROJECT_ROOT/lan_mesh/model_pool.example.yaml"
POOL_DST="$PROJECT_ROOT/lan_mesh/model_pool.yaml"
if [ -f "$POOL_SRC" ] && [ ! -f "$POOL_DST" ]; then
    echo "[3/3] 复制模型池配置模板..."
    cp "$POOL_SRC" "$POOL_DST"
    echo "      已创建 lan_mesh/model_pool.yaml (请编辑填入 API Key)"
elif [ -f "$POOL_DST" ]; then
    echo "[3/3] model_pool.yaml 已存在, 跳过"
else
    echo "[3/3] model_pool.example.yaml 不存在, 跳过"
fi

echo ""
echo "========================================"
echo " 环境初始化完成!"
echo ""
echo " 下一步:"
echo "   1. 编辑 lan_mesh/model_pool.yaml 配置 API Key"
echo "   2. 设置环境变量 (可选):"
echo "      export DEEPSEEK_API_KEY=\"sk-xxx\""
echo "      export OPENAI_API_KEY=\"sk-xxx\""
echo "      export ALIYUN_TOKENPLAN_API_KEY=\"你的TokenPlan专属Key\""
echo "   3. 启动 Station Director: bash scripts/start_station.sh"
echo "      → 在 Web UI 中点击「启动秘书」激活 Secretary"
echo "   4. 启动 Worker: bash scripts/start_worker.sh"
echo "      (或向后兼容: bash scripts/start_secretary.sh 直接启动 Secretary)"
echo "========================================"
