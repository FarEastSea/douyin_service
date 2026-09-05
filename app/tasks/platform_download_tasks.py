"""通用平台主页媒体下载任务。"""

from collections import deque
from pathlib import Path
from datetime import datetime
import logging
import mimetypes
import traceback

from sqlalchemy import select, update

from app.core import redis_client
from app.core.config import settings
from app.core.traffic_control import global_download_slot
from app.models.database import get_sync_db
from app.models.models import PlatformDownloadTask, PlatformMediaAsset
from app.services.platform_profile_download import (
    build_profile_download_engine,
    cleanup_platform_cookie_file,
    get_profile_platform_spec,
    materialize_platform_cookie_file,
    profile_storage_key,
)
from app.services.platform_task_service import (
    finalize_platform_task,
)
from app.services.x_downloader import is_media_download_line
from app.tasks.celery_app import celery_app


logger = logging.getLogger(__name__)


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

        with global_download_slot(getattr(self.request, "id", None) or f"platform:{task.id}"):
            result = engine.download_profile(
                spec=spec,
                source_url=source_url,
                source_key=source_key,
                source_type=task.source_type or "profile",
                destination=spec.download_root(),
                cookie_file=cookie_path,
                on_line=on_line,
                on_process=lambda pid: redis_client.set_platform_task_pid(platform_id, task_id, pid),
            )

        task = db.execute(
            select(PlatformDownloadTask).where(PlatformDownloadTask.id == task_id)
        ).scalar_one_or_none()
        if not task:
            return {"success": False, "deleted": True}
        if task.status == "cancelled":
            return {"success": False, "cancelled": True}

        task.download_dir = str(Path(spec.download_root()) / profile_storage_key(task.source_key))
        existing_paths = set(db.execute(
            select(PlatformMediaAsset.file_path).where(
                PlatformMediaAsset.task_id == task.id
            )
        ).scalars().all())
        for file_path in result.files:
            if file_path in existing_paths:
                continue
            path = Path(file_path)
            mime_type, _ = mimetypes.guess_type(file_path)
            db.add(PlatformMediaAsset(
                task_id=task.id,
                platform=task.platform,
                media_type="video" if (mime_type or "").startswith("video/") else "image",
                file_path=file_path,
                filename=path.name,
                size_bytes=path.stat().st_size if path.is_file() else 0,
                mime_type=mime_type,
            ))
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
        return {
            "success": result.success,
            "file_count": result.file_count,
            "error": result.error_message,
            "error_code": result.error_code,
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
