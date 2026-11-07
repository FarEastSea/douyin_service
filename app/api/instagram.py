"""Instagram 账号与订阅 API；解析、登录检测和下载均异步入队。"""

import json
from hashlib import sha256
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.database import get_async_db, get_sync_db
from app.models.models import (
    InstagramCollection,
    InstagramCollectionItem,
    PlatformAuthor,
    PlatformDownloadTask,
    PlatformSubscriptionRun,
    PlatformSubscriptionState,
    PlatformWork,
)
from app.services.instagram_input import resolve_instagram_input
from app.services.instagram_jobs import options_for
from app.services.instagram_runtime import (
    connection,
    normalize_cookie,
    readiness,
    set_proxy,
    utcnow,
)
from app.services.instagram_store import (
    author_payload,
    configure_subscription,
    work_complete,
)
from app.services.platform_task_service import (
    create_platform_task,
    prepare_platform_task_for_retry,
    serialize_platform_task,
)

router = APIRouter(prefix="/platform-downloads/instagram", tags=["Instagram"])
Kind = Literal["posts", "reels", "stories", "highlights"]


def session():
    db = get_sync_db()
    try:
        yield db
    finally:
        db.close()


def require_enabled():
    if not settings.snapshot().INSTAGRAM_ENABLED:
        raise HTTPException(
            409, "请先在 Instagram 账号设置中导入并检测会话，再启用采集"
        )


def notify_dispatch():
    from app.tasks.platform_download_tasks import dispatch_instagram

    try:
        dispatch_instagram.delay()
    except Exception:
        # 意图已持久保存，Beat 下一周期会继续投递。
        pass


def lock_source(db, source_key):
    if db.get_bind().dialect.name == "postgresql":
        key = int.from_bytes(
            sha256(("instagram:" + source_key).encode()).digest()[:8],
            "big",
            signed=True,
        )
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def queue_job(db, resolved, options=None):
    lock_source(db, resolved.source_key)
    active = db.scalar(
        select(PlatformDownloadTask)
        .where(
            PlatformDownloadTask.platform == "instagram",
            PlatformDownloadTask.source_key == resolved.source_key,
            PlatformDownloadTask.status.in_(("pending", "downloading")),
        )
        .order_by(PlatformDownloadTask.id.desc())
    )
    if active:
        if (
            options
            and options.get("subscribe")
            and not options_for(active).get("subscribe")
        ):
            raise HTTPException(409, "该作者已有下载任务，请等待完成后添加订阅")
        return active
    task = create_platform_task(
        "instagram", resolved.source_key, resolved.source_url, resolved.source_type
    )
    task.instagram_options = json.dumps(options or {})
    db.add(task)
    db.commit()
    db.refresh(task)
    notify_dispatch()
    return task


class DownloadRequest(BaseModel):
    source: str = Field(min_length=1, max_length=4096)
    allow_profile: bool = False


class AuthorCreate(BaseModel):
    source: str = Field(min_length=1, max_length=4096)
    kinds: list[Kind] = Field(default_factory=lambda: ["posts", "reels"], min_length=1)


class SubscriptionUpdate(BaseModel):
    is_subscribed: bool
    kinds: list[Kind] | None = Field(None, min_length=1)


