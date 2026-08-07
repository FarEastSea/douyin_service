#!/bin/bash
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_NAME="$(basename "$PROJECT_DIR")"
BRANCH="main"
PORT="${APP_PORT:-15000}"
VENV_DIR="${VENV_DIR:-$PROJECT_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-}"
LOG_DIR="$PROJECT_DIR/logs"

echo "Deploying ${PROJECT_NAME}..."

cd "$PROJECT_DIR"

echo "Pulling latest code..."
git fetch origin "$BRANCH"
git reset --hard "origin/$BRANCH"

echo "Installing dependencies..."
if [ ! -x "$VENV_DIR/bin/python" ]; then
    if [ -z "$PYTHON_BIN" ]; then
        for candidate in python3.12 python3.11; do
            if command -v "$candidate" >/dev/null 2>&1; then
                PYTHON_BIN="$candidate"
                break
            fi
        done
    fi
    if [ -z "$PYTHON_BIN" ] || ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info[:2] not in ((3, 11), (3, 12)))'; then
        echo "Deploy failed: Python 3.11 or 3.12 is required. Set PYTHON_BIN to a supported interpreter." >&2
        exit 1
    fi
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

if [ -f "$PROJECT_DIR/requirements.txt" ]; then
    "$VENV_DIR/bin/python" -m pip install --upgrade pip
    "$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"
fi

echo "Restarting production service..."
mkdir -p "$LOG_DIR"

# 不在 shell 中加载 .env。应用通过 app/core/env_config.py 的 ENV_PATH
# 自行读取网页持久化配置，避免配置值被 shell 当作命令解释。

chmod +x "$PROJECT_DIR/start.sh" "$PROJECT_DIR/stop.sh"
"$PROJECT_DIR/stop.sh"
APP_PORT="$PORT" VENV_DIR="$VENV_DIR" "$PROJECT_DIR/start.sh"

if ss -lntp | grep -q ":${PORT}"; then
    echo "Deploy success. ${PROJECT_NAME} is running on port ${PORT}."
else
    echo "Deploy failed. Port ${PORT} is not listening."
    echo "Check logs: $LOG_DIR/gunicorn-error.log"
    exit 1
fi
