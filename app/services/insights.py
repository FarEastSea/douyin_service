"""只读聚合：媒体库规模、入库趋势、任务结果与订阅产出。

所有数字来自现有下载表；不扫描磁盘文件，也不请求外部平台。语句构造与结果整理
分开，网页接口（异步会话）和每日摘要任务（同步会话）共用同一套口径。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import func, select

from app.models.models import (
    DownloadTask,
    PlatformDownloadTask,
    PlatformSubscriptionRun,
    SubscriptionCheckReport,
    XDownloadTask,
)
from app.services.douyin_errors import ERROR_INFO, classify_stored_task_error
from app.services.media_library import media_query
from app.services.task_scope import visible_platform_tasks

ALLOWED_WINDOWS = (7, 30, 90, 180, 365)
TOP_AUTHOR_LIMIT = 10
FAILURE_REASON_LIMIT = 8
# 抖音失败原因保存在自由文本里，先按文本聚合，再流式分类，避免截断后少算。
LIVE_TYPES = {"live", "live_photo"}


def window_start(days: int, today: date | None = None) -> datetime:
    """窗口含今天在内共 days 天，从当天零点起算。"""
    return datetime.combine((today or date.today()) - timedelta(days=days - 1), time.min)


def _day_key(value: Any) -> str:
    # PostgreSQL 返回 date，SQLite 返回字符串；统一成 YYYY-MM-DD。
    return str(value)[:10]


def _int(value: Any) -> int:
    return int(value or 0)


# ---- 语句 ----

def library_statement():
    media = media_query().subquery()
    return select(
        media.c.platform, media.c.media_type, media.c.library_status,
        func.count().label("files"),
        func.coalesce(func.sum(media.c.file_size), 0).label("bytes"),
    ).group_by(media.c.platform, media.c.media_type, media.c.library_status)


def trend_statement(since: datetime, until: datetime | None = None):
    media = media_query().subquery()
    day = func.date(media.c.completed_at)
    conditions = [media.c.completed_at >= since]
    if until is not None:
        conditions.append(media.c.completed_at < until)
    return select(
        day.label("day"), media.c.platform,
        func.count().label("files"),
        func.coalesce(func.sum(media.c.file_size), 0).label("bytes"),
    ).where(*conditions).group_by(day, media.c.platform)


def top_authors_statement(since: datetime | None = None, limit: int = TOP_AUTHOR_LIMIT):
    media = media_query().subquery()
    total_bytes = func.coalesce(func.sum(media.c.file_size), 0)
    conditions = [media.c.author_name.is_not(None), media.c.author_name != ""]
    if since is not None:
        conditions.append(media.c.completed_at >= since)
    return select(
        media.c.platform, media.c.author_name,
        func.count().label("files"), total_bytes.label("bytes"),
    ).where(*conditions).group_by(media.c.platform, media.c.author_name).order_by(
        total_bytes.desc(), func.count().desc(), media.c.platform, media.c.author_name,
    ).limit(limit)


def task_status_statements() -> dict[str, Any]:
    return {
        "douyin": select(DownloadTask.status, func.count(DownloadTask.id)).group_by(DownloadTask.status),
        "x": select(XDownloadTask.status, func.count(XDownloadTask.id)).group_by(XDownloadTask.status),
        "generic": select(
            PlatformDownloadTask.platform, PlatformDownloadTask.status, func.count(PlatformDownloadTask.id),
        ).where(visible_platform_tasks()).group_by(PlatformDownloadTask.platform, PlatformDownloadTask.status),
    }


def failure_statements() -> dict[str, Any]:
    douyin_count = func.count(DownloadTask.id)
    return {
        "douyin": select(DownloadTask.error_message, douyin_count).where(
            DownloadTask.status == "failed",
        ).group_by(DownloadTask.error_message),
        "x": select(XDownloadTask.error_code, func.count(XDownloadTask.id)).where(
            XDownloadTask.status == "failed",
        ).group_by(XDownloadTask.error_code),
        "generic": select(
            PlatformDownloadTask.platform, PlatformDownloadTask.error_code, func.count(PlatformDownloadTask.id),
        ).where(
            PlatformDownloadTask.status == "failed", visible_platform_tasks(),
        ).group_by(PlatformDownloadTask.platform, PlatformDownloadTask.error_code),
    }


def douyin_report_statement(since: datetime):
    day = func.date(SubscriptionCheckReport.started_at)
    return select(
        day.label("day"),
        func.count(SubscriptionCheckReport.id).label("runs"),
        func.coalesce(func.sum(SubscriptionCheckReport.new_works), 0).label("new_works"),
        func.coalesce(func.sum(
            func.coalesce(SubscriptionCheckReport.failed_authors, 0)
            + func.coalesce(SubscriptionCheckReport.warning_authors, 0)
        ), 0).label("attention"),
    ).where(SubscriptionCheckReport.started_at >= since).group_by(day)


def platform_run_statement(since_local: datetime, *, dialect: str = "postgresql"):
    # 分片运行记录用 UTC 无时区时间保存；按本地日期归并需要先换算窗口起点。
    since_utc = since_local.astimezone(timezone.utc).replace(tzinfo=None)
    column = PlatformSubscriptionRun.started_at
    if dialect == "postgresql":
        # UTC naive → 当前连接本地时区 → 本地日期；聚合后最多每天一行。
        local_time = func.timezone(func.current_setting("TimeZone"), func.timezone("UTC", column))
        day = func.date(local_time)
    elif dialect == "sqlite":
        from app.core.time_basis import local_offset
        day = func.date(column, f"{int(local_offset().total_seconds())} seconds")
    else:
        offset = datetime.now().astimezone().strftime("%z")
        day = func.date(func.convert_tz(column, "+00:00", offset[:3] + ":" + offset[3:]))
    return select(
        day, func.coalesce(func.sum(PlatformSubscriptionRun.saved), 0),
    ).where(
        PlatformSubscriptionRun.started_at >= since_utc, PlatformSubscriptionRun.saved > 0,
    ).group_by(day)


# ---- 结果整理 ----

def shape_library(rows: Iterable[Any]) -> dict[str, Any]:
    platforms: dict[str, dict[str, int]] = {}
    types: dict[str, dict[str, int]] = {}
    totals = {"files": 0, "bytes": 0, "orphaned_files": 0, "orphaned_bytes": 0}
    for platform, media_type, library_status, files, size in rows:
        files, size = _int(files), _int(size)
        kind = "live" if media_type in LIVE_TYPES else str(media_type or "unknown")
        for bucket, key in ((platforms, str(platform)), (types, kind)):
            entry = bucket.setdefault(key, {"files": 0, "bytes": 0})
            entry["files"] += files
            entry["bytes"] += size
        totals["files"] += files
        totals["bytes"] += size
        if library_status == "orphaned":
            totals["orphaned_files"] += files
            totals["orphaned_bytes"] += size

    def ranked(bucket: dict[str, dict[str, int]], name: str) -> list[dict[str, Any]]:
        return sorted(
            ({name: key, **value} for key, value in bucket.items()),
            key=lambda item: (-item["bytes"], -item["files"], item[name]),
        )

    return {**totals, "by_platform": ranked(platforms, "platform"), "by_type": ranked(types, "media_type")}


def shape_trend(rows: Iterable[Any], days: int, today: date | None = None) -> list[dict[str, Any]]:
    end = today or date.today()
    series = {
        (end - timedelta(days=offset)).isoformat(): {"files": 0, "bytes": 0, "by_platform": {}}
        for offset in range(days - 1, -1, -1)
    }
    for day, platform, files, size in rows:
        entry = series.get(_day_key(day))
        if entry is None:
            continue
        files, size = _int(files), _int(size)
        entry["files"] += files
        entry["bytes"] += size
        per_platform = entry["by_platform"].setdefault(str(platform), {"files": 0, "bytes": 0})
        per_platform["files"] += files
        per_platform["bytes"] += size
    return [{"date": key, **value} for key, value in series.items()]


def shape_task_status(douyin: Iterable[Any], x: Iterable[Any], generic: Iterable[Any]) -> dict[str, Any]:
    by_platform: dict[str, dict[str, int]] = {}
    for status, count in douyin:
        by_platform.setdefault("douyin", {})[str(status)] = _int(count)
    for status, count in x:
        by_platform.setdefault("x", {})[str(status)] = _int(count)
    for platform, status, count in generic:
        by_platform.setdefault(str(platform), {})[str(status)] = _int(count)
    totals: dict[str, int] = {}
    for statuses in by_platform.values():
        for status, count in statuses.items():
            totals[status] = totals.get(status, 0) + count
    finished = totals.get("completed", 0) + totals.get("failed", 0)
    return {
        "totals": totals,
        "by_platform": [
            {"platform": platform, "statuses": statuses, "total": sum(statuses.values())}
            for platform, statuses in sorted(by_platform.items(), key=lambda item: -sum(item[1].values()))
        ],
        # 只在已有终态任务时给出成功率；没有样本时返回 None，避免把“无数据”显示成 0% 或 100%。
        "success_rate": round(totals.get("completed", 0) / finished * 100, 1) if finished else None,
    }


def shape_failures(
    douyin: Iterable[Any], x: Iterable[Any], generic: Iterable[Any], limit: int = FAILURE_REASON_LIMIT,
    *, douyin_classified: bool = False,
) -> list[dict[str, Any]]:
    reasons: dict[tuple[str, str], int] = {}
    for message, count in douyin:
        code = message if douyin_classified else classify_stored_task_error(message).get("error_code") or "unknown"
        reasons[("douyin", str(code))] = reasons.get(("douyin", str(code)), 0) + _int(count)
    for code, count in x:
        key = ("x", str(code or "unknown"))
        reasons[key] = reasons.get(key, 0) + _int(count)
    for platform, code, count in generic:
        key = (str(platform), str(code or "unknown"))
        reasons[key] = reasons.get(key, 0) + _int(count)
    ranked = sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [
        {
            "platform": platform, "code": code, "count": count,
            "label": _failure_label(platform, code),
        }
        for (platform, code), count in ranked
    ]


def _failure_label(platform: str, code: str) -> str:
    if code == "unknown":
        return "未分类的失败"
    if platform == "douyin":
        info = ERROR_INFO.get(code)
        if info:
            return info.message.rstrip("。")
        return "任务执行错误" if code == "task_error" else code
    return code


def shape_subscriptions(
    reports: Iterable[Any], platform_runs: Iterable[Any], days: int, today: date | None = None,
) -> dict[str, Any]:
    end = today or date.today()
    series = {
        (end - timedelta(days=offset)).isoformat(): {"runs": 0, "new_works": 0, "attention": 0, "saved_files": 0}
        for offset in range(days - 1, -1, -1)
    }
    for day, runs, new_works, attention in reports:
        entry = series.get(_day_key(day))
        if entry is None:
            continue
        entry["runs"] += _int(runs)
        entry["new_works"] += _int(new_works)
        entry["attention"] += _int(attention)
    for day, saved in platform_runs:
        if day is None:
            continue
        entry = series.get(_day_key(day))
        if entry is not None:
            entry["saved_files"] += _int(saved)
    daily = [{"date": key, **value} for key, value in series.items()]
    return {
        "daily": daily,
        "runs": sum(item["runs"] for item in daily),
        "new_works": sum(item["new_works"] for item in daily),
        "attention": sum(item["attention"] for item in daily),
        "saved_files": sum(item["saved_files"] for item in daily),
    }


def shape_period(rows: Iterable[Any], days: int, today: date | None = None) -> dict[str, Any]:
    """从覆盖两个周期的按日结果里，分别汇总当前周期与上一周期，省去两次单独的聚合查询。"""
    boundary = ((today or date.today()) - timedelta(days=days - 1)).isoformat()
    totals = {"files": 0, "bytes": 0, "previous_files": 0, "previous_bytes": 0}
    for day, _platform, files, size in rows:
        prefix = "" if _day_key(day) >= boundary else "previous_"
        totals[prefix + "files"] += _int(files)
        totals[prefix + "bytes"] += _int(size)
    return totals


# ---- 组合 ----
# 全时段统计与窗口统计分开计算并分别缓存（见 app/services/insights_cache.py）：
# 前者需要扫描全部媒体记录，四个时间窗口共用同一份结果。

async def build_overall(db) -> dict[str, Any]:
    """与时间窗口无关的统计：媒体库规模、作者排行、任务结果。同一会话顺序执行。"""
    library = shape_library((await db.execute(library_statement())).all())
    top_authors = [
        {"platform": str(platform), "author_name": author_name, "files": _int(files), "bytes": _int(size)}
        for platform, author_name, files, size in (await db.execute(top_authors_statement())).all()
    ]
    status_statements = task_status_statements()
    tasks = shape_task_status(
        (await db.execute(status_statements["douyin"])).all(),
        (await db.execute(status_statements["x"])).all(),
        (await db.execute(status_statements["generic"])).all(),
    )
    failure = failure_statements()
    errors = await db.stream(failure["douyin"].execution_options(yield_per=500))
    douyin_counts: dict[str, int] = {}
    try:
        async for message, count in errors:
            code = classify_stored_task_error(message).get("error_code") or "unknown"
            douyin_counts[code] = douyin_counts.get(code, 0) + _int(count)
    finally:
        await errors.close()
    tasks["failure_reasons"] = shape_failures(
        douyin_counts.items(),
        (await db.execute(failure["x"])).all(),
        (await db.execute(failure["generic"])).all(),
        douyin_classified=True,
    )
    return {"library": library, "top_authors": top_authors, "tasks": tasks}


async def build_window(db, days: int) -> dict[str, Any]:
    """窗口内的入库趋势、周期对比与订阅产出；只读取最近两个周期的记录。"""
    today = date.today()
    since = window_start(days, today)
    rows = (await db.execute(trend_statement(since - timedelta(days=days)))).all()
    subscriptions = shape_subscriptions(
        (await db.execute(douyin_report_statement(since))).all(),
        (await db.execute(platform_run_statement(since, dialect=db.get_bind().dialect.name))).all(),
        days, today,
    )
    return {
        "days": days,
        "trend": shape_trend(rows, days, today),
        "period": shape_period(rows, days, today),
        "subscriptions": subscriptions,
    }
