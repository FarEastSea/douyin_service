"""Subscription history, session record repair and bounded metadata retention."""

import json
from datetime import UTC, timedelta

from sqlalchemy import case, delete, func, literal, select, union_all, update
from sqlalchemy.orm import defer

from app.core.config import settings
from app.core.runtime_config import get_runtime_config_sync
from app.core.time_basis import local_offset, local_to_utc
from app.models.models import (
    PlatformAuthor,
    PlatformDownloadTask,
    PlatformSubscriptionRun,
    PlatformSubscriptionState,
    PlatformTaskWork,
    PlatformWork,
    SubscriptionCheckReport,
    SystemConfig,
)
from app.services.instagram_runtime import utcnow
from app.services.subscription_report_format import douyin_report_payload

TERMINAL_TASK_STATUSES = ("completed", "failed", "cancelled")
BATCH_SIZE = 100
CYCLE_STATE_KEY = "subscription_check_cycle_state"


def sync_session_runs(db, *, task_id=None, token=None):
    status = case(
        (PlatformDownloadTask.status == "downloading", "running"),
        else_=PlatformDownloadTask.status,
    )
    matching = (
        select(PlatformSubscriptionRun.id)
        .where(
            PlatformSubscriptionRun.task_id == PlatformDownloadTask.id,
            PlatformSubscriptionRun.status == status,
            # 运行记录是 UTC、任务是服务器本地时间，只比较“是否已结束”，具体时间按 task_id 同步时换算。
            PlatformSubscriptionRun.finished_at.is_(None)
            == PlatformDownloadTask.completed_at.is_(None),
        )
        .exists()
    )
    query = select(PlatformDownloadTask).where(
        PlatformDownloadTask.platform == "instagram",
        PlatformDownloadTask.source_type == "session",
    )
    if task_id is not None:
        query = query.where(PlatformDownloadTask.id == task_id)
    else:
        query = query.where(~matching)
    tasks = list(
        db.scalars(
            query.order_by(PlatformDownloadTask.id)
            .limit(BATCH_SIZE)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    )
    for task in tasks:
        if token is not None and task.celery_task_id != token:
            continue
        run = db.scalar(
            select(PlatformSubscriptionRun)
            .where(PlatformSubscriptionRun.task_id == task.id)
            .order_by(PlatformSubscriptionRun.id.desc())
            .limit(1)
        )
        if run is None:
            run = PlatformSubscriptionRun(
                platform="instagram",
                task_id=task.id,
                content_kind="session",
                scan_mode="session",
                started_at=local_to_utc(task.started_at or task.created_at),
            )
            db.add(run)
        run.status = "running" if task.status == "downloading" else task.status
        run.message = task.error_message or (
            "会话检测通过"
            if task.status == "completed"
            else "会话检测已取消"
            if task.status == "cancelled"
            else "正在检测会话"
            if task.status == "downloading"
            else "等待会话检测"
        )
        run.finished_at = local_to_utc(task.completed_at)
    db.flush()


def cleanup_records(db):
    sync_session_runs(db)
    cutoff = utcnow() - timedelta(
        days=settings.snapshot().SUBSCRIPTION_RECORD_RETENTION_DAYS
    )
    # 抖音报告与任务表是服务器本地时间，需用本地截止时间比较。
    local_cutoff = cutoff + local_offset()
    for model in (PlatformSubscriptionRun, SubscriptionCheckReport):
        inactive = model.status != "running"
        if model is PlatformSubscriptionRun:
            # Aborted workers can leave "running" behind. A missing or terminal
            # parent proves that an expired run no longer has an active owner.
            inactive = (
                inactive
                | model.task_id.is_(None)
                | select(PlatformDownloadTask.id)
                .where(
                    PlatformDownloadTask.id == model.task_id,
                    PlatformDownloadTask.status.in_(TERMINAL_TASK_STATUSES),
                )
                .exists()
            )
        ids = list(
            db.scalars(
                select(model.id)
                .where(
                    inactive,
                    func.coalesce(model.finished_at, model.started_at)
                    < (cutoff if model is PlatformSubscriptionRun else local_cutoff),
                )
                .order_by(model.id)
                .limit(BATCH_SIZE)
                .with_for_update(skip_locked=True)
            )
        )
        if ids:
            db.execute(delete(model).where(model.id.in_(ids)))
    # The task row lock serializes collection with retry/cancel/worker claims.
    # A locked candidate cannot turn into an active execution before deletion.
    ids = list(
        db.scalars(
            select(PlatformDownloadTask.id)
            .where(
                PlatformDownloadTask.platform == "instagram",
                PlatformDownloadTask.source_type != "work",
                PlatformDownloadTask.status.in_(TERMINAL_TASK_STATUSES),
                func.coalesce(
                    PlatformDownloadTask.completed_at, PlatformDownloadTask.created_at
                )
                < local_cutoff,
                ~PlatformDownloadTask.media_assets.any(),
                ~select(PlatformWork.id)
                .where(PlatformWork.download_task_id == PlatformDownloadTask.id)
                .exists(),
            )
            .order_by(PlatformDownloadTask.id)
            .limit(BATCH_SIZE)
            .with_for_update(skip_locked=True)
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


def iso_utc(value):
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def instagram_payload(db, row, *, include_details=True, author=None):
    if author is None and row.author_id:
        author = db.get(PlatformAuthor, row.author_id)
    session = row.content_kind == "session"
    checked = int(row.author_id is not None)
    failed = int(row.status in ("failed", "partial", "interrupted")) * checked
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
        author_id=row.author_id,
        author_name=(author.nickname or author.username) if author else None,
        started_at=iso_utc(row.started_at),
        finished_at=iso_utc(row.finished_at),
        details=[
            dict(
                author_id=row.author_id,
                nickname=(author.nickname or author.username)
                if author
                else "会话检测"
                if session
                else "作者未确认",
                status=row.status,
                new_works=row.discovered or 0,
                message=row.message,
            )
        ]
        if include_details
        else [],
    )


def unified_run_detail(db, run_key):
    try:
        platform, identity = run_key.split(":")
        identity = int(identity)
        if identity < 1:
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError("运行记录不存在") from None
    if platform == "instagram":
        row = db.get(PlatformSubscriptionRun, identity)
        if row and row.platform == platform:
            return instagram_payload(db, row)
    elif platform == "douyin":
        row = db.get(SubscriptionCheckReport, identity)
        if row:
            newest = db.scalar(
                select(SubscriptionCheckReport.id)
                .order_by(
                    SubscriptionCheckReport.started_at.desc(),
                    SubscriptionCheckReport.id.desc(),
                )
                .limit(1)
            )
            return douyin_report_payload(
                row, newest, get_runtime_config_sync(db)["subscription_check_interval"]
            )
    raise ValueError("运行记录不存在")


def unified_runs(db, page, page_size):
    interval = get_runtime_config_sync(db)["subscription_check_interval"]
    sources = union_all(
        select(
            literal("douyin").label("platform"),
            SubscriptionCheckReport.id.label("id"),
            SubscriptionCheckReport.started_at.label("at"),
        ),
        select(
            literal("instagram"),
            PlatformSubscriptionRun.id,
            # Instagram 记录是 UTC，换算成服务器本地时间后才能与抖音记录统一排序。
            PlatformSubscriptionRun.started_at + literal(local_offset()),
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
    identities = list(
        db.execute(
            select(sources)
            .order_by(sources.c.at.desc(), sources.c.platform, sources.c.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    douyin_ids = [
        identity for platform, identity, _ in identities if platform == "douyin"
    ]
    instagram_ids = [
        identity for platform, identity, _ in identities if platform == "instagram"
    ]
    douyin_rows = (
        {
            row.id: row
            for row in db.scalars(
                select(SubscriptionCheckReport)
                .options(defer(SubscriptionCheckReport.details_json))
                .where(SubscriptionCheckReport.id.in_(douyin_ids))
            )
        }
        if douyin_ids
        else {}
    )
    instagram_rows = (
        {
            row.id: row
            for row in db.scalars(
                select(PlatformSubscriptionRun).where(
                    PlatformSubscriptionRun.id.in_(instagram_ids)
                )
            )
        }
        if instagram_ids
        else {}
    )
    author_ids = {row.author_id for row in instagram_rows.values() if row.author_id}
    instagram_authors = (
        {
            author.id: author
            for author in db.scalars(
                select(PlatformAuthor).where(PlatformAuthor.id.in_(author_ids))
            )
        }
        if author_ids
        else {}
    )
    items = []
    for platform, identity, _ in identities:
        if platform == "instagram" and identity in instagram_rows:
            row = instagram_rows[identity]
            items.append(
                instagram_payload(
                    db,
                    row,
                    include_details=False,
                    author=instagram_authors.get(row.author_id),
                )
            )
        elif platform == "douyin" and identity in douyin_rows:
            items.append(
                douyin_report_payload(douyin_rows[identity], newest, interval, False)
            )
    cycle_row = db.scalar(
        select(SystemConfig).where(SystemConfig.key == CYCLE_STATE_KEY)
    )
    try:
        cycle = json.loads(cycle_row.value) if cycle_row else {}
    except (ValueError, TypeError):
        cycle = {}
    if not isinstance(cycle, dict):
        cycle = {}
    if not cycle and newest is not None:
        latest = douyin_rows.get(newest)
        if latest is None:
            latest = db.scalar(
                select(SubscriptionCheckReport)
                .options(defer(SubscriptionCheckReport.details_json))
                .where(SubscriptionCheckReport.id == newest)
            )
        if latest:
            payload = douyin_report_payload(latest, newest, interval, False)
            cycle = dict(
                active=payload["status"] == "running",
                **{
                    key: payload[key]
                    for key in (
                        "total_authors",
                        "checked_authors",
                        "remaining_authors",
                        "new_works",
                    )
                },
            )
    return dict(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        pages=max(1, (total + page_size - 1) // page_size),
        cycle=cycle,
    )
