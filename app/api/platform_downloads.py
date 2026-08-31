"""平台注册表驱动的主页媒体下载 API。"""

from pathlib import Path
from datetime import datetime
import asyncio
import os
import signal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import redis_client
from app.core.config import settings
from app.models.database import get_async_db
from app.models.models import PlatformDownloadTask, PlatformMediaAsset
from app.models.schemas import (
    MessageResponse,
    PaginatedPlatformTasksResponse,
    PlatformCookieUpdate,
    PlatformDownloadRequest,
    PlatformDownloadTaskResponse,
    PlatformMediaAssetResponse,
)
from app.services.media_paths import resolve_media_path
from app.services.platform_profile_download import (
    get_profile_platform_spec,
    resolve_profile_input,
)
from app.services.platform_task_service import (
    ACTIVE_PLATFORM_TASK_STATUSES,
    create_platform_task,
    prepare_platform_task_for_retry,
    serialize_platform_task,
)
from app.services.platform_credentials import (
    get_platform_credential_status,
    save_platform_cookie,
)
from app.tasks.platform_download_tasks import download_platform_profile


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


@router.post("/{platform}/download", response_model=PlatformDownloadTaskResponse)
async def create_download(
    platform: str,
    request: PlatformDownloadRequest,
    db: AsyncSession = Depends(get_async_db),
):
    spec = _require_platform(platform)
    try:
        source_key, source_url = resolve_profile_input(spec.id, request.source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    active = (await db.execute(
        select(PlatformDownloadTask)
        .options(selectinload(PlatformDownloadTask.media_assets))
        .where(
            PlatformDownloadTask.platform == spec.id,
            PlatformDownloadTask.source_key == source_key,
            PlatformDownloadTask.status.in_(ACTIVE_PLATFORM_TASK_STATUSES),
        )
        .order_by(PlatformDownloadTask.created_at.desc())
    )).scalars().first()
    if active:
        state = await asyncio.to_thread(
            redis_client.get_platform_task_state, spec.id, active.id
        )
        return serialize_platform_task(active, state)

    task = create_platform_task(spec.id, source_key, source_url)
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
    spec = _require_platform(platform)
    task = (await db.execute(select(PlatformDownloadTask).where(
        PlatformDownloadTask.id == task_id,
        PlatformDownloadTask.platform == spec.id,
    ))).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status not in ACTIVE_PLATFORM_TASK_STATUSES:
        raise HTTPException(status_code=400, detail=f"任务状态为 {task.status}，无法取消")
    await asyncio.to_thread(_kill_task, spec.id, task.id)
    task.status = "cancelled"
    task.phase = "cancelled"
    task.completed_at = datetime.now()
    await db.commit()
    await asyncio.to_thread(_clear_runtime, spec.id, task.id)
    return MessageResponse(success=True, message="任务已取消")


@router.post("/{platform}/tasks/{task_id}/retry", response_model=MessageResponse)
async def retry_task(platform: str, task_id: int, db: AsyncSession = Depends(get_async_db)):
    spec = _require_platform(platform)
    task = (await db.execute(select(PlatformDownloadTask).where(
        PlatformDownloadTask.id == task_id,
        PlatformDownloadTask.platform == spec.id,
    ))).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status not in ("failed", "cancelled"):
        raise HTTPException(status_code=400, detail=f"任务状态为 {task.status}，无需重试")
    prepare_platform_task_for_retry(task)
    await db.commit()
    await asyncio.to_thread(_clear_runtime, spec.id, task.id)
    await _dispatch_download(task, db)
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
