"""Subscription report formatting shared by legacy and unified APIs."""

import json
from datetime import datetime, timedelta


def douyin_report_payload(
    report, newest_id: int | None, global_interval: int, include_details: bool = True
):
    details = []
    if include_details:
        try:
            details = json.loads(report.details_json or "[]")
        except (TypeError, ValueError):
            details = []
        details = (
            [item for item in details if isinstance(item, dict)]
            if isinstance(details, list)
            else []
        )
    display_status = report.status
    display_summary = report.summary
    if display_status == "running" and report.id != newest_id:
        display_status = "interrupted"
        display_summary = "该批次未正常写入结束状态，后续批次已经接管续检"
    elif (
        display_status == "running"
        and report.started_at
        and (datetime.now() - report.started_at).total_seconds() > 2100
    ):
        display_status = "interrupted"
        display_summary = (
            "任务超过 35 分钟仍未结束，可能被 Worker 中断；下一轮会继续检查未处理作者"
        )
    return {
        "id": report.id,
        "key": f"douyin:{report.id}",
        "platform": "douyin",
        "celery_task_id": report.celery_task_id,
        "trigger_type": report.trigger_type,
        "status": display_status,
        "total_authors": report.total_authors or 0,
        "due_authors": report.due_authors or 0,
        "checked_authors": report.checked_authors or 0,
        "success_authors": report.success_authors or 0,
        "new_works": report.new_works or 0,
        "warning_authors": report.warning_authors or 0,
        "failed_authors": report.failed_authors or 0,
        "skipped_authors": report.skipped_authors or 0,
        "remaining_authors": report.remaining_authors or 0,
        "summary": display_summary,
        "details": details,
        "started_at": report.started_at.isoformat() if report.started_at else None,
        "finished_at": report.finished_at.isoformat() if report.finished_at else None,
        "global_interval_seconds": global_interval,
        "expected_next_cycle_at": (
            (report.started_at + timedelta(seconds=global_interval)).isoformat()
            if report.started_at and report.trigger_type == "auto"
            else None
        ),
    }
