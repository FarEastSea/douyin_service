"""通用平台下载任务状态机。"""

from datetime import datetime
import os
from typing import Any, Mapping, Optional

from app.models.models import PlatformDownloadTask
from app.models.schemas import PlatformDownloadTaskResponse
from app.services.platform_profile_download import get_profile_platform_spec, profile_storage_key


ACTIVE_PLATFORM_TASK_STATUSES = ("pending", "downloading")


def create_platform_task(platform: str, source_key: str, source_url: str) -> PlatformDownloadTask:
    spec = get_profile_platform_spec(platform)
    return PlatformDownloadTask(
        platform=spec.id,
        source_key=source_key,
        source_url=source_url,
        status="pending",
        phase="queued",
        engine_name="gallery-dl",
        download_dir=os.path.join(spec.download_root(), profile_storage_key(source_key)),
    )


def prepare_platform_task_for_retry(task: PlatformDownloadTask) -> None:
    spec = get_profile_platform_spec(task.platform)
    task.status = "pending"
    task.phase = "queued"
    task.celery_task_id = None
    task.error_message = None
    task.error_code = None
    task.output_log = ""
    task.file_count = 0
    task.downloaded_media_count = 0
    task.progress_percent = 0.0
    task.last_log_line = None
    task.started_at = None
    task.completed_at = None
    task.last_heartbeat_at = None
    task.retry_count = (task.retry_count or 0) + 1
    task.download_dir = os.path.join(spec.download_root(), profile_storage_key(task.source_key))


def mark_platform_task_running(task: PlatformDownloadTask, celery_task_id: Optional[str]) -> None:
    now = datetime.now()
    task.status = "downloading"
    task.phase = "preparing"
    task.celery_task_id = celery_task_id
    task.started_at = now
    task.completed_at = None
    task.last_heartbeat_at = now
    task.error_message = None
    task.error_code = None


def update_platform_task_runtime(
    task: PlatformDownloadTask, *, phase: Optional[str] = None,
    downloaded_media_count: Optional[int] = None, last_log_line: Optional[str] = None,
) -> None:
    task.last_heartbeat_at = datetime.now()
    if phase:
        task.phase = phase
    if downloaded_media_count is not None:
        task.downloaded_media_count = max(0, downloaded_media_count)
        task.file_count = max(task.file_count or 0, task.downloaded_media_count)
    if last_log_line:
        task.last_log_line = last_log_line[:500]


def finalize_platform_task(
    task: PlatformDownloadTask, *, success: bool, file_count: int,
    error_message: Optional[str], error_code: Optional[str], output_log: str,
) -> None:
    now = datetime.now()
    task.file_count = max(0, file_count)
    task.downloaded_media_count = max(task.downloaded_media_count or 0, task.file_count)
    task.output_log = output_log
    task.completed_at = now
    task.last_heartbeat_at = now
    if success:
        task.status = "completed"
        task.phase = "completed"
        task.progress_percent = 100.0
        task.error_message = None
        task.error_code = None
    else:
        task.status = "failed"
        task.phase = "failed"
        task.error_message = error_message
        task.error_code = error_code


def serialize_platform_task(
    task: PlatformDownloadTask, runtime_state: Optional[Mapping[str, Any]] = None,
) -> PlatformDownloadTaskResponse:
    item = PlatformDownloadTaskResponse.model_validate(task)
    item.preview_count = len(getattr(task, "media_assets", ()) or ())
    if runtime_state:
        for field_name in (
            "status", "phase", "engine_name", "file_count",
            "downloaded_media_count", "progress_percent", "last_log_line",
        ):
            value = runtime_state.get(field_name)
            if value is not None:
                setattr(item, field_name, value)
    if item.status == "completed":
        item.progress_percent = 100.0
    return item
