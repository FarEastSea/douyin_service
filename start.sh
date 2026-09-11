#!/bin/bash
set -euo pipefail

# 生产同构启动脚本：Gunicorn 托管单个 Uvicorn worker，Celery Worker/Beat
# 继续由 FastAPI lifespan 自动管理。路径从脚本位置推导，不写入部署目标信息。
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-${PROJECT_DIR}/.venv}"
if [ ! -x "${VENV_DIR}/bin/gunicorn" ] && [ -x "${VENV_DIR}/gunicorn" ] && [ -x "${VENV_DIR}/python" ]; then
    VENV_DIR="$(cd "${VENV_DIR}/.." && pwd -P)"
fi
APP_PORT="${APP_PORT:-15000}"
RUNTIME_DIR="${RUNTIME_DIR:-$PROJECT_DIR}"
LOG_DIR="${RUNTIME_DIR}/logs"
STATE_DIR="${PROJECT_DIR}/.runtime"
PID_FILE="${LOG_DIR}/gunicorn.pid"
GUNICORN_BIN="${VENV_DIR}/bin/gunicorn"
XHS_ENGINE_DIR="${PROJECT_DIR}/.xhs-engine/current"
XHS_API_BIN="${XHS_ENGINE_DIR}/source/.venv/bin/xhs-api"
XHS_API_LAUNCHER="${PROJECT_DIR}/integrations/xhs_api_launcher.py"
XHS_PID_FILE="${LOG_DIR}/xhs-api.pid"
XHS_SERVICE_USER="douyin-xhs"
XHS_DATA_DIR="${STATE_DIR}/xhs"
XHS_HOME_DIR="${XHS_DATA_DIR}/home"
XHS_RUNTIME_DIR="${XHS_DATA_DIR}/runtime"
XHS_BROWSER_CACHE="${PROJECT_DIR}/.xhs-engine/browser-cache"

cd "$PROJECT_DIR"
mkdir -p "$LOG_DIR" "$STATE_DIR" "$XHS_DATA_DIR" "$XHS_HOME_DIR" "$XHS_RUNTIME_DIR"
chmod 700 "$XHS_RUNTIME_DIR"

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
    local browser_executable=""
    local xhs_group=""
    local nologin_shell=""

    [ -x "$XHS_API_BIN" ] || {
        echo "Start failed: isolated xhs-api is unavailable; run Jenkins deployment first." >&2
        return 1
    }
    [ -f "$XHS_API_LAUNCHER" ] || {
        echo "Start failed: Xiaohongshu integration launcher is unavailable." >&2
        return 1
    }
    browser_executable="$(PLAYWRIGHT_BROWSERS_PATH="$XHS_BROWSER_CACHE" \
        "${XHS_ENGINE_DIR}/source/.venv/bin/python" - <<'PY'
from playwright.sync_api import sync_playwright
with sync_playwright() as playwright:
    print(playwright.chromium.executable_path)
PY
)"
    [ -x "$browser_executable" ] || {
        echo "Start failed: managed Chromium is unavailable; run Jenkins deployment first." >&2
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

    if [ "$(id -u)" -eq 0 ]; then
        if ! id "$XHS_SERVICE_USER" >/dev/null 2>&1; then
            command -v useradd >/dev/null 2>&1 || {
                echo "Start failed: useradd is required to create the isolated xhs service account." >&2
                return 1
            }
            nologin_shell="$(command -v nologin || true)"
            [ -n "$nologin_shell" ] || nologin_shell="/bin/false"
            useradd --system --user-group \
                --home-dir "$XHS_HOME_DIR" \
                --shell "$nologin_shell" \
                "$XHS_SERVICE_USER"
        fi
        command -v setpriv >/dev/null 2>&1 || {
            echo "Start failed: setpriv is required to start the isolated xhs service account." >&2
            return 1
        }
        xhs_group="$(id -gn "$XHS_SERVICE_USER")"
        chown -R "$XHS_SERVICE_USER:$xhs_group" "$XHS_DATA_DIR"
        chmod 700 "$XHS_HOME_DIR" "$XHS_RUNTIME_DIR"
        run_as=(
            setpriv
            "--reuid=$(id -u "$XHS_SERVICE_USER")"
            "--regid=$(id -g "$XHS_SERVICE_USER")"
            --init-groups
            --
        )
    fi

    echo "Starting isolated Xiaohongshu collector on ${xhs_host}:${xhs_port}..."
    nohup "${run_as[@]}" env \
        HOME="$XHS_HOME_DIR" \
        XDG_CONFIG_HOME="$XHS_HOME_DIR/.config" \
        XDG_CACHE_HOME="$XHS_HOME_DIR/.cache" \
        XDG_RUNTIME_DIR="$XHS_RUNTIME_DIR" \
        XHS_WORK_PATH="$XHS_DATA_DIR" \
        XHS_FOLDER_NAME="download" \
        PLAYWRIGHT_BROWSERS_PATH="$XHS_BROWSER_CACHE" \
        XHS_ROUTE_STRATEGY="http_first" \
        XHS_BROWSER_DRIVER="managed" \
        XHS_MANAGED_BROWSER_EXECUTABLE="$browser_executable" \
        XHS_MANAGED_BROWSER_HEADLESS="true" \
        XHS_MAX_CONCURRENCY="1" \
        XHS_LIVE_DOWNLOAD="true" \
        "${XHS_ENGINE_DIR}/source/.venv/bin/python" \
        "$XHS_API_LAUNCHER" --host "$xhs_host" --port "$xhs_port" \
        < /dev/null > "$LOG_DIR/xhs-api.log" 2>&1 &
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
    < /dev/null > "$LOG_DIR/gunicorn.log" 2>&1 &

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
