"""Authenticated local media browsing; no remote source fetching or disk crawling."""

from __future__ import annotations

import asyncio
import mimetypes
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.database import get_async_db
from app.models.models import DownloadTask, OrphanMediaFile, PlatformMediaAsset, XMediaAsset
from app.models.schemas import MessageResponse
from app.services.media_library import (
    LIBRARY_STATUSES,
    PLATFORMS,
    douyin_media_type,
    media_query,
    media_root,
    serialize_media,
)
from app.services.media_paths import resolve_media_path
from app.services.media_thumbnails import ThumbnailUnavailable, ensure_media_thumbnail


router = APIRouter(prefix="/media", tags=["媒体库"])


@router.get("")
async def list_media_library(
    platform: str = Query("", max_length=32),
    media_type: str = Query("", max_length=16),
    q: str = Query("", max_length=255),
    author: str = Query("", max_length=255),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    sort_by: Literal["created_desc", "created_asc"] = "created_desc",
    library_status: str = Query("", max_length=16, description="indexed 已索引 / orphaned 无索引媒体"),
    db: AsyncSession = Depends(get_async_db),
):
    if library_status and library_status not in LIBRARY_STATUSES:
        raise HTTPException(400, "媒体分类不正确")
    if platform and platform not in PLATFORMS:
        raise HTTPException(404, "平台不存在")
    if media_type and media_type not in {"image", "video", "live", "live_photo"}:
        raise HTTPException(400, "媒体类型不正确")
    statement = media_query(
        platform=platform, media_type=media_type, search=q.strip(), author=author.strip(),
        library_status=library_status,
    )
    total = int(await db.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    rows = (await db.execute(statement.order_by(
        statement.selected_columns.completed_at.asc() if sort_by == "created_asc" else statement.selected_columns.completed_at.desc(),
        statement.selected_columns.platform, statement.selected_columns.id.desc(),
    ).offset((page - 1) * page_size).limit(page_size))).mappings().all()
    config = await asyncio.to_thread(settings.snapshot)
    items = await asyncio.to_thread(lambda: [serialize_media(row, config) for row in rows])
    orphaned = media_query(platform=platform, library_status="orphaned").subquery()
    orphaned_total = int(await db.scalar(select(func.count()).select_from(orphaned)) or 0)
    return {
        "items": items, "total": total, "page": page, "page_size": page_size, "pages": pages,
        "orphaned_total": orphaned_total,
    }


async def _orphan_file(db: AsyncSession, orphan_id: int):
    orphan = await db.get(OrphanMediaFile, orphan_id)
    if orphan is None:
        raise HTTPException(404, "无索引媒体不存在")
    config = await asyncio.to_thread(settings.snapshot)
    root = media_root(config, orphan.platform)
    try:
        path = await asyncio.to_thread(resolve_media_path, orphan.file_path, root)
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(404, "媒体文件不在当前下载目录内") from exc
    if not await asyncio.to_thread(path.is_file):
        raise HTTPException(404, "媒体文件不存在")
    return orphan, path, root


@router.get("/orphan/{orphan_id}/thumbnail")
async def orphan_thumbnail(orphan_id: int, db: AsyncSession = Depends(get_async_db)):
    orphan, path, root = await _orphan_file(db, orphan_id)
    try:
        thumbnail = await asyncio.to_thread(
            ensure_media_thumbnail, str(path), root, "orphan", orphan.id, orphan.media_type,
        )
    except ThumbnailUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise HTTPException(404, "媒体文件不可用于缩略图") from exc
    return FileResponse(thumbnail, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@router.get("/orphan/{orphan_id}/preview")
async def orphan_preview(orphan_id: int, db: AsyncSession = Depends(get_async_db)):
    orphan, path, _ = await _orphan_file(db, orphan_id)
    return FileResponse(path, media_type=mimetypes.guess_type(orphan.filename)[0])


@router.get("/orphan/{orphan_id}/download")
async def orphan_download(orphan_id: int, db: AsyncSession = Depends(get_async_db)):
    orphan, path, _ = await _orphan_file(db, orphan_id)
    return FileResponse(path, media_type=mimetypes.guess_type(orphan.filename)[0], filename=orphan.filename)


@router.delete("/orphan/{orphan_id}", response_model=MessageResponse)
async def delete_orphan(orphan_id: int, db: AsyncSession = Depends(get_async_db)):
    """删除无索引媒体及其文件；文件若已被新的下载记录引用，只移除这条无索引登记。"""
    from app.services.media_deletion import stage_media_deletion
    from app.services.task_file_cleanup import path_is_indexed, platform_root

    orphan = await db.get(OrphanMediaFile, orphan_id, with_for_update=True)
    if orphan is None:
        raise HTTPException(404, "无索引媒体不存在")
    outside = False
    indexed = await db.run_sync(lambda session: path_is_indexed(session, orphan.file_path))
    try:
        removed = 0 if indexed else await db.run_sync(
            lambda session: stage_media_deletion(session, [orphan.file_path], root=platform_root(orphan.platform))
        )
    except ValueError:
        # 文件已不在当前下载目录内（目录变更或被替换为链接）：不动该文件，只移除这条登记。
        removed, outside = 0, True
    except OSError as exc:
        await db.rollback()
        raise HTTPException(409, f"文件未删除：{exc}") from exc
    await db.delete(orphan)
    await db.commit()
    message = (
        "文件已被新的下载记录使用，已移除无索引登记" if indexed
        else "文件不在当前下载目录内，未删除文件，已移除无索引登记" if outside
        else "已删除文件" if removed
        else "文件已不存在，已移除无索引登记"
    )
    return MessageResponse(success=True, message=message, data={"removed": removed})


@router.delete("/{platform}/{resource_id}", response_model=MessageResponse)
async def delete_indexed(
    platform: str,
    resource_id: int,
    block_redownload: bool = Query(False, description="阻止订阅重新下载；默认之后的检查遇到会重新下载"),
    db: AsyncSession = Depends(get_async_db),
):
    """在媒体库直接删除已索引媒体；是否重新下载由用户选择。"""
    from app.services.media_removal import MediaRemovalError, delete_indexed_media, removal_message

    if platform not in PLATFORMS or resource_id < 1:
        raise HTTPException(404, "媒体资源不存在")
    try:
        result = await delete_indexed_media(db, platform, resource_id, block_redownload=block_redownload)
        await db.commit()
    except MediaRemovalError as exc:
        await db.rollback()
        raise HTTPException(exc.status_code, str(exc)) from exc
    except Exception:
        await db.rollback()
        raise
    return MessageResponse(success=True, message=removal_message(result), data=result)


@router.get("/{platform}/{resource_id}/thumbnail")
async def media_thumbnail(platform: str, resource_id: int, db: AsyncSession = Depends(get_async_db)):
    if platform not in PLATFORMS or resource_id < 1:
        raise HTTPException(404, "媒体资源不存在")
    if platform == "douyin":
        from app.models.models import Work
        row = (await db.execute(select(DownloadTask.file_path, Work.work_type)
            .join(Work, Work.id == DownloadTask.work_id)
            .where(DownloadTask.id == resource_id, DownloadTask.status == "completed"))).first()
        path_value = row.file_path if row else None
        kind = douyin_media_type(row.work_type, path_value) if row else ""
    else:
        model = XMediaAsset if platform == "x" else PlatformMediaAsset
        statement = select(model.file_path, model.media_type).where(model.id == resource_id)
        if model is PlatformMediaAsset:
            statement = statement.where(model.platform == platform)
        row = (await db.execute(statement)).first()
        path_value = row.file_path if row else None
        kind = row.media_type if row else ""
    if not path_value:
        raise HTTPException(404, "媒体资源不存在")
    config = await asyncio.to_thread(settings.snapshot)
    try:
        thumbnail = await asyncio.to_thread(ensure_media_thumbnail, path_value, media_root(config, platform), platform, resource_id, kind)
    except FileNotFoundError as exc:
        raise HTTPException(404, "媒体文件不存在") from exc
    except ThumbnailUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(404, "媒体文件不可用于缩略图") from exc
    return FileResponse(thumbnail, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})
