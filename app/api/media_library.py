"""Authenticated local media browsing; no remote source fetching or disk crawling."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
import logging
import mimetypes
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import media_cookie
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
logger = logging.getLogger(__name__)

MediaSort = Literal["created_desc", "created_asc", "size_desc", "size_asc", "published_desc"]
BATCH_DELETE_LIMIT = 100


class MediaBatchDeleteRequest(BaseModel):
    keys: list[str] = Field(..., min_length=1, max_length=BATCH_DELETE_LIMIT)
    block_redownload: bool = False


@router.get("")
async def list_media_library(
    platform: str = Query("", max_length=32),
    media_type: str = Query("", max_length=16),
    q: str = Query("", max_length=255),
    author: str = Query("", max_length=255),
    author_id: int | None = Query(None, ge=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    sort_by: MediaSort = "created_desc",
    library_status: str = Query("", max_length=16, description="indexed 已索引 / orphaned 无索引媒体"),
    date_from: date | None = Query(None, description="入库日期下限（含），YYYY-MM-DD"),
    date_to: date | None = Query(None, description="入库日期上限（含），YYYY-MM-DD"),
    with_counts: bool = Query(True, description="为 false 时跳过总数统计，只取当前页；用于只需要最近几项的轮询"),
    db: AsyncSession = Depends(get_async_db),
):
    if library_status and library_status not in LIBRARY_STATUSES:
        raise HTTPException(400, "媒体分类不正确")
    if platform and platform not in PLATFORMS:
        raise HTTPException(404, "平台不存在")
    if author_id is not None and not platform:
        raise HTTPException(400, "按作者标识筛选时必须指定平台")
    if media_type and media_type not in {"image", "video", "live", "live_photo"}:
        raise HTTPException(400, "媒体类型不正确")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(400, "开始日期不能晚于结束日期")
    statement = media_query(
        platform=platform, media_type=media_type, search=q.strip(), author=author.strip(), author_id=author_id,
        library_status=library_status,
        since=datetime.combine(date_from, time.min) if date_from else None,
        # date.max 没有“次日”，此时不设上限。
        until=datetime.combine(date_to + timedelta(days=1), time.min) if date_to and date_to < date.max else None,
    )
    if with_counts:
        total = int(await db.scalar(select(func.count()).select_from(statement.subquery())) or 0)
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
    columns = statement.selected_columns
    ordering = {
        "created_desc": [columns.completed_at.desc()],
        "created_asc": [columns.completed_at.asc()],
        "size_desc": [columns.file_size.desc().nullslast(), columns.completed_at.desc()],
        "size_asc": [columns.file_size.asc().nullslast(), columns.completed_at.desc()],
        "published_desc": [columns.published_at.desc().nullslast(), columns.completed_at.desc()],
    }[sort_by]
    rows = (await db.execute(statement.order_by(
        *ordering, columns.platform, columns.id.desc(),
    ).offset((page - 1) * page_size).limit(page_size))).mappings().all()
    config = await asyncio.to_thread(settings.snapshot)
    items = await asyncio.to_thread(lambda: [serialize_media(row, config) for row in rows])
    # 日期筛选与分组都以服务器本地日期为准；把“今天”一并返回，页面不必依赖浏览器时区。
    today = date.today().isoformat()
    if not with_counts:
        # 统计总数要扫描全部媒体记录；轮询场景只返回当前页。
        return {
            "items": items, "total": None, "page": page, "page_size": page_size, "pages": None,
            "orphaned_total": None, "today": today,
        }
    orphaned = media_query(platform=platform, library_status="orphaned").subquery()
    orphaned_total = int(await db.scalar(select(func.count()).select_from(orphaned)) or 0)
    return {
        "items": items, "total": total, "page": page, "page_size": page_size, "pages": pages,
        "orphaned_total": orphaned_total, "today": today,
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
@media_cookie
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
@media_cookie
async def orphan_preview(orphan_id: int, db: AsyncSession = Depends(get_async_db)):
    orphan, path, _ = await _orphan_file(db, orphan_id)
    return FileResponse(path, media_type=mimetypes.guess_type(orphan.filename)[0])


@router.get("/orphan/{orphan_id}/download")
@media_cookie
async def orphan_download(orphan_id: int, db: AsyncSession = Depends(get_async_db)):
    orphan, path, _ = await _orphan_file(db, orphan_id)
    return FileResponse(path, media_type=mimetypes.guess_type(orphan.filename)[0], filename=orphan.filename)


async def _remove_orphan(db: AsyncSession, orphan_id: int) -> tuple[str, int]:
    """暂存无索引媒体的删除；调用方负责提交。返回提示文案与实际删除的文件数。"""
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
    message = (
        "文件已被新的下载记录使用，已移除无索引登记" if indexed
        else "文件不在当前下载目录内，未删除文件，已移除无索引登记" if outside
        else "已删除文件" if removed
        else "文件已不存在，已移除无索引登记"
    )
    return message, removed


@router.delete("/orphan/{orphan_id}", response_model=MessageResponse)
async def delete_orphan(orphan_id: int, db: AsyncSession = Depends(get_async_db)):
    """删除无索引媒体及其文件；文件若已被新的下载记录引用，只移除这条无索引登记。"""
    message, removed = await _remove_orphan(db, orphan_id)
    await db.commit()
    return MessageResponse(success=True, message=message, data={"removed": removed})


def _parse_media_key(key: str) -> tuple[str, int]:
    kind, separator, raw_id = str(key).partition(":")
    if not separator or (kind != "orphan" and kind not in PLATFORMS) or not raw_id.isdigit() or int(raw_id) < 1:
        raise ValueError("媒体标识不正确")
    return kind, int(raw_id)


@router.post("/batch-delete", response_model=MessageResponse)
async def batch_delete_media(request: MediaBatchDeleteRequest, db: AsyncSession = Depends(get_async_db)):
    """逐项删除媒体库中选中的媒体。

    每一项独立提交：单项失败不影响已完成的项，结果逐项返回。遇到无法归类的异常时
    停止后续处理，避免在状态不明的情况下继续删除文件。
    """
    from app.services.media_removal import MediaRemovalError, delete_indexed_media

    keys = list(dict.fromkeys(request.keys))
    succeeded: list[str] = []
    failed: list[dict[str, str]] = []
    removed_files = 0
    for position, key in enumerate(keys):
        try:
            kind, resource_id = _parse_media_key(key)
        except ValueError as exc:
            failed.append({"key": key, "message": str(exc)})
            continue
        try:
            if kind == "orphan":
                _, removed = await _remove_orphan(db, resource_id)
            else:
                result = await delete_indexed_media(
                    db, kind, resource_id, block_redownload=request.block_redownload,
                )
                removed = int(result.get("removed") or 0)
            await db.commit()
        except (MediaRemovalError, HTTPException) as exc:
            await db.rollback()
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            failed.append({"key": key, "message": str(detail)})
            continue
        except Exception:
            await db.rollback()
            logger.exception("媒体批量删除中断: key=%s", key)
            failed.append({"key": key, "message": "删除时发生未预期的错误，已停止后续处理"})
            failed.extend({"key": rest, "message": "未处理"} for rest in keys[position + 1:])
            break
        succeeded.append(key)
        removed_files += removed
    message = f"已删除 {len(succeeded)} 项" + (f"，{len(failed)} 项未删除" if failed else "")
    return MessageResponse(
        success=bool(succeeded) or not failed, message=message,
        data={"succeeded": succeeded, "failed": failed, "removed_files": removed_files},
    )


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
@media_cookie
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
