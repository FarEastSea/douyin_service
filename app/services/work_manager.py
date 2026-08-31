"""
作品管理服务

为什么这样设计：
1. 集中处理「删除作品 / 删除单个文件」时的磁盘文件清理，避免散落在各 API 中
2. 删除文件前强制校验路径在 DOWNLOAD_DIR 内，杜绝越权删除
3. 删除历史再删任务兼容尚未升级级联外键的旧数据库
4. 删除/重下后回写 Author.downloaded_works，保持统计一致
"""

import os
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Set

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, selectinload

from app.core import redis_client
from app.core.config import settings
from app.models.models import Author, Work, DownloadTask, DownloadHistory


def _safe_remove_file(path: Optional[str]) -> bool:
    """删除单个文件，仅当其位于 DOWNLOAD_DIR 内且存在时执行。返回是否实际删除。"""
    if not path:
        return False
    try:
        target = Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False

    try:
        download_root = Path(settings.DOWNLOAD_DIR).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False

    try:
        target.relative_to(download_root)
    except ValueError:
        # 不在下载目录内，拒绝删除
        return False

    removed = False
    candidates = (
        target,
        target.with_name(target.name + ".downloading"),
        target.with_name(target.name + ".json"),
        target.with_name(target.name + ".csv"),
        target.with_name(target.name + ".json.tmp"),
        target.with_name(target.name + ".csv.tmp"),
    )
    for candidate in candidates:
        try:
            if candidate.is_file():
                candidate.unlink()
                removed = True
        except OSError:
            pass
    return removed


def _collect_task_file_paths(task: DownloadTask) -> List[str]:
    """收集一个任务可能落盘的所有路径。"""
    paths: List[str] = []
    if task.file_path:
        paths.append(task.file_path)
    if task.temp_file_path:
        paths.append(task.temp_file_path)
    return paths


def _collect_history_file_paths(history: DownloadHistory) -> List[str]:
    """收集下载历史中记录的落盘路径。"""
    if history.file_path:
        return [history.file_path]
    return []


def _revoke_celery_task(celery_task_id: Optional[str]) -> bool:
    """最佳努力撤销 Celery 任务。"""
    if not celery_task_id:
        return False

    try:
        from app.tasks.celery_app import celery_app

        celery_app.control.revoke(celery_task_id, terminate=True)
        return True
    except Exception:
        return False


async def _purge_tasks(db: AsyncSession, tasks: Iterable[DownloadTask]) -> int:
    """删除给定任务及其磁盘文件与下载历史。返回删除的文件数。"""
    removed_files = 0
    for task in tasks:
        # 正在下载的任务，先暂停并清理进度缓存
        if task.status == "downloading":
            try:
                redis_client.pause_task(task.id)
            except Exception:
                pass
        try:
            redis_client.delete_progress(task.id)
        except Exception:
            pass

        for path in _collect_task_file_paths(task):
            if _safe_remove_file(path):
                removed_files += 1

        # 先删历史（无级联），再删任务
        history_rows = await db.execute(
            select(DownloadHistory).where(DownloadHistory.task_id == task.id)
        )
        for history in history_rows.scalars().all():
            await db.delete(history)

        await db.delete(task)

    return removed_files


async def recalc_author_counts(db: AsyncSession, author: Author) -> None:
    """按未排除作品数与完整下载作品数回写作者计数。"""
    if author is None:
        return
    # 会话关闭了 autoflush，先落实本次任务增删和作品排除状态，
    # 否则下面的聚合查询会读到事务内的旧值。
    await db.flush()

    downloaded = await db.execute(
        select(func.count(Work.id))
        .where(
            Work.author_id == author.id,
            Work.is_excluded == False,  # noqa: E712
            exists(select(DownloadTask.id).where(DownloadTask.work_id == Work.id)),
            ~exists(select(DownloadTask.id).where(
                DownloadTask.work_id == Work.id,
                or_(DownloadTask.status != "completed", DownloadTask.status.is_(None)),
            )),
        )
    )
    author.downloaded_works = int(downloaded.scalar() or 0)

    total = await db.execute(
        select(func.count(Work.id)).where(
            Work.author_id == author.id,
            Work.is_excluded == False,  # noqa: E712
        )
    )
    author.total_works = int(total.scalar() or 0)


def refresh_work_download_state_sync(db: Session, work: Work) -> bool:
    """在同步任务中按所有文件任务状态重算作品下载完成标记。"""
    db.flush()
    task_count = int(db.execute(
        select(func.count(DownloadTask.id)).where(DownloadTask.work_id == work.id)
    ).scalar() or 0)
    incomplete_count = int(db.execute(
        select(func.count(DownloadTask.id)).where(
            DownloadTask.work_id == work.id,
            or_(DownloadTask.status != "completed", DownloadTask.status.is_(None)),
        )
    ).scalar() or 0)
    work.is_downloaded = task_count > 0 and incomplete_count == 0
    return bool(work.is_downloaded)


