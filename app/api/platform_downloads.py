"""平台注册表驱动的主页媒体下载 API。"""

import asyncio
import os
import signal
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import redis_client
from app.core.config import settings
from app.models.database import get_async_db
from app.models.models import (
    PlatformAuthor,
    PlatformDownloadTask,
    PlatformMediaAsset,
    PlatformWork,
)
from app.models.schemas import (
    MessageResponse,
    PaginatedPlatformAuthorsResponse,
    PaginatedPlatformTasksResponse,
    PaginatedPlatformWorksResponse,
    PlatformAuthorResponse,
    PlatformAuthorSubscriptionUpdate,
    PlatformCookieUpdate,
    PlatformDownloadRequest,
    PlatformDownloadTaskResponse,
    PlatformMediaAssetResponse,
    PlatformWorkResponse,
    XhsBrowserStatusResponse,
    XhsLoginQrResponse,
)
from app.services.media_paths import resolve_media_path
from app.services.platform_credentials import (
    get_platform_credential_status,
    save_platform_cookie,
)
from app.services.platform_profile_download import (
    get_profile_platform_spec,
    resolve_platform_input,
)
from app.services.platform_task_service import (
    ACTIVE_PLATFORM_TASK_STATUSES,
    create_platform_task,
    serialize_platform_task,
)
from app.services.xhs_profile import (
    XhsProfileError,
    ensure_managed_browser,
    get_browser_login_status,
    get_login_qrcode,
)
from app.tasks.platform_download_tasks import download_platform_profile
from app.services.unified_task_operations import TaskOperationError, operate_task

router = APIRouter(prefix="/platform-downloads", tags=["多平台下载"])


def _require_platform(platform: str):
    try:
        return get_profile_platform_spec(platform)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def _dispatch_download(task: PlatformDownloadTask, db: AsyncSession) -> None:
    """投递失败时落库为明确失败，避免留下无人认领的 pending 任务。"""
    try:
        queued = await asyncio.to_thread(download_platform_profile.delay, task.id)
    except Exception as exc:
        task.status = "failed"
        task.phase = "failed"
        task.error_code = "queue_unavailable"
        task.error_message = "任务队列暂不可用，下载任务未投递"
        task.completed_at = datetime.now()
        await db.commit()
        raise HTTPException(status_code=503, detail=task.error_message) from exc
    task.celery_task_id = queued.id
    await db.commit()


def _require_xhs(platform: str) -> None:
    if _require_platform(platform).id != "xhs":
        raise HTTPException(status_code=404, detail="该能力当前仅适用于小红书")
    raise HTTPException(
        status_code=410,
        detail="小红书作者主页批量采集已搁置；当前仅保留单条笔记下载",
    )


