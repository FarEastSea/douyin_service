"""Cross-platform operations views: readiness, unified tasks, and storage audit."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import importlib.util
import logging
import os
from pathlib import Path
import shutil
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import redis_client
from app.core.config import settings
from app.models.database import get_async_db
from app.models.models import (
    Author,
    DouyinAccountProfile,
    DownloadHistory,
    DownloadTask,
    MediaStatsSnapshot,
    PlatformCredential,
    PlatformDownloadTask,
    PlatformMediaAsset,
    SystemConfig,
    Work,
    WorkStatsSnapshot,
    XAuthor,
    XDownloadTask,
    XMediaAsset,
)
from app.services.platform_registry import platform_registry
from app.services.storage_maintenance import (
    apply_storage_repair_plan,
    build_storage_repair_plan,
    find_rebase_candidate,
)
from app.services.unified_task_operations import TaskOperationError, operate_task
from app.services.x_cookie_manager import X_COOKIE_CONFIG_KEY
from app.models.schemas import MessageResponse, UnifiedTaskActionRequest


router = APIRouter(prefix="/operations", tags=["统一运维"])
logger = logging.getLogger(__name__)


class StorageRepairTarget(BaseModel):
    issue_type: Literal[
        "stale_record_path", "missing_record", "zero_byte_file", "partial_file", "orphan_file",
    ]
    record_kind: Literal["download_task", "download_history", "x_media", "platform_media"] | None = None
    record_id: int | None = Field(None, ge=1)
    path: str = Field(..., min_length=1, max_length=4096)


class StorageRepairRequest(BaseModel):
    targets: list[StorageRepairTarget] = Field(..., min_length=1, max_length=200)
    dry_run: bool = True


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
            try:
                if path.relative_to(root).parts[0] == ".quarantine":
                    continue
            except (IndexError, ValueError):
                continue
            scanned_files += 1
            resolved = str(path.resolve(strict=False))
            if path.suffix.lower() in {".part", ".tmp", ".downloading"} and len(partials) < 200:
                stat = path.stat()
                age_seconds = max(0, datetime.now().timestamp() - stat.st_mtime)
                if age_seconds < 6 * 3600:
                    continue
                partials.append({
                    "path": resolved, "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "stale_seconds": int(age_seconds),
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


async def _latest_artifact_evidence(db: AsyncSession) -> dict[str, dict[str, dict[str, Any]]]:
    """核验最近成功记录对应的本地文件，证明当前版本可继续提供预览。"""
    result: dict[str, dict[str, dict[str, Any]]] = {}

    douyin_rows = (await db.execute(
        select(DownloadTask.id, DownloadTask.file_path, DownloadTask.completed_at)
        .where(DownloadTask.status == "completed", DownloadTask.file_path.is_not(None))
        .order_by(DownloadTask.completed_at.desc().nullslast(), DownloadTask.id.desc())
        .limit(20)
    )).all()
    if douyin_rows:
        row = douyin_rows[0]
        exists, size = await asyncio.to_thread(_safe_stat, row.file_path)
        result["douyin"] = {"work": {
            "healthy": exists and size > 0, "task_id": int(row.id), "size_bytes": size,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }}

    for source_type in ("profile", "work"):
        source_condition = (
            XDownloadTask.profile_url.ilike("%/status/%")
            if source_type == "work"
            else ~XDownloadTask.profile_url.ilike("%/status/%")
        )
        row = (await db.execute(
            select(XDownloadTask.id, XMediaAsset.id, XMediaAsset.file_path)
            .join(XMediaAsset, XMediaAsset.task_id == XDownloadTask.id)
            .where(XDownloadTask.status == "completed", source_condition)
            .order_by(XDownloadTask.completed_at.desc().nullslast(), XMediaAsset.id.desc())
            .limit(1)
        )).first()
        if not row:
            continue
        task_id, asset_id, file_path = row
        exists, size = await asyncio.to_thread(_safe_stat, file_path)
        result.setdefault("x", {})[source_type] = {
            "healthy": exists and size > 0, "task_id": int(task_id),
            "asset_id": int(asset_id), "size_bytes": size,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    for definition in platform_registry.list():
        if definition.id in {"douyin", "x"}:
            continue
        capabilities = definition.to_dict()["capabilities"]
        for source in ("profile", "work"):
            if not capabilities[f"{source}_download"]:
                continue
            row = (await db.execute(
                select(PlatformDownloadTask.id, PlatformMediaAsset.id, PlatformMediaAsset.file_path)
                .join(PlatformMediaAsset, PlatformMediaAsset.task_id == PlatformDownloadTask.id)
                .where(
                    PlatformDownloadTask.platform == definition.id,
                    PlatformDownloadTask.source_type == source,
                    PlatformDownloadTask.status == "completed",
                )
                .order_by(PlatformDownloadTask.completed_at.desc().nullslast(), PlatformMediaAsset.id.desc())
                .limit(1)
            )).first()
            if not row:
                continue
            task_id, asset_id, file_path = row
            exists, size = await asyncio.to_thread(_safe_stat, file_path)
            result.setdefault(definition.id, {})[source] = {
                "healthy": exists and size > 0, "task_id": int(task_id),
                "asset_id": int(asset_id), "size_bytes": size,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
    return result


def _runtime_revision() -> str | None:
    """不启动子进程地读取部署工作树版本。"""
    for key in ("JENKINS_TARGET_SHA", "GIT_COMMIT"):
        if value := str(os.environ.get(key) or "").strip():
            return value
    try:
        git_path = Path(__file__).resolve().parents[2] / ".git"
        if git_path.is_file():
            line = git_path.read_text(encoding="utf-8").strip()
            git_path = (git_path.parent / line.removeprefix("gitdir:").strip()).resolve()
        head = (git_path / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            return (git_path / head.removeprefix("ref:").strip()).read_text(encoding="utf-8").strip()
        return head
    except (OSError, RuntimeError, ValueError):
        return None


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
    artifact_evidence = await _latest_artifact_evidence(db)
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
        recorded_sources = sorted(
            set(platform_evidence.get("validated_sources") or []) & set(supported_sources)
        )
        artifact_validation = artifact_evidence.get(platform_id, {})
        validated_sources = sorted(
            source for source in recorded_sources
            if artifact_validation.get(source, {}).get("healthy") is True
        )
        broken_sources = sorted(set(recorded_sources) - set(validated_sources))
        if broken_sources:
            warnings.append(
                "最近成功记录的本地媒体已缺失或为空：" + "、".join(broken_sources)
            )
        missing_validation = [name for name in supported_sources if name not in validated_sources]
        external_tested = bool(recorded_sources)
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
            "recorded_sources": recorded_sources,
            "missing_validation": missing_validation,
            "artifact_validation": artifact_validation,
            "last_external_success_at": last_success_at.isoformat() if last_success_at else None,
            "successful_task_count": int(platform_evidence.get("successful_task_count") or 0),
        })
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "revision": _runtime_revision(),
        "scope": "local_preflight_with_observed_success",
        "external_validation_required": external_validation_required,
        "items": items,
    }


VALID_TASK_STATUSES = {
    "pending", "downloading", "paused", "completed", "skipped", "failed", "cancelled",
}


def _merge_status_rows(target: dict[str, int], rows) -> None:
    for row in rows:
        key = str(row.status or "unknown")
        target[key] = target.get(key, 0) + int(row.task_count or 0)


def _serialize_douyin_task(task: DownloadTask) -> dict[str, Any]:
    work = task.work
    has_stats = any(getattr(work, name, None) is not None for name in (
        "play_count", "digg_count", "comment_count", "share_count",
    ))
    return {
        "key": f"douyin:{task.id}", "platform": "douyin", "id": task.id,
        "source_type": "work", "source_label": work.title or work.aweme_id,
        "author_name": work.author.nickname if work.author else None,
        "published_at": work.published_at,
        "media_type": "image" if work.work_type == "images" else "video",
        "cover_url": work.cover_url, "status": task.status, "phase": None,
        "progress_percent": task.progress_percent,
        "file_count": 1 if task.status == "completed" and task.file_path else 0,
        "error_message": task.error_message, "error_code": None,
        "preview_count": 1 if task.status == "completed" and task.file_path else 0,
        "preview_endpoint": f"/tasks/{task.id}/preview",
        "has_stats": has_stats,
        "stats_endpoint": f"/operations/tasks/douyin/{task.id}/stats" if has_stats else None,
        "retry_endpoint": f"/tasks/{task.id}/retry",
        "cancel_endpoint": f"/tasks/{task.id}/cancel",
        "created_at": task.created_at, "started_at": task.started_at,
        "completed_at": task.completed_at,
    }


def _first_media_asset(task: Any) -> Any | None:
    assets = list(getattr(task, "media_assets", None) or [])
    return min(assets, key=lambda item: int(item.id or 0)) if assets else None


def _serialize_x_task(task: XDownloadTask) -> dict[str, Any]:
    asset = _first_media_asset(task)
    has_stats = bool(asset and any(getattr(asset, name, None) is not None for name in (
        "view_count", "like_count", "comment_count", "share_count",
    )))
    return {
        "key": f"x:{task.id}", "platform": "x", "id": task.id,
        "source_type": "work" if "/status/" in task.profile_url else "profile",
        "source_label": asset.title if asset and asset.title else f"@{task.username}",
        "author_name": (
            asset.author_name if asset and asset.author_name
            else task.x_author.display_name if task.x_author else task.username
        ),
        "published_at": asset.published_at if asset else None,
        "media_type": asset.media_type if asset else None,
        "cover_url": asset.cover_url if asset else None,
        "status": task.status, "phase": task.phase,
        "progress_percent": task.progress_percent, "file_count": task.file_count,
        "error_message": task.error_message, "error_code": task.error_code,
        "preview_count": len(task.media_assets), "media_endpoint": f"/x/tasks/{task.id}/media",
        "has_stats": has_stats,
        "stats_endpoint": f"/operations/tasks/x/{task.id}/stats" if has_stats else None,
        "retry_endpoint": f"/x/tasks/{task.id}/retry",
        "cancel_endpoint": f"/x/tasks/{task.id}/cancel",
        "created_at": task.created_at, "started_at": task.started_at,
        "completed_at": task.completed_at,
    }


def _serialize_platform_task(task: PlatformDownloadTask) -> dict[str, Any]:
    asset = _first_media_asset(task)
    has_stats = bool(asset and any(getattr(asset, name, None) is not None for name in (
        "view_count", "like_count", "comment_count", "share_count",
    )))
    return {
        "key": f"{task.platform}:{task.id}", "platform": task.platform, "id": task.id,
        "source_type": task.source_type,
        "source_label": asset.title if asset and asset.title else task.source_key,
        "author_name": asset.author_name if asset else None,
        "published_at": asset.published_at if asset else None,
        "media_type": asset.media_type if asset else None,
        "cover_url": asset.cover_url if asset else None,
        "status": task.status, "phase": task.phase,
        "progress_percent": task.progress_percent, "file_count": task.file_count,
        "error_message": task.error_message, "error_code": task.error_code,
        "preview_count": len(task.media_assets),
        "has_stats": has_stats,
        "stats_endpoint": (
            f"/operations/tasks/{task.platform}/{task.id}/stats" if has_stats else None
        ),
        "media_endpoint": f"/platform-downloads/{task.platform}/tasks/{task.id}/media",
        "retry_endpoint": f"/platform-downloads/{task.platform}/tasks/{task.id}/retry",
        "cancel_endpoint": f"/platform-downloads/{task.platform}/tasks/{task.id}/cancel",
        "created_at": task.created_at, "started_at": task.started_at,
        "completed_at": task.completed_at,
    }


def _snapshot_values(snapshot: Any, *, douyin: bool = False) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "view_count": getattr(snapshot, "play_count" if douyin else "view_count", None),
        "like_count": getattr(snapshot, "digg_count" if douyin else "like_count", None),
        "comment_count": snapshot.comment_count,
        "share_count": snapshot.share_count,
        "observed_at": snapshot.observed_at,
        "source": snapshot.source,
    }


@router.get("/tasks/{platform}/{task_id}/stats")
async def unified_task_stats(
    platform: str,
    task_id: int,
    limit: int = Query(200, ge=1, le=1000),
    db: AsyncSession = Depends(get_async_db),
):
    """返回统一字段的互动趋势；无历史快照时仍返回当前采集值。"""
    platform_id = str(platform or "").lower()
    snapshots: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    label = ""
    if platform_id == "douyin":
        task = (await db.execute(
            select(DownloadTask).options(selectinload(DownloadTask.work))
            .where(DownloadTask.id == task_id)
        )).scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        work = task.work
        label = work.title or work.aweme_id
        rows = (await db.execute(
            select(WorkStatsSnapshot)
            .where(WorkStatsSnapshot.work_id == work.id)
            .order_by(WorkStatsSnapshot.observed_at.desc(), WorkStatsSnapshot.id.desc())
            .limit(limit)
        )).scalars().all()
        snapshots = [_snapshot_values(item, douyin=True) for item in reversed(rows)]
        current = {
            "view_count": work.play_count, "like_count": work.digg_count,
            "comment_count": work.comment_count, "share_count": work.share_count,
            "observed_at": work.metadata_refreshed_at or work.discovered_at,
            "source": "current",
        }
    elif platform_id == "x":
        task = (await db.execute(
            select(XDownloadTask).options(selectinload(XDownloadTask.media_assets))
            .where(XDownloadTask.id == task_id)
        )).scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        asset = _first_media_asset(task)
        label = asset.title if asset and asset.title else f"@{task.username}"
        if asset:
            rows = (await db.execute(
                select(MediaStatsSnapshot)
                .where(
                    MediaStatsSnapshot.platform == "x",
                    MediaStatsSnapshot.asset_kind == "x_media",
                    MediaStatsSnapshot.asset_id == asset.id,
                )
                .order_by(MediaStatsSnapshot.observed_at.desc(), MediaStatsSnapshot.id.desc())
                .limit(limit)
            )).scalars().all()
            snapshots = [_snapshot_values(item) for item in reversed(rows)]
            current = {
                name: getattr(asset, name) for name in (
                    "view_count", "like_count", "comment_count", "share_count",
                )
            } | {"observed_at": asset.created_at, "source": "current"}
    else:
        try:
            platform_registry.get(platform_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="平台不存在") from exc
        task = (await db.execute(
            select(PlatformDownloadTask).options(selectinload(PlatformDownloadTask.media_assets))
            .where(
                PlatformDownloadTask.id == task_id,
                PlatformDownloadTask.platform == platform_id,
            )
        )).scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        asset = _first_media_asset(task)
        label = asset.title if asset and asset.title else task.source_key
        if asset:
            rows = (await db.execute(
                select(MediaStatsSnapshot)
                .where(
                    MediaStatsSnapshot.platform == platform_id,
                    MediaStatsSnapshot.asset_kind == "platform_media",
                    MediaStatsSnapshot.asset_id == asset.id,
                )
                .order_by(MediaStatsSnapshot.observed_at.desc(), MediaStatsSnapshot.id.desc())
                .limit(limit)
            )).scalars().all()
            snapshots = [_snapshot_values(item) for item in reversed(rows)]
            current = {
                name: getattr(asset, name) for name in (
                    "view_count", "like_count", "comment_count", "share_count",
                )
            } | {"observed_at": asset.created_at, "source": "current"}
    if current and any(current.get(name) is not None for name in (
        "view_count", "like_count", "comment_count", "share_count",
    )):
        comparable = {name: current.get(name) for name in (
            "view_count", "like_count", "comment_count", "share_count",
        )}
        if not snapshots or any(snapshots[-1].get(name) != value for name, value in comparable.items()):
            snapshots.append({"id": None, **current})
    return {"platform": platform_id, "task_id": task_id, "label": label, "snapshots": snapshots}


@router.get("/tasks")
async def unified_tasks(
    platform: str | None = Query(None, max_length=32),
    status: str | None = Query(None, max_length=32),
    q: str | None = Query(None, max_length=255),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_async_db),
):
    """跨三套任务表统一检索、汇总，并通过数据库联合分页限制内存占用。"""
    wanted = str(platform or "").strip().lower()
    search = str(q or "").strip()
    if status and status not in VALID_TASK_STATUSES:
        raise HTTPException(status_code=400, detail="不支持的任务状态")
    known_platforms = {item.id for item in platform_registry.list()}
    if wanted and wanted not in known_platforms:
        raise HTTPException(status_code=404, detail="平台不存在")

    key_statements = []
    status_summary: dict[str, int] = {}

    if wanted in {"", "douyin"}:
        base_conditions = []
        if search:
            base_conditions.append(or_(
                Work.title.contains(search, autoescape=True),
                Work.aweme_id.contains(search, autoescape=True),
                Author.nickname.contains(search, autoescape=True),
                DownloadTask.file_name.contains(search, autoescape=True),
            ))
        summary_rows = (await db.execute(
            select(DownloadTask.status, func.count(DownloadTask.id).label("task_count"))
            .join(DownloadTask.work).join(Work.author)
            .where(*base_conditions).group_by(DownloadTask.status)
        )).all()
        _merge_status_rows(status_summary, summary_rows)
        conditions = [*base_conditions]
        if status:
            conditions.append(DownloadTask.status == status)
        key_statements.append(
            select(
                literal("douyin").label("platform"), DownloadTask.id.label("task_id"),
                DownloadTask.created_at.label("created_at"),
            ).join(DownloadTask.work).join(Work.author).where(*conditions)
        )

    if wanted in {"", "x"}:
        base_conditions = []
        if search:
            base_conditions.append(or_(
                XDownloadTask.username.contains(search, autoescape=True),
                XDownloadTask.profile_url.contains(search, autoescape=True),
                XDownloadTask.media_assets.any(or_(
                    XMediaAsset.title.contains(search, autoescape=True),
                    XMediaAsset.author_name.contains(search, autoescape=True),
                    XMediaAsset.filename.contains(search, autoescape=True),
                )),
            ))
        summary_rows = (await db.execute(
            select(XDownloadTask.status, func.count(XDownloadTask.id).label("task_count"))
            .where(*base_conditions).group_by(XDownloadTask.status)
        )).all()
        _merge_status_rows(status_summary, summary_rows)
        conditions = [*base_conditions]
        if status:
            conditions.append(XDownloadTask.status == status)
        key_statements.append(
            select(
                literal("x").label("platform"), XDownloadTask.id.label("task_id"),
                XDownloadTask.created_at.label("created_at"),
            ).where(*conditions)
        )

    platform_ids = known_platforms - {"douyin", "x"}
    generic_ids = platform_ids if not wanted else ({wanted} if wanted in platform_ids else set())
    if generic_ids:
        base_conditions = [PlatformDownloadTask.platform.in_(generic_ids)]
        if search:
            base_conditions.append(or_(
                PlatformDownloadTask.source_key.contains(search, autoescape=True),
                PlatformDownloadTask.source_url.contains(search, autoescape=True),
                PlatformDownloadTask.media_assets.any(or_(
                    PlatformMediaAsset.title.contains(search, autoescape=True),
                    PlatformMediaAsset.author_name.contains(search, autoescape=True),
                    PlatformMediaAsset.filename.contains(search, autoescape=True),
                )),
            ))
        summary_rows = (await db.execute(
            select(PlatformDownloadTask.status, func.count(PlatformDownloadTask.id).label("task_count"))
            .where(*base_conditions).group_by(PlatformDownloadTask.status)
        )).all()
        _merge_status_rows(status_summary, summary_rows)
        conditions = [*base_conditions]
        if status:
            conditions.append(PlatformDownloadTask.status == status)
        key_statements.append(
            select(
                PlatformDownloadTask.platform.label("platform"),
                PlatformDownloadTask.id.label("task_id"),
                PlatformDownloadTask.created_at.label("created_at"),
            ).where(*conditions)
        )

    totals = status_summary.get(status, 0) if status else sum(status_summary.values())

    combined_statement = key_statements[0] if len(key_statements) == 1 else union_all(*key_statements)
    combined = combined_statement.subquery()
    page_rows = (await db.execute(
        select(combined.c.platform, combined.c.task_id, combined.c.created_at)
        .order_by(combined.c.created_at.desc().nullslast(), combined.c.platform, combined.c.task_id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).all()
    ordered_keys = [(str(row.platform), int(row.task_id)) for row in page_rows]
    ids_by_platform: dict[str, list[int]] = {}
    for item_platform, task_id in ordered_keys:
        ids_by_platform.setdefault(item_platform, []).append(task_id)

    serialized: dict[tuple[str, int], dict[str, Any]] = {}
    if ids_by_platform.get("douyin"):
        rows = (await db.execute(
            select(DownloadTask).join(DownloadTask.work).join(Work.author)
            .options(selectinload(DownloadTask.work).selectinload(Work.author))
            .where(DownloadTask.id.in_(ids_by_platform["douyin"]))
        )).scalars().all()
        serialized.update({("douyin", task.id): _serialize_douyin_task(task) for task in rows})
    if ids_by_platform.get("x"):
        rows = (await db.execute(
            select(XDownloadTask)
            .options(selectinload(XDownloadTask.x_author), selectinload(XDownloadTask.media_assets))
            .where(XDownloadTask.id.in_(ids_by_platform["x"]))
        )).scalars().all()
        serialized.update({("x", task.id): _serialize_x_task(task) for task in rows})
    generic_task_ids = [
        task_id for item_platform, task_id in ordered_keys if item_platform not in {"douyin", "x"}
    ]
    if generic_task_ids:
        rows = (await db.execute(
            select(PlatformDownloadTask).options(selectinload(PlatformDownloadTask.media_assets))
            .where(PlatformDownloadTask.id.in_(generic_task_ids))
        )).scalars().all()
        serialized.update({(task.platform, task.id): _serialize_platform_task(task) for task in rows})

    items = [serialized[key] for key in ordered_keys if key in serialized]
    return {
        "items": items, "total": totals, "page": page,
        "page_size": page_size, "pages": max(1, (totals + page_size - 1) // page_size),
        "status_summary": status_summary,
    }


@router.post("/tasks/actions", response_model=MessageResponse)
async def unified_task_actions(
    request: UnifiedTaskActionRequest,
    db: AsyncSession = Depends(get_async_db),
):
    """对显式选中的跨平台任务执行批量重试或取消，并逐项返回结果。"""
    task_keys = list(dict.fromkeys(request.task_keys))
    succeeded: list[str] = []
    failed: list[dict[str, Any]] = []
    for task_key in task_keys:
        try:
            await operate_task(db, task_key, request.action)
            succeeded.append(task_key)
        except TaskOperationError as exc:
            failed.append({"task_key": task_key, "message": str(exc), "status_code": exc.status_code})
    action_label = "重试" if request.action == "retry" else "取消"
    message = f"已{action_label} {len(succeeded)} 个任务"
    if failed:
        message += f"，{len(failed)} 个未处理"
    try:
        await asyncio.to_thread(
            redis_client.append_activity_log,
            "warning" if failed else "info",
            "unified-tasks",
            message,
            ", ".join(succeeded[:20]) or "无成功任务",
        )
    except Exception as exc:
        logger.warning("统一任务操作已完成，但活动日志写入失败: %s", exc)
    return MessageResponse(
        success=bool(succeeded), message=message,
        data={"succeeded": succeeded, "failed": failed},
    )


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
    relinkable: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    zero_byte: list[dict[str, Any]] = []
    scanned_records = 0
    total_records = 0
    for count_statement in (
        select(func.count(DownloadTask.id)).where(
            DownloadTask.status == "completed", DownloadTask.file_path.is_not(None),
        ),
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
                candidate = Path(path_value).expanduser()
                if not candidate.is_absolute():
                    candidate = root / candidate
                normalized = str(candidate.resolve(strict=False))
            except (OSError, RuntimeError, ValueError):
                normalized = str(path_value)
            known.add(normalized)
            exists, size = _safe_stat(normalized)
            if not exists:
                rebased = find_rebase_candidate(root, normalized)
                if rebased is not None:
                    rebased_path = str(rebased)
                    known.add(rebased_path)
                    if len(relinkable) + len(missing) < 200:
                        relinkable.append({
                            "kind": kind,
                            "id": record_id,
                            "path": normalized,
                            "suggested_path": rebased_path,
                        })
                elif len(relinkable) + len(missing) < 200:
                    missing.append({"kind": kind, "id": record_id, "path": normalized})
            elif exists and size == 0 and len(zero_byte) < 200:
                zero_byte.append({"kind": kind, "id": record_id, "path": normalized})

    await consume("download_task", select(DownloadTask.id, DownloadTask.file_path).where(
        DownloadTask.status == "completed", DownloadTask.file_path.is_not(None),
    ))
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
        "relinkable_records": relinkable,
        "missing_records": missing, "zero_byte_files": zero_byte,
        "partial_files": partials, "orphan_files": orphan_files,
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free, "used_percent": round(disk.used / disk.total * 100, 1) if disk.total else 0},
        "note": "结果仅用于核对；旧根目录记录可在预演确认后回填，文件不会移动。其他问题也不会自动处理。",
    }


@router.post("/storage-repair")
async def storage_repair(
    request: StorageRepairRequest,
    db: AsyncSession = Depends(get_async_db),
):
    """先预演、再回填旧路径或执行可恢复隔离。"""
    current = await asyncio.to_thread(settings.snapshot)
    root = Path(current.DOWNLOAD_ROOT).expanduser().resolve(strict=False)
    if not root.is_dir():
        raise HTTPException(status_code=409, detail="下载根目录不存在，不能执行存储维护")
    targets = [item.model_dump() for item in request.targets]
    if request.dry_run:
        plan = await build_storage_repair_plan(db, root, targets)
        return {
            "dry_run": True,
            "planned": len(plan),
            "eligible": sum(1 for item in plan if item["eligible"]),
            "items": plan,
            "note": "预演没有修改任何文件或记录；确认后才会回填旧路径或执行可恢复隔离。",
        }
    result = await apply_storage_repair_plan(db, root, targets)
    try:
        await asyncio.to_thread(
            redis_client.append_activity_log,
            "warning", "storage-maintenance",
            f"存储维护已处理 {result['applied']} 项",
            f"回填路径：{len(result['relinked'])}；隔离目录：{result['quarantine_root'] or '无'}；标记任务：{len(result['marked_tasks'])}",
        )
    except Exception as exc:
        logger.warning("存储维护已完成，但活动日志写入失败: %s", exc)
    return {
        "dry_run": False,
        **result,
        "message": "处理完成；旧路径已回填，文件未删除，隔离项可按清单恢复",
    }