def recalc_author_counts_sync(db: Session, author: Author) -> None:
    """Celery 同步会话使用的作者计数重算。"""
    if author is None:
        return
    db.flush()
    author.downloaded_works = int(db.execute(
        select(func.count(Work.id)).where(
            Work.author_id == author.id,
            Work.is_excluded == False,  # noqa: E712
            exists(select(DownloadTask.id).where(DownloadTask.work_id == Work.id)),
            ~exists(select(DownloadTask.id).where(
                DownloadTask.work_id == Work.id,
                or_(DownloadTask.status != "completed", DownloadTask.status.is_(None)),
            )),
        )
    ).scalar() or 0)
    author.total_works = int(db.execute(
        select(func.count(Work.id)).where(
            Work.author_id == author.id,
            Work.is_excluded == False,  # noqa: E712
        )
    ).scalar() or 0)


async def delete_work(db: AsyncSession, work: Work) -> dict:
    """软删除整个作品：清理磁盘文件 + 任务 + 历史，标记排除，回写计数。

    调用方负责 commit。
    """
    tasks = list(work.download_tasks or [])
    removed_files = await _purge_tasks(db, tasks)

    work.is_excluded = True
    work.excluded_at = datetime.utcnow()
    work.is_downloaded = False
    # 整作品删除时清空单文件排除（语义上整作品已排除）
    work.excluded_file_indices = []

    author = None
    if work.author_id:
        author_row = await db.execute(
            select(Author).where(Author.id == work.author_id)
        )
        author = author_row.scalar_one_or_none()
    await recalc_author_counts(db, author)

    return {"removed_files": removed_files, "removed_tasks": len(tasks)}


async def delete_work_file(db: AsyncSession, work: Work, file_index: int) -> dict:
    """删除图集中的单个文件：清理该 file_index 对应任务的文件/历史/任务行，并记录排除索引。

    调用方负责 commit。
    """
    matched = [t for t in (work.download_tasks or []) if (t.file_index or 0) == file_index]
    removed_files = await _purge_tasks(db, matched)

    excluded = set(work.excluded_file_indices)
    excluded.add(int(file_index))
    work.excluded_file_indices = sorted(excluded)

    # 若该作品已无任何已完成文件，则同步取消 is_downloaded
    remaining_completed = await db.execute(
        select(func.count(DownloadTask.id)).where(
            DownloadTask.work_id == work.id,
            DownloadTask.status == "completed",
        )
    )
    if int(remaining_completed.scalar() or 0) == 0:
        work.is_downloaded = False

    author = None
    if work.author_id:
        author_row = await db.execute(
            select(Author).where(Author.id == work.author_id)
        )
        author = author_row.scalar_one_or_none()
    await recalc_author_counts(db, author)

    return {"removed_files": removed_files, "removed_tasks": len(matched)}


async def delete_author_hard(db: AsyncSession, author_id: int) -> dict:
    """硬删除作者及其所有作品、任务、历史和本地文件，不写入排除列表。"""
    redis_client.mark_author_deleting(author_id)

    task_ids: List[int] = []
    try:
        result = await db.execute(
            select(Author)
            .options(selectinload(Author.works).selectinload(Work.download_tasks))
            .where(Author.id == author_id)
        )
        author = result.scalar_one_or_none()
        if not author:
            raise ValueError("author_not_found")

        works = list(author.works or [])
        tasks = [task for work in works for task in (work.download_tasks or [])]
        task_ids = [task.id for task in tasks]
        work_ids = [work.id for work in works]

        histories: List[DownloadHistory] = []
        if work_ids:
            history_rows = await db.execute(
                select(DownloadHistory).where(DownloadHistory.work_id.in_(work_ids))
            )
            histories = history_rows.scalars().all()

        removed_files = 0
        revoked_tasks = 0
        file_paths: Set[str] = set()

        for task in tasks:
            if task.status in {"pending", "downloading", "paused"}:
                try:
                    redis_client.pause_task(task.id)
                except Exception:
                    pass

                if await asyncio.to_thread(_revoke_celery_task, task.celery_task_id):
                    revoked_tasks += 1

            try:
                redis_client.delete_progress(task.id)
            except Exception:
                pass

            file_paths.update(_collect_task_file_paths(task))

        for history in histories:
            file_paths.update(_collect_history_file_paths(history))

        for path in file_paths:
            if _safe_remove_file(path):
                removed_files += 1

        for history in histories:
            await db.delete(history)

        for task in tasks:
            await db.delete(task)

        for work in works:
            await db.delete(work)

        author_name = author.nickname or f"作者{author_id}"
        await db.delete(author)
        await db.commit()

        try:
            redis_client.invalidate_stats_cache()
            redis_client.append_activity_log(
                "info",
                "api",
                f"🗑️ 已删除作者: {author_name}",
                (
                    f"author_id={author_id}, works={len(works)}, tasks={len(tasks)}, "
                    f"history={len(histories)}, files={removed_files}, revoked={revoked_tasks}"
                ),
            )
        except Exception:
            pass

        return {
            "author_id": author_id,
            "author_name": author_name,
            "removed_works": len(works),
            "removed_tasks": len(tasks),
            "removed_history": len(histories),
            "removed_files": removed_files,
            "revoked_tasks": revoked_tasks,
        }
    except Exception:
        await db.rollback()
        raise
    finally:
        try:
            redis_client.clear_task_pause_states(task_ids)
        except Exception:
            pass
        try:
            redis_client.clear_author_deleting(author_id)
        except Exception:
            pass
