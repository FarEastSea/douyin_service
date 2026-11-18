"""统一任务列表、统计和批量操作的业务范围。"""

from sqlalchemy import and_, or_

from app.models.models import PlatformDownloadTask
from app.services.platform_registry import platform_registry


def visible_platform_tasks(platform: str = ""):
    """Instagram 主页任务是内部扫描，不属于面向用户的作品下载任务。"""
    registered = {item.id for item in platform_registry.list()} - {"douyin", "x"}
    platforms = registered & {platform} if platform else registered
    return and_(
        PlatformDownloadTask.platform.in_(platforms),
        or_(PlatformDownloadTask.platform != "instagram", PlatformDownloadTask.source_type == "work"),
    )
