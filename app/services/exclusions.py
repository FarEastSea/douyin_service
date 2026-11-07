"""排除项目维护：汇总删除后选择不再下载的项目，并支持恢复下载。

来源：
- 抖音：作品级排除（works.is_excluded）与图集单张排除（works.excluded_file_indices）。
- 其他平台：媒体库删除时登记的 MediaRedownloadBlock（Instagram 媒体 ID，或 gallery-dl / yt-dlp 归档键）。

恢复失败时保留排除记录，便于稍后重试。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time_basis import local_iso
from app.models.models import Author, MediaRedownloadBlock, PlatformDownloadTask, PlatformWork, Work

logger = logging.getLogger(__name__)
SCAN_LIMIT = 5000
ARCHIVE_NAMES = {".download-archive.sqlite3": "sqlite", ".download-archive.txt": "text"}


def _timestamp(value: datetime | None, *, utc: bool) -> tuple[str | None, float]:
    """统一成带偏移的 ISO 字符串与排序用时间戳；抖音排除时间以 UTC 写入，其余为服务器本地时间。"""
    if value is None:
        return None, 0.0
    aware = value.replace(tzinfo=UTC) if utc else value.astimezone()
    return local_iso(aware.astimezone().replace(tzinfo=None)), aware.timestamp()


async def list_exclusions(db: AsyncSession, *, platform: str = "", q: str = "") -> list[dict]:
    """按排除时间倒序返回排除项目；关键词在数据库中筛选，每类最多 SCAN_LIMIT 条。"""
    items: list[dict] = []
    needle = q.strip()
    if not platform or platform == "douyin":
        statement = (
            select(Work, Author.nickname)
            .join(Author, Author.id == Work.author_id)
            .where(
                (Work.is_excluded.is_(True))
                | (Work._excluded_file_indices.is_not(None) & (Work._excluded_file_indices != "[]"))
            )
            .order_by(Work.excluded_at.desc().nullslast(), Work.id.desc())
            .limit(SCAN_LIMIT)
        )
        if needle:
            statement = statement.where(or_(
                Work.title.contains(needle, autoescape=True), Author.nickname.contains(needle, autoescape=True),
            ))
        for work, nickname in (await db.execute(statement)).all():
            base = {"platform": "douyin", "title": work.title, "author_name": nickname, "restorable": True}
            if work.is_excluded:
                excluded_at, order = _timestamp(work.excluded_at, utc=True)
                items.append({
                    **base, "key": f"douyin-work:{work.id}", "kind": "work", "detail": "整个作品",
                    "excluded_at": excluded_at, "_order": order,
                })
                continue
            for index in work.excluded_file_indices:
                items.append({
                    **base, "key": f"douyin-file:{work.id}:{index}", "kind": "file",
                    "detail": f"图集第 {index + 1} 张", "excluded_at": None, "_order": 0.0,
                })
    if platform != "douyin":
        statement = select(MediaRedownloadBlock).order_by(MediaRedownloadBlock.id.desc()).limit(SCAN_LIMIT)
        if platform:
            statement = statement.where(MediaRedownloadBlock.platform == platform)
        if needle:
            statement = statement.where(or_(*(
                column.contains(needle, autoescape=True)
                for column in (MediaRedownloadBlock.title, MediaRedownloadBlock.author_name, MediaRedownloadBlock.filename)
            )))
        for block in (await db.scalars(statement)).all():
            excluded_at, order = _timestamp(block.created_at, utc=False)
            items.append({
                "key": f"block:{block.id}", "platform": block.platform, "kind": "media",
                "title": block.title or block.filename, "author_name": block.author_name,
                "detail": block.filename or block.media_key, "excluded_at": excluded_at, "_order": order,
                "restorable": block.platform == "instagram" or bool(block.archive_path),
            })
    items.sort(key=lambda item: item.pop("_order"), reverse=True)
    return items


async def _restore_douyin(db: AsyncSession, work_id: int, file_index: int | None) -> tuple[str, list[int]]:
    from app.services import work_manager
    from app.services.archive_rules import get_archive_rules, serialize_archive_rules, work_matches_archive_rules
    from app.services.download_state_lock import lock_works
    from app.services.download_task_factory import ensure_download_task_async

    work = await db.get(Work, work_id)
    if work is None:
        raise ValueError("作品不存在")
    await lock_works(db, [work.id])
    await db.refresh(work)
    if file_index is not None:
        if work.is_excluded:
            raise ValueError("作品整体处于排除状态，请先恢复整个作品")
        if work.work_type != "images" or file_index not in work.excluded_file_indices:
            raise ValueError("该图片未处于排除状态，列表可能已过期，请刷新")
    elif not work.is_excluded:
        raise ValueError("作品未处于排除状态，列表可能已过期，请刷新")
    rules = await get_archive_rules(db)
    matches, reason = work_matches_archive_rules(work, rules)
    if not matches:
        raise ValueError(f"作品不符合当前归档规则：{reason}")
    if file_index is None:
        work.is_excluded, work.excluded_at = False, None
        if work.work_type == "video":
            indices = [0]
        else:
            count = max(int(work.image_count or 0), len(work.image_urls or []))
            # 恢复整个作品时保留单张排除，单张需在列表中单独恢复。
            skipped = set(work.excluded_file_indices)
            indices = [index for index in range(count or 1) if index not in skipped]
    else:
        work.excluded_file_indices = [index for index in work.excluded_file_indices if index != file_index]
        indices = [file_index]
    snapshot = serialize_archive_rules(rules)
    dispatch = []
    for index in indices:
        task, action = await ensure_download_task_async(db, work.id, index, archive_rule_snapshot=snapshot)
        if action in {"created", "reused"}:
            dispatch.append(task.id)
    if dispatch:
        work.is_downloaded = False
    await work_manager.recalc_author_counts(db, await db.get(Author, work.author_id))
    message = f"已提交 {len(dispatch)} 个下载任务" if dispatch else "已取消排除，没有需要新建的任务"
    return message, dispatch


def _checked_archive(block: MediaRedownloadBlock) -> Path:
    """归档路径来自数据库，仍按固定文件名、非链接、位于平台下载目录内校验。"""
    from app.services.task_file_cleanup import platform_root

    archive = Path(block.archive_path)
    if archive.name not in ARCHIVE_NAMES or archive.is_symlink():
        raise ValueError("排除记录中的下载归档路径无效，记录已保留")
    root = Path(platform_root(block.platform)).expanduser().resolve(strict=False)
    if not archive.resolve(strict=False).is_relative_to(root):
        raise ValueError("下载归档不在当前平台下载目录内（可能已切换目录），记录已保留")
    if not archive.is_file():
        raise ValueError("下载归档已不存在，记录已保留")
    return archive


async def _restore_archive(db: AsyncSession, block: MediaRedownloadBlock) -> str:
    from app.services.media_removal import ArchiveRelease, _active_folder, apply_archive_release, archive_contains

    archive = _checked_archive(block)
    release = ArchiveRelease(ARCHIVE_NAMES[archive.name], archive, block.media_key)
    if release.kind == "text" and await _active_folder(db, archive.parent):
        raise ValueError("同目录仍有下载在运行，暂不能改写归档，请稍后再试")
    removed = await asyncio.to_thread(apply_archive_release, release)
    if not removed and await asyncio.to_thread(archive_contains, release) is not False:
        raise ValueError("下载归档暂不可写，记录已保留，请稍后重试")
    return "已恢复；作者处于订阅状态时，下一次订阅检查会重新下载"


async def _restore_block(db: AsyncSession, block_id: int) -> str:
    from app.services.instagram_runtime import utcnow
    from app.services.platform_task_service import create_platform_task

    block = await db.get(MediaRedownloadBlock, block_id, with_for_update=True)
    if block is None:
        raise ValueError("排除记录不存在，列表可能已过期，请刷新")
    if block.platform == "instagram":
        work = await db.get(PlatformWork, block.work_id) if block.work_id else None
        if work is not None and work.external_work_id.startswith("post:"):
            source_key = "post-" + work.external_work_id.split(":", 1)[1]
            active = await db.scalar(select(PlatformDownloadTask.id).where(
                PlatformDownloadTask.platform == "instagram",
                PlatformDownloadTask.source_key == source_key,
                PlatformDownloadTask.status.in_(("pending", "downloading", "paused")),
            ).limit(1))
            if active is None:
                task = create_platform_task("instagram", source_key, work.source_url, "work")
                task.instagram_options = json.dumps({"not_before": utcnow().isoformat()})
                db.add(task)
            message = "已恢复，并已排入下载"
        else:
            message = "已恢复；Story 可能已过期，无法保证补回"
    elif block.archive_path and not block.media_key.startswith("file:"):
        message = await _restore_archive(db, block)
    else:
        raise ValueError("删除时未能记录下载归档位置，无法自动恢复")
    await db.delete(block)
    return message


async def _dispatch_douyin(task_ids: list[int]) -> bool:
    from app.tasks.download_tasks import download_single_file

    try:
        for task_id in task_ids:
            await asyncio.to_thread(download_single_file.delay, task_id)
        return True
    except Exception:
        logger.warning("排除项目已恢复，但下载任务投递失败", exc_info=True)
        return False


async def restore_exclusions(db: AsyncSession, keys: list[str]) -> dict:
    """逐项恢复，每项独立提交；抖音任务在提交成功后才投递，单项失败不影响其他项。"""
    succeeded, failed = [], []
    instagram_restored = False
    for key in dict.fromkeys(keys):
        dispatch: list[int] = []
        try:
            kind, _, rest = key.partition(":")
            if kind == "douyin-work":
                message, dispatch = await _restore_douyin(db, int(rest), None)
            elif kind == "douyin-file":
                work_id, _, index = rest.partition(":")
                message, dispatch = await _restore_douyin(db, int(work_id), int(index))
            elif kind == "block":
                message = await _restore_block(db, int(rest))
                instagram_restored = instagram_restored or "排入下载" in message
            else:
                raise ValueError("无法识别的排除项目")
            await db.commit()
        except Exception as exc:
            await db.rollback()
            if not isinstance(exc, (ValueError, TypeError)):
                logger.exception("恢复排除项目失败: %s", key)
            failed.append({"key": key, "message": str(exc) or type(exc).__name__})
            continue
        if dispatch and not await _dispatch_douyin(dispatch):
            message += "；下载任务投递失败，可在任务列表中重试"
        succeeded.append({"key": key, "message": message})
    if instagram_restored:
        from app.tasks.platform_download_tasks import dispatch_instagram

        try:
            await asyncio.to_thread(dispatch_instagram.delay)
        except Exception:
            logger.warning("Instagram 调度投递失败，等待周期调度领取", exc_info=True)
    return {"succeeded": succeeded, "failed": failed}
