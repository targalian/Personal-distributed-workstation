#!/usr/bin/env bash
# Start Station on Linux/macOS; preserve existing environments and report failures.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CALLER_DIR="$PWD"

info() { printf '[Station] %s\n' "$*"; }
fail() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
usage() {
    cat <<'EOF'
Usage: scripts/start_workstation.sh [options]
  --port PORT          Station port (default: 45470)
  --name NAME          Device name
  --config FILE        Config file, relative to the caller's directory
  --shared DIR         Shared directory, relative to the caller's directory
  --with-worker        Run a local Worker; stop it when Station exits
  --worker-port PORT   Local Worker port (default: 45460)
  --python EXECUTABLE  Python 3.10+ used to create a new virtual environment
  --venv DIR           Virtual environment (default: project/.venv)
  --install-dev        Install/check dependencies needed for the local dev loop
  --check              Prepare and verify the environment without starting services
  --dev                Enable Station code reload (without --with-worker)
  -h, --help           Show this help without changing the environment
EOF
}
require_value() {
    if [[ $# -lt 2 || -z "$2" || "$2" == --* ]]; then
        fail "$1 requires a value. Use --help for usage."
    fi
}
absolute_path() {
    case "$1" in /*) printf '%s\n' "$1" ;; *) printf '%s/%s\n' "$CALLER_DIR" "$1" ;; esac
}
validate_port() {
    [[ "$2" =~ ^[0-9]{1,5}$ ]] || fail "$1 must be an integer from 1 to 65535."
    ((10#$2 >= 1 && 10#$2 <= 65535)) || fail "$1 must be from 1 to 65535."
}
# Do not let ROS, user site packages, or an activated environment leak into this venv.
clean_python() { env -u PYTHONPATH -u PYTHONHOME PYTHONNOUSERSITE=1 "$@"; }
PORT=45470
WORKER_PORT=45460
NAME=""
CONFIG=""
SHARED=""
BOOTSTRAP_PYTHON=""
VENV_DIR="$PROJECT_ROOT/.venv"
WITH_WORKER=false
INSTALL_DEV=false
CHECK_ONLY=false
DEV=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) require_value "$@"; PORT="$2"; shift 2 ;;
        --worker-port) require_value "$@"; WORKER_PORT="$2"; shift 2 ;;
        --name) require_value "$@"; NAME="$2"; shift 2 ;;
        --config) require_value "$@"; CONFIG="$(absolute_path "$2")"; shift 2 ;;
        --shared) require_value "$@"; SHARED="$(absolute_path "$2")"; shift 2 ;;
        --python) require_value "$@"; BOOTSTRAP_PYTHON="$2"; shift 2 ;;
        --venv) require_value "$@"; VENV_DIR="$(absolute_path "$2")"; shift 2 ;;
        --with-worker) WITH_WORKER=true; shift ;;
        --install-dev) INSTALL_DEV=true; shift ;;
        --check) CHECK_ONLY=true; shift ;;
        --dev) DEV=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) fail "Unknown option: $1. Use --help for usage." ;;
    esac
done
validate_port --port "$PORT"
validate_port --worker-port "$WORKER_PORT"
PORT=$((10#$PORT))
WORKER_PORT=$((10#$WORKER_PORT))
if $WITH_WORKER && ((PORT == WORKER_PORT)); then fail "Station and Worker must use different ports."; fi
if $WITH_WORKER && $DEV; then fail "--dev cannot be combined with --with-worker; Station already embeds a Worker."; fi
if [[ -n "$CONFIG" && ! -f "$CONFIG" ]]; then fail "Config file does not exist: $CONFIG"; fi
if [[ -n "$BOOTSTRAP_PYTHON" ]]; then
    BOOTSTRAP_PYTHON="$(command -v "$BOOTSTRAP_PYTHON")" || fail "Selected Python executable was not found."
    [[ "$BOOTSTRAP_PYTHON" == /* ]] || BOOTSTRAP_PYTHON="$(absolute_path "$BOOTSTRAP_PYTHON")"
fi
cd "$PROJECT_ROOT"
VENV_PYTHON="$VENV_DIR/bin/python"
if [[ ! -e "$VENV_DIR" && ! -L "$VENV_DIR" ]]; then
    if [[ -z "$BOOTSTRAP_PYTHON" ]]; then
        for candidate in python3 python; do
            if command -v "$candidate" >/dev/null 2>&1 && clean_python "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
                BOOTSTRAP_PYTHON="$(command -v "$candidate")"
                break
            fi
        done
    fi
    [[ -n "$BOOTSTRAP_PYTHON" ]] || fail "Python 3.10+ is required; install it or use --python /path/to/python."
    clean_python "$BOOTSTRAP_PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || fail "Python 3.10+ is required."
    info "Creating virtual environment: $VENV_DIR"
    if ! clean_python "$BOOTSTRAP_PYTHON" -m venv "$VENV_DIR"; then
        fail "Cannot create venv. On Ubuntu/Debian install python3-venv; then use --venv with a new directory. Existing files were preserved."
    fi
fi
[[ -x "$VENV_PYTHON" && -f "$VENV_DIR/pyvenv.cfg" ]] || fail "Existing environment is incomplete or from Windows: $VENV_DIR. It was preserved. Use --venv with a new directory."
if ! clean_python "$VENV_PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10) or sys.prefix == sys.base_prefix)'; then
    fail "Existing venv is broken or uses Python older than 3.10. It was preserved. Use --python and --venv with a new directory."
fi
info "Python: $(clean_python "$VENV_PYTHON" --version) ($VENV_PYTHON)"
if ! clean_python "$VENV_PYTHON" -m pip --version >/dev/null 2>&1; then
    clean_python "$VENV_PYTHON" -m ensurepip --upgrade || fail "Cannot initialize pip; repair the virtual environment."
fi
REQ_FILE="$PROJECT_ROOT/requirements.txt"
CHECK_ARGS=()
if $INSTALL_DEV; then REQ_FILE="$PROJECT_ROOT/requirements-dev.txt"; CHECK_ARGS+=(--dev); fi
if ! clean_python "$VENV_PYTHON" "$SCRIPT_DIR/check_environment.py" "${CHECK_ARGS[@]}"; then
    info "Installing missing or incompatible dependencies..."
    if ! clean_python "$VENV_PYTHON" -m pip install --disable-pip-version-check -r "$REQ_FILE"; then
        fail "Dependency installation failed. Check network/proxy/index settings and retry this command. Manual repair: '$VENV_PYTHON' -m pip install -r '$REQ_FILE'"
    fi
    clean_python "$VENV_PYTHON" "$SCRIPT_DIR/check_environment.py" "${CHECK_ARGS[@]}" || fail "Dependencies still fail validation; see the errors above."
else
    info "Dependencies ready; no download required."
fi
# Import the application as well: installed package metadata alone is insufficient.
clean_python "$VENV_PYTHON" -c 'from lan_mesh.station_controller import StationController; from lan_mesh.worker import WorkerAgent' || fail "Application import failed. See the traceback above."
POOL_SRC="$PROJECT_ROOT/lan_mesh/model_pool.example.yaml"
POOL_DST="$PROJECT_ROOT/lan_mesh/model_pool.yaml"
if [[ -f "$POOL_SRC" && ! -e "$POOL_DST" ]]; then
    cp "$POOL_SRC" "$POOL_DST"
    info "Created model_pool.yaml. Configure models and keys through the Web UI or .env."
fi
if $CHECK_ONLY; then
    info "Environment ready. No Station or Worker process was started."
    exit 0
fi
STATION_ARGS=("$PROJECT_ROOT/main.py" station --port "$PORT")
WORKER_ARGS=("$PROJECT_ROOT/main.py" worker --port "$WORKER_PORT")
if [[ -n "$NAME" ]]; then STATION_ARGS+=(--name "$NAME"); WORKER_ARGS+=(--name "${NAME}-Worker"); fi
if [[ -n "$CONFIG" ]]; then STATION_ARGS+=(--config "$CONFIG"); WORKER_ARGS+=(--config "$CONFIG"); fi
if [[ -n "$SHARED" ]]; then STATION_ARGS+=(--shared "$SHARED"); WORKER_ARGS+=(--shared "$SHARED"); fi
if $DEV; then STATION_ARGS+=(--dev); fi
info "Starting Station: http://localhost:$PORT (Secretary election is automatic)."
if ! $WITH_WORKER; then
    exec env -u PYTHONPATH -u PYTHONHOME PYTHONNOUSERSITE=1 "$VENV_PYTHON" "${STATION_ARGS[@]}"
fi
# Supervise only processes created by this launcher; no global process-name kills.
STATION_PID=""
WORKER_PID=""
cleanup() {
    local pid
    for pid in "$STATION_PID" "$WORKER_PID"; do
        if [[ -n "$pid" ]]; then kill "$pid" 2>/dev/null || true; fi
    done
    for pid in "$STATION_PID" "$WORKER_PID"; do
        if [[ -n "$pid" ]]; then wait "$pid" 2>/dev/null || true; fi
    done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
WORKER_LOG_DIR="$PROJECT_ROOT/.lan_mesh/logs"
mkdir -p "$WORKER_LOG_DIR"
WORKER_LOG="$(mktemp "$WORKER_LOG_DIR/worker-$WORKER_PORT.XXXXXX")"
info "Local Worker log: $WORKER_LOG"
env -u PYTHONPATH -u PYTHONHOME PYTHONNOUSERSITE=1 "$VENV_PYTHON" "${WORKER_ARGS[@]}" > "$WORKER_LOG" 2>&1 &
WORKER_PID=$!
env -u PYTHONPATH -u PYTHONHOME PYTHONNOUSERSITE=1 "$VENV_PYTHON" "${STATION_ARGS[@]}" &
STATION_PID=$!
while kill -0 "$STATION_PID" 2>/dev/null; do
    if ! kill -0 "$WORKER_PID" 2>/dev/null; then
        fail "Local Worker exited; stopping Station. Check $WORKER_LOG"
    fi
    sleep 1
done
STATION_STATUS=0
wait "$STATION_PID" || STATION_STATUS=$?
exit "$STATION_STATUS"
