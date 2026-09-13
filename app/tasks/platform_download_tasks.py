"""通用平台主页媒体下载任务。"""

import logging
import mimetypes
import traceback
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select, update

from app.core import redis_client
from app.core.config import settings
from app.core.runtime_config import get_runtime_config_sync
from app.core.traffic_control import global_download_slot
from app.models.database import get_sync_db
from app.models.models import (
    PlatformAuthor,
    PlatformDownloadTask,
    PlatformMediaAsset,
    PlatformWork,
)
from app.services.platform_profile_download import (
    build_profile_download_engine,
    cleanup_platform_cookie_file,
    get_profile_platform_spec,
    materialize_platform_cookie_file,
    profile_storage_key,
)
from app.services.platform_task_service import (
    create_platform_task,
    finalize_platform_task,
)
from app.services.platform_metadata import (
    apply_media_metadata,
    metadata_for_media,
    record_media_stats_snapshot,
)
from app.services.x_downloader import is_media_download_line
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _xhs_author_id(source_key: str) -> str:
    return source_key.removeprefix("user-") if source_key.startswith("user-") else source_key


def _persist_xhs_profile_result(
    db,
    task: PlatformDownloadTask,
    result,
    *,
    full_reconcile: bool = False,
) -> list[int]:
    profile = getattr(result, "author_profile", None)
    works = getattr(result, "discovered_works", None)
    if task.platform != "xhs" or task.source_type != "profile" or not isinstance(profile, dict):
        return []
    external_user_id = str(profile.get("user_id") or _xhs_author_id(task.source_key)).strip()
    if not external_user_id:
        raise ValueError("小红书作者采集结果缺少作者 ID")
    author = db.execute(
        select(PlatformAuthor).where(
            PlatformAuthor.platform == "xhs",
            PlatformAuthor.external_user_id == external_user_id,
        )
    ).scalar_one_or_none()
    if author is None:
        author = PlatformAuthor(
            platform="xhs",
            external_user_id=external_user_id,
            profile_url=task.source_url,
            is_subscribed=False,
            check_interval=settings.DEFAULT_CHECK_INTERVAL,
        )
        db.add(author)
        db.flush()
    author.profile_url = task.source_url
    author.nickname = str(profile.get("nickname") or "")[:255] or None
    author.red_id = str(profile.get("red_id") or "")[:255] or None
    author.avatar_url = str(profile.get("avatar_url") or "") or None
    author.description = str(profile.get("description") or "")[:5000] or None
    author.account_status = "healthy"
    author.last_error = None
    author.last_check_time = datetime.now()
    author.last_success_at = datetime.now()
    if full_reconcile:
        author.last_full_reconcile_at = datetime.now()

    child_task_ids: list[int] = []
    for item in works if isinstance(works, list) else []:
        if not isinstance(item, dict):
            continue
        work_id = str(item.get("note_id") or "").strip()
        source_url = str(item.get("source_url") or "").strip()
        if not work_id or not source_url:
            continue
        work = db.execute(
            select(PlatformWork).where(
                PlatformWork.platform == "xhs",
                PlatformWork.external_work_id == work_id,
            )
        ).scalar_one_or_none()
        if work is None:
            work = PlatformWork(
                platform="xhs",
                external_work_id=work_id,
                author_id=author.id,
                source_url=source_url,
            )
            db.add(work)
            db.flush()
        work.author_id = author.id
        work.source_url = source_url
        work.xsec_token = str(item.get("xsec_token") or "") or None
        work.title = str(item.get("title") or "")[:500] or None
        work.work_type = str(item.get("work_type") or "unknown")[:16]
        work.cover_url = str(item.get("cover_url") or "") or None
        timestamp = item.get("published_at")
        work.published_at = datetime.fromtimestamp(timestamp) if isinstance(timestamp, int) else None
        work.is_pinned = bool(item.get("is_pinned"))
        work.last_seen_at = datetime.now()

        existing_task = db.execute(
            select(PlatformDownloadTask)
            .where(
                PlatformDownloadTask.platform == "xhs",
                PlatformDownloadTask.source_key == f"note-{work_id}",
                PlatformDownloadTask.source_type == "work",
            )
            .order_by(PlatformDownloadTask.id.desc())
            .limit(1)
        ).scalars().first()
        if existing_task is None:
            existing_task = create_platform_task(
                "xhs", f"note-{work_id}", source_url, "work",
            )
            existing_task.download_dir = str(
                Path(task.download_dir) / profile_storage_key(existing_task.source_key)
            )
            db.add(existing_task)
            db.flush()
            child_task_ids.append(existing_task.id)
        work.download_task_id = existing_task.id
    db.flush()
    author.total_works = int(db.execute(
        select(func.count(PlatformWork.id)).where(PlatformWork.author_id == author.id)
    ).scalar_one() or 0)
    return child_task_ids


