"""跨平台任务操作服务，供各平台接口与统一任务入口共同使用。"""

from __future__ import annotations

import asyncio
from datetime import datetime
import os
import signal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis_client
from app.models.models import DownloadTask, PlatformDownloadTask, XDownloadTask
from app.services.platform_registry import platform_registry
from app.services.platform_task_service import prepare_platform_task_for_retry
from app.services.x_task_service import cancel_x_task, prepare_x_task_for_retry
from app.tasks.download_tasks import download_single_file
from app.tasks.platform_download_tasks import download_platform_profile
from app.tasks.x_download_tasks import download_x_profile
from app.services.download_lifecycle import prepare_download_retry


ACTIVE_STATUSES = {"pending", "downloading", "paused"}
RETRYABLE_STATUSES = {"failed", "cancelled"}


class TaskOperationError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def parse_task_key(task_key: str) -> tuple[str, int]:
    platform, separator, raw_id = str(task_key or "").strip().lower().partition(":")
    if not separator or not platform or not raw_id.isdigit() or int(raw_id) <= 0:
        raise TaskOperationError(f"任务标识无效：{task_key}")
    try:
        platform_registry.get(platform)
    except KeyError as exc:
        raise TaskOperationError(str(exc), 404) from exc
    return platform, int(raw_id)


