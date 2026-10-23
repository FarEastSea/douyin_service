"""外部下载的执行权与可持久恢复的协调延期。"""
from datetime import datetime
import logging
from uuid import uuid4

from redis.exceptions import RedisError
from sqlalchemy import or_, select

from app.core import redis_client
from app.core.traffic_control import CoordinationUnavailable, assert_download_coordination
from app.models.models import PlatformDownloadTask, XDownloadTask
from app.services.download_lifecycle import StaleDownloadAttempt

logger = logging.getLogger(__name__)
COORDINATION_PREFIX = "CoordinationUnavailable:"


def check_external_execution(platform: str, task_id: int, token: str) -> None:
    assert_download_coordination()
    try:
        current = redis_client.external_attempt_current(platform, task_id, token)
    except RedisError as exc:
        raise CoordinationUnavailable("下载执行协调暂不可用，等待 Redis 恢复") from exc
    if not current:
        # Redis 重启可能丢失执行键。数据库仍确认本执行时，应重新协调，
        # 不能把它当作用户取消后留下永久 downloading 记录。
        from app.models.database import get_sync_db
        model = XDownloadTask if platform == "x" else PlatformDownloadTask
        with get_sync_db() as db:
            owner = db.execute(select(model.status, model.celery_task_id).where(model.id == task_id)).first()
        if owner is not None and owner.status == "downloading" and owner.celery_task_id == token:
            raise CoordinationUnavailable("共享执行键已失效，等待重新协调")
        raise StaleDownloadAttempt("下载执行已被取消或替换")


def register_external_process(platform: str, task_id: int, token: str, process_id: int) -> None:
    try:
        redis_client.set_external_attempt_pid(platform, task_id, token, process_id)
    except StaleDownloadAttempt:
        # 创建进程与登记 PID 之间可能恰好发生 Redis 重启，仍需按数据库
        # 执行权区分基础设施故障和用户取消，不能留下永久运行状态。
        check_external_execution(platform, task_id, token)
        raise


def _reset_for_coordination(task) -> None:
    task.status, task.phase = "pending", "queued"
    task.celery_task_id = None
    task.started_at = task.completed_at = None
    task.error_code = "coordination_unavailable"
    task.error_message = COORDINATION_PREFIX + " 共享下载协调暂不可用，等待自动恢复"
    task.last_heartbeat_at = datetime.now()


def defer_external_download(model, task_id: int, token: str, dispatcher) -> dict:
    """结束旧执行后延期；协调仍断开时由持久标记等待下一轮后台恢复。"""
    from app.models.database import get_sync_db
    with get_sync_db() as db:
        task = db.scalar(select(model).where(model.id == task_id).with_for_update())
        if task is None or task.status != "downloading" or task.celery_task_id != token:
            return {"success": False, "superseded": True}
        _reset_for_coordination(task)
        db.commit()
    next_token = str(uuid4())
    try:
        dispatcher.apply_async(args=[task_id], countdown=30, task_id=next_token)
    except Exception:
        # 生产者可能已把投递失败写成 failed。保留专用标记以便 Beat 恢复；
        # 不改写已被其他执行认领或被用户取消的任务。
        with get_sync_db() as db:
            task = db.scalar(select(model).where(model.id == task_id).with_for_update())
            if (task and task.status in {"pending", "failed"}
                    and task.celery_task_id in {None, next_token}
                    and (task.error_message or "").startswith(COORDINATION_PREFIX)):
                _reset_for_coordination(task)
                db.commit()
        logger.warning("外部下载 %s:%s 已延期，等待后台恢复", model.__tablename__, task_id)
    return {"success": False, "deferred": True, "retry_after": 30}


def recover_external_coordination_tasks() -> int:
    """只恢复已明确标记的基础设施故障，不自动重复用户操作或业务失败。"""
    from app.models.database import get_sync_db
    from app.tasks.x_download_tasks import download_x_profile
    from app.tasks.platform_download_tasks import download_platform_profile
    recovered = 0
    for model, dispatcher in ((XDownloadTask, download_x_profile), (PlatformDownloadTask, download_platform_profile)):
        with get_sync_db() as db:
            ids = list(db.scalars(select(model.id).where(
                model.error_message.startswith(COORDINATION_PREFIX),
                or_(model.status == "failed", (model.status == "pending") & model.celery_task_id.is_(None)),
            ).order_by(model.id).limit(100)))
        for task_id in ids:
            with get_sync_db() as db:
                task = db.scalar(select(model).where(model.id == task_id).with_for_update())
                if (task is None or not (task.error_message or "").startswith(COORDINATION_PREFIX)
                        or task.status not in {"pending", "failed"}
                        or (task.status == "pending" and task.celery_task_id)):
                    continue
                _reset_for_coordination(task)
                db.commit()
            try:
                dispatcher.apply_async(args=[task_id], countdown=30)
                recovered += 1
            except Exception:
                # FencedTask 的投递失败仍写入同一故障前缀，下次周期继续恢复。
                logger.warning("外部任务 %s:%s 暂未恢复投递", model.__tablename__, task_id)
                return recovered
    return recovered
