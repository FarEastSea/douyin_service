"""Cross-platform operations views: readiness, unified tasks, and storage audit."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import shutil
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.database import get_async_db
from app.models.models import (
    Author,
    DouyinAccountProfile,
    DownloadHistory,
    DownloadTask,
    PlatformCredential,
    PlatformDownloadTask,
    PlatformMediaAsset,
    SystemConfig,
    Work,
    XAuthor,
    XDownloadTask,
    XMediaAsset,
)
from app.services.platform_registry import platform_registry
from app.services.x_cookie_manager import X_COOKIE_CONFIG_KEY


router = APIRouter(prefix="/operations", tags=["统一运维"])


def _safe_stat(path_value: str | None) -> tuple[bool, int]:
    if not path_value:
        return False, 0
    try:
        path = Path(path_value).expanduser()
        return path.is_file(), path.stat().st_size if path.is_file() else 0
    except (OSError, RuntimeError, ValueError):
        return False, 0


def _root_check(path_value: str) -> dict[str, Any]:
    try:
        path = Path(path_value).expanduser().resolve(strict=False)
        exists = path.is_dir()
        return {
            "path": str(path),
            "exists": exists,
            "writable": exists and os.access(path, os.W_OK),
        }
    except (OSError, RuntimeError, ValueError):
        return {"path": str(path_value), "exists": False, "writable": False}


def _xhs_service_check() -> dict[str, Any]:
    try:
        import requests

        session = requests.Session()
        session.trust_env = False
        try:
            response = session.get("http://127.0.0.1:5556/health", timeout=(1, 2))
            return {"ok": response.status_code < 500, "status_code": response.status_code}
        finally:
            session.close()
    except Exception:
        return {"ok": False, "status_code": None}


def _scan_storage_files(
    root: Path, known: set[str], max_files: int, allow_orphans: bool,
) -> tuple[list[dict[str, Any]], list[str], int]:
    partials: list[dict[str, Any]] = []
    orphan_files: list[str] = []
    scanned_files = 0
    if not root.is_dir():
        return partials, orphan_files, scanned_files
    for path in root.rglob("*"):
        if scanned_files >= max_files:
            break
        try:
            if not path.is_file():
                continue
            scanned_files += 1
            resolved = str(path.resolve(strict=False))
            if path.suffix.lower() in {".part", ".tmp", ".downloading"} and len(partials) < 200:
                stat = path.stat()
                partials.append({
                    "path": resolved, "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })
            elif (
                allow_orphans
                and path.suffix.lower() in {
                    ".jpg", ".jpeg", ".png", ".gif", ".webp",
                    ".mp4", ".webm", ".mov", ".m4v",
                }
                and resolved not in known
                and len(orphan_files) < 200
            ):
                orphan_files.append(resolved)
        except OSError:
            continue
    return partials, orphan_files, scanned_files


async def _platform_success_evidence(db: AsyncSession) -> dict[str, dict[str, Any]]:
    """汇总真实完成且已登记媒体的任务，不把本地预检等同于外部验收。"""
    evidence: dict[str, dict[str, Any]] = {}

    douyin_row = (await db.execute(
        select(
            func.max(DownloadTask.completed_at),
            func.count(func.distinct(DownloadTask.id)),
        )
        .join(DownloadHistory, DownloadHistory.task_id == DownloadTask.id)
        .where(DownloadTask.status == "completed")
    )).one()
    if douyin_row[0]:
        evidence["douyin"] = {
            "validated_sources": {"work"},
            "last_success_at": douyin_row[0],
            "successful_task_count": int(douyin_row[1] or 0),
        }

    x_source_type = case(
        (XDownloadTask.profile_url.ilike("%/status/%"), "work"),
        else_="profile",
    )
    x_rows = (await db.execute(
        select(
            x_source_type.label("source_type"),
            func.max(XDownloadTask.completed_at).label("last_success_at"),
            func.count(func.distinct(XDownloadTask.id)).label("task_count"),
        )
        .join(XMediaAsset, XMediaAsset.task_id == XDownloadTask.id)
        .where(XDownloadTask.status == "completed")
        .group_by(x_source_type)
    )).all()
    if x_rows:
        x_success_times = [row.last_success_at for row in x_rows if row.last_success_at]
        evidence["x"] = {
            "validated_sources": {str(row.source_type) for row in x_rows},
            "last_success_at": max(x_success_times, default=None),
            "successful_task_count": sum(int(row.task_count or 0) for row in x_rows),
        }

    platform_rows = (await db.execute(
        select(
            PlatformDownloadTask.platform,
            PlatformDownloadTask.source_type,
            func.max(PlatformDownloadTask.completed_at).label("last_success_at"),
            func.count(func.distinct(PlatformDownloadTask.id)).label("task_count"),
        )
        .join(PlatformMediaAsset, PlatformMediaAsset.task_id == PlatformDownloadTask.id)
        .where(PlatformDownloadTask.status == "completed")
        .group_by(PlatformDownloadTask.platform, PlatformDownloadTask.source_type)
    )).all()
    for row in platform_rows:
        item = evidence.setdefault(str(row.platform), {
            "validated_sources": set(),
            "last_success_at": None,
            "successful_task_count": 0,
        })
        item["validated_sources"].add(str(row.source_type or "profile"))
        if row.last_success_at and (
            item["last_success_at"] is None or row.last_success_at > item["last_success_at"]
        ):
            item["last_success_at"] = row.last_success_at
        item["successful_task_count"] += int(row.task_count or 0)
    return evidence


@router.get("/platform-readiness")
async def platform_readiness(db: AsyncSession = Depends(get_async_db)):
    """返回本地就绪状态，以及数据库中可核验的真实成功任务证据。"""
    current = await asyncio.to_thread(settings.snapshot)
    credentials = {
        str(value): True
        for value in (await db.execute(select(PlatformCredential.platform))).scalars().all()
    }
    x_cookie = await db.scalar(select(SystemConfig.id).where(SystemConfig.key == X_COOKIE_CONFIG_KEY))
    douyin_account = await db.scalar(select(DouyinAccountProfile).order_by(DouyinAccountProfile.id).limit(1))
    module_status = {
        "gallery-dl": importlib.util.find_spec("gallery_dl") is not None,
        "yt-dlp": importlib.util.find_spec("yt_dlp") is not None,
    }
    ffmpeg = shutil.which("ffmpeg") is not None
    xhs_service = await asyncio.to_thread(_xhs_service_check)
    success_evidence = await _platform_success_evidence(db)
    items = []
    external_validation_required = False
    for definition in platform_registry.list():
        platform_id = definition.id
        if platform_id in {"douyin", "x"}:
            engine = "native" if platform_id == "douyin" else "gallery-dl"
        else:
            engine = str(getattr(current, f"{platform_id.upper()}_DOWNLOAD_ENGINE", "native"))
        engine_ready = True if engine == "native" else module_status.get(engine, False)
        if platform_id == "xhs":
            engine_ready = bool(xhs_service["ok"])
        cookie_file_key = f"{platform_id.upper()}_COOKIE_FILE"
        cookie_file = getattr(current, cookie_file_key, None)
        cookie_configured = bool(
            (platform_id == "douyin" and douyin_account and douyin_account.encrypted_cookie)
            or (platform_id == "x" and (x_cookie or getattr(current, "X_COOKIE", None)))
            or credentials.get(platform_id)
            or (cookie_file and Path(cookie_file).is_file())
        )
        root_value = getattr(current, f"{platform_id.upper()}_DOWNLOAD_DIR", current.DOWNLOAD_ROOT)
        root = await asyncio.to_thread(_root_check, str(root_value))
        blockers = []
        warnings = []
        if not engine_ready:
            blockers.append("下载引擎未就绪")
        if not root["exists"] or not root["writable"]:
            blockers.append("下载目录不可写")
        if platform_id == "bilibili" and not ffmpeg:
            warnings.append("FFmpeg 未安装，将使用可直接播放的单文件格式，最高画质可能受限")
        if not cookie_configured:
            warnings.append("未配置 Cookie，公开视频可能可用，受限内容无法验收")
        capabilities = definition.to_dict()["capabilities"]
        supported_sources = [name for name, enabled in {
            "profile": capabilities["profile_download"],
            "work": capabilities["work_download"],
        }.items() if enabled]
        platform_evidence = success_evidence.get(platform_id, {})
        validated_sources = sorted(
            set(platform_evidence.get("validated_sources") or []) & set(supported_sources)
        )
        missing_validation = [name for name in supported_sources if name not in validated_sources]
        external_tested = bool(validated_sources)
        validation_complete = bool(supported_sources) and not missing_validation
        external_validation_required = external_validation_required or not validation_complete
        last_success_at = platform_evidence.get("last_success_at")
        items.append({
            "platform": platform_id,
            "name": definition.name,
            "status": "blocked" if blockers else ("degraded" if warnings else "ready"),
            "engine": engine,
            "engine_ready": engine_ready,
            "cookie_configured": cookie_configured,
            "ffmpeg_ready": ffmpeg,
            "download_root": root,
            "supported_sources": supported_sources,
            "blockers": blockers,
            "warnings": warnings,
            "external_tested": external_tested,
            "external_validation_complete": validation_complete,
            "validated_sources": validated_sources,
            "missing_validation": missing_validation,
            "last_external_success_at": last_success_at.isoformat() if last_success_at else None,
            "successful_task_count": int(platform_evidence.get("successful_task_count") or 0),
        })
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "scope": "local_preflight_with_observed_success",
        "external_validation_required": external_validation_required,
        "items": items,
    }


def _task_sort_key(item: dict[str, Any]):
    value = item.get("created_at")
    if value is None:
        return datetime.min
    return value


@router.get("/tasks")
async def unified_tasks(
    platform: str | None = Query(None, max_length=32),
    status: str | None = Query(None, max_length=32),
    q: str | None = Query(None, max_length=255),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_async_db),
):
    """One stable task contract over the three legacy task stores."""
    wanted = str(platform or "").strip().lower()
    search = str(q or "").strip()
    fetch_limit = page * page_size
    items: list[dict[str, Any]] = []
    totals = 0

    if wanted in {"", "douyin"}:
        conditions = []
        if status:
            conditions.append(DownloadTask.status == status)
        if search:
            conditions.append(or_(Work.title.contains(search, autoescape=True), Author.nickname.contains(search, autoescape=True)))
        query = (
            select(DownloadTask)
            .join(DownloadTask.work)
            .join(Work.author)
            .options(selectinload(DownloadTask.work).selectinload(Work.author))
            .where(*conditions)
            .order_by(DownloadTask.created_at.desc(), DownloadTask.id.desc())
            .limit(fetch_limit)
        )
        rows = (await db.execute(query)).scalars().all()
        totals += int((await db.scalar(
            select(func.count(DownloadTask.id)).join(DownloadTask.work).join(Work.author).where(*conditions)
        )) or 0)
        for task in rows:
            work = task.work
            items.append({
                "key": f"douyin:{task.id}", "platform": "douyin", "id": task.id,
                "source_type": "work", "source_label": work.title or work.aweme_id,
                "author_name": work.author.nickname if work.author else None,
                "published_at": work.published_at,
                "media_type": "image" if work.work_type == "images" else "video",
                "cover_url": work.cover_url, "status": task.status, "phase": None,
                "progress_percent": task.progress_percent, "file_count": 1 if task.status == "completed" else 0,
                "error_message": task.error_message, "error_code": None,
                "preview_count": 1 if task.status == "completed" and task.file_path else 0,
                "preview_endpoint": f"/tasks/{task.id}/preview",
                "retry_endpoint": f"/tasks/{task.id}/retry", "cancel_endpoint": f"/tasks/{task.id}/cancel",
                "created_at": task.created_at, "started_at": task.started_at, "completed_at": task.completed_at,
            })

    if wanted in {"", "x"}:
        conditions = []
        if status:
            conditions.append(XDownloadTask.status == status)
        if search:
            conditions.append(XDownloadTask.username.contains(search, autoescape=True))
        query = (
            select(XDownloadTask).options(selectinload(XDownloadTask.x_author), selectinload(XDownloadTask.media_assets))
            .where(*conditions).order_by(XDownloadTask.created_at.desc(), XDownloadTask.id.desc()).limit(fetch_limit)
        )
        rows = (await db.execute(query)).scalars().all()
        totals += int((await db.scalar(select(func.count(XDownloadTask.id)).where(*conditions))) or 0)
        for task in rows:
            asset = task.media_assets[0] if task.media_assets else None
            items.append({
                "key": f"x:{task.id}", "platform": "x", "id": task.id,
                "source_type": "work" if "/status/" in task.profile_url else "profile",
                "source_label": asset.title if asset and asset.title else f"@{task.username}",
                "author_name": asset.author_name if asset and asset.author_name else (task.x_author.display_name if task.x_author else task.username),
                "published_at": asset.published_at if asset else None,
                "media_type": asset.media_type if asset else None, "cover_url": asset.cover_url if asset else None,
                "status": task.status, "phase": task.phase, "progress_percent": task.progress_percent,
                "file_count": task.file_count, "error_message": task.error_message, "error_code": task.error_code,
                "preview_count": len(task.media_assets), "media_endpoint": f"/x/tasks/{task.id}/media",
                "retry_endpoint": f"/x/tasks/{task.id}/retry", "cancel_endpoint": f"/x/tasks/{task.id}/cancel",
                "created_at": task.created_at, "started_at": task.started_at, "completed_at": task.completed_at,
            })

    platform_ids = {item.id for item in platform_registry.list()} - {"douyin", "x"}
    generic_ids = platform_ids if not wanted else ({wanted} if wanted in platform_ids else set())
    if generic_ids:
        conditions = [PlatformDownloadTask.platform.in_(generic_ids)]
        if status:
            conditions.append(PlatformDownloadTask.status == status)
        if search:
            conditions.append(PlatformDownloadTask.source_key.contains(search, autoescape=True))
        query = (
            select(PlatformDownloadTask).options(selectinload(PlatformDownloadTask.media_assets))
            .where(*conditions).order_by(PlatformDownloadTask.created_at.desc(), PlatformDownloadTask.id.desc()).limit(fetch_limit)
        )
        rows = (await db.execute(query)).scalars().all()
        totals += int((await db.scalar(select(func.count(PlatformDownloadTask.id)).where(*conditions))) or 0)
        for task in rows:
            asset = task.media_assets[0] if task.media_assets else None
            items.append({
                "key": f"{task.platform}:{task.id}", "platform": task.platform, "id": task.id,
                "source_type": task.source_type, "source_label": asset.title if asset and asset.title else task.source_key,
                "author_name": asset.author_name if asset else None, "published_at": asset.published_at if asset else None,
                "media_type": asset.media_type if asset else None, "cover_url": asset.cover_url if asset else None,
                "status": task.status, "phase": task.phase, "progress_percent": task.progress_percent,
                "file_count": task.file_count, "error_message": task.error_message, "error_code": task.error_code,
                "preview_count": len(task.media_assets),
                "media_endpoint": f"/platform-downloads/{task.platform}/tasks/{task.id}/media",
                "retry_endpoint": f"/platform-downloads/{task.platform}/tasks/{task.id}/retry",
                "cancel_endpoint": f"/platform-downloads/{task.platform}/tasks/{task.id}/cancel",
                "created_at": task.created_at, "started_at": task.started_at, "completed_at": task.completed_at,
            })

    items.sort(key=_task_sort_key, reverse=True)
    start = (page - 1) * page_size
    return {
        "items": items[start:start + page_size], "total": totals, "page": page,
        "page_size": page_size, "pages": max(1, (totals + page_size - 1) // page_size),
    }


@router.get("/storage-audit")
async def storage_audit(
    max_records: int = Query(200_000, ge=100, le=200_000),
    max_files: int = Query(50_000, ge=100, le=200_000),
    db: AsyncSession = Depends(get_async_db),
):
    """Read-only bounded audit. No file or database record is changed."""
    current = await asyncio.to_thread(settings.snapshot)
    root = Path(current.DOWNLOAD_ROOT).expanduser().resolve(strict=False)
    known: set[str] = set()
    missing: list[dict[str, Any]] = []
    zero_byte: list[dict[str, Any]] = []
    scanned_records = 0
    total_records = 0
    for count_statement in (
        select(func.count(DownloadTask.id)).where(DownloadTask.file_path.is_not(None)),
        select(func.count(DownloadHistory.id)).where(DownloadHistory.file_path.is_not(None)),
        select(func.count(XMediaAsset.id)),
        select(func.count(PlatformMediaAsset.id)),
    ):
        total_records += int((await db.scalar(count_statement)) or 0)
    record_rows: list[tuple[str, int, str]] = []

    async def consume(kind: str, statement, path_index: int = 1):
        nonlocal scanned_records
        rows = (await db.execute(statement.limit(max_records - scanned_records))).all()
        for row in rows:
            record_id, path_value = row[0], row[path_index]
            if not path_value:
                continue
            scanned_records += 1
            record_rows.append((kind, int(record_id), str(path_value)))

    def inspect_records():
        for kind, record_id, path_value in record_rows:
            try:
                normalized = str(Path(path_value).expanduser().resolve(strict=False))
            except (OSError, RuntimeError, ValueError):
                normalized = str(path_value)
            known.add(normalized)
            exists, size = _safe_stat(path_value)
            if not exists and len(missing) < 200:
                missing.append({"kind": kind, "id": record_id, "path": str(path_value)})
            elif exists and size == 0 and len(zero_byte) < 200:
                zero_byte.append({"kind": kind, "id": record_id, "path": str(path_value)})

    await consume("download_task", select(DownloadTask.id, DownloadTask.file_path).where(DownloadTask.file_path.is_not(None)))
    if scanned_records < max_records:
        await consume("download_history", select(DownloadHistory.id, DownloadHistory.file_path).where(DownloadHistory.file_path.is_not(None)))
    if scanned_records < max_records:
        await consume("x_media", select(XMediaAsset.id, XMediaAsset.file_path))
    if scanned_records < max_records:
        await consume("platform_media", select(PlatformMediaAsset.id, PlatformMediaAsset.file_path))
    await asyncio.to_thread(inspect_records)

    records_truncated = total_records > scanned_records
    partials, orphan_files, scanned_files = await asyncio.to_thread(
        _scan_storage_files, root, known, max_files, not records_truncated,
    )
    disk_target = root
    while not disk_target.exists() and disk_target != disk_target.parent:
        disk_target = disk_target.parent
    disk = await asyncio.to_thread(shutil.disk_usage, disk_target)
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(), "read_only": True,
        "root": str(root), "scanned_records": scanned_records, "scanned_files": scanned_files,
        "total_records": total_records, "records_truncated": records_truncated, "files_truncated": scanned_files >= max_files,
        "orphan_scan_reliable": not records_truncated,
        "missing_records": missing, "zero_byte_files": zero_byte,
        "partial_files": partials, "orphan_files": orphan_files,
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free, "used_percent": round(disk.used / disk.total * 100, 1) if disk.total else 0},
        "note": "结果仅用于核对；不会自动删除文件或修改历史记录。",
    }