def _terminate_pid(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


async def _load_for_update(db: AsyncSession, model, task_id: int, *conditions):
    task = (await db.execute(
        select(model).where(model.id == task_id, *conditions).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not task:
        raise TaskOperationError("任务不存在", 404)
    return task


async def _dispatch_or_fail(db: AsyncSession, task, dispatcher, message: str) -> None:
    try:
        await asyncio.to_thread(dispatcher.delay, task.id)
    except Exception as exc:
        raise TaskOperationError(message, 503) from exc
    await db.refresh(task)


async def _cancel_douyin(db: AsyncSession, task_id: int) -> None:
    task = await _load_for_update(db, DownloadTask, task_id)
    if task.status not in ACTIVE_STATUSES:
        raise TaskOperationError(f"任务状态为 {task.status}，无法取消")
    old_token = task.celery_task_id
    await asyncio.to_thread(redis_client.pause_task, task.id)
    task.status = "cancelled"
    task.celery_task_id = None
    task.completed_at = datetime.now()
    from app.models.models import Work, Author
    from app.services.work_manager import refresh_work_download_state, recalc_author_counts
    work = await db.get(Work, task.work_id)
    if work:
        await refresh_work_download_state(db, work)
        await recalc_author_counts(db, await db.get(Author, work.author_id))
    if old_token:
        await asyncio.to_thread(redis_client.invalidate_download_attempt, task.id, old_token)
    await db.commit()


async def _retry_douyin(db: AsyncSession, task_id: int, *, force_refresh: bool = False,
                        resume: bool = False) -> None:
    task = await _load_for_update(db, DownloadTask, task_id)
    if task.status not in (RETRYABLE_STATUSES | ({"paused"} if resume else set())):
        raise TaskOperationError(f"任务状态为 {task.status}，无需重试")
    await asyncio.to_thread(redis_client.resume_task, task.id)
    await asyncio.to_thread(redis_client.delete_progress, task.id)
    prepare_download_retry(task)
    await db.commit()
    try:
        await asyncio.to_thread(download_single_file.apply_async, args=[task.id],
                                kwargs={"force_refresh": force_refresh})
    except Exception as exc:
        raise TaskOperationError("任务队列暂不可用，任务未重新投递", 503) from exc


async def _cancel_x(db: AsyncSession, task_id: int) -> None:
    task = await _load_for_update(db, XDownloadTask, task_id)
    if task.status not in ACTIVE_STATUSES:
        raise TaskOperationError(f"任务状态为 {task.status}，无法取消")
    pid = await asyncio.to_thread(redis_client.get_x_task_pid, task.id)
    await asyncio.to_thread(_terminate_pid, pid)
    cancel_x_task(task)
    old_token = task.celery_task_id
    task.celery_task_id = None
    if old_token:
        await asyncio.to_thread(redis_client.clear_external_attempt, "x", task.id, old_token)
    await db.commit()


async def _retry_x(db: AsyncSession, task_id: int) -> None:
    task = await _load_for_update(db, XDownloadTask, task_id)
    if task.status not in RETRYABLE_STATUSES:
        raise TaskOperationError(f"任务状态为 {task.status}，无需重试")
    prepare_x_task_for_retry(task)
    await asyncio.to_thread(redis_client.delete_x_task_state, task.id)
    await asyncio.to_thread(redis_client.delete_x_task_pid, task.id)
    await db.commit()
    await _dispatch_or_fail(db, task, download_x_profile, "任务队列暂不可用，X 任务未重新投递")


async def _cancel_platform(db: AsyncSession, platform: str, task_id: int) -> None:
    task = await _load_for_update(
        db, PlatformDownloadTask, task_id, PlatformDownloadTask.platform == platform,
    )
    if task.status not in ACTIVE_STATUSES:
        raise TaskOperationError(f"任务状态为 {task.status}，无法取消")
    pid = await asyncio.to_thread(redis_client.get_platform_task_pid, platform, task.id)
    await asyncio.to_thread(_terminate_pid, pid)
    task.status = "cancelled"
    task.phase = "cancelled"
    task.completed_at = datetime.now()
    old_token = task.celery_task_id
    task.celery_task_id = None
    if old_token:
        await asyncio.to_thread(redis_client.clear_external_attempt, platform, task.id, old_token)
    await db.commit()


async def _retry_platform(db: AsyncSession, platform: str, task_id: int) -> None:
    task = await _load_for_update(
        db, PlatformDownloadTask, task_id, PlatformDownloadTask.platform == platform,
    )
    if task.status not in RETRYABLE_STATUSES:
        raise TaskOperationError(f"任务状态为 {task.status}，无需重试")
    if platform == "xhs" and task.source_type == "profile":
        raise TaskOperationError("小红书作者主页批量采集已搁置；当前仅保留单条笔记下载", 410)
    prepare_platform_task_for_retry(task)
    await asyncio.to_thread(redis_client.delete_platform_task_state, platform, task.id)
    await asyncio.to_thread(redis_client.delete_platform_task_pid, platform, task.id)
    await db.commit()
    await _dispatch_or_fail(
        db, task, download_platform_profile, f"任务队列暂不可用，{platform} 任务未重新投递",
    )


async def operate_task(db: AsyncSession, task_key: str, action: str) -> None:
    platform, task_id = parse_task_key(task_key)
    if action not in {"retry", "cancel", "pause", "resume", "refresh_retry"}:
        raise TaskOperationError(f"不支持的任务操作：{action}")
    try:
        if platform == "douyin":
            if action == "cancel":
                await _cancel_douyin(db, task_id)
            elif action == "pause":
                task = await _load_for_update(db, DownloadTask, task_id)
                if task.status not in {"pending", "downloading"}:
                    raise TaskOperationError("只有排队或下载中的任务可以暂停")
                await asyncio.to_thread(redis_client.pause_task, task_id)
                if task.status == "pending":
                    task.status = "paused"
                    await db.commit()
            else:
                await _retry_douyin(db, task_id, force_refresh=action == "refresh_retry",
                                    resume=action == "resume")
        elif action in {"pause", "resume", "refresh_retry"}:
            raise TaskOperationError("此平台不支持该操作")
        elif platform == "x":
            await (_retry_x(db, task_id) if action == "retry" else _cancel_x(db, task_id))
        else:
            await (
                _retry_platform(db, platform, task_id)
                if action == "retry"
                else _cancel_platform(db, platform, task_id)
            )
    except TaskOperationError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise
