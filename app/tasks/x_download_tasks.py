"""X/Twitter Celery 下载任务。"""

from datetime import datetime
import logging
import os
import traceback

from sqlalchemy import select

from app.core import redis_client
from app.core.config import settings
from app.models.database import get_sync_db, init_db_sync
from app.models.models import XAuthor, XDownloadTask
from app.services.x_cookie_manager import cleanup_x_cookie_file, materialize_x_cookie_file
from app.services.x_downloader import build_x_download_engine, is_media_download_line
from app.services.x_task_service import (
    ACTIVE_X_TASK_STATUSES,
    create_x_download_task,
    finalize_x_task,
    mark_x_task_running,
    sync_x_author,
    update_x_task_runtime,
)
from app.tasks.celery_app import celery_app

logs_dir = 'logs'
if not os.path.exists(logs_dir):
    os.makedirs(logs_dir)

logger = logging.getLogger(__name__)
if not logger.handlers:
    logger.setLevel(logging.INFO)
    file_handler = logging.FileHandler('logs/x_download_tasks.log', encoding='utf-8')
    console_handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


AUTHOR_ERROR_STATUS_MAP = {
    "not_found": ("deleted", "无法访问"),
    "auth_required": ("restricted", "需要登录"),
    "invalid_url": ("invalid", "链接无效"),
}


@celery_app.task(bind=True, name="app.tasks.x_download_tasks.download_x_profile")
def download_x_profile(self, task_id: int):
    """
    下载 X/Twitter 用户媒体的 Celery 任务

    Args:
        task_id: XDownloadTask 的数据库 ID
    """
    init_db_sync()
    db = get_sync_db()
    cookie_path = None
    managed_cookie = False

    try:
        task = db.execute(
            select(XDownloadTask).where(XDownloadTask.id == task_id)
        ).scalar_one_or_none()

        if not task:
            logger.error(f"X 任务 {task_id} 不存在")
            return {"success": False, "error": "任务不存在"}

        mark_x_task_running(task, self.request.id)
        db.commit()
        redis_client.update_x_task_state(task_id, {
            "status": task.status,
            "phase": task.phase,
            "engine_name": task.engine_name,
            "file_count": task.file_count,
            "total_media_count": task.total_media_count,
            "downloaded_media_count": task.downloaded_media_count,
            "progress_percent": task.progress_percent,
            "last_heartbeat_at": task.last_heartbeat_at,
        })

        logger.info(f"开始 X 下载任务 {task_id}: @{task.username}")

        cookie_path, managed_cookie = materialize_x_cookie_file(db, task_id=task_id)
        engine = build_x_download_engine(task.engine_name)

        log_lines = []
        downloaded_media_count = 0

        def on_line(line: str):
            nonlocal downloaded_media_count
            log_lines.append(line)
            redis_client.append_x_task_log(task_id, line)
            if is_media_download_line(line):
                downloaded_media_count += 1

            update_x_task_runtime(
                task,
                phase="running",
                downloaded_media_count=downloaded_media_count,
                last_log_line=line,
            )
            redis_client.update_x_task_state(task_id, {
                "status": task.status,
                "phase": task.phase,
                "engine_name": task.engine_name,
                "file_count": task.file_count,
                "total_media_count": task.total_media_count,
                "downloaded_media_count": task.downloaded_media_count,
                "progress_percent": task.progress_percent,
                "last_log_line": task.last_log_line,
                "last_heartbeat_at": task.last_heartbeat_at,
            })

        result = engine.download_profile(
            profile_url=task.profile_url,
            username=task.username,
            destination=settings.X_DOWNLOAD_DIR,
            cookie_file=cookie_path,
            on_line=on_line,
            task_id=task_id,
        )

        task.download_dir = os.path.join(settings.X_DOWNLOAD_DIR, task.username)
        update_x_task_runtime(
            task,
            phase="finalizing",
            downloaded_media_count=max(downloaded_media_count, result.file_count),
            total_media_count=max(downloaded_media_count, result.file_count),
            last_log_line=log_lines[-1] if log_lines else None,
        )
        finalize_x_task(
            task,
            success=result.success,
            file_count=result.file_count,
            error_message=result.error_message,
            error_code=result.error_code,
            output_log="\n".join(log_lines[-settings.X_TASK_LOG_MAX_LINES:]),
        )

        author = None
        if task.x_author_id:
            author = db.execute(
                select(XAuthor).where(XAuthor.id == task.x_author_id)
            ).scalar_one_or_none()

        if result.success:
            logger.info(f"X 任务 {task_id} 完成: {result.file_count} 个文件")
            if author:
                author.total_downloads = (author.total_downloads or 0) + result.file_count
                author.last_check_time = datetime.now()
                sync_x_author(author, profile_url=task.profile_url)
        else:
            logger.error(f"X 任务 {task_id} 失败: {task.error_message}")
            if author:
                account_status, account_status_label = AUTHOR_ERROR_STATUS_MAP.get(
                    result.error_code or "",
                    (author.account_status or "active", author.account_status_label or "状态异常"),
                )
                sync_x_author(
                    author,
                    profile_url=task.profile_url,
                    account_status=account_status,
                    account_status_label=account_status_label,
                    last_error=task.error_message,
                )

        db.commit()
        return {
            "success": result.success,
            "file_count": result.file_count,
            "error": result.error_message,
            "error_code": result.error_code,
            "return_code": result.return_code,
        }

    except Exception as e:
        error_trace = traceback.format_exc()
        logger.error(f"X 任务 {task_id} 异常:\n{error_trace}")

        try:
            task = db.execute(
                select(XDownloadTask).where(XDownloadTask.id == task_id)
            ).scalar_one_or_none()
            if task:
                finalize_x_task(
                    task,
                    success=False,
                    file_count=task.file_count or 0,
                    error_message=f"{type(e).__name__}: {str(e)[:200]}",
                    error_code="worker_exception",
                    output_log=task.output_log or "",
                )
                db.commit()
        except Exception as db_error:
            logger.error(f"更新 X 任务 {task_id} 失败状态时出错: {db_error}")

        raise
    finally:
        cleanup_x_cookie_file(cookie_path, managed_cookie)
        redis_client.delete_x_task_state(task_id)
        redis_client.delete_x_task_pid(task_id)
        db.close()


@celery_app.task(name="app.tasks.x_download_tasks.check_x_subscriptions")
def check_x_subscriptions():
    """检查所有订阅的 X 用户，为到期的用户创建下载任务（gallery-dl 自动跳过已下载）"""
    init_db_sync()
    db = get_sync_db()
    try:
        authors = db.execute(
            select(XAuthor).where(XAuthor.is_subscribed == True)
        ).scalars().all()

        results = []
        for author in authors:
            if author.last_check_time:
                elapsed = (datetime.now() - author.last_check_time).total_seconds()
                if elapsed < author.check_interval:
                    continue

            active_task = db.execute(
                select(XDownloadTask).where(
                    XDownloadTask.x_author_id == author.id,
                    XDownloadTask.status.in_(ACTIVE_X_TASK_STATUSES),
                )
            )
            if active_task.scalar_one_or_none():
                continue

            task = create_x_download_task(author)
            db.add(task)
            db.flush()

            download_x_profile.delay(task.id)
            results.append({"author": author.username, "task_id": task.id})
            logger.info(f"X 订阅检查: 为 @{author.username} 创建下载任务 {task.id}")

        db.commit()
        return {"success": True, "checked": len(results), "results": results}
    except Exception as e:
        db.rollback()
        logger.error(f"X 订阅检查失败: {e}")
        return {"success": False, "error": str(e)}
    finally:
        db.close()
