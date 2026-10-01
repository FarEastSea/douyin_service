"""下载执行租约：队列 ID 是持久化 fencing token，不允许旧执行回写。"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from app.core.queue_configuration import DynamicQueueTask
from sqlalchemy import event, select, update

from app.models.models import DownloadTask, XDownloadTask, PlatformDownloadTask


class StaleDownloadAttempt(RuntimeError):
    pass


def prepare_download_retry(task, *, allow_completed: bool = False) -> None:
    if task.status in {"downloading", "pending"}:
        raise ValueError("任务仍在执行或排队，请先暂停或取消")
    if task.status == "completed" and not allow_completed:
        raise ValueError("任务已经完成")
    if task.status == "cancelled":
        task.temp_file_path = None
    task.status = "pending"
    task.celery_task_id = None
    task.error_message = None
    task.started_at = None
    task.completed_at = None
    task.download_speed = 0
    task.downloaded_bytes = 0
    task.retry_count = (task.retry_count or 0) + 1


def lock_download_attempt(db, task_id: int, token: str, model=DownloadTask) -> None:
    if db.info.get("download_attempt_locked") == (model, task_id, token):
        return
    with db.no_autoflush:
        row = db.execute(select(model.status, model.celery_task_id)
                         .where(model.id == task_id).with_for_update()).first()
    if row is None or row.status != "downloading" or row.celery_task_id != token:
        raise StaleDownloadAttempt("执行已被暂停、取消或替换，忽略旧结果")
    db.info["download_attempt_locked"] = (model, task_id, token)


def bind_download_attempt(db, task_id: int, token: str, model=DownloadTask) -> None:
    # before_flush covers autoflush, metadata, counters and history, not just commit().
    def guard(session, context, instances):
        lock_download_attempt(session, task_id, token, model)
    event.listen(db, "before_flush", guard)
    def unlock(session, transaction):
        if transaction.parent is None:
            session.info.pop("download_attempt_locked", None)
    event.listen(db, "after_transaction_end", unlock)


class FencedDownloadTask(DynamicQueueTask):
    """所有 delay/apply_async 入口在发布消息前登记执行 ID。"""
    abstract = True
    execution_model = DownloadTask

    def apply_async(self, args=None, kwargs=None, **options):
        from app.core.config_transaction import configuration_lock
        with configuration_lock():
            return self._reserve_and_publish(args, kwargs, options)

    def _reserve_and_publish(self, args, kwargs, options):
        from app.models.database import get_sync_db
        from app.core import redis_client
        task_id = (args or [None])[0] or (kwargs or {}).get("task_id")
        token = str(options.pop("task_id", None) or uuid4())
        db = get_sync_db()
        model = self.execution_model
        platform = None
        try:
            claimed = db.execute(update(model).where(
                model.id == task_id, model.status == "pending",
            ).values(celery_task_id=token))
            if claimed.rowcount != 1:
                db.rollback()
                raise StaleDownloadAttempt("任务不再处于待下载状态，未重复投递")
            try:
                if model is DownloadTask:
                    redis_client.resume_task(task_id)
                    redis_client.activate_download_attempt(task_id, token)
                else:
                    platform = "x" if model is XDownloadTask else db.scalar(select(model.platform).where(model.id == task_id))
                    redis_client.activate_external_attempt(platform, task_id, token)
                # 注册 Redis 所有者时仍持有数据库行锁，阻止并发投递倒序覆盖。
                db.commit()
                return self._publish(args, kwargs, {**options, "task_id": token})
            except Exception:
                db.rollback()
                db.execute(update(model).where(
                    model.id == task_id, model.status == "pending",
                    model.celery_task_id == token,
                ).values(status="failed", error_message="任务队列不可用，请重试",
                         completed_at=datetime.now()))
                db.commit()
                if model is DownloadTask:
                    redis_client.invalidate_download_attempt(task_id, token)
                elif platform:
                    redis_client.clear_external_attempt(platform, task_id, token)
                raise
        finally:
            db.close()


class FencedXTask(FencedDownloadTask):
    abstract = True
    execution_model = XDownloadTask


class FencedPlatformTask(FencedDownloadTask):
    abstract = True
    execution_model = PlatformDownloadTask
