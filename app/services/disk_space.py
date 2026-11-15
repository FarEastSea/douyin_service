"""下载根目录所在磁盘的空间读取；阈值在每次调用时取最新网页配置。"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

GIGABYTE = 1024 ** 3


def disk_usage(config: Any) -> dict[str, Any]:
    """返回下载根目录所在磁盘的容量。读取失败时明确标记为不可用，不伪装成充足。"""
    try:
        threshold = max(0.0, float(config.DISK_WARN_FREE_GB)) * GIGABYTE
    except (TypeError, ValueError):
        threshold = 0.0
    unavailable = {
        "available": False, "total_bytes": None, "used_bytes": None, "free_bytes": None,
        "used_percent": None, "warn_free_bytes": int(threshold), "low": False,
    }
    try:
        usage = shutil.disk_usage(Path(config.DOWNLOAD_ROOT).expanduser())
    except (OSError, ValueError):
        return unavailable
    if usage.total <= 0:
        return unavailable
    return {
        "available": True,
        "total_bytes": int(usage.total),
        "used_bytes": int(usage.used),
        "free_bytes": int(usage.free),
        "used_percent": round(usage.used / usage.total * 100, 1),
        "warn_free_bytes": int(threshold),
        "low": bool(threshold and usage.free < threshold),
    }


def format_bytes(value: int | float | None) -> str:
    if value is None:
        return "未知"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in {"B", "KB"} else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
