"""每日摘要：汇总指定日期的入库、订阅检查、当前队列与磁盘空间。

与数据洞察页共用 app.services.insights 的统计口径；这里使用同步会话，供 Celery
定时任务和“立即发送”接口调用。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.services import insights
from app.services.disk_space import disk_usage, format_bytes
from app.services.platform_registry import platform_registry


def _platform_names() -> dict[str, str]:
    return {definition.id: definition.name for definition in platform_registry.list()}


def build_daily_digest(db: Session, config: Any, day: date) -> dict[str, Any]:
    since = datetime.combine(day, time.min)
    until = since + timedelta(days=1)

    trend = insights.shape_trend(db.execute(insights.trend_statement(since, until)).all(), 1, day)[0]
    statements = insights.task_status_statements()
    tasks = insights.shape_task_status(
        db.execute(statements["douyin"]).all(),
        db.execute(statements["x"]).all(),
        db.execute(statements["generic"]).all(),
    )
    subscriptions = insights.shape_subscriptions(
        (row for row in db.execute(insights.douyin_report_statement(since)).all()
         if str(row[0])[:10] == day.isoformat()),
        db.execute(insights.platform_run_statement(since, dialect=db.get_bind().dialect.name)).all(),
        1, day,
    )
    disk = disk_usage(config)

    names = _platform_names()
    lines = []
    if trend["files"]:
        lines.append(f"入库：{trend['files']:,} 个文件，{format_bytes(trend['bytes'])}")
        breakdown = sorted(trend["by_platform"].items(), key=lambda item: -item[1]["files"])
        lines.append("　" + " · ".join(
            f"{names.get(platform, platform)} {value['files']:,}" for platform, value in breakdown
        ))
    else:
        lines.append("入库：没有新文件")

    if subscriptions["runs"] or subscriptions["saved_files"]:
        parts = []
        if subscriptions["runs"]:
            parts.append(f"抖音检查 {subscriptions['runs']} 次，发现新作品 {subscriptions['new_works']:,} 个")
        if subscriptions["attention"]:
            parts.append(f"{subscriptions['attention']} 位作者需核对")
        if subscriptions["saved_files"]:
            parts.append(f"分片订阅保存 {subscriptions['saved_files']:,} 个文件")
        lines.append("订阅：" + "，".join(parts))
    else:
        lines.append("订阅：没有检查记录")

    totals = tasks["totals"]
    lines.append(
        f"当前队列：下载中 {totals.get('downloading', 0):,} · "
        f"等待 {totals.get('pending', 0):,} · 失败 {totals.get('failed', 0):,}"
    )
    if disk["available"]:
        warning = "，已低于预警线" if disk["low"] else ""
        lines.append(f"磁盘：剩余 {format_bytes(disk['free_bytes'])}（已用 {disk['used_percent']}%）{warning}")
    else:
        lines.append("磁盘：下载目录空间读取失败")

    return {
        "title": f"媒体工作台每日摘要 · {day.isoformat()}",
        "body": "\n".join(lines),
        "data": {
            "date": day.isoformat(), "ingest": trend, "subscriptions": subscriptions,
            "tasks": totals, "disk": disk,
        },
    }