async def _create_xhs_author_scan(author: PlatformAuthor, db: AsyncSession) -> PlatformDownloadTask:
    active = (await db.execute(
        select(PlatformDownloadTask).where(
            PlatformDownloadTask.platform == "xhs",
            PlatformDownloadTask.source_key == f"user-{author.external_user_id}",
            PlatformDownloadTask.source_type == "profile",
            PlatformDownloadTask.status.in_(ACTIVE_PLATFORM_TASK_STATUSES),
        ).order_by(PlatformDownloadTask.created_at.desc())
    )).scalars().first()
    if active:
        return active
    task = create_platform_task(
        "xhs", f"user-{author.external_user_id}", author.profile_url, "profile",
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    await _dispatch_download(task, db)
    return task


@router.post("/{platform}/download", response_model=PlatformDownloadTaskResponse)
async def create_download(
    platform: str,
    request: PlatformDownloadRequest,
    db: AsyncSession = Depends(get_async_db),
):
    spec = _require_platform(platform)
    try:
        resolved = resolve_platform_input(spec.id, request.source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    active = (await db.execute(
        select(PlatformDownloadTask)
        .options(selectinload(PlatformDownloadTask.media_assets))
        .where(
            PlatformDownloadTask.platform == spec.id,
            PlatformDownloadTask.source_key == resolved.source_key,
            PlatformDownloadTask.source_type == resolved.source_type,
            PlatformDownloadTask.status.in_(ACTIVE_PLATFORM_TASK_STATUSES),
        )
        .order_by(PlatformDownloadTask.created_at.desc())
    )).scalars().first()
    if active:
        state = await asyncio.to_thread(
            redis_client.get_platform_task_state, spec.id, active.id
        )
        return serialize_platform_task(active, state)

    task = create_platform_task(
        spec.id, resolved.source_key, resolved.source_url, resolved.source_type
    )
    db.add(task)
    await db.commit()
    await db.refresh(task, attribute_names=["media_assets"])
    await _dispatch_download(task, db)
    return serialize_platform_task(task)


@router.get("/{platform}/tasks", response_model=PaginatedPlatformTasksResponse)
async def list_tasks(
    platform: str,
    status: str | None = Query(None),
    q: str | None = Query(None, max_length=255),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_async_db),
):
    spec = _require_platform(platform)
    conditions = [PlatformDownloadTask.platform == spec.id]
    if status:
        conditions.append(PlatformDownloadTask.status == status)
    search_text = str(q or "").strip()
    if search_text:
        conditions.append(PlatformDownloadTask.source_key.contains(search_text, autoescape=True))
    query = (
        select(PlatformDownloadTask, func.count(PlatformDownloadTask.id).over().label("_total"))
        .options(selectinload(PlatformDownloadTask.media_assets))
        .where(*conditions)
        .order_by(PlatformDownloadTask.created_at.desc(), PlatformDownloadTask.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(query)).all()
    tasks = [row[0] for row in rows]
    if rows:
        total = int(rows[0][1] or 0)
    else:
        total = int((await db.execute(
            select(func.count(PlatformDownloadTask.id)).where(*conditions)
        )).scalar() or 0)
    states = await asyncio.to_thread(
        redis_client.get_platform_task_states, spec.id, [task.id for task in tasks]
    )
    return PaginatedPlatformTasksResponse(
        items=[serialize_platform_task(task, states.get(task.id)) for task in tasks],
        total=total,
        page=page,
        page_size=page_size,
        pages=max(1, (total + page_size - 1) // page_size),
    )


@router.get("/{platform}/tasks/{task_id}/media", response_model=list[PlatformMediaAssetResponse])
async def list_media(platform: str, task_id: int, db: AsyncSession = Depends(get_async_db)):
    spec = _require_platform(platform)
    task = (await db.execute(select(PlatformDownloadTask).where(
        PlatformDownloadTask.id == task_id,
        PlatformDownloadTask.platform == spec.id,
    ))).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    assets = (await db.execute(
        select(PlatformMediaAsset)
        .where(PlatformMediaAsset.task_id == task.id)
        .order_by(PlatformMediaAsset.created_at, PlatformMediaAsset.id)
    )).scalars().all()
    return [PlatformMediaAssetResponse(
        id=item.id,
        task_id=item.task_id,
        media_type=item.media_type,
        filename=item.filename,
        size_bytes=item.size_bytes or 0,
        mime_type=item.mime_type,
        title=item.title,
        author_name=item.author_name,
        published_at=item.published_at,
        cover_url=item.cover_url,
        duration_ms=item.duration_ms,
        width=item.width,
        height=item.height,
        view_count=item.view_count,
        like_count=item.like_count,
        comment_count=item.comment_count,
        share_count=item.share_count,
        preview_url=f"/api/platform-downloads/{spec.id}/media/{item.id}/preview",
        download_url=f"/api/platform-downloads/{spec.id}/media/{item.id}/download",
        created_at=item.created_at,
    ) for item in assets]


async def _load_media(platform: str, asset_id: int, db: AsyncSession):
    spec = _require_platform(platform)
    asset = (await db.execute(select(PlatformMediaAsset).where(
        PlatformMediaAsset.id == asset_id,
        PlatformMediaAsset.platform == spec.id,
    ))).scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="资源不存在")
    try:
        path = await asyncio.to_thread(resolve_media_path, asset.file_path, spec.download_root())
    except (ValueError, OSError):
        raise HTTPException(status_code=403, detail="资源路径不在当前平台下载目录内")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="资源文件不存在")
    return asset, path


@router.get("/{platform}/media/{asset_id}/preview")
async def preview_media(platform: str, asset_id: int, db: AsyncSession = Depends(get_async_db)):
    asset, path = await _load_media(platform, asset_id, db)
    return FileResponse(path, media_type=asset.mime_type)


@router.get("/{platform}/media/{asset_id}/download")
async def download_media(platform: str, asset_id: int, db: AsyncSession = Depends(get_async_db)):
    asset, path = await _load_media(platform, asset_id, db)
    return FileResponse(path, media_type=asset.mime_type, filename=asset.filename)


@router.get("/{platform}/tasks/{task_id}/log")
async def get_task_log(platform: str, task_id: int, start: int = Query(0, ge=0)):
    spec = _require_platform(platform)
    lines, total = await asyncio.to_thread(
        lambda: (
            redis_client.get_platform_task_log(spec.id, task_id, start),
            redis_client.get_platform_task_log_size(spec.id, task_id),
        )
    )
    return {"task_id": task_id, "start": start, "lines": lines, "total": total}


def _kill_task(platform: str, task_id: int) -> None:
    pid = redis_client.get_platform_task_pid(platform, task_id)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        redis_client.delete_platform_task_pid(platform, task_id)


def _clear_runtime(platform: str, task_id: int, include_log: bool = False) -> None:
    redis_client.delete_platform_task_state(platform, task_id)
    redis_client.delete_platform_task_pid(platform, task_id)
    if include_log:
        redis_client.delete_platform_task_log(platform, task_id)


@router.post("/{platform}/tasks/{task_id}/cancel", response_model=MessageResponse)
async def cancel_task(platform: str, task_id: int, db: AsyncSession = Depends(get_async_db)):
    try:
        await operate_task(db, f"{platform}:{task_id}", "cancel")
    except TaskOperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return MessageResponse(success=True, message="任务已取消")


@router.post("/{platform}/tasks/{task_id}/retry", response_model=MessageResponse)
async def retry_task(platform: str, task_id: int, db: AsyncSession = Depends(get_async_db)):
    try:
        await operate_task(db, f"{platform}:{task_id}", "retry")
    except TaskOperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return MessageResponse(success=True, message="任务已重新提交")


@router.delete("/{platform}/tasks/{task_id}", response_model=MessageResponse)
async def delete_task(platform: str, task_id: int, db: AsyncSession = Depends(get_async_db)):
    spec = _require_platform(platform)
    task = (await db.execute(select(PlatformDownloadTask).where(
        PlatformDownloadTask.id == task_id,
        PlatformDownloadTask.platform == spec.id,
    ))).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status in ACTIVE_PLATFORM_TASK_STATUSES:
        await asyncio.to_thread(_kill_task, spec.id, task.id)
    await asyncio.to_thread(_clear_runtime, spec.id, task.id, True)
    await db.delete(task)
    await db.commit()
    return MessageResponse(success=True, message="任务已删除")


@router.get("/{platform}/config/cookie")
async def cookie_status(platform: str, db: AsyncSession = Depends(get_async_db)):
    spec = _require_platform(platform)
    status = await get_platform_credential_status(db, spec.id, spec.cookie_env_key)
    current = await asyncio.to_thread(settings.snapshot)
    cookie_file = getattr(current, spec.cookie_file_env_key, None)
    status["configured"] = bool(status["configured"] or (cookie_file and Path(cookie_file).is_file()))
    return status


@router.post("/{platform}/config/cookie", response_model=MessageResponse)
async def save_cookie(
    platform: str,
    request: PlatformCookieUpdate,
    db: AsyncSession = Depends(get_async_db),
):
    spec = _require_platform(platform)
    cookie = request.cookie.strip()
    try:
        status = await save_platform_cookie(db, spec.id, cookie)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MessageResponse(
        success=True,
        message=f"{spec.name} Cookie 已加密保存",
        data=status,
    )


@router.get("/{platform}/authors", response_model=PaginatedPlatformAuthorsResponse)
async def list_platform_authors(
    platform: str,
    q: str | None = Query(None, max_length=255),
    subscribed: bool | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_async_db),
):
    _require_xhs(platform)
    conditions = [PlatformAuthor.platform == "xhs"]
    if subscribed is not None:
        conditions.append(PlatformAuthor.is_subscribed == subscribed)
    search = str(q or "").strip()
    if search:
        conditions.append(
            PlatformAuthor.nickname.contains(search, autoescape=True)
            | PlatformAuthor.external_user_id.contains(search, autoescape=True)
            | PlatformAuthor.red_id.contains(search, autoescape=True)
        )
    total = int((await db.execute(
        select(func.count(PlatformAuthor.id)).where(*conditions)
    )).scalar() or 0)
    items = (await db.execute(
        select(PlatformAuthor).where(*conditions)
        .order_by(PlatformAuthor.updated_at.desc(), PlatformAuthor.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return PaginatedPlatformAuthorsResponse(
        items=[PlatformAuthorResponse.model_validate(item) for item in items],
        total=total, page=page, page_size=page_size,
        pages=max(1, (total + page_size - 1) // page_size),
    )


@router.post("/{platform}/authors/{author_id}/subscription", response_model=PlatformAuthorResponse)
async def update_platform_author_subscription(
    platform: str,
    author_id: int,
    request: PlatformAuthorSubscriptionUpdate,
    db: AsyncSession = Depends(get_async_db),
):
    _require_xhs(platform)
    author = (await db.execute(select(PlatformAuthor).where(
        PlatformAuthor.id == author_id, PlatformAuthor.platform == "xhs",
    ))).scalar_one_or_none()
    if not author:
        raise HTTPException(status_code=404, detail="作者不存在")
    author.is_subscribed = request.is_subscribed
    if request.check_interval is not None:
        author.check_interval = request.check_interval
    await db.commit()
    await db.refresh(author)
    return PlatformAuthorResponse.model_validate(author)


@router.post("/{platform}/authors/{author_id}/scan", response_model=PlatformDownloadTaskResponse)
async def scan_platform_author(
    platform: str,
    author_id: int,
    db: AsyncSession = Depends(get_async_db),
):
    _require_xhs(platform)
    author = (await db.execute(select(PlatformAuthor).where(
        PlatformAuthor.id == author_id, PlatformAuthor.platform == "xhs",
    ))).scalar_one_or_none()
    if not author:
        raise HTTPException(status_code=404, detail="作者不存在")
    task = await _create_xhs_author_scan(author, db)
    return serialize_platform_task(task)


@router.get("/{platform}/authors/{author_id}/works", response_model=PaginatedPlatformWorksResponse)
async def list_platform_author_works(
    platform: str,
    author_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    db: AsyncSession = Depends(get_async_db),
):
    _require_xhs(platform)
    exists = (await db.execute(select(PlatformAuthor.id).where(
        PlatformAuthor.id == author_id, PlatformAuthor.platform == "xhs",
    ))).scalar_one_or_none()
    if not exists:
        raise HTTPException(status_code=404, detail="作者不存在")
    conditions = [PlatformWork.platform == "xhs", PlatformWork.author_id == author_id]
    total = int((await db.execute(
        select(func.count(PlatformWork.id)).where(*conditions)
    )).scalar() or 0)
    items = (await db.execute(
        select(PlatformWork).where(*conditions)
        .order_by(PlatformWork.published_at.desc().nullslast(), PlatformWork.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return PaginatedPlatformWorksResponse(
        items=[PlatformWorkResponse.model_validate(item) for item in items],
        total=total, page=page, page_size=page_size,
        pages=max(1, (total + page_size - 1) // page_size),
    )


@router.post("/{platform}/browser/start", response_model=XhsBrowserStatusResponse)
async def start_xhs_browser(platform: str):
    _require_xhs(platform)
    try:
        await asyncio.to_thread(ensure_managed_browser)
        return await asyncio.to_thread(get_browser_login_status)
    except XhsProfileError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/{platform}/browser/status", response_model=XhsBrowserStatusResponse)
async def xhs_browser_status(platform: str):
    _require_xhs(platform)
    try:
        return await asyncio.to_thread(get_browser_login_status)
    except XhsProfileError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/{platform}/browser/qrcode", response_model=XhsLoginQrResponse)
async def xhs_browser_qrcode(platform: str):
    _require_xhs(platform)
    try:
        return await asyncio.to_thread(get_login_qrcode)
    except XhsProfileError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
