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
XHS_ENGINE_DIR="${PROJECT_DIR}/.xhs-engine/current"
XHS_API_BIN="${XHS_ENGINE_DIR}/source/.venv/bin/xhs-api"
XHS_PID_FILE="${LOG_DIR}/xhs-api.pid"
XHS_DATA_DIR="${STATE_DIR}/xhs"

cd "$PROJECT_DIR"
mkdir -p "$LOG_DIR" "$STATE_DIR" "$XHS_DATA_DIR"

if [ -f "${VENV_DIR}/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "${VENV_DIR}/bin/activate"
fi

if [ ! -x "$GUNICORN_BIN" ]; then
    echo "Start failed: gunicorn is not available in ${VENV_DIR}." >&2
    exit 1
fi

SERVICE_ALREADY_RUNNING=0
EXISTING_PID=""
if [ -f "$PID_FILE" ]; then
    EXISTING_PID="$(tr -dc '0-9' < "$PID_FILE")"
    if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
        SERVICE_ALREADY_RUNNING=1
    else
        rm -f "$PID_FILE"
    fi
fi

GUNICORN_USER_ARGS=()
if [ "$(id -u)" -eq 0 ] && id www >/dev/null 2>&1; then
    RUNTIME_GROUP="$(id -gn www)"
    chown -R "www:${RUNTIME_GROUP}" "$LOG_DIR" "$STATE_DIR"
    GUNICORN_USER_ARGS=(--user www --group "$RUNTIME_GROUP")
fi

start_xhs_engine() {
    local xhs_host="127.0.0.1"
    local xhs_port="5556"
    local xhs_api_url="http://127.0.0.1:5556"
    local pid=""
    local run_as=()

    [ -x "$XHS_API_BIN" ] || {
        echo "Start failed: isolated xhs-api is unavailable; run Jenkins deployment first." >&2
        return 1
    }
    if [ -f "$XHS_PID_FILE" ]; then
        pid="$(tr -dc '0-9' < "$XHS_PID_FILE")"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            if "$VENV_DIR/bin/python" -c \
                'import sys, urllib.request; urllib.request.urlopen(sys.argv[1] + "/health", timeout=3).read()' \
                "$xhs_api_url"; then
                echo "Xiaohongshu collector is already running (PID=${pid})."
                return 0
            fi
            echo "Start failed: managed Xiaohongshu collector is running but unhealthy." >&2
            return 1
        fi
        rm -f "$XHS_PID_FILE"
    fi

    if [ "$(id -u)" -eq 0 ] && id www >/dev/null 2>&1; then
        command -v runuser >/dev/null 2>&1 || {
            echo "Start failed: runuser is required to start xhs-api as www." >&2
            return 1
        }
        run_as=(runuser -u www --)
    fi

    echo "Starting isolated Xiaohongshu collector on ${xhs_host}:${xhs_port}..."
    nohup "${run_as[@]}" env \
        XHS_WORK_PATH="$XHS_DATA_DIR" \
        XHS_FOLDER_NAME="download" \
        XHS_ROUTE_STRATEGY="http_only" \
        XHS_MAX_CONCURRENCY="1" \
        XHS_LIVE_DOWNLOAD="true" \
        "$XHS_API_BIN" --host "$xhs_host" --port "$xhs_port" \
        > "$LOG_DIR/xhs-api.log" 2>&1 &
    pid=$!
    printf '%s\n' "$pid" > "$XHS_PID_FILE"

    for _ in $(seq 1 30); do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "Start failed: Xiaohongshu collector exited; check $LOG_DIR/xhs-api.log." >&2
            rm -f "$XHS_PID_FILE"
            return 1
        fi
        if "$VENV_DIR/bin/python" -c \
            'import sys, urllib.request; urllib.request.urlopen(sys.argv[1] + "/health", timeout=3).read()' \
            "$xhs_api_url" 2>/dev/null; then
            echo "Xiaohongshu collector started (PID=${pid})."
            return 0
        fi
        sleep 1
    done
    echo "Start failed: Xiaohongshu collector health check timed out." >&2
    kill -TERM "$pid" 2>/dev/null || true
    rm -f "$XHS_PID_FILE"
    return 1
}

start_xhs_engine

if [ "$SERVICE_ALREADY_RUNNING" -eq 1 ]; then
    echo "Service is already running (PID=${EXISTING_PID})."
    exit 0
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
