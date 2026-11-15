"""订阅清单的导出与导入，用于迁移或重建实例。

备份只包含作者标识、名称、订阅开关和检查间隔，不包含 Cookie、代理或任何凭据。
导入不请求外部平台：只按稳定标识补建缺失的作者记录，已有作者保持原样。
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Author, PlatformAuthor, XAuthor
from app.services.douyin_source import build_author_profile_url
from app.services.x_profile import normalize_x_profile_url, parse_x_username
from app.services.x_task_service import create_x_author

BACKUP_FORMAT = "media-workbench-subscriptions"
BACKUP_VERSION = 1
MAX_IMPORT_ITEMS = 5000
MAX_CHECK_INTERVAL = 7 * 24 * 3600
X_MIN_CHECK_INTERVAL = 3600
_SEC_UID_PATTERN = re.compile(r"[A-Za-z0-9_\-.]{8,255}")
_LOOKUP_CHUNK = 500


class BackupFormatError(ValueError):
    """备份文件结构不符合预期。"""


async def export_subscriptions(db: AsyncSession) -> dict[str, Any]:
    douyin = (await db.execute(
        select(Author.sec_uid, Author.nickname, Author.is_subscribed, Author.check_interval)
        .order_by(Author.id)
    )).all()
    x_authors = (await db.execute(
        select(XAuthor.username, XAuthor.display_name, XAuthor.is_subscribed, XAuthor.check_interval)
        .order_by(XAuthor.id)
    )).all()
    platform_authors = (await db.execute(
        select(
            PlatformAuthor.platform, PlatformAuthor.external_user_id, PlatformAuthor.username,
            PlatformAuthor.nickname, PlatformAuthor.profile_url, PlatformAuthor.is_subscribed,
            PlatformAuthor.check_interval,
        ).order_by(PlatformAuthor.platform, PlatformAuthor.id)
    )).all()
    return {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "douyin": [
            {"sec_uid": row.sec_uid, "nickname": row.nickname,
             "is_subscribed": bool(row.is_subscribed), "check_interval": row.check_interval}
            for row in douyin
        ],
        "x": [
            {"username": row.username, "display_name": row.display_name,
             "is_subscribed": bool(row.is_subscribed), "check_interval": row.check_interval}
            for row in x_authors
        ],
        "platform_authors": [
            {"platform": row.platform, "external_user_id": row.external_user_id,
             "username": row.username, "nickname": row.nickname, "profile_url": row.profile_url,
             "is_subscribed": bool(row.is_subscribed), "check_interval": row.check_interval}
            for row in platform_authors
        ],
    }


def _entries(backup: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = backup.get(key, [])
    if not isinstance(value, list):
        raise BackupFormatError(f"备份中的 {key} 不是列表")
    return [item for item in value if isinstance(item, dict)]


def _interval(value: Any, default: int, minimum: int) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(MAX_CHECK_INTERVAL, seconds))


def _clean_name(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text[:255] or None


async def _existing(db: AsyncSession, column, values: list[str]) -> set[str]:
    found: set[str] = set()
    for start in range(0, len(values), _LOOKUP_CHUNK):
        chunk = values[start:start + _LOOKUP_CHUNK]
        found.update((await db.execute(select(column).where(column.in_(chunk)))).scalars().all())
    return found


async def import_subscriptions(
    db: AsyncSession, backup: dict[str, Any], *, restore_subscriptions: bool, dry_run: bool,
    douyin_min_interval: int, douyin_default_interval: int,
) -> dict[str, Any]:
    """调用方负责提交。dry_run 只统计，不写入。"""
    if not isinstance(backup, dict) or backup.get("format") != BACKUP_FORMAT:
        raise BackupFormatError("这不是媒体工作台导出的订阅备份文件")
    if backup.get("version") != BACKUP_VERSION:
        raise BackupFormatError(f"暂不支持版本为 {backup.get('version')} 的备份文件")
    douyin_entries = _entries(backup, "douyin")
    x_entries = _entries(backup, "x")
    platform_entries = _entries(backup, "platform_authors")
    if len(douyin_entries) + len(x_entries) + len(platform_entries) > MAX_IMPORT_ITEMS:
        raise BackupFormatError(f"单次最多导入 {MAX_IMPORT_ITEMS} 位作者，请拆分备份文件")

    douyin_result = {"created": 0, "existing": 0, "invalid": 0}
    candidates: dict[str, dict[str, Any]] = {}
    for entry in douyin_entries:
        sec_uid = str(entry.get("sec_uid") or "").strip()
        if not _SEC_UID_PATTERN.fullmatch(sec_uid):
            douyin_result["invalid"] += 1
            continue
        candidates.setdefault(sec_uid, entry)
    known = await _existing(db, Author.sec_uid, list(candidates))
    for sec_uid, entry in candidates.items():
        if sec_uid in known:
            douyin_result["existing"] += 1
            continue
        douyin_result["created"] += 1
        if dry_run:
            continue
        db.add(Author(
            sec_uid=sec_uid,
            nickname=_clean_name(entry.get("nickname")),
            share_url=build_author_profile_url(sec_uid),
            is_subscribed=restore_subscriptions and entry.get("is_subscribed") is True,
            check_interval=_interval(entry.get("check_interval"), douyin_default_interval, douyin_min_interval),
        ))

    x_result = {"created": 0, "existing": 0, "invalid": 0}
    x_candidates: dict[str, dict[str, Any]] = {}
    for entry in x_entries:
        try:
            username = parse_x_username(str(entry.get("username") or ""))
        except ValueError:
            x_result["invalid"] += 1
            continue
        x_candidates.setdefault(username.lower(), {**entry, "username": username})
    known_x = {name.lower() for name in await _existing_x(db, list(x_candidates))}
    for lowered, entry in x_candidates.items():
        if lowered in known_x:
            x_result["existing"] += 1
            continue
        x_result["created"] += 1
        if dry_run:
            continue
        username = entry["username"]
        author = create_x_author(
            username, normalize_x_profile_url(username, username),
            is_subscribed=restore_subscriptions and entry.get("is_subscribed") is True,
            check_interval=_interval(entry.get("check_interval"), X_MIN_CHECK_INTERVAL, X_MIN_CHECK_INTERVAL),
        )
        display_name = _clean_name(entry.get("display_name"))
        if display_name:
            author.display_name = display_name
        db.add(author)

    # 其他平台的订阅依赖各自的会话、分片断点与内容开关，不能只靠一行记录重建。
    unsupported: dict[str, int] = {}
    for entry in platform_entries:
        platform = str(entry.get("platform") or "unknown")[:32]
        unsupported[platform] = unsupported.get(platform, 0) + 1

    if not dry_run:
        await db.flush()
    return {"dry_run": dry_run, "douyin": douyin_result, "x": x_result, "unsupported": unsupported}


async def _existing_x(db: AsyncSession, lowered_usernames: list[str]) -> set[str]:
    """X 用户名不区分大小写；按小写比较，避免导入出仅大小写不同的重复作者。"""
    from sqlalchemy import func

    found: set[str] = set()
    for start in range(0, len(lowered_usernames), _LOOKUP_CHUNK):
        chunk = lowered_usernames[start:start + _LOOKUP_CHUNK]
        found.update((await db.execute(
            select(XAuthor.username).where(func.lower(XAuthor.username).in_(chunk))
        )).scalars().all())
    return found
