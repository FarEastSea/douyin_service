#!/bin/bash
# 抖音下载管理系统停止脚本

echo "Stopping all services..."

# 优雅停止 Celery Worker（等待当前任务完成）
celery -A app.tasks.celery_app control shutdown 2>/dev/null || true
sleep 2

# 强制杀掉残存进程
pkill -f "celery -A app.tasks.celery_app" 2>/dev/null || true
pkill -f "uvicorn main:app" 2>/dev/null || true
sleep 1

# 确认杀干净
pkill -9 -f "celery -A app.tasks.celery_app" 2>/dev/null || true
pkill -9 -f "uvicorn main:app" 2>/dev/null || true

echo "All services stopped!"

