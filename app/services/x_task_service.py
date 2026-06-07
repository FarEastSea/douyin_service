"""X/Twitter 任务与作者领域辅助函数。"""

from __future__ import annotations

from datetime import datetime
import os
from typing import Any, Mapping, Optional

from app.core.config import settings
from app.models.models import XAuthor, XDownloadTask
from app.models.schemas import XAuthorResponse, XDownloadTaskResponse

ACTIVE_X_TASK_STATUSES = ("pending", "downloading")
DEFAULT_X_AUTHOR_STATUS = "active"
DEFAULT_X_AUTHOR_STATUS_LABEL = "正常"


def build_x_download_dir(username: str) -> str:
    """构建 X 用户下载目录。"""
    return os.path.join(settings.X_DOWNLOAD_DIR, username)


def create_x_author(
    username: str,
    profile_url: str,
    *,
    is_subscribed: bool = False,
    check_interval: int = 3600,
) -> XAuthor:
    """创建新的 X 作者记录。"""
    return XAuthor(
        username=username,
        profile_url=profile_url,
        is_subscribed=is_subscribed,
        check_interval=check_interval,
        display_name=f"@{username}",
        account_status=DEFAULT_X_AUTHOR_STATUS,
        account_status_label=DEFAULT_X_AUTHOR_STATUS_LABEL,
        last_synced_at=datetime.now(),
    )


def sync_x_author(
    author: XAuthor,
    *,
    profile_url: str,
    display_name: Optional[str] = None,
    avatar_url: Optional[str] = None,
    account_status: str = DEFAULT_X_AUTHOR_STATUS,
    account_status_label: str = DEFAULT_X_AUTHOR_STATUS_LABEL,
    last_error: Optional[str] = None,
    last_synced_at: Optional[datetime] = None,
) -> XAuthor:
    """同步 X 作者元数据。"""
    author.profile_url = profile_url
    author.display_name = display_name or author.display_name or f"@{author.username}"
    if avatar_url:
        author.avatar_url = avatar_url
    author.account_status = account_status
    author.account_status_label = account_status_label
    author.last_error = last_error
    author.last_synced_at = last_synced_at or datetime.now()
    return author


def create_x_download_task(author: XAuthor) -> XDownloadTask:
    """基于作者记录创建新的 X 下载任务。"""
    return XDownloadTask(
        username=author.username,
        profile_url=author.profile_url,
        x_author_id=author.id,
        x_author=author,
        status="pending",
        phase="queued",
        engine_name=settings.X_DOWNLOAD_ENGINE,
        download_dir=build_x_download_dir(author.username),
        total_media_count=0,
        downloaded_media_count=0,
        progress_percent=0.0,
        file_count=0,
    )


def prepare_x_task_for_retry(task: XDownloadTask) -> XDownloadTask:
    """重置任务，供失败/取消后重试。"""
    task.status = "pending"
    task.phase = "queued"
    task.error_message = None
    task.error_code = None
    task.output_log = ""
    task.file_count = 0
    task.total_media_count = 0
    task.downloaded_media_count = 0
    task.progress_percent = 0.0
    task.last_log_line = None
    task.last_heartbeat_at = None
    task.started_at = None
    task.completed_at = None
    task.celery_task_id = None
    task.download_dir = build_x_download_dir(task.username)
    task.engine_name = task.engine_name or settings.X_DOWNLOAD_ENGINE
    task.retry_count = (task.retry_count or 0) + 1
    return task


def mark_x_task_running(task: XDownloadTask, celery_task_id: Optional[str]) -> XDownloadTask:
    """标记任务开始执行。"""
    now = datetime.now()
    task.status = "downloading"
    task.phase = "preparing"
    task.celery_task_id = celery_task_id
    task.started_at = now
    task.completed_at = None
    task.last_heartbeat_at = now
    task.error_message = None
    task.error_code = None
    task.engine_name = task.engine_name or settings.X_DOWNLOAD_ENGINE
    task.download_dir = task.download_dir or build_x_download_dir(task.username)
    task.last_log_line = None
    return task


def update_x_task_runtime(
    task: XDownloadTask,
    *,
    phase: Optional[str] = None,
    downloaded_media_count: Optional[int] = None,
    total_media_count: Optional[int] = None,
    last_log_line: Optional[str] = None,
) -> XDownloadTask:
    """更新任务运行期状态。"""
    task.last_heartbeat_at = datetime.now()
    if phase:
        task.phase = phase
    if downloaded_media_count is not None:
        task.downloaded_media_count = max(downloaded_media_count, 0)
        task.file_count = max(task.file_count or 0, task.downloaded_media_count)
    if total_media_count is not None:
        task.total_media_count = max(total_media_count, task.total_media_count or 0)
    if last_log_line:
        task.last_log_line = last_log_line[:500]

    if task.total_media_count and task.total_media_count > 0:
        task.progress_percent = round(
            min(100.0, (task.downloaded_media_count or 0) / task.total_media_count * 100),
            2,
        )
    elif task.status == "completed":
        task.progress_percent = 100.0

    return task


def finalize_x_task(
    task: XDownloadTask,
    *,
    success: bool,
    file_count: int,
    error_message: Optional[str],
    error_code: Optional[str],
    output_log: str,
) -> XDownloadTask:
    """用下载结果收敛任务最终状态。"""
    now = datetime.now()
    task.file_count = max(file_count, 0)
    task.downloaded_media_count = max(task.downloaded_media_count or 0, task.file_count)
    task.total_media_count = max(task.total_media_count or 0, task.file_count)
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
        if task.total_media_count and task.total_media_count > 0:
            task.progress_percent = round(
                min(100.0, (task.downloaded_media_count or 0) / task.total_media_count * 100),
                2,
            )
    return task


def cancel_x_task(task: XDownloadTask) -> XDownloadTask:
    """标记任务已取消。"""
    now = datetime.now()
    task.status = "cancelled"
    task.phase = "cancelled"
    task.completed_at = now
    task.last_heartbeat_at = now
    return task


def serialize_x_task(
    task: XDownloadTask,
    runtime_state: Optional[Mapping[str, Any]] = None,
) -> XDownloadTaskResponse:
    """将任务 ORM 与 Redis 运行态合并为响应对象。"""
    item = XDownloadTaskResponse.model_validate(task)
    author = getattr(task, "x_author", None)
    if author:
        item.author_display_name = author.display_name or f"@{author.username}"
        item.author_account_status = author.account_status or DEFAULT_X_AUTHOR_STATUS

    if runtime_state:
        for field_name in (
            "status",
            "phase",
            "engine_name",
            "file_count",
            "total_media_count",
            "downloaded_media_count",
            "progress_percent",
            "last_log_line",
            "last_heartbeat_at",
        ):
            runtime_value = runtime_state.get(field_name)
            if runtime_value is not None:
                setattr(item, field_name, runtime_value)
        item.has_live_state = True

    if item.status == "completed":
        item.progress_percent = 100.0
    elif item.total_media_count and item.total_media_count > 0 and not item.progress_percent:
        item.progress_percent = round(item.downloaded_media_count / item.total_media_count * 100, 2)

    return item


def serialize_x_author(author: XAuthor) -> XAuthorResponse:
    """将 X 作者 ORM 转换为响应对象。"""
    item = XAuthorResponse.model_validate(author)
    item.display_name = item.display_name or f"@{author.username}"
    item.account_status = item.account_status or DEFAULT_X_AUTHOR_STATUS
    item.account_status_label = item.account_status_label or DEFAULT_X_AUTHOR_STATUS_LABEL
    return item