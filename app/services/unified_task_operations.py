"""跨平台任务操作服务，供各平台接口与统一任务入口共同使用。"""

from __future__ import annotations

import asyncio
from datetime import datetime
import os
import signal

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis_client
from app.models.models import (
    Author, Work, DownloadHistory, DownloadTask, PlatformDownloadTask, XDownloadTask,
)
from app.services.platform_registry import platform_registry
from app.services.platform_task_service import prepare_platform_task_for_retry
from app.services.x_task_service import cancel_x_task, prepare_x_task_for_retry
from app.tasks.download_tasks import download_single_file
from app.tasks.platform_download_tasks import download_platform_profile
from app.tasks.x_download_tasks import download_x_profile
from app.services.download_lifecycle import prepare_download_retry
from app.services.download_state_lock import lock_task_graph


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
        return
    # 最多等 5 秒让进程收尾，避免随后的文件清理移走它仍在写入的分片。
    try:
        import psutil

        psutil.Process(pid).wait(timeout=5)
    except Exception:
        pass


async def _load_for_update(db: AsyncSession, model, task_id: int, *conditions):
    if model is DownloadTask:
        task = await lock_task_graph(db, task_id)
        if task is None:
            raise TaskOperationError("任务不存在", 404)
        return task
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
    from app.services.media_deletion import retire_attempt_artifacts
    await db.run_sync(lambda session: retire_attempt_artifacts(session, task))
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
    from app.services.work_manager import refresh_work_download_state, recalc_author_counts
    work = await db.get(Work, task.work_id)
    await refresh_work_download_state(db, work)
    await recalc_author_counts(db, await db.get(Author, work.author_id))
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
    if platform == "instagram":
        from datetime import timedelta
        from app.models.models import PlatformSubscriptionState
        from app.services.instagram_jobs import options_for
        from app.services.instagram_runtime import utcnow
        from app.services.instagram_store import interval_for
        state_id = options_for(task).get("state_id")
        state = await db.get(PlatformSubscriptionState, state_id) if state_id else None
        if state and state.scan_mode == "history":
            states = (await db.scalars(select(PlatformSubscriptionState).where(PlatformSubscriptionState.author_id == state.author_id, PlatformSubscriptionState.scan_mode == "history"))).all()
            for stream in states:
                stream.enabled = stream.manual = False
                stream.deferred_reason = "历史任务已取消，补采已暂停"
        elif state:
            state.manual = False
            state.next_check_at = utcnow() + timedelta(seconds=interval_for(state.content_kind))
            state.deferred_reason = "本次检查已取消，等待下个周期"
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
    if platform == "instagram":
        from app.tasks.platform_download_tasks import dispatch_instagram
        import json
        options = json.loads(task.instagram_options or "{}")
        options.pop("not_before", None)
        from app.models.models import PlatformSubscriptionState
        from app.services.instagram_runtime import utcnow
        state_id = options.get("state_id")
        state = await db.get(PlatformSubscriptionState, state_id) if state_id else None
        if state:
            state.blocked_code = None
            state.enabled = state.manual = True
            state.next_check_at, state.deferred_reason = utcnow(), None
            # 暂停取消时会清空 task_id；重试后重新绑定，手动检查才能找到并解除它的延期。
            state.task_id = task.id
        task.instagram_options = json.dumps(options)
        await db.commit()
        await asyncio.to_thread(dispatch_instagram.delay)
        return
    await db.commit()
    await _dispatch_or_fail(
        db, task, download_platform_profile, f"任务队列暂不可用，{platform} 任务未重新投递",
    )


async def _delete_failed_task(
    db: AsyncSession, platform: str, task_id: int, delete_files: bool = False,
) -> dict:
    """删除已结束任务；完整文件默认保留，损坏或未完成的文件随任务清理。"""
    model = DownloadTask if platform == "douyin" else XDownloadTask if platform == "x" else PlatformDownloadTask
    conditions = (PlatformDownloadTask.platform == platform,) if model is PlatformDownloadTask else ()
    task = await _load_for_update(db, model, task_id, *conditions)
    if task.status not in RETRYABLE_STATUSES | {"completed", "skipped"}:
        raise TaskOperationError("只可删除已结束任务；排队、下载或暂停任务请先取消", 409)
    from app.services.task_file_cleanup import stage_task_files

    try:
        files = await db.run_sync(
            lambda session: stage_task_files(session, platform, task, delete_files=delete_files)
        )
    except (ValueError, OSError) as exc:
        raise TaskOperationError(f"文件未删除：{exc}", 409) from exc
    old_token = task.celery_task_id
    if old_token:
        if platform == "douyin":
            await asyncio.to_thread(redis_client.invalidate_download_attempt, task_id, old_token)
        else:
            await asyncio.to_thread(redis_client.clear_external_attempt, platform, task_id, old_token)
    if platform == "douyin":
        from app.services.work_manager import refresh_work_download_state, recalc_author_counts
        work = await db.get(Work, task.work_id)
        # 删除任务不等于排除作品；后续扫描确认作品仍存在时允许补建任务。
        # 兼容尚未启用级联外键的历史安装。只删记录，不触碰磁盘文件。
        await db.execute(delete(DownloadHistory).where(DownloadHistory.task_id == task_id))
        await db.delete(task)
        await refresh_work_download_state(db, work)
        await recalc_author_counts(db, await db.get(Author, work.author_id))
    else:
        x_author_id = getattr(task, "x_author_id", None) if platform == "x" else None
        await db.delete(task)
        if x_author_id:
            # 任务的媒体随任务级联删除，立即重算作者下载数。
            from app.services.media_removal import refresh_media_counts

            await db.flush()
            await refresh_media_counts(db, "x", task_id=None, x_author_id=x_author_id)
    await db.commit()
    # 缓存清理失败不改变数据库操作已经成功的事实。
    try:
        if platform == "douyin":
            await asyncio.to_thread(redis_client.delete_progress, task_id)
        elif platform == "x":
            await asyncio.to_thread(redis_client.delete_x_task_state, task_id)
            await asyncio.to_thread(redis_client.delete_x_task_pid, task_id)
        else:
            await asyncio.to_thread(redis_client.delete_platform_task_state, platform, task_id)
            await asyncio.to_thread(redis_client.delete_platform_task_pid, platform, task_id)
    except Exception:
        import logging
        logging.getLogger(__name__).warning("任务 %s:%s 已删除，但缓存清理失败", platform, task_id, exc_info=True)
    return files


