#!/bin/bash
set -euo pipefail

# 生产同构启动脚本：Gunicorn 托管单个 Uvicorn worker，Celery Worker/Beat
# 继续由 FastAPI lifespan 自动管理。路径从脚本位置推导，不写入部署目标信息。
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-${PROJECT_DIR}/.venv}"
APP_PORT="${APP_PORT:-15000}"
RUNTIME_DIR="${RUNTIME_DIR:-$PROJECT_DIR}"
LOG_DIR="${RUNTIME_DIR}/logs"
STATE_DIR="${PROJECT_DIR}/.runtime"
PID_FILE="${LOG_DIR}/gunicorn.pid"
GUNICORN_BIN="${VENV_DIR}/bin/gunicorn"

cd "$PROJECT_DIR"
mkdir -p "$LOG_DIR" "$STATE_DIR"

if [ -f "${VENV_DIR}/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "${VENV_DIR}/bin/activate"
fi

if [ ! -x "$GUNICORN_BIN" ]; then
    echo "Start failed: gunicorn is not available in ${VENV_DIR}." >&2
    exit 1
fi

if [ -f "$PID_FILE" ]; then
    EXISTING_PID="$(tr -dc '0-9' < "$PID_FILE")"
    if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
        echo "Service is already running (PID=${EXISTING_PID})."
        exit 0
    fi
    rm -f "$PID_FILE"
fi

GUNICORN_USER_ARGS=()
if [ "$(id -u)" -eq 0 ] && id www >/dev/null 2>&1; then
    RUNTIME_GROUP="$(id -gn www)"
    chown -R "www:${RUNTIME_GROUP}" "$LOG_DIR" "$STATE_DIR"
    GUNICORN_USER_ARGS=(--user www --group "$RUNTIME_GROUP")
fi

echo "Starting media download service on port ${APP_PORT}..."
nohup "$GUNICORN_BIN" main:app \
    --bind "0.0.0.0:${APP_PORT}" \
    --workers 1 \
    --threads 1 \
    --worker-class uvicorn.workers.UvicornWorker \
    --chdir "$PROJECT_DIR" \
    --pid "$PID_FILE" \
    "${GUNICORN_USER_ARGS[@]}" \
    --access-logfile "$LOG_DIR/gunicorn-access.log" \
    --error-logfile "$LOG_DIR/gunicorn-error.log" \
    > "$LOG_DIR/gunicorn.log" 2>&1 &

for _ in $(seq 1 20); do
    if [ -s "$PID_FILE" ]; then
        GUNICORN_PID="$(tr -dc '0-9' < "$PID_FILE")"
        if [ -n "$GUNICORN_PID" ] && kill -0 "$GUNICORN_PID" 2>/dev/null; then
            echo "Service started (PID=${GUNICORN_PID})."
            echo "Web UI: http://127.0.0.1:${APP_PORT}"
            echo "API Docs: http://127.0.0.1:${APP_PORT}/docs"
            exit 0
        fi
    fi
    sleep 0.5
done

echo "Start failed. Check ${LOG_DIR}/gunicorn-error.log." >&2
exit 1
