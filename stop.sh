#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${PROJECT_DIR}/logs"
PID_FILE="${LOG_DIR}/gunicorn.pid"

matches_project_gunicorn() {
    local pid="$1"
    local cmdline=""
    if [ -r "/proc/${pid}/cmdline" ]; then
        cmdline="$(tr '\0' ' ' < "/proc/${pid}/cmdline")"
    fi
    [[ "$cmdline" == *"gunicorn"* && "$cmdline" == *"main:app"* && "$cmdline" == *"${PROJECT_DIR}"* ]]
}

stop_pid() {
    local pid="$1"
    if ! kill -0 "$pid" 2>/dev/null; then
        return 0
    fi
    echo "Stopping Gunicorn (PID=${pid})..."
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
        if ! kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        sleep 0.5
    done
    echo "Gunicorn did not stop gracefully; sending SIGKILL (PID=${pid})."
    kill -KILL "$pid" 2>/dev/null || true
}

FOUND=0
if [ -f "$PID_FILE" ]; then
    PID="$(tr -dc '0-9' < "$PID_FILE")"
    if [ -n "$PID" ] && matches_project_gunicorn "$PID"; then
        stop_pid "$PID"
        FOUND=1
    fi
    rm -f "$PID_FILE"
fi

# 兼容旧版本未写 pidfile 的 Gunicorn，只终止 --chdir 指向本项目的进程。
while IFS= read -r PID; do
    [ -n "$PID" ] || continue
    if matches_project_gunicorn "$PID"; then
        stop_pid "$PID"
        FOUND=1
    fi
done < <(pgrep -f "gunicorn.*main:app" 2>/dev/null || true)

if [ "$FOUND" -eq 0 ]; then
    echo "Service is not running."
else
    echo "Service stopped. FastAPI lifespan will stop its Celery Worker and Beat children."
fi
