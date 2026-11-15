"""通知投递任务。

外部 Webhook、邮件等网络请求不占用下载任务的执行路径；失败渠道单独有限重试，
已经成功的渠道不会重复投递。
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging
from typing import Any

from app.core import redis_client
from app.core.config import settings
from app.services.disk_space import disk_usage, format_bytes
from app.services.notifications import send_notification
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

LOW_DISK_ALERT_KEY = "notification:low-disk:alerted"
# 空间持续不足时每 6 小时最多提醒一次；恢复到预警线以上后清除标记，下次不足立即提醒。
LOW_DISK_REALERT_SECONDS = 6 * 3600
DAILY_DIGEST_KEY_PREFIX = "notification:daily-digest:"
DAILY_DIGEST_MARK_SECONDS = 36 * 3600
# 发送期间先占用一个短期标记：进程在发送途中被终止时，标记很快过期，当天仍会补发。
DAILY_DIGEST_SENDING_SECONDS = 15 * 60


@celery_app.task(
    bind=True,
    name="app.tasks.notification_tasks.deliver_notification",
    max_retries=2,
)
def deliver_notification(
    self,
    event: str,
    title: str,
    body: str,
    level: str = "info",
    metadata: dict[str, Any] | None = None,
    channels: list[str] | None = None,
    dedupe_key: str = "",
    force: bool = False,
):
    result = send_notification(
        event,
        title,
        body,
        level=level,
        metadata=metadata,
        channels=channels,
        dedupe_key=dedupe_key,
        force=force,
    )
    failed_channels = [
        name
        for name, channel_result in result.get("channels", {}).items()
        if not channel_result.get("success") and not channel_result.get("skipped")
    ]
    if failed_channels and self.request.retries < self.max_retries:
        retry_number = self.request.retries + 1
        raise self.retry(
            kwargs={
                "event": event,
                "title": title,
                "body": body,
                "level": level,
                "metadata": metadata,
                "channels": failed_channels,
                "dedupe_key": "",
                "force": True,
            },
            countdown=30 * retry_number,
        )

    if not result.get("skipped"):
        try:
            redis_client.append_activity_log(
                "info" if not failed_channels else "warning",
                "notification",
                f"通知投递完成：成功 {int(result.get('sent', 0))} 个渠道",
                json.dumps(result.get("channels", {}), ensure_ascii=False),
            )
        except Exception:
            # 活动日志是可观测辅助，不应改写已完成的外部投递结果。
            pass
    return result


def _release_mark(key: str) -> None:
    try:
        redis_client.redis_client.delete(key)
    except Exception as exc:
        logger.warning("通知标记清除失败，下一周期重试: %s", type(exc).__name__)


@celery_app.task(name="app.tasks.notification_tasks.cleanup_thumbnails")
def cleanup_thumbnails():
    from app.services.media_library import PLATFORMS, media_root
    from app.services.media_thumbnails import cleanup_thumbnail_cache
    current = settings.snapshot()
    removed = 0
    for platform in sorted(PLATFORMS):
        try:
            removed += cleanup_thumbnail_cache(media_root(current, platform))
        except OSError:
            logger.exception("平台 %s 的缩略图缓存清理失败，下次继续", platform)
    return {"removed": removed}


@celery_app.task(name="app.tasks.notification_tasks.check_disk_space")
def check_disk_space():
    """定时读取下载磁盘剩余空间；预警线取自最新网页配置，0 表示不预警。"""
    usage = disk_usage(settings.snapshot())
    if not usage["available"] or not usage["low"]:
        _release_mark(LOW_DISK_ALERT_KEY)
        return {"low": False, "available": usage["available"]}
    try:
        first_alert = bool(redis_client.redis_client.set(
            LOW_DISK_ALERT_KEY, "1", nx=True, ex=LOW_DISK_REALERT_SECONDS,
        ))
    except Exception as exc:
        # 无法确认是否已经提醒过时宁可不发，避免每 10 分钟重复打扰。
        logger.warning("磁盘预警去重状态读取失败: %s", type(exc).__name__)
        return {"low": True, "notified": False, "reason": "dedupe_unavailable"}
    if not first_alert:
        return {"low": True, "notified": False, "reason": "already_alerted"}

    detail = (
        f"下载目录所在磁盘剩余 {format_bytes(usage['free_bytes'])}"
        f"（已用 {usage['used_percent']}%），低于预警线 {format_bytes(usage['warn_free_bytes'])}。"
    )
    try:
        redis_client.append_activity_log(
            "warning", "system", "下载磁盘剩余空间不足", detail, event_code="low_disk_space",
        )
    except Exception as exc:
        logger.warning("磁盘预警活动日志写入失败: %s", type(exc).__name__)
    try:
        deliver_notification.apply_async(kwargs={
            "event": "low_disk",
            "title": "下载磁盘剩余空间不足",
            "body": detail + "\n请清理不需要的媒体或扩容；空间耗尽会导致下载失败。",
            "level": "warning",
        })
    except Exception as exc:
        # 入队失败时撤销标记，下一周期重新尝试提醒。
        logger.warning("磁盘预警通知入队失败: %s", type(exc).__name__)
        _release_mark(LOW_DISK_ALERT_KEY)
        return {"low": True, "notified": False, "reason": "enqueue_failed"}
    return {"low": True, "notified": True}


def send_digest(day=None, *, force: bool = False) -> dict[str, Any]:
    """生成并发送指定日期（默认昨天）的摘要；force 用于网页“立即发送”。"""
    from app.models.database import get_sync_db
    from app.services.daily_digest import build_daily_digest

    target = day or (datetime.now().date() - timedelta(days=1))
    db = get_sync_db()
    try:
        digest = build_daily_digest(db, settings.snapshot(), target)
    finally:
        db.close()
    result = send_notification(
        "daily_digest", digest["title"], digest["body"], level="info", force=force,
    )
    return {"digest": digest, "result": result}


@celery_app.task(name="app.tasks.notification_tasks.send_daily_digest")
def send_daily_digest():
    """每小时检查一次：到达网页配置的发送时间且当天尚未发送时，汇总前一天的情况。"""
    config = settings.snapshot()
    if not (config.NOTIFY_ENABLED and config.NOTIFY_DAILY_DIGEST):
        return {"sent": 0, "skipped": True, "reason": "disabled"}
    now = datetime.now()
    if now.hour < int(config.NOTIFY_DAILY_DIGEST_HOUR):
        return {"sent": 0, "skipped": True, "reason": "not_due"}
    key = f"{DAILY_DIGEST_KEY_PREFIX}{now.date().isoformat()}"
    try:
        if not redis_client.redis_client.set(key, "sending", nx=True, ex=DAILY_DIGEST_SENDING_SECONDS):
            return {"sent": 0, "skipped": True, "reason": "already_sent"}
    except Exception as exc:
        logger.warning("每日摘要去重状态读取失败: %s", type(exc).__name__)
        return {"sent": 0, "skipped": True, "reason": "dedupe_unavailable"}
    try:
        result = send_digest()["result"]
    except Exception:
        _release_mark(key)
        raise
    attempted = [
        name for name, item in result.get("channels", {}).items() if not item.get("skipped")
    ]
    if attempted and not result.get("sent"):
        # 已启用的渠道全部投递失败时撤销标记，下一小时再试；没有启用任何渠道则当天不再重复尝试。
        _release_mark(key)
    else:
        try:
            redis_client.redis_client.set(key, "sent", ex=DAILY_DIGEST_MARK_SECONDS)
        except Exception as exc:
            # 延长失败时短期标记过期后可能重发一次，好过漏发。
            logger.warning("每日摘要完成标记写入失败: %s", type(exc).__name__)
    try:
        redis_client.append_activity_log(
            "info" if result.get("sent") else "warning", "notification",
            f"每日摘要投递完成：成功 {int(result.get('sent', 0))} 个渠道",
            json.dumps(result.get("channels", {}), ensure_ascii=False),
        )
    except Exception as exc:
        logger.warning("每日摘要活动日志写入失败: %s", type(exc).__name__)
    return result
