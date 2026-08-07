"""
Celery 应用配置

为什么这样设计：
1. 独立的 Celery 配置文件，便于维护
2. 配置任务路由，不同类型任务可以分配到不同队列
3. 配置定时任务 (Celery Beat)，用于订阅检查
"""

from celery import Celery
from celery.schedules import crontab
from app.core.config import settings

# 创建 Celery 应用
celery_app = Celery(
    "douyin_downloader",
    broker=settings.redis_url_with_auth,
    backend=settings.redis_url_with_auth,
    include=["app.tasks.download_tasks", "app.tasks.x_download_tasks"]
)

# Celery 配置
celery_app.conf.update(
    # 任务序列化
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    
    # 时区
    timezone="Asia/Shanghai",
    enable_utc=True,
    
    # 任务结果过期时间（7天）
    result_expires=7 * 24 * 3600,
    
    # 任务确认
    task_acks_late=True,  # 任务完成后才确认
    task_reject_on_worker_lost=True,  # worker 异常退出时拒绝任务
    
    # 并发控制
    worker_prefetch_multiplier=1,  # 每个 worker 一次只取一个任务
    worker_concurrency=settings.MAX_CONCURRENT_DOWNLOADS,  # 同时下载任务数
    
    # 任务超时保护（防止任务挂死导致 Worker 卡住）
    task_time_limit=1800,           # 30 分钟硬限制（SIGKILL）
    task_soft_time_limit=1500,      # 25 分钟软限制（SoftTimeLimitExceeded 异常）
    
    # 显式指定默认队列，确保 .delay() 和 Worker 消费同一个队列
    task_default_queue='celery',
    task_default_exchange='celery',
    task_default_routing_key='celery',
    
    # 路由清空 — 所有任务走默认 celery 队列
    task_routes={},
    
    # 定时任务配置 (Celery Beat)
    beat_schedule={
        "check-subscriptions-runtime": {
            "task": "app.tasks.download_tasks.check_subscriptions",
            "schedule": crontab(minute=0),
        },
        "check-x-subscriptions-hourly": {
            "task": "app.tasks.x_download_tasks.check_x_subscriptions",
            "schedule": crontab(minute=30),
        },
        "detect-stuck-tasks": {
            "task": "app.tasks.download_tasks.detect_stuck_tasks",
            "schedule": crontab(minute="*/5"),
        },
    },
)


# 自动发现任务
celery_app.autodiscover_tasks(["app.tasks"])


# ============ 简单测试任务 ============
# 用于验证 Worker 是否真的能消费队列中的任务

@celery_app.task(name="app.tasks.celery_app.echo_test")
def echo_test():
    """最简单的测试任务 — 如果 Worker 能执行，会在活动日志中写入记录"""
    from datetime import datetime
    from app.core import redis_client as rc
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    rc.append_activity_log("info", "system",
        "🔔 测试任务已执行",
        f"Worker 正常消费队列中的任务 - {ts}")
    return {"ok": True, "timestamp": ts}


# ============ Worker 生命周期信号 ============

from celery.signals import worker_ready, worker_shutdown, task_prerun, task_postrun, task_failure


@worker_ready.connect
def on_worker_ready(**kwargs):
    """Worker 启动时只执行一次迁移，并记录已注册任务。"""
    try:
        from app.models.database import init_db_sync
        init_db_sync()
    except Exception as e:
        try:
            from app.core import redis_client
            redis_client.append_activity_log("error", "system", "Celery Worker 数据库迁移失败", str(e)[:200])
        except Exception:
            pass
        # 迁移异常必须进入 Worker 启动日志，不能静默带病继续。
        raise RuntimeError("Celery Worker 数据库迁移失败") from e

    # Redis 活动日志不影响迁移成功判定；Redis 短暂不可用时 Worker 仍可启动。
    try:
        from app.core import redis_client
        registered = sorted([t for t in celery_app.tasks if not t.startswith('celery.')])
        redis_client.append_activity_log(
            "info", "system",
            "✅ Celery Worker 已启动并就绪",
            f"已注册 {len(registered)} 个任务: {', '.join(registered[:15])}"
        )
    except Exception:
        pass


@worker_shutdown.connect
def on_worker_shutdown(**kwargs):
    """Worker 关闭时记录"""
    try:
        from app.core import redis_client
        redis_client.append_activity_log(
            "warning", "system",
            "⚠️ Celery Worker 正在关闭",
            "下载任务将无法执行，直到 Worker 重新启动"
        )
    except Exception:
        pass


@task_prerun.connect
def on_task_prerun(task_id, task, args, **kwargs):
    """任务开始执行时记录"""
    try:
        from app.core import redis_client
        task_short = task.name.rsplit(".", 1)[-1] if task.name else str(task)
        redis_client.append_activity_log(
            "info", "task",
            f"▶ 任务开始执行: {task_short}",
            f"celery_task_id={task_id}, args={args}"
        )
    except Exception:
        pass


@task_failure.connect
def on_task_failure(task_id, exception, traceback, sender, **kwargs):
    """任务执行异常时记录"""
    try:
        from app.core import redis_client
        task_short = sender.name.rsplit(".", 1)[-1] if sender and sender.name else str(task_id)
        redis_client.append_activity_log(
            "error", "task",
            f"❌ 任务异常退出: {task_short}",
            f"celery_task_id={task_id}, error={type(exception).__name__}: {str(exception)[:200]}"
        )
    except Exception:
        pass

