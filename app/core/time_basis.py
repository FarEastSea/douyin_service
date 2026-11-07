"""本地 naive 时间与 UTC naive 时间的换算，不假设服务器所在时区。

旧表以 datetime.now() 写入服务器本地时间，Instagram 相关表以 UTC 写入。
对 naive 时间调用 astimezone() 时，Python 按当前服务器时区（含夏令时规则）解释，
因此无论服务器是 UTC 还是东八区，都能得到正确的绝对时间。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta


def local_to_utc(value: datetime | None) -> datetime | None:
    """服务器本地 naive 时间 → UTC naive 时间。"""
    if value is None:
        return None
    return value.astimezone(UTC).replace(tzinfo=None)


def local_iso(value: datetime | None) -> str | None:
    """输出带服务器时区偏移的 ISO 时间，浏览器无论在哪个时区都能正确显示。"""
    if value is None:
        return None
    return value.astimezone().isoformat()


def local_offset() -> timedelta:
    """服务器当前相对 UTC 的偏移，用于在 SQL 中把 UTC 列换算成本地时间比较。"""
    return datetime.now().astimezone().utcoffset() or timedelta(0)
