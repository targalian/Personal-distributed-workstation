#!/usr/bin/env bash
# start_workstation.sh - One-click start for LAN Mesh Work Station
# Usage: bash scripts/start_workstation.sh [--port 45470] [--name "Control Center"] [--with-worker]
set -euo pipefail

# Resolve project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# Colors
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
DARK='\033[0;90m'
NC='\033[0m'

step()  { echo -e "${CYAN}[step] $1${NC}"; }
ok()    { echo -e "  ${GREEN}-> $1${NC}"; }
skip()  { echo -e "  ${DARK}-> $1 (skip)${NC}"; }
warn()  { echo -e "  ${YELLOW}-> $1${NC}"; }
fail()  { echo -e "  ${RED}[ERROR] $1${NC}"; }

# Parse args
PORT=45470
NAME=""
CONFIG=""
SHARED=""
WITH_WORKER=false
WORKER_PORT=45460

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)        PORT="$2"; shift 2 ;;
        --name)        NAME="$2"; shift 2 ;;
        --config)      CONFIG="$2"; shift 2 ;;
        --shared)      SHARED="$2"; shift 2 ;;
        --with-worker) WITH_WORKER=true; shift ;;
        --worker-port) WORKER_PORT="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo ""
echo "========================================"
echo " LAN Mesh Work Station - One-Click Start"
echo "========================================"
echo ""

# ── Step 1: Check Python ──
step "1/5 Check Python..."
if ! command -v python3 &>/dev/null; then
    if ! command -v python &>/dev/null; then
        fail "Python not found. Please install Python 3.10+"
        echo "  Download: https://www.python.org/downloads/"
        exit 1
    fi
    PYTHON=python
else
    PYTHON=python3
fi
PY_VER="$($PYTHON --version 2>&1)"
ok "Python: $PY_VER"

# ── Step 2: Virtual Environment ──
step "2/5 Check venv..."
VENV_PYTHON=".venv/bin/python"
VENV_PIP=".venv/bin/pip"

if [ ! -f "$VENV_PYTHON" ]; then
    if [ -d ".venv" ]; then
        warn ".venv exists but not Unix format, rebuilding..."
        rm -rf ".venv"
    fi
    echo -e "  ${YELLOW}Creating virtual environment...${NC}"
    $PYTHON -m venv .venv
    if [ ! -f "$VENV_PYTHON" ]; then
        fail "venv creation failed"
        exit 1
    fi
    ok "Created .venv/"
else
    skip "venv already exists"
fi

# ── Step 3: Install Dependencies ──
step "3/5 Check dependencies..."
if [ ! -f "requirements.txt" ]; then
    skip "requirements.txt not found"
else
    if ! $VENV_PIP show fastapi &>/dev/null; then
        echo -e "  ${YELLOW}Installing dependencies (may take 1-2 min on first run)...${NC}"
        $VENV_PIP install -r requirements.txt -q
        if [ $? -ne 0 ]; then
            fail "Dependencies install failed"
            echo "  Please run manually: .venv/bin/pip install -r requirements.txt"
            exit 1
        fi
        ok "Dependencies installed"
    else
        skip "Dependencies already installed"
    fi
fi

# ── Step 4: Config File ──
step "4/5 Check config..."
POOL_SRC="lan_mesh/model_pool.example.yaml"
POOL_DST="lan_mesh/model_pool.yaml"
if [ -f "$POOL_SRC" ] && [ ! -f "$POOL_DST" ]; then
    cp "$POOL_SRC" "$POOL_DST"
    ok "Created lan_mesh/model_pool.yaml (edit to add API Key)"
elif [ -f "$POOL_DST" ]; then
    skip "model_pool.yaml already exists"
else
    skip "model_pool.example.yaml not found"
fi

# Check API Key
HAS_KEY=false
for KEY_NAME in DEEPSEEK_API_KEY OPENAI_API_KEY ALIYUN_TOKENPLAN_API_KEY QWEN_API_KEY; do
    if [ -n "${!KEY_NAME:-}" ]; then
        HAS_KEY=true
        break
    fi
done
if [ "$HAS_KEY" = false ]; then
    warn "No LLM API Key detected, secretary chat will not use LLM"
    echo -e "  ${DARK}Set: export DEEPSEEK_API_KEY='sk-xxx'${NC}"
fi

# ── Step 5: Launch ──
step "5/5 Launch Station Director..."
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN} LAN Mesh Station Director${NC}"
echo -e "${GREEN} Port: $PORT${NC}"
echo -e "${GREEN} Web UI: http://localhost:$PORT${NC}"
if [ -n "$NAME" ]; then echo -e "${GREEN} Name: $NAME${NC}"; fi
echo -e "${YELLOW} Secretary: inactive (activate in Web UI)${NC}"
if [ "$WITH_WORKER" = true ]; then
    echo -e "${GREEN} Local Worker: http://localhost:$WORKER_PORT (background)${NC}"
fi
echo -e "${GREEN}========================================${NC}"
echo ""

# Build launch args
STATION_ARGS=("main.py" "station" "--port" "$PORT")
if [ -n "$NAME" ];   then STATION_ARGS+=("--name" "$NAME"); fi
if [ -n "$CONFIG" ]; then STATION_ARGS+=("--config" "$CONFIG"); fi
if [ -n "$SHARED" ]; then STATION_ARGS+=("--shared" "$SHARED"); fi

# Optional: start local Worker in background
if [ "$WITH_WORKER" = true ]; then
    WORKER_ARGS=("main.py" "worker" "--port" "$WORKER_PORT")
    if [ -n "$NAME" ]; then WORKER_ARGS+=("--name" "${NAME}-Worker"); fi
    echo -e "${DARK}[INFO] Starting local Worker (port $WORKER_PORT)...${NC}"
    nohup "$VENV_PYTHON" "${WORKER_ARGS[@]}" > /tmp/lanmesh_worker.log 2>&1 &
    sleep 2
fi

# Foreground launch Station Director
exec "$VENV_PYTHON" "${STATION_ARGS[@]}"