@celery_app.task(bind=True, name="app.tasks.platform_download_tasks.download_platform_profile")
def download_platform_profile(self, task_id: int):
    db = get_sync_db()
    cookie_path = None
    managed_cookie = False
    task = None
    platform_id = None
    try:
        now = datetime.now()
        claimed = db.execute(
            update(PlatformDownloadTask)
            .where(
                PlatformDownloadTask.id == task_id,
                PlatformDownloadTask.status == "pending",
            )
            .values(
                status="downloading",
                phase="preparing",
                celery_task_id=self.request.id,
                started_at=now,
                completed_at=None,
                last_heartbeat_at=now,
                error_message=None,
                error_code=None,
            )
        )
        db.commit()
        if claimed.rowcount != 1:
            task = db.execute(
                select(PlatformDownloadTask).where(PlatformDownloadTask.id == task_id)
            ).scalar_one_or_none()
            return {
                "success": False,
                "skipped": True,
                "status": task.status if task else "missing",
            }

        task = db.execute(
            select(PlatformDownloadTask).where(PlatformDownloadTask.id == task_id)
        ).scalar_one_or_none()
        if not task:
            return {"success": False, "error": "平台下载任务不存在"}

        spec = get_profile_platform_spec(task.platform)
        platform_id = task.platform
        if platform_id == "xhs" and task.source_type == "profile":
            finalize_platform_task(
                task,
                success=False,
                file_count=task.file_count or 0,
                error_message="小红书作者主页批量采集已搁置；当前仅保留单条笔记下载",
                error_code="feature_shelved",
                output_log=task.output_log or "",
            )
            db.commit()
            return {"success": False, "skipped": True, "reason": "feature_shelved"}
        source_url = task.source_url
        source_key = task.source_key
        engine_name = task.engine_name
        redis_client.update_platform_task_state(platform_id, task_id, {
            "status": "downloading", "phase": "preparing",
            "file_count": 0, "downloaded_media_count": 0, "progress_percent": 0,
        })

        cookie_path, managed_cookie = materialize_platform_cookie_file(db, spec, task_id)
        db.commit()  # 外部下载期间不占用数据库事务或连接。
        engine = build_profile_download_engine(platform_id, engine_name)
        log_lines: deque[str] = deque(maxlen=settings.X_TASK_LOG_MAX_LINES)
        downloaded = 0

        def on_line(line: str) -> None:
            nonlocal downloaded
            log_lines.append(line)
            redis_client.append_platform_task_log(platform_id, task_id, line)
            if is_media_download_line(line):
                downloaded += 1
            redis_client.update_platform_task_state(platform_id, task_id, {
                "status": "downloading", "phase": "running",
                "file_count": downloaded, "downloaded_media_count": downloaded,
                "progress_percent": 0, "last_log_line": line[:500],
            })

        engine_options = {}
        xhs_full_reconcile = False
        if platform_id == "xhs" and task.source_type == "profile":
            author_external_id = _xhs_author_id(source_key)
            author = db.execute(
                select(PlatformAuthor).where(
                    PlatformAuthor.platform == "xhs",
                    PlatformAuthor.external_user_id == author_external_id,
                )
            ).scalar_one_or_none()
            runtime = get_runtime_config_sync(db)
            reconcile_interval = timedelta(
                seconds=runtime["subscription_full_reconcile_interval"]
            )
            xhs_full_reconcile = bool(
                author is None
                or author.last_full_reconcile_at is None
                or author.last_full_reconcile_at <= datetime.now() - reconcile_interval
            )
            on_line(
                "[小红书] 本次执行有界全量对账"
                if xhs_full_reconcile
                else "[小红书] 本次执行有界增量检查"
            )
            known_ids = db.execute(
                select(PlatformWork.external_work_id)
                .join(PlatformAuthor, PlatformWork.author_id == PlatformAuthor.id)
                .where(
                    PlatformAuthor.platform == "xhs",
                    PlatformAuthor.external_user_id == author_external_id,
                )
            ).scalars().all()
            engine_options = {
                "known_work_ids": [] if xhs_full_reconcile else known_ids,
                "max_items": runtime["xhs_profile_max_items"],
                "max_scrolls": runtime["xhs_profile_max_scrolls"],
                "known_streak": runtime["xhs_profile_known_streak"],
                "scroll_delay": runtime["xhs_profile_scroll_delay"],
            }
        db.commit()

        download_destination = spec.download_root()
        if platform_id == "xhs" and task.source_type == "work" and task.download_dir:
            download_destination = str(Path(task.download_dir).parent)

        with global_download_slot(getattr(self.request, "id", None) or f"platform:{task.id}"):
            result = engine.download_profile(
                spec=spec,
                source_url=source_url,
                source_key=source_key,
                source_type=task.source_type or "profile",
                destination=download_destination,
                cookie_file=cookie_path,
                on_line=on_line,
                on_process=lambda pid: redis_client.set_platform_task_pid(platform_id, task_id, pid),
                **engine_options,
            )

        task = db.execute(
            select(PlatformDownloadTask).where(PlatformDownloadTask.id == task_id)
        ).scalar_one_or_none()
        if not task:
            return {"success": False, "deleted": True}
        if task.status == "cancelled":
            return {"success": False, "cancelled": True}

        if not task.download_dir:
            task.download_dir = str(Path(spec.download_root()) / profile_storage_key(task.source_key))
        existing_assets = {
            item.file_path: item for item in db.execute(
            select(PlatformMediaAsset).where(
                PlatformMediaAsset.task_id == task.id
            )
        ).scalars().all()
        }
        for file_path in result.files:
            path = Path(file_path)
            mime_type, _ = mimetypes.guess_type(file_path)
            metadata = metadata_for_media(
                file_path,
                fallback_title=f"{spec.name} {'作品' if task.source_type == 'work' else '主页媒体'} {task.source_key}",
                fallback_author=task.source_key if task.source_type == "profile" else None,
                fallback_data=getattr(result, "metadata", None),
            )
            if existing := existing_assets.get(file_path):
                apply_media_metadata(existing, metadata)
                record_media_stats_snapshot(
                    db, existing, platform=task.platform, asset_kind="platform_media",
                )
                continue
            asset = PlatformMediaAsset(
                task_id=task.id,
                platform=task.platform,
                media_type="video" if (mime_type or "").startswith("video/") else "image",
                file_path=file_path,
                filename=path.name,
                size_bytes=path.stat().st_size if path.is_file() else 0,
                mime_type=mime_type,
                **metadata.as_model_values(),
            )
            db.add(asset)
            db.flush()
            record_media_stats_snapshot(
                db, asset, platform=task.platform, asset_kind="platform_media",
            )
        child_task_ids = _persist_xhs_profile_result(
            db,
            task,
            result,
            full_reconcile=xhs_full_reconcile,
        )
        if task.platform == "xhs" and task.source_type == "profile" and not result.author_profile:
            author = db.execute(select(PlatformAuthor).where(
                PlatformAuthor.platform == "xhs",
                PlatformAuthor.external_user_id == _xhs_author_id(task.source_key),
            )).scalar_one_or_none()
            if author:
                author.account_status = (
                    "auth_required" if result.error_code == "auth_required" else "degraded"
                )
                author.last_error = (result.error_message or result.error_code or "主页检查失败")[:500]
        finalize_platform_task(
            task,
            success=result.success,
            file_count=result.file_count,
            error_message=result.error_message,
            error_code=result.error_code,
            output_log="\n".join(log_lines),
        )
        task.last_log_line = log_lines[-1][:500] if log_lines else None
        db.commit()
        dispatched_children = 0
        for child_task_id in child_task_ids:
            child_task = db.get(PlatformDownloadTask, child_task_id)
            try:
                queued = download_platform_profile.delay(child_task_id)
                if child_task:
                    child_task.celery_task_id = queued.id
                dispatched_children += 1
            except Exception as exc:
                if child_task:
                    finalize_platform_task(
                        child_task,
                        success=False,
                        file_count=0,
                        error_message="任务队列暂不可用，小红书作品下载未投递",
                        error_code="queue_unavailable",
                        output_log="",
                    )
                logger.warning(
                    "小红书作品任务 %s 投递失败: %s",
                    child_task_id,
                    type(exc).__name__,
                )
        db.commit()
        discovered_works = getattr(result, "discovered_works", None)
        return {
            "success": result.success,
            "file_count": result.file_count,
            "error": result.error_message,
            "error_code": result.error_code,
            "discovered_works": len(discovered_works) if isinstance(discovered_works, list) else 0,
            "queued_downloads": dispatched_children,
        }
    except Exception as exc:
        db.rollback()
        logger.error("平台下载任务 %s 异常:\n%s", task_id, traceback.format_exc())
        try:
            task = db.execute(
                select(PlatformDownloadTask).where(PlatformDownloadTask.id == task_id)
            ).scalar_one_or_none()
            if task and task.status != "cancelled":
                finalize_platform_task(
                    task,
                    success=False,
                    file_count=task.file_count or 0,
                    error_message=f"{type(exc).__name__}: {str(exc)[:300]}",
                    error_code="worker_exception",
                    output_log=task.output_log or "",
                )
                db.commit()
        except Exception:
            db.rollback()
        raise
    finally:
        cleanup_platform_cookie_file(cookie_path, managed_cookie)
        if platform_id:
            try:
                redis_client.delete_platform_task_state(platform_id, task_id)
                redis_client.delete_platform_task_pid(platform_id, task_id)
            except Exception:
                pass
        db.close()


