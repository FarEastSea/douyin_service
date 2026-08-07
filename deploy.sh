#!/bin/bash
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_NAME="$(basename "$PROJECT_DIR")"
BRANCH="main"
PORT="${APP_PORT:-15000}"
VENV_DIR="${VENV_DIR:-$PROJECT_DIR/venv}"
LOG_DIR="$PROJECT_DIR/logs"

echo "Deploying ${PROJECT_NAME}..."

cd "$PROJECT_DIR"

echo "Pulling latest code..."
git fetch origin "$BRANCH"
git reset --hard "origin/$BRANCH"

echo "Installing dependencies..."
if [ -f "$VENV_DIR/bin/activate" ] && [ -f "$PROJECT_DIR/requirements.txt" ]; then
    source "$VENV_DIR/bin/activate"
    pip install -r "$PROJECT_DIR/requirements.txt"
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