class BatchUpdate(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    is_subscribed: bool = False


class HistoryUpdate(BaseModel):
    enabled: bool = True


class CookieUpdate(BaseModel):
    cookie: str = Field(min_length=1, max_length=131072)


class ConnectionUpdate(BaseModel):
    proxy: str = Field(max_length=2048)


def resolved_input(source):
    try:
        return resolve_instagram_input(source)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/download")
def download(request: DownloadRequest, db=Depends(session)):
    require_enabled()
    resolved = resolved_input(request.source)
    lock_source(db, resolved.source_key)
    if resolved.source_type == "profile" and not request.allow_profile:
        raise HTTPException(
            409,
            detail={
                "code": "author_link_requires_confirmation",
                "message": "这是作者主页，将分批下载 Posts/Reels 的全部可访问历史，请明确确认批量下载",
            },
        )
    # p/reel 等入口规范化为同一个数字作品 ID，避免重建已存在任务。
    identity = (
        resolved.source_key.replace("post-", "post:", 1)
        if resolved.source_key.startswith("post-")
        else resolved.source_key.replace("story-", "story:", 1)
    )
    work = db.scalar(
        select(PlatformWork).where(
            PlatformWork.platform == "instagram",
            PlatformWork.external_work_id == identity,
        )
    )
    if work and work.download_task_id:
        existing = db.scalar(
            select(PlatformDownloadTask)
            .where(PlatformDownloadTask.id == work.download_task_id)
            .with_for_update()
        )
        if existing and existing.status == "completed" and not work_complete(db, work):
            prepare_platform_task_for_retry(existing)
            options = options_for(existing)
            options.pop("checkpoint", None)
            options.pop("not_before", None)
            existing.instagram_options = json.dumps(options)
            db.commit()
            notify_dispatch()
        if existing and existing.status in {"pending", "downloading", "completed"}:
            return serialize_platform_task(existing)
    return serialize_platform_task(queue_job(db, resolved))


@router.get("/session")
def get_session(db=Depends(session)):
    result = readiness(db)
    tests = db.scalar(
        select(PlatformDownloadTask)
        .where(
            PlatformDownloadTask.platform == "instagram",
            PlatformDownloadTask.source_type == "session",
        )
        .order_by(PlatformDownloadTask.id.desc())
    )
    result["test_task"] = serialize_platform_task(tests) if tests else None
    if tests:
        run = db.scalar(
            select(PlatformSubscriptionRun).where(
                PlatformSubscriptionRun.task_id == tests.id
            )
        )
        result["test_run_key"] = f"instagram:{run.id}" if run else None
    db.commit()
    return result


def queue_session_test(db):
    from app.services.platform_profile_download import ResolvedPlatformInput

    fingerprint = connection(db)[2]
    # 会话变更时必须生成新检测意图，不能把正在检测旧会话的任务当成新检测。
    key = "session-check-" + fingerprint[:16]
    db.execute(
        update(PlatformDownloadTask)
        .where(
            PlatformDownloadTask.platform == "instagram",
            PlatformDownloadTask.source_type == "session",
            PlatformDownloadTask.source_key != key,
            PlatformDownloadTask.status == "pending",
            PlatformDownloadTask.celery_task_id.is_(None),
        )
        .values(status="cancelled", phase="cancelled")
    )
    db.commit()
    task = queue_job(
        db,
        ResolvedPlatformInput(key, "https://www.instagram.com/", "session"),
        {"fingerprint": fingerprint},
    )
    from app.services.subscription_records import sync_session_runs

    sync_session_runs(db, task_id=task.id)
    db.commit()
    return task


@router.post("/session/test")
def test_session(db=Depends(session)):
    return serialize_platform_task(queue_session_test(db))


@router.post("/config/cookie")
async def save_cookie(request: CookieUpdate, db: AsyncSession = Depends(get_async_db)):
    import asyncio

    from app.core.configuration_service import save_platform_credential

    try:
        value = normalize_cookie(request.cookie)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    # 复用统一配置锁及异步凭据事务，避免另建明文配置载体。
    status = await save_platform_credential(db, "instagram", value)

    def enqueue():
        sync_db = get_sync_db()
        try:
            return queue_session_test(sync_db).id
        finally:
            sync_db.close()

    task_id = await asyncio.to_thread(enqueue)
    return {
        "success": True,
        "message": "会话已加密保存，检测任务已提交",
        "data": {**status, "task_id": task_id},
    }


@router.post("/connection")
def save_connection(request: ConnectionUpdate, db=Depends(session)):
    from app.core.config_transaction import configuration_lock
    from app.core.configuration_service import _require_writable_configuration

    try:
        with configuration_lock():
            _require_writable_configuration()
            set_proxy(db, request.proxy.strip())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    task = queue_session_test(db)
    return {
        "success": True,
        "message": "固定出口已加密保存，检测任务已提交",
        "task_id": task.id,
    }


@router.post("/authors")
def create_author(request: AuthorCreate, db=Depends(session)):
    require_enabled()
    resolved = resolved_input(request.source)
    if resolved.source_type != "profile":
        raise HTTPException(400, "订阅需要作者用户名或主页链接")
    task = queue_job(
        db, resolved, {"subscribe": True, "kinds": list(dict.fromkeys(request.kinds))}
    )
    return {
        "pending": True,
        "task_id": task.id,
        "message": "作者解析与订阅已提交，身份确认后自动补齐历史",
    }


@router.get("/authors")
def authors(
    q: str = Query("", max_length=255),
    subscribed: bool | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db=Depends(session),
):
    conditions = [PlatformAuthor.platform == "instagram"]
    if q.strip():
        conditions.append(
            PlatformAuthor.username.contains(q.strip(), autoescape=True)
            | PlatformAuthor.nickname.contains(q.strip(), autoescape=True)
            | PlatformAuthor.external_user_id.contains(q.strip(), autoescape=True)
        )
    if subscribed is not None:
        conditions.append(PlatformAuthor.is_subscribed == subscribed)
    total = db.scalar(select(func.count(PlatformAuthor.id)).where(*conditions))
    rows = db.scalars(
        select(PlatformAuthor)
        .where(*conditions)
        .order_by(PlatformAuthor.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [author_payload(db, row) for row in rows],
        "total": total,
        "page": page,
        "pages": max(1, (total + page_size - 1) // page_size),
    }


def author_by_id(db, author_id):
    author = db.get(PlatformAuthor, author_id)
    if not author or author.platform != "instagram":
        raise HTTPException(404, "Instagram 作者不存在")
    return author


@router.post("/authors/batch")
def update_batch(request: BatchUpdate, db=Depends(session)):
    result = db.execute(
        update(PlatformAuthor)
        .where(
            PlatformAuthor.platform == "instagram", PlatformAuthor.id.in_(request.ids)
        )
        .values(is_subscribed=request.is_subscribed)
    )
    if not request.is_subscribed:
        db.execute(
            update(PlatformSubscriptionState)
            .where(
                PlatformSubscriptionState.author_id.in_(
                    select(PlatformAuthor.id).where(
                        PlatformAuthor.platform == "instagram",
                        PlatformAuthor.id.in_(request.ids),
                    )
                )
            )
            .values(manual=False)
        )
    db.commit()
    return {"success": True, "updated": result.rowcount}


@router.get("/authors/{author_id}")
def author_detail(author_id: int, db=Depends(session)):
    return author_payload(db, author_by_id(db, author_id))


@router.post("/authors/{author_id}/subscription")
def update_subscription(
    author_id: int, request: SubscriptionUpdate, db=Depends(session)
):
    author = author_by_id(db, author_id)
    kinds = request.kinds
    if kinds is None:
        kinds = list(
            db.scalars(
                select(PlatformSubscriptionState.content_kind)
                .where(
                    PlatformSubscriptionState.author_id == author_id,
                    PlatformSubscriptionState.scan_mode == "recent",
                    PlatformSubscriptionState.enabled.is_(True),
                )
                .distinct()
            )
        ) or ["posts", "reels"]
    configure_subscription(db, author, subscribed=request.is_subscribed, kinds=kinds)
    if not request.is_subscribed:
        db.execute(
            update(PlatformSubscriptionState)
            .where(PlatformSubscriptionState.author_id == author_id)
            .values(manual=False)
        )
    db.commit()
    return author_payload(db, author)


def release_waiting_task(db, state):
    """手动触发时解除该 stream 旧任务的延期，否则调度会一直停在这个 stream。"""
    task = db.get(PlatformDownloadTask, state.task_id) if state.task_id else None
    if task and task.status == "pending" and task.celery_task_id is None:
        options = options_for(task)
        if options.pop("not_before", None) is not None:
            task.instagram_options = json.dumps(options)


@router.post("/authors/{author_id}/scan")
def scan_author(author_id: int, db=Depends(session)):
    require_enabled()
    author_by_id(db, author_id)
    rows = db.scalars(
        select(PlatformSubscriptionState).where(
            PlatformSubscriptionState.author_id == author_id,
            PlatformSubscriptionState.scan_mode == "recent",
            PlatformSubscriptionState.enabled.is_(True),
        )
    )
    for row in rows:
        row.blocked_code = None
        row.next_check_at, row.manual = utcnow(), True
        release_waiting_task(db, row)
    db.commit()
    notify_dispatch()
    return {"success": True, "message": "检查已排入调度，仍遵守全局预算与冷却"}


@router.post("/authors/{author_id}/history")
def history_author(author_id: int, request: HistoryUpdate, db=Depends(session)):
    require_enabled()
    author_by_id(db, author_id)
    selected = set(
        db.scalars(
            select(PlatformSubscriptionState.content_kind).where(
                PlatformSubscriptionState.author_id == author_id,
                PlatformSubscriptionState.scan_mode == "recent",
                PlatformSubscriptionState.enabled.is_(True),
            )
        )
    )
    for row in db.scalars(
        select(PlatformSubscriptionState).where(
            PlatformSubscriptionState.author_id == author_id,
            PlatformSubscriptionState.scan_mode == "history",
        )
    ):
        row.enabled = row.manual = request.enabled and row.content_kind in selected
        if row.enabled:
            row.blocked_code = None
            row.history_complete, row.next_check_at = False, utcnow()
            release_waiting_task(db, row)
    db.commit()
    notify_dispatch()
    return {"success": True}


@router.get("/authors/{author_id}/works")
def author_works(
    author_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    db=Depends(session),
):
    author_by_id(db, author_id)
    conditions = [
        PlatformWork.platform == "instagram",
        PlatformWork.author_id == author_id,
    ]
    total = db.scalar(select(func.count(PlatformWork.id)).where(*conditions))
    rows = db.scalars(
        select(PlatformWork)
        .where(*conditions)
        .order_by(PlatformWork.published_at.desc(), PlatformWork.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [
            {
                key: getattr(row, key)
                for key in (
                    "id",
                    "title",
                    "source_url",
                    "content_kind",
                    "work_type",
                    "published_at",
                    "expires_at",
                    "download_task_id",
                    "expected_media_count",
                )
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "pages": max(1, (total + page_size - 1) // page_size),
    }


@router.get("/authors/{author_id}/collections")
def author_collections(
    author_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db=Depends(session),
):
    author_by_id(db, author_id)
    total = db.scalar(
        select(func.count(InstagramCollection.id)).where(
            InstagramCollection.author_id == author_id
        )
    )
    rows = db.scalars(
        select(InstagramCollection)
        .where(InstagramCollection.author_id == author_id)
        .order_by(InstagramCollection.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [
            {
                "id": row.id,
                "title": row.title,
                "source_url": f"https://www.instagram.com/stories/highlights/{row.external_id}/",
                "work_ids": list(
                    db.scalars(
                        select(InstagramCollectionItem.work_id)
                        .where(InstagramCollectionItem.collection_id == row.id)
                        .order_by(
                            InstagramCollectionItem.position,
                            InstagramCollectionItem.work_id,
                        )
                    )
                ),
            }
            for row in rows
        ],
        "total": total,
        "pages": max(1, (total + page_size - 1) // page_size),
    }


@router.get("/reports")
def reports(
    author_id: int | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db=Depends(session),
):
    conditions = [PlatformSubscriptionRun.platform == "instagram"]
    if author_id is not None:
        author_by_id(db, author_id)
        conditions.append(PlatformSubscriptionRun.author_id == author_id)
    total = db.scalar(select(func.count(PlatformSubscriptionRun.id)).where(*conditions))
    rows = db.scalars(
        select(PlatformSubscriptionRun)
        .where(*conditions)
        .order_by(PlatformSubscriptionRun.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [
            {
                key: getattr(row, key)
                for key in (
                    "id",
                    "author_id",
                    "task_id",
                    "content_kind",
                    "scan_mode",
                    "status",
                    "discovered",
                    "saved",
                    "message",
                    "started_at",
                    "finished_at",
                )
            }
            for row in rows
        ],
        "total": total,
        "pages": max(1, (total + page_size - 1) // page_size),
    }


@router.get("/runs")
def unified_run_list(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db=Depends(session),
):
    from app.services.subscription_records import unified_runs

    return unified_runs(db, page, page_size)


@router.get("/runs/{run_key}")
def unified_run_detail(run_key: str, db=Depends(session)):
    from app.services.subscription_records import unified_run_detail as get_detail

    try:
        return get_detail(db, run_key)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
