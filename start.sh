#!/bin/bash
# 抖音下载管理系统启动脚本
# Worker 和 Beat 由 FastAPI 应用自动管理，无需手动启动

# 进入项目目录
cd "$(dirname "$0")"

# 激活虚拟环境
source venv/bin/activate

# 先杀掉旧进程
echo "Stopping old processes..."
pkill -f "celery -A app.tasks.celery_app" 2>/dev/null || true
pkill -f "uvicorn main:app" 2>/dev/null || true
sleep 2

# 确保日志目录存在
mkdir -p logs

# 启动 FastAPI（Worker 和 Beat 会在应用启动时自动拉起）
echo "Starting FastAPI server..."
uvicorn main:app --host 0.0.0.0 --port 8000 &

echo "Service started!"
echo "Web UI: http://localhost:8000"
echo "API Docs: http://localhost:8000/docs"
echo "Worker & Beat are managed by the app automatically."

# 等待所有后台进程
wait

