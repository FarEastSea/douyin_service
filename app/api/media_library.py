"""Authenticated local media browsing; no remote source fetching or disk crawling."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.database import get_async_db
from app.models.models import DownloadTask, PlatformMediaAsset, XMediaAsset
from app.services.media_library import PLATFORMS, douyin_media_type, media_query, media_root, serialize_media
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
    db: AsyncSession = Depends(get_async_db),
):
    if platform and platform not in PLATFORMS:
        raise HTTPException(404, "平台不存在")
    if media_type and media_type not in {"image", "video", "live", "live_photo"}:
        raise HTTPException(400, "媒体类型不正确")
    statement = media_query(platform=platform, media_type=media_type, search=q.strip(), author=author.strip())
    total = int(await db.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    rows = (await db.execute(statement.order_by(
        statement.selected_columns.completed_at.asc() if sort_by == "created_asc" else statement.selected_columns.completed_at.desc(),
        statement.selected_columns.platform, statement.selected_columns.id.desc(),
    ).offset((page - 1) * page_size).limit(page_size))).mappings().all()
    config = await asyncio.to_thread(settings.snapshot)
    items = await asyncio.to_thread(lambda: [serialize_media(row, config) for row in rows])
    return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": pages}


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
