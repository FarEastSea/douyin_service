"""并发安全地创建或复用单文件下载任务。"""

from __future__ import annotations

from typing import Literal, Tuple

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.models import DownloadTask


TaskAction = Literal["created", "reused", "existing"]


def _reuse_if_terminal(task: DownloadTask) -> TaskAction:
    if task.status in {"failed", "cancelled"}:
        task.status = "pending"
        task.error_message = None
        return "reused"
    return "existing"


def _sync_existing(db: Session, work_id: int, file_index: int) -> DownloadTask | None:
    return db.execute(select(DownloadTask).where(
        DownloadTask.work_id == work_id,
        DownloadTask.file_index == file_index,
    )).scalar_one_or_none()


async def _async_existing(
    db: AsyncSession,
    work_id: int,
    file_index: int,
) -> DownloadTask | None:
    result = await db.execute(select(DownloadTask).where(
        DownloadTask.work_id == work_id,
        DownloadTask.file_index == file_index,
    ))
    return result.scalar_one_or_none()


def ensure_download_task_sync(
    db: Session,
    work_id: int,
    file_index: int,
    archive_rule_snapshot: str | None = None,
) -> Tuple[DownloadTask, TaskAction]:
    """Celery 同步会话中的原子 get-or-create。"""
    if db.bind.dialect.name == "postgresql":
        inserted_id = db.execute(
            postgresql_insert(DownloadTask)
            .values(
                work_id=work_id,
                file_index=file_index,
                status="pending",
                archive_rule_snapshot=archive_rule_snapshot,
            )
            .on_conflict_do_nothing(index_elements=["work_id", "file_index"])
            .returning(DownloadTask.id)
        ).scalar_one_or_none()
        task = db.get(DownloadTask, inserted_id) if inserted_id is not None else _sync_existing(db, work_id, file_index)
        if task is None:
            raise RuntimeError("下载任务原子创建后无法读取")
        if not task.archive_rule_snapshot and archive_rule_snapshot:
            task.archive_rule_snapshot = archive_rule_snapshot
        return task, "created" if inserted_id is not None else _reuse_if_terminal(task)

    existing = _sync_existing(db, work_id, file_index)
    if existing is not None:
        if not existing.archive_rule_snapshot and archive_rule_snapshot:
            existing.archive_rule_snapshot = archive_rule_snapshot
        return existing, _reuse_if_terminal(existing)
    try:
        with db.begin_nested():
            task = DownloadTask(
                work_id=work_id,
                file_index=file_index,
                status="pending",
                archive_rule_snapshot=archive_rule_snapshot,
            )
            db.add(task)
            db.flush()
        return task, "created"
    except IntegrityError:
        existing = _sync_existing(db, work_id, file_index)
        if existing is None:
            raise
        if not existing.archive_rule_snapshot and archive_rule_snapshot:
            existing.archive_rule_snapshot = archive_rule_snapshot
        return existing, _reuse_if_terminal(existing)


async def ensure_download_task_async(
    db: AsyncSession,
    work_id: int,
    file_index: int,
    archive_rule_snapshot: str | None = None,
) -> Tuple[DownloadTask, TaskAction]:
    """FastAPI 异步会话中的原子 get-or-create。"""
    if db.bind.dialect.name == "postgresql":
        inserted_id = (await db.execute(
            postgresql_insert(DownloadTask)
            .values(
                work_id=work_id,
                file_index=file_index,
                status="pending",
                archive_rule_snapshot=archive_rule_snapshot,
            )
            .on_conflict_do_nothing(index_elements=["work_id", "file_index"])
            .returning(DownloadTask.id)
        )).scalar_one_or_none()
        task = await db.get(DownloadTask, inserted_id) if inserted_id is not None else await _async_existing(db, work_id, file_index)
        if task is None:
            raise RuntimeError("下载任务原子创建后无法读取")
        if not task.archive_rule_snapshot and archive_rule_snapshot:
            task.archive_rule_snapshot = archive_rule_snapshot
        return task, "created" if inserted_id is not None else _reuse_if_terminal(task)

    existing = await _async_existing(db, work_id, file_index)
    if existing is not None:
        if not existing.archive_rule_snapshot and archive_rule_snapshot:
            existing.archive_rule_snapshot = archive_rule_snapshot
        return existing, _reuse_if_terminal(existing)
    try:
        async with db.begin_nested():
            task = DownloadTask(
                work_id=work_id,
                file_index=file_index,
                status="pending",
                archive_rule_snapshot=archive_rule_snapshot,
            )
            db.add(task)
            await db.flush()
        return task, "created"
    except IntegrityError:
        existing = await _async_existing(db, work_id, file_index)
        if existing is None:
            raise
        if not existing.archive_rule_snapshot and archive_rule_snapshot:
            existing.archive_rule_snapshot = archive_rule_snapshot
        return existing, _reuse_if_terminal(existing)
