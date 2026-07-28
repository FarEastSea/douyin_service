"""X/Twitter Cookie 读取与临时文件物化服务。"""

from __future__ import annotations

import os
import tempfile
from typing import Optional

from sqlalchemy import select

from app.core import redis_client
from app.core.config import settings
from app.models.models import SystemConfig
from app.services.x_downloader import convert_cookie_header_to_netscape

X_COOKIE_CONFIG_KEY = "x_cookie"


def get_x_cookie_value(db) -> Optional[str]:
    """读取 X Cookie，网页值优先，其次才使用环境配置。"""
    cached_cookie = redis_client.get_x_cookie()
    if cached_cookie and cached_cookie.strip():
        return cached_cookie.strip()

    config = db.execute(
        select(SystemConfig).where(SystemConfig.key == X_COOKIE_CONFIG_KEY)
    ).scalar_one_or_none()
    if not config or not config.value or not config.value.strip():
        return settings.X_COOKIE.strip() if settings.X_COOKIE and settings.X_COOKIE.strip() else None

    cookie = config.value.strip()
    redis_client.set_x_cookie(cookie)
    return cookie


def materialize_x_cookie_file(db, task_id: int | None = None) -> tuple[Optional[str], bool]:
    """将 X Cookie 写入临时 Netscape 文件，返回路径及是否需要清理。"""
    cookie = get_x_cookie_value(db)
    if not cookie:
        if settings.X_COOKIE_FILE and os.path.isfile(settings.X_COOKIE_FILE):
            return settings.X_COOKIE_FILE, False
        return None, False

    cookie_dir = os.path.join(settings.X_DOWNLOAD_DIR, ".tmp")
    os.makedirs(cookie_dir, exist_ok=True)

    file_prefix = f"x_cookie_{task_id}_" if task_id is not None else "x_cookie_"
    fd, cookie_path = tempfile.mkstemp(
        suffix=".txt",
        prefix=file_prefix,
        dir=cookie_dir,
    )

    netscape_cookie = convert_cookie_header_to_netscape(cookie)
    with os.fdopen(fd, "w", encoding="utf-8") as cookie_file:
        cookie_file.write(netscape_cookie)

    return cookie_path, True


def cleanup_x_cookie_file(cookie_path: Optional[str], managed: bool) -> None:
    """删除当前任务创建的临时 Cookie 文件。"""
    if not managed or not cookie_path:
        return

    try:
        os.remove(cookie_path)
    except OSError:
        pass