def deletion_summary(files: dict | None) -> str:
    """把文件处理结果转成给用户看的说明。"""
    files = files or {}
    parts = []
    if files.get("removed"):
        parts.append(f"已清理 {files['removed']} 个文件")
    if files.get("kept"):
        parts.append(f"保留 {files['kept']} 个完整文件，已归入媒体库的无索引媒体")
    return "，".join(parts)


async def delete_task_with_files(db: AsyncSession, task_key: str, *, delete_files: bool = False) -> dict:
    """单任务删除入口：X 与通用平台进行中的任务先取消，再按文件完整性处理。"""
    platform, task_id = parse_task_key(task_key)
    if platform != "douyin":
        model = XDownloadTask if platform == "x" else PlatformDownloadTask
        status = await db.scalar(select(model.status).where(model.id == task_id))
        if status is None:
            raise TaskOperationError("任务不存在", 404)
        if status in ACTIVE_STATUSES:
            try:
                await operate_task(db, task_key, "cancel")
            except TaskOperationError:
                # 读取状态后任务可能刚好结束；已结束就直接删除，否则如实报错。
                if await db.scalar(select(model.status).where(model.id == task_id)) in ACTIVE_STATUSES:
                    raise
            try:
                return await operate_task(db, task_key, "delete", delete_files=delete_files) or {}
            except TaskOperationError as exc:
                raise TaskOperationError(f"任务已取消，但删除未完成：{exc}", exc.status_code) from exc
    return await operate_task(db, task_key, "delete", delete_files=delete_files) or {}


async def operate_task(
    db: AsyncSession, task_key: str, action: str, *, delete_files: bool = False,
) -> dict | None:
    platform, task_id = parse_task_key(task_key)
    if action not in {"retry", "cancel", "pause", "resume", "refresh_retry", "delete"}:
        raise TaskOperationError(f"不支持的任务操作：{action}")
    try:
        if action == "delete":
            return await _delete_failed_task(db, platform, task_id, delete_files)
        elif platform == "douyin":
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


async def delete_douyin_tasks(db: AsyncSession, task_ids: list[int]) -> int:
    """批量删除保持单任务事务语义；先预锁全部父记录，避免跨作者倒序。"""
    from app.services.download_state_lock import lock_works
    ids = sorted(set(task_ids))
    work_ids = list((await db.scalars(select(DownloadTask.work_id).where(DownloadTask.id.in_(ids)))).all())
    await lock_works(db, work_ids)
    tasks = list((await db.scalars(select(DownloadTask).where(DownloadTask.id.in_(ids))
        .order_by(DownloadTask.id).with_for_update().execution_options(populate_existing=True))).all())
    if any(task.status not in {"failed", "cancelled", "completed", "skipped"} for task in tasks):
        raise TaskOperationError("排队、下载或暂停任务请先取消，再删除", 409)
    from app.services.work_manager import refresh_work_download_state, recalc_author_counts
    from app.services.task_file_cleanup import stage_task_files
    owner_ids = set()
    for task in tasks:
        # 批量删除不提供删除完整文件的选项，但损坏或未完成的文件同样随任务清理。
        try:
            await db.run_sync(lambda session, item=task: stage_task_files(session, "douyin", item, delete_files=False))
        except (ValueError, OSError) as exc:
            raise TaskOperationError(f"文件未删除：{exc}", 409) from exc
        if task.celery_task_id:
            await asyncio.to_thread(redis_client.invalidate_download_attempt, task.id, task.celery_task_id)
        await db.execute(delete(DownloadHistory).where(DownloadHistory.task_id == task.id))
        await db.delete(task)
    for work_id in sorted(set(work_ids)):
        work = await db.get(Work, work_id)
        if work:
            await refresh_work_download_state(db, work)
            owner_ids.add(work.author_id)
    for owner_id in sorted(owner_ids):
        await recalc_author_counts(db, await db.get(Author, owner_id))
    await db.commit()
    for task_id in ids:
        try:
            await asyncio.to_thread(redis_client.delete_progress, task_id)
        except Exception:
            pass
    return len(tasks)