@celery_app.task(name="app.tasks.platform_download_tasks.check_xhs_subscriptions")
def check_xhs_subscriptions():
    """按作者自己的检查周期创建幂等的小红书主页扫描任务。"""
    db = get_sync_db()
    queued_ids: list[int] = []
    try:
        runtime = get_runtime_config_sync(db)
        if not runtime.get("auto_check_enabled", True):
            return {"success": True, "skipped": True, "reason": "auto_check_disabled"}
        now = datetime.now()
        authors = db.execute(select(PlatformAuthor).where(
            PlatformAuthor.platform == "xhs",
            PlatformAuthor.is_subscribed.is_(True),
        ).order_by(PlatformAuthor.id)).scalars().all()
        for author in authors:
            interval = max(3600, int(author.check_interval or settings.DEFAULT_CHECK_INTERVAL))
            if author.last_check_time and author.last_check_time > now - timedelta(seconds=interval):
                continue
            source_key = f"user-{author.external_user_id}"
            active = db.execute(select(PlatformDownloadTask.id).where(
                PlatformDownloadTask.platform == "xhs",
                PlatformDownloadTask.source_key == source_key,
                PlatformDownloadTask.source_type == "profile",
                PlatformDownloadTask.status.in_(("pending", "downloading")),
            ).limit(1)).scalars().first()
            if active:
                continue
            task = PlatformDownloadTask(
                platform="xhs",
                source_key=source_key,
                source_url=author.profile_url,
                source_type="profile",
                status="pending",
                phase="queued",
                engine_name="xhs-api",
                download_dir=str(Path(settings.XHS_DOWNLOAD_DIR) / profile_storage_key(source_key)),
            )
            db.add(task)
            db.flush()
            queued_ids.append(task.id)
        db.commit()
        dispatched: list[int] = []
        for task_id in queued_ids:
            try:
                queued = download_platform_profile.delay(task_id)
                task = db.get(PlatformDownloadTask, task_id)
                if task:
                    task.celery_task_id = queued.id
                    dispatched.append(task_id)
            except Exception as exc:
                task = db.get(PlatformDownloadTask, task_id)
                if task:
                    finalize_platform_task(
                        task, success=False, file_count=0,
                        error_message="任务队列暂不可用，小红书订阅检查未投递",
                        error_code="queue_unavailable", output_log="",
                    )
                logger.warning("小红书订阅任务 %s 投递失败: %s", task_id, type(exc).__name__)
        db.commit()
        return {"success": True, "due": len(queued_ids), "dispatched": len(dispatched)}
    finally:
        db.close()
