"""Read-only, bounded media queries across the existing download tables."""

from __future__ import annotations

from pathlib import Path
from sqlalchemy import BigInteger, Integer, case, cast, func, literal, or_, select, union_all

from app.models.models import Author, DownloadTask, PlatformDownloadTask, PlatformMediaAsset, Work, XDownloadTask, XMediaAsset
from app.services.media_paths import resolve_media_path


PLATFORMS = {"douyin", "x", "tiktok", "weibo", "bilibili", "xhs"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v", ".mkv"}


def douyin_media_type(work_type: str, file_path: str | None, filename: str | None = None) -> str:
    if work_type != "images":
        return "video"
    return "live" if Path(file_path or filename or "").suffix.lower() in VIDEO_EXTENSIONS else "image"


def media_query(*, platform: str = "", media_type: str = "", search: str = "", author: str = ""):
    """Use resource IDs, never a profile task ID as an invented work identity."""
    statements = []
    if not platform or platform == "douyin":
        path = func.lower(func.coalesce(DownloadTask.file_path, DownloadTask.file_name, ""))
        video_file = or_(*(path.endswith(extension) for extension in sorted(VIDEO_EXTENSIONS)))
        statements.append(select(
            literal("douyin").label("platform"), DownloadTask.id.label("id"),
            DownloadTask.id.label("task_id"), Work.id.label("work_id"),
            Author.id.label("author_id"), Author.nickname.label("author_name"),
            Work.title.label("title"), DownloadTask.file_name.label("filename"),
            DownloadTask.file_path.label("file_path"),
            case((Work.work_type != "images", "video"), (video_file, "live"), else_="image").label("media_type"),
            cast(DownloadTask.total_bytes, BigInteger).label("file_size"),
            DownloadTask.created_at.label("created_at"),
            func.coalesce(DownloadTask.completed_at, DownloadTask.created_at).label("completed_at"),
            Work.published_at.label("published_at"), Work.width.label("width"),
            Work.height.label("height"), Work.duration_ms.label("duration_ms"),
        ).join(Work, Work.id == DownloadTask.work_id).join(Author, Author.id == Work.author_id)
        .where(DownloadTask.status == "completed", DownloadTask.file_path.is_not(None)))
    if not platform or platform == "x":
        statements.append(select(
            literal("x").label("platform"), XMediaAsset.id.label("id"),
            XMediaAsset.task_id.label("task_id"), cast(literal(None), Integer).label("work_id"),
            XMediaAsset.x_author_id.label("author_id"),
            func.coalesce(XMediaAsset.author_name, XDownloadTask.username).label("author_name"),
            XMediaAsset.title.label("title"), XMediaAsset.filename.label("filename"),
            XMediaAsset.file_path.label("file_path"), XMediaAsset.media_type.label("media_type"),
            cast(XMediaAsset.size_bytes, BigInteger).label("file_size"),
            XMediaAsset.created_at.label("created_at"),
            func.coalesce(XDownloadTask.completed_at, XMediaAsset.created_at).label("completed_at"),
            XMediaAsset.published_at.label("published_at"), XMediaAsset.width.label("width"),
            XMediaAsset.height.label("height"), XMediaAsset.duration_ms.label("duration_ms"),
        ).join(XDownloadTask, XDownloadTask.id == XMediaAsset.task_id))
    if not platform or platform not in {"douyin", "x"}:
        generic = select(
            PlatformMediaAsset.platform.label("platform"), PlatformMediaAsset.id.label("id"),
            PlatformMediaAsset.task_id.label("task_id"), cast(literal(None), Integer).label("work_id"),
            cast(literal(None), Integer).label("author_id"), PlatformMediaAsset.author_name.label("author_name"),
            PlatformMediaAsset.title.label("title"), PlatformMediaAsset.filename.label("filename"),
            PlatformMediaAsset.file_path.label("file_path"), PlatformMediaAsset.media_type.label("media_type"),
            cast(PlatformMediaAsset.size_bytes, BigInteger).label("file_size"),
            PlatformMediaAsset.created_at.label("created_at"),
            func.coalesce(PlatformDownloadTask.completed_at, PlatformMediaAsset.created_at).label("completed_at"),
            PlatformMediaAsset.published_at.label("published_at"), PlatformMediaAsset.width.label("width"),
            PlatformMediaAsset.height.label("height"), PlatformMediaAsset.duration_ms.label("duration_ms"),
        ).join(PlatformDownloadTask, PlatformDownloadTask.id == PlatformMediaAsset.task_id)
        generic = generic.where(PlatformMediaAsset.platform.in_(PLATFORMS - {"douyin", "x"}))
        if platform:
            generic = generic.where(PlatformMediaAsset.platform == platform)
        statements.append(generic)
    combined = (union_all(*statements) if len(statements) > 1 else statements[0]).subquery()
    conditions = []
    if media_type:
        conditions.append(combined.c.media_type == media_type)
    if search:
        conditions.append(or_(*(column.contains(search, autoescape=True) for column in (
            combined.c.title, combined.c.filename, combined.c.author_name,
        ))))
    if author:
        conditions.append(combined.c.author_name.contains(author, autoescape=True))
    return select(combined).where(*conditions)


def media_root(config, platform: str) -> str:
    return config.DOWNLOAD_DIR if platform == "douyin" else getattr(config, f"{platform.upper()}_DOWNLOAD_DIR")


def serialize_media(row, config) -> dict:
    item = dict(row)
    platform, resource_id, task_id = item["platform"], item["id"], item.pop("task_id")
    path_value = item.pop("file_path")
    try:
        path = resolve_media_path(path_value, media_root(config, platform))
        available = path.is_file() and path.stat().st_size > 0
    except (OSError, ValueError, RuntimeError):
        available = False
    if platform == "douyin":
        preview = f"/tasks/{resource_id}/preview"
        download = preview
    elif platform == "x":
        preview, download = (f"/x/media/{resource_id}/{action}" for action in ("preview", "download"))
    else:
        preview, download = (f"/platform-downloads/{platform}/media/{resource_id}/{action}" for action in ("preview", "download"))
    return {
        **item, "key": f"{platform}:{resource_id}", "task_key": f"{platform}:{task_id}",
        "file_size": item["file_size"] or 0, "available": available,
        "preview_endpoint": preview, "download_endpoint": download,
        "thumbnail_endpoint": f"/operations/media/{platform}/{resource_id}/thumbnail",
    }
