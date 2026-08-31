"""
作品管理 API 路由

为什么这样设计：
1. 提供作品级管理能力：删除（单个/批量）、图集单文件删除、重新下载、重试失败
2. 删除统一走 app.services.work_manager，保证级联清理磁盘文件/任务/历史并防止订阅重下
3. 重新下载/重试复用 download_single_file Celery 任务，URL 过期会在任务内自动刷新
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from typing import List
import asyncio

from app.models.database import get_async_db
from app.models.models import Work, WorkStatsSnapshot
from app.models.schemas import MessageResponse, WorkStatsSnapshotResponse
from app.services import work_manager
from app.services.download_task_factory import ensure_download_task_async
from app.services.archive_rules import (
    get_archive_rules,
    serialize_archive_rules,
    work_matches_archive_rules,
)
from app.tasks.download_tasks import download_single_file
from app.core import redis_client

router = APIRouter(prefix="/works", tags=["作品管理"])


class BatchDeleteWorksRequest(BaseModel):
    work_ids: List[int] = Field(default_factory=list)


def _clear_task_progress(task_ids: List[int]) -> None:
    for task_id in task_ids:
        redis_client.delete_progress(task_id)


def _dispatch_download_tasks(task_ids: List[int]) -> None:
    for task_id in task_ids:
        download_single_file.delay(task_id)


async def _load_work_with_tasks(db: AsyncSession, work_id: int) -> Work:
    result = await db.execute(
        select(Work)
        .options(selectinload(Work.download_tasks), selectinload(Work.author))
        .where(Work.id == work_id)
    )
    work = result.scalar_one_or_none()
    if not work:
        raise HTTPException(status_code=404, detail="作品不存在")
    return work


@router.get("/{work_id}/stats", response_model=List[WorkStatsSnapshotResponse])
async def get_work_stats_history(
    work_id: int,
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_async_db),
):
    """按时间倒序返回作品互动统计快照。"""
    exists = await db.scalar(select(Work.id).where(Work.id == work_id))
    if exists is None:
        raise HTTPException(status_code=404, detail="作品不存在")
    result = await db.execute(
        select(WorkStatsSnapshot)
        .where(WorkStatsSnapshot.work_id == work_id)
        .order_by(WorkStatsSnapshot.observed_at.desc(), WorkStatsSnapshot.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


@router.delete("/{work_id}", response_model=MessageResponse)
async def delete_work(work_id: int, db: AsyncSession = Depends(get_async_db)):
    """删除单个作品：清理磁盘文件 + 任务 + 历史，并标记排除防止订阅重下。"""
    work = await _load_work_with_tasks(db, work_id)
    stats = await work_manager.delete_work(db, work)
    await db.commit()
    return MessageResponse(
        success=True,
        message=f"作品已删除（清理 {stats['removed_files']} 个文件、{stats['removed_tasks']} 个任务）",
        data=stats,
    )


@router.post("/batch-delete", response_model=MessageResponse)
async def batch_delete_works(
    payload: BatchDeleteWorksRequest,
    db: AsyncSession = Depends(get_async_db),
):
    """批量删除作品（多选）。"""
    work_ids = [int(i) for i in (payload.work_ids or [])]
    if not work_ids:
        raise HTTPException(status_code=400, detail="未提供要删除的作品")

    result = await db.execute(
        select(Work)
        .options(selectinload(Work.download_tasks))
        .where(Work.id.in_(work_ids))
    )
    works = result.scalars().all()

    deleted = 0
    removed_files = 0
    removed_tasks = 0
    for work in works:
        stats = await work_manager.delete_work(db, work)
        deleted += 1
        removed_files += stats["removed_files"]
        removed_tasks += stats["removed_tasks"]

    await db.commit()
    return MessageResponse(
        success=True,
        message=f"已删除 {deleted} 个作品（清理 {removed_files} 个文件、{removed_tasks} 个任务）",
        data={"deleted": deleted, "removed_files": removed_files, "removed_tasks": removed_tasks},
    )


@router.delete("/{work_id}/files/{file_index}", response_model=MessageResponse)
async def delete_work_file(
    work_id: int,
    file_index: int,
    db: AsyncSession = Depends(get_async_db),
):
    """删除图集作品中的单个文件。"""
    work = await _load_work_with_tasks(db, work_id)
    if work.work_type != "images":
        raise HTTPException(status_code=400, detail="仅图集作品支持单文件删除")
    stats = await work_manager.delete_work_file(db, work, file_index)
    await db.commit()
    return MessageResponse(
        success=True,
        message=f"已删除第 {file_index + 1} 个文件",
        data=stats,
    )


@router.post("/{work_id}/redownload", response_model=MessageResponse)
async def redownload_work(work_id: int, db: AsyncSession = Depends(get_async_db)):
    """重新下载作品：清除排除标记，重建缺失任务并分发（已完成文件保留）。"""
    work = await _load_work_with_tasks(db, work_id)
    archive_rules = await get_archive_rules(db)
    matches, reason = work_matches_archive_rules(work, archive_rules)
    if not matches:
        raise HTTPException(status_code=400, detail=f"作品不符合当前归档规则：{reason}")
    archive_snapshot = serialize_archive_rules(archive_rules)

    # 清除作品级与文件级排除标记
    work.is_excluded = False
    work.excluded_at = None
    work.excluded_file_indices = []

    if work.work_type == "video":
        needed_indices = [0]
    else:
        count = max(int(work.image_count or 0), len(work.image_urls or []))
        needed_indices = list(range(count)) if count > 0 else [0]

    dispatch_ids: List[int] = []
    clear_progress_ids: List[int] = []
    for idx in needed_indices:
        task, action = await ensure_download_task_async(
            db, work.id, idx, archive_rule_snapshot=archive_snapshot,
        )
        if action in {"created", "reused"}:
            dispatch_ids.append(task.id)
        elif task.status != "completed":
            task.status = "pending"
            task.error_message = None
            task.archive_rule_snapshot = archive_snapshot
            clear_progress_ids.append(task.id)
            dispatch_ids.append(task.id)

    if dispatch_ids:
        work.is_downloaded = False
    await work_manager.recalc_author_counts(db, work.author)

    if clear_progress_ids:
        await asyncio.to_thread(_clear_task_progress, clear_progress_ids)
    await db.commit()

    await asyncio.to_thread(_dispatch_download_tasks, dispatch_ids)

    return MessageResponse(
        success=True,
        message=f"已提交 {len(dispatch_ids)} 个下载任务",
        data={"dispatched": len(dispatch_ids)},
    )


@router.post("/{work_id}/retry-failed", response_model=MessageResponse)
async def retry_work_failed(work_id: int, db: AsyncSession = Depends(get_async_db)):
    """重试该作品下所有失败/取消的任务。"""
    work = await _load_work_with_tasks(db, work_id)

    failed_tasks = [
        t for t in (work.download_tasks or [])
        if t.status in ("failed", "cancelled")
    ]
    if not failed_tasks:
        return MessageResponse(success=True, message="该作品没有失败任务", data={"count": 0})

    failed_task_ids = [task.id for task in failed_tasks]
    await asyncio.to_thread(_clear_task_progress, failed_task_ids)
    for task in failed_tasks:
        task.status = "pending"
        task.error_message = None

    await db.commit()

    await asyncio.to_thread(_dispatch_download_tasks, failed_task_ids)

    return MessageResponse(
        success=True,
        message=f"已重新提交 {len(failed_tasks)} 个失败任务",
        data={"count": len(failed_tasks)},
    )
