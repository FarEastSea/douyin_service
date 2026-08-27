"""作品采集结果的统一持久化规则。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.models.models import Work, WorkStatsSnapshot
from app.services.downloader import (
    is_video_work_payload,
    latest_video_url,
    payload_image_urls,
    payload_live_photo_urls,
)


STAT_FIELDS = (
    "digg_count",
    "comment_count",
    "collect_count",
    "share_count",
    "play_count",
)


def _non_negative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _apply_media(work: Work, payload: dict, *, preserve_existing: bool) -> None:
    work_type = "video" if is_video_work_payload(payload) else "images"
    image_urls = payload_image_urls(payload)
    live_photo_urls = payload_live_photo_urls(payload)
    video_url = latest_video_url(payload)
    work.work_type = work_type

    if work_type == "video":
        work.image_count = 0
        if video_url or not preserve_existing:
            work.video_url = video_url
        if not preserve_existing:
            work.image_urls = []
            work.live_photo_urls = []
        return

    if image_urls or not preserve_existing:
        work.image_urls = image_urls
        work.image_count = len(image_urls)
    if live_photo_urls or not preserve_existing:
        work.live_photo_urls = live_photo_urls
    work.video_url = None


def apply_work_payload(
    db: Any,
    work: Work,
    payload: dict,
    *,
    preserve_existing: bool = False,
    source: str = "douyin_web",
) -> bool:
    """更新作品媒体与元数据；统计发生变化时追加一条历史快照。"""
    _apply_media(work, payload, preserve_existing=preserve_existing)

    try:
        create_time = int(payload.get("create_time") or 0)
    except (TypeError, ValueError):
        create_time = 0
    if create_time > 0:
        work.published_at = datetime.fromtimestamp(create_time)

    title = str(payload.get("desc") or "").strip()
    if title:
        work.title = title

    for field in ("cover_url", "music_title", "music_author", "music_url"):
        value = payload.get(field)
        if value or not preserve_existing:
            setattr(work, field, value or None)

    for field in ("duration_ms", "width", "height"):
        value = _non_negative_int(payload.get(field))
        if value is not None or not preserve_existing:
            setattr(work, field, value)

    if "hashtags" in payload:
        work.hashtags = payload.get("hashtags") or []
    work.metadata_schema_version = _non_negative_int(
        payload.get("metadata_schema_version")
    ) or 1
    work.raw_data_version = _non_negative_int(payload.get("raw_data_version")) or 1
    work.metadata_refreshed_at = datetime.now()

    incoming_stats = payload.get("statistics")
    if not isinstance(incoming_stats, dict):
        return False

    merged_stats: dict[str, int | None] = {}
    has_observation = False
    for field in STAT_FIELDS:
        incoming = _non_negative_int(incoming_stats.get(field))
        if incoming is not None:
            has_observation = True
            merged_stats[field] = incoming
        else:
            merged_stats[field] = getattr(work, field)
    if not has_observation:
        return False

    previous = tuple(getattr(work, field) for field in STAT_FIELDS)
    current = tuple(merged_stats[field] for field in STAT_FIELDS)
    if previous == current:
        return False

    snapshot = WorkStatsSnapshot(work=work, source=source, **merged_stats)
    db.add(snapshot)
    for field, value in merged_stats.items():
        setattr(work, field, value)
    return True
