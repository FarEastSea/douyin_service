#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="${RUNTIME_DIR:-$PROJECT_DIR}"
LOG_DIR="${RUNTIME_DIR}/logs"
PID_FILE="${LOG_DIR}/gunicorn.pid"

matches_project_server() {
    local pid="$1"
    local cmdline=""
    local process_cwd=""
    if [ -r "/proc/${pid}/cmdline" ]; then
        cmdline="$(tr '\0' ' ' < "/proc/${pid}/cmdline")"
    fi
    if [ -L "/proc/${pid}/cwd" ]; then
        process_cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"
    fi
    if [[ "$cmdline" == *"gunicorn"* && "$cmdline" == *"main:app"* && "$cmdline" == *"${PROJECT_DIR}"* ]]; then
        return 0
    fi
    if [ "$process_cwd" = "$(readlink -f "$PROJECT_DIR")" ] && \
       { [[ "$cmdline" == *"uvicorn"* && "$cmdline" == *"main:app"* ]] || [[ "$cmdline" == *"main.py"* ]]; }; then
        return 0
    fi
    return 1
}

stop_pid() {
    local pid="$1"
    if ! kill -0 "$pid" 2>/dev/null; then
        return 0
    fi
    echo "Stopping application server (PID=${pid})..."
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
        if ! kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        sleep 0.5
    done
    echo "Application server did not stop gracefully; sending SIGKILL (PID=${pid})."
    kill -KILL "$pid" 2>/dev/null || true
}

FOUND=0
if [ -f "$PID_FILE" ]; then
    PID="$(tr -dc '0-9' < "$PID_FILE")"
    if [ -n "$PID" ] && matches_project_server "$PID"; then
        stop_pid "$PID"
        FOUND=1
    fi
    rm -f "$PID_FILE"
fi

# 兼容未写 pidfile 的 Gunicorn，以及宝塔从项目目录直接启动的 Uvicorn/main.py。
while IFS= read -r PID; do
    [ -n "$PID" ] || continue
    if matches_project_server "$PID"; then
        stop_pid "$PID"
        FOUND=1
    fi
done < <(pgrep -f "main:app|main.py" 2>/dev/null || true)

if [ "$FOUND" -eq 0 ]; then
    echo "Service is not running."
else
    echo "Service stopped. FastAPI lifespan will stop its Celery Worker and Beat children."
fi
