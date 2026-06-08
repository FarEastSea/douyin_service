#!/bin/bash
set -e

PROJECT_NAME="douyin_service"
PROJECT_DIR="/www/wwwroot/douyin_service"
BRANCH="main"
PORT="15000"
VENV_DIR="$PROJECT_DIR/venv"
ENV_FILE="$PROJECT_DIR/.env"
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

echo "Restarting gunicorn..."
mkdir -p "$LOG_DIR"

pkill -f "gunicorn.*main:app.*${PORT}" 2>/dev/null || true
pkill -f "gunicorn.*${PROJECT_DIR}" 2>/dev/null || true

sleep 2

cd "$PROJECT_DIR"

if [ -f "$VENV_DIR/bin/activate" ]; then
    source "$VENV_DIR/bin/activate"
fi

set -a
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
fi
set +a

nohup gunicorn main:app \
    --bind 0.0.0.0:${PORT} \
    --workers 1 \
    --threads 1 \
    --worker-class uvicorn.workers.UvicornWorker \
    --chdir "$PROJECT_DIR" \
    --user www \
    --access-logfile "$LOG_DIR/gunicorn-access.log" \
    --error-logfile "$LOG_DIR/gunicorn-error.log" \
    > "$LOG_DIR/gunicorn.log" 2>&1 &

sleep 3

if ss -lntp | grep -q ":${PORT}"; then
    echo "Deploy success. ${PROJECT_NAME} is running on port ${PORT}."
else
    echo "Deploy failed. Port ${PORT} is not listening."
    echo "Check logs: $LOG_DIR/gunicorn-error.log"
    exit 1
fi