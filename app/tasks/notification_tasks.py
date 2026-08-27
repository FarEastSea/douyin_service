"""通知投递任务。

外部 Webhook、邮件等网络请求不占用下载任务的执行路径；失败渠道单独有限重试，
已经成功的渠道不会重复投递。
"""

from __future__ import annotations

import json
from typing import Any

from app.core import redis_client
from app.services.notifications import send_notification
from app.tasks.celery_app import celery_app


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
