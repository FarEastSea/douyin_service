"""Normalize trusted fields from downloader sidecar metadata.

The raw JSON is never persisted or returned by the API: downloader sidecars may
contain cookies, headers, or extractor internals.  Only the explicit allow-list
below is copied to media records.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping, Optional


@dataclass(frozen=True, slots=True)
class NormalizedMediaMetadata:
    title: Optional[str] = None
    author_name: Optional[str] = None
    published_at: Optional[datetime] = None
    cover_url: Optional[str] = None
    duration_ms: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    view_count: Optional[int] = None
    like_count: Optional[int] = None
    comment_count: Optional[int] = None
    share_count: Optional[int] = None

    def as_model_values(self) -> dict[str, Any]:
        return {
            item.name: value
            for item in fields(self)
            if (value := getattr(self, item.name)) is not None
        }


def _first(data: Mapping[str, Any], *paths: str) -> Any:
    for path in paths:
        value: Any = data
        for part in path.split("."):
            if not isinstance(value, Mapping):
                value = None
                break
            value = value.get(part)
        if value not in (None, "", [], {}):
            return value
    return None


def _text(value: Any, limit: int = 1000) -> Optional[str]:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    normalized = str(value).strip()
    return normalized[:limit] if normalized else None


def _integer(value: Any) -> Optional[int]:
    try:
        return max(0, int(float(value))) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _published_at(data: Mapping[str, Any]) -> Optional[datetime]:
    timestamp = _first(data, "timestamp", "release_timestamp", "date_timestamp", "published_at")
    try:
        if timestamp is not None:
            number = float(timestamp)
            if number > 10_000_000_000:
                number /= 1000
            return datetime.fromtimestamp(number, timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    value = _text(_first(data, "upload_date", "date", "published_at"), 40)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (
            parsed.astimezone(timezone.utc).replace(tzinfo=None)
            if parsed.tzinfo else parsed
        )
    except ValueError:
        pass
    for pattern in ("%Y%m%d", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:19], pattern)
        except ValueError:
            continue
    return None


def normalize_media_metadata(
    data: Mapping[str, Any] | None,
    *,
    fallback_title: str | None = None,
    fallback_author: str | None = None,
) -> NormalizedMediaMetadata:
    source = data if isinstance(data, Mapping) else {}
    duration = _first(source, "duration", "duration_seconds")
    duration_ms = _integer(_first(source, "duration_ms"))
    if duration_ms is None and duration is not None:
        try:
            duration_ms = max(0, int(float(duration) * 1000))
        except (TypeError, ValueError, OverflowError):
            duration_ms = None
    return NormalizedMediaMetadata(
        title=_text(_first(source, "title", "description", "content", "text")) or _text(fallback_title),
        author_name=_text(_first(
            source, "uploader", "uploader_id", "channel", "channel_id", "username",
            "author.name", "author.nickname", "user.name", "user.nickname",
        ), 255) or _text(fallback_author, 255),
        published_at=_published_at(source),
        cover_url=_text(_first(source, "thumbnail", "cover", "cover_url"), 4096),
        duration_ms=duration_ms,
        width=_integer(_first(source, "width", "dimensions.width")),
        height=_integer(_first(source, "height", "dimensions.height")),
        view_count=_integer(_first(source, "view_count", "play_count", "views")),
        like_count=_integer(_first(source, "like_count", "favorite_count", "likes")),
        comment_count=_integer(_first(source, "comment_count", "comments")),
        share_count=_integer(_first(source, "repost_count", "share_count", "retweet_count", "shares")),
    )


def metadata_for_media(
    file_path: str,
    *,
    fallback_title: str | None = None,
    fallback_author: str | None = None,
    fallback_data: Mapping[str, Any] | None = None,
) -> NormalizedMediaMetadata:
    """Read a bounded adjacent sidecar produced by gallery-dl or yt-dlp."""
    path = Path(file_path)
    candidates = (
        path.with_suffix(path.suffix + ".json"),
        path.with_suffix(".info.json"),
        path.with_suffix(".json"),
    )
    for candidate in candidates:
        try:
            if not candidate.is_file() or candidate.stat().st_size > 5 * 1024 * 1024:
                continue
            parsed = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(parsed, Mapping):
                return normalize_media_metadata(
                    parsed, fallback_title=fallback_title, fallback_author=fallback_author,
                )
        except (OSError, UnicodeDecodeError, ValueError):
            continue
    return normalize_media_metadata(
        fallback_data, fallback_title=fallback_title, fallback_author=fallback_author,
    )


def fill_missing_media_metadata(record: Any, metadata: NormalizedMediaMetadata) -> bool:
    """Fill only absent normalized fields; never overwrite existing history."""
    changed = False
    for name, value in metadata.as_model_values().items():
        if getattr(record, name, None) is None:
            setattr(record, name, value)
            changed = True
    return changed
