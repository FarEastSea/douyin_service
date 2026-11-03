"""Shared presentation of subscription runs and bounded metadata retention."""

from datetime import timedelta
from sqlalchemy import select, func, literal, union_all, delete, update
from app.core.config import settings
from app.models.models import (
    PlatformDownloadTask,
    PlatformSubscriptionRun,
    PlatformSubscriptionState,
    PlatformAuthor,
    PlatformWork,
    PlatformTaskWork,
    SubscriptionCheckReport,
)
from app.services.instagram_runtime import utcnow


def sync_session_runs(db):
    tasks = db.scalars(
        select(PlatformDownloadTask)
        .where(
            PlatformDownloadTask.platform == "instagram",
            PlatformDownloadTask.source_type == "session",
        )
        .with_for_update()
    )
    for task in tasks:
        run = db.scalar(
            select(PlatformSubscriptionRun).where(
                PlatformSubscriptionRun.task_id == task.id
            )
        )
        if run is None:
            run = PlatformSubscriptionRun(
                platform="instagram",
                task_id=task.id,
                content_kind="session",
                scan_mode="session",
                started_at=task.created_at,
            )
            db.add(run)
        run.status = "running" if task.status == "downloading" else task.status
        run.message = task.error_message or (
            "会话检测通过" if task.status == "completed" else "等待会话检测"
        )
        run.finished_at = task.completed_at
    db.flush()


def cleanup_records(db):
    sync_session_runs(db)
    cutoff = utcnow() - timedelta(
        days=settings.snapshot().SUBSCRIPTION_RECORD_RETENTION_DAYS
    )
    for model in (PlatformSubscriptionRun, SubscriptionCheckReport):
        ids = list(
            db.scalars(
                select(model.id)
                .where(
                    model.status != "running",
                    func.coalesce(model.finished_at, model.started_at) < cutoff,
                )
                .order_by(model.id)
                .limit(100)
            )
        )
        if ids:
            db.execute(delete(model).where(model.id.in_(ids)))
    ids = list(
        db.scalars(
            select(PlatformDownloadTask.id)
            .where(
                PlatformDownloadTask.platform == "instagram",
                PlatformDownloadTask.source_type != "work",
                PlatformDownloadTask.status.in_(("completed", "failed", "cancelled")),
                func.coalesce(
                    PlatformDownloadTask.completed_at, PlatformDownloadTask.created_at
                )
                < cutoff,
                ~PlatformDownloadTask.media_assets.any(),
                ~select(PlatformWork.id)
                .where(PlatformWork.download_task_id == PlatformDownloadTask.id)
                .exists(),
            )
            .order_by(PlatformDownloadTask.id)
            .limit(100)
        )
    )
    if ids:
        db.execute(
            update(PlatformSubscriptionState)
            .where(PlatformSubscriptionState.task_id.in_(ids))
            .values(task_id=None)
        )
        db.execute(
            update(PlatformSubscriptionRun)
            .where(PlatformSubscriptionRun.task_id.in_(ids))
            .values(task_id=None)
        )
        db.execute(delete(PlatformTaskWork).where(PlatformTaskWork.task_id.in_(ids)))
        db.execute(delete(PlatformDownloadTask).where(PlatformDownloadTask.id.in_(ids)))
    db.commit()


def instagram_payload(db, row):
    author = db.get(PlatformAuthor, row.author_id) if row.author_id else None
    session = row.content_kind == "session"
    checked = int(author is not None)
    failed = int(row.status in ("failed", "partial", "interrupted"))
    return dict(
        id=row.id,
        key=f"instagram:{row.id}",
        platform="instagram",
        trigger_type="session" if session else row.scan_mode,
        status=row.status,
        total_authors=checked,
        due_authors=checked,
        checked_authors=checked,
        success_authors=checked if row.status == "completed" else 0,
        failed_authors=failed,
        warning_authors=0,
        skipped_authors=0,
        remaining_authors=checked
        if row.status in ("deferred", "running", "pending")
        else 0,
        new_works=row.discovered or 0,
        downloaded_files=row.saved or 0,
        summary=row.message,
        content_kind=row.content_kind,
        started_at=(row.started_at.isoformat() + "Z") if row.started_at else None,
        finished_at=(row.finished_at.isoformat() + "Z") if row.finished_at else None,
        details=[
            dict(
                author_id=row.author_id,
                nickname=(author.nickname or author.username) if author else "会话检测",
                status=row.status,
                new_works=row.discovered or 0,
                message=row.message,
            )
        ],
    )


def unified_runs(db, page, page_size):
    from app.api.authors import _subscription_report_payload

    sources = union_all(
        select(
            literal("douyin").label("platform"),
            SubscriptionCheckReport.id.label("id"),
            SubscriptionCheckReport.started_at.label("at"),
        ),
        select(
            literal("instagram"),
            PlatformSubscriptionRun.id,
            PlatformSubscriptionRun.started_at,
        ).where(PlatformSubscriptionRun.platform == "instagram"),
    ).subquery()
    total = db.scalar(select(func.count()).select_from(sources))
    newest = db.scalar(
        select(SubscriptionCheckReport.id)
        .order_by(
            SubscriptionCheckReport.started_at.desc(), SubscriptionCheckReport.id.desc()
        )
        .limit(1)
    )
    items = []
    for platform, identity, _ in db.execute(
        select(sources)
        .order_by(sources.c.at.desc(), sources.c.platform, sources.c.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ):
        if platform == "instagram":
            items.append(
                instagram_payload(db, db.get(PlatformSubscriptionRun, identity))
            )
        else:
            item = _subscription_report_payload(
                db.get(SubscriptionCheckReport, identity),
                newest,
                settings.snapshot().DEFAULT_CHECK_INTERVAL,
                False,
            )
            item.update(key=f"douyin:{identity}", platform="douyin")
            items.append(item)
    return dict(
        items=items,
        total=total,
        page=page,
        pages=max(1, (total + page_size - 1) // page_size),
    )
