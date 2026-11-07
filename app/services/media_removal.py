"""媒体库删除已索引媒体：由用户选择之后的订阅检查是否重新下载，默认重新下载。

各平台“是否重新下载”的落点不同：
- 抖音：删除任务并让下一轮检查做全量对账以补建任务；阻止时沿用作品/单文件排除标记。
- X 与 gallery-dl 类平台：下载归档会让引擎跳过已下载过的媒体；允许重新下载时移除对应归档条目。
- yt-dlp 类平台：归档文本同理，按文件名中的 [ID] 移除。
- Instagram：为该帖子在下次检查时排一次作品任务；阻止时登记 MediaRedownloadBlock。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import string
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    Author,
    DownloadTask,
    MediaRedownloadBlock,
    OrphanMediaFile,
    PlatformDownloadTask,
    PlatformMediaAsset,
    PlatformSubscriptionState,
    PlatformWork,
    Work,
    XAuthor,
    XDownloadTask,
    XMediaAsset,
)

logger = logging.getLogger(__name__)


class MediaRemovalError(Exception):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def _format_fields(fmt: str) -> set[str]:
    """archive_fmt 引用的顶层字段名，例如 {tweet_id}_{num} → {tweet_id, num}。"""
    return {
        re.split(r"[.\[!:]", field)[0]
        for _, field, _, _ in string.Formatter().parse(fmt)
        if field
    }


def gallery_dl_archive_key(meta: dict) -> str | None:
    """按 gallery-dl 自身规则（category + archive_fmt）计算归档键。

    gallery-dl 的格式化遇到缺失字段会填 "None" 而不报错，所以先确认字段齐全，
    否则宁可返回 None（无法保证重新下载），也不能算出一个错误的键。
    """
    category = meta.get("category")
    if not category:
        return None
    try:
        from gallery_dl import extractor, formatter
    except ImportError:
        return None
    classes = [cls for cls in extractor.extractors() if getattr(cls, "category", None) == category]
    exact = {
        cls.archive_fmt for cls in classes
        if getattr(cls, "subcategory", None) == meta.get("subcategory") and isinstance(getattr(cls, "archive_fmt", None), str)
    }
    # 没有精确匹配的子类时，只有同平台所有提取器格式一致才能确定，否则宁可放弃。
    formats = exact or {cls.archive_fmt for cls in classes if isinstance(getattr(cls, "archive_fmt", None), str)}
    if len(formats) != 1:
        return None
    archive_fmt = formats.pop()
    if not _format_fields(archive_fmt) <= meta.keys():
        return None
    try:
        return formatter.parse(category + archive_fmt).format_map(meta)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


@dataclass(frozen=True)
class ArchiveRelease:
    kind: str  # "sqlite" | "text"
    archive: Path
    key: str


def plan_archive_release(file_path: str) -> ArchiveRelease | None:
    """在文件和 .json 元数据被移走之前定位归档条目；无法定位时返回 None。"""
    path = Path(file_path)
    folder = path.parent
    archive = folder / ".download-archive.sqlite3"
    sidecar = path.with_name(path.name + ".json")
    if archive.is_file() and not archive.is_symlink() and sidecar.is_file():
        try:
            key = gallery_dl_archive_key(json.loads(sidecar.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            key = None
        return ArchiveRelease("sqlite", archive, key) if key else None
    text_archive = folder / ".download-archive.txt"
    match = re.search(r"\[([^\[\]/\\]+)\]\.[^.]+$", path.name)
    if text_archive.is_file() and not text_archive.is_symlink() and match:
        return ArchiveRelease("text", text_archive, match[1])
    return None


def _locked(lock_path: Path, attempts: int = 50) -> int | None:
    """跨进程的简易排他锁，防止并发删除同时改写同一份归档文本。"""
    for _ in range(attempts):
        try:
            return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            time.sleep(0.1)
    return None


def apply_archive_release(release: ArchiveRelease) -> bool:
    """移除归档条目；返回是否确实移除。归档里没有该条目时返回 False。"""
    if release.kind == "sqlite":
        try:
            with closing(sqlite3.connect(release.archive, timeout=10)) as connection:
                cursor = connection.execute("DELETE FROM archive WHERE entry = ?", (release.key,))
                connection.commit()
                return cursor.rowcount > 0
        except sqlite3.Error:
            logger.warning("下载归档暂不可写，未移除条目: %s", release.archive, exc_info=True)
            return False
    lock_path = release.archive.with_name(release.archive.name + ".lock")
    descriptor = _locked(lock_path)
    if descriptor is None:
        return False
    try:
        try:
            lines = release.archive.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return False
        kept = [line for line in lines if line.split(" ", 1)[-1].strip() != release.key]
        if len(kept) == len(lines):
            return False
        temporary = release.archive.with_name(f"{release.archive.name}.{uuid4().hex}.tmp")
        temporary.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8")
        os.replace(temporary, release.archive)
        return True
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def archive_contains(release: ArchiveRelease) -> bool | None:
    """归档中是否仍有该条目；无法读取时返回 None。"""
    try:
        if release.kind == "sqlite":
            with closing(sqlite3.connect(release.archive, timeout=10)) as connection:
                return connection.execute(
                    "SELECT 1 FROM archive WHERE entry = ? LIMIT 1", (release.key,)
                ).fetchone() is not None
        lines = release.archive.read_text(encoding="utf-8").splitlines()
        return any(line.split(" ", 1)[-1].strip() == release.key for line in lines)
    except (OSError, UnicodeDecodeError, sqlite3.Error):
        return None


def release_download_archive(file_path: str) -> bool | None:
    """定位并移除归档条目；None 表示无法定位，False 表示未能移除。"""
    release = plan_archive_release(file_path)
    return None if release is None else apply_archive_release(release)


async def _delete_douyin(db: AsyncSession, task_id: int, block: bool) -> dict:
    from app.services import work_manager

    task = await db.get(DownloadTask, task_id)
    if task is None or task.status != "completed":
        raise MediaRemovalError("媒体不存在或尚未下载完成", 404)
    work = await db.get(Work, task.work_id)
    if work is None:
        raise MediaRemovalError("关联作品不存在", 404)
    if block:
        # 沿用作品页的排除语义：图集排除单张，视频排除整个作品。
        if work.work_type == "images":
            stats = await work_manager.delete_work_file(db, work, task.file_index)
        else:
            stats = await work_manager.delete_work(db, work)
        return {"removed": stats.get("removed_files", 0), "redownload": "blocked"}
    removed = await work_manager._purge_tasks(db, [task])
    await work_manager.refresh_work_download_state(db, work)
    author = await db.get(Author, work.author_id)
    await work_manager.recalc_author_counts(db, author)
    if author is None or not author.is_subscribed:
        return {"removed": removed, "redownload": "manual"}
    # 增量检查遇到已知作品会提前停止，旧作品要靠全量对账补建；置空后下一轮检查即全量对账。
    author.last_full_reconcile_at = None
    return {"removed": removed, "redownload": "scheduled"}


async def _active_folder(db: AsyncSession, folder: Path) -> bool:
    """同目录仍有下载在运行时，引擎可能正在追加归档，不能改写。"""
    target = folder.resolve(strict=False)
    for model in (XDownloadTask, PlatformDownloadTask):
        directories = (await db.execute(
            select(model.download_dir).where(model.status.in_(("pending", "downloading", "paused")))
        )).scalars().all()
        for directory in directories:
            if directory and target.is_relative_to(Path(directory).resolve(strict=False)):
                return True
    return False


async def _queue_instagram_redownload(db: AsyncSession, asset: PlatformMediaAsset) -> str:
    """Instagram 增量检查遇到已知作品会停止；为该帖子单独排一次作品任务，在下次检查时执行。"""
    from app.services.instagram_runtime import utcnow
    from app.services.instagram_store import interval_for
    from app.services.platform_task_service import create_platform_task

    work = await db.get(PlatformWork, asset.work_id) if asset.work_id else None
    if work is None or not work.external_work_id.startswith("post:"):
        return "unknown"  # Stories 会过期，无法保证补回。
    source_key = "post-" + work.external_work_id.split(":", 1)[1]
    active = await db.scalar(select(PlatformDownloadTask.id).where(
        PlatformDownloadTask.platform == "instagram",
        PlatformDownloadTask.source_key == source_key,
        PlatformDownloadTask.status.in_(("pending", "downloading")),
    ).limit(1))
    if active is None:
        due = await db.scalar(select(func.min(PlatformSubscriptionState.next_check_at)).where(
            PlatformSubscriptionState.author_id == work.author_id,
            PlatformSubscriptionState.scan_mode == "recent",
            PlatformSubscriptionState.enabled.is_(True),
        )) or utcnow() + timedelta(seconds=interval_for("posts"))
        task = create_platform_task("instagram", source_key, work.source_url, "work")
        task.instagram_options = json.dumps({"not_before": max(due, utcnow()).isoformat()})
        db.add(task)
    return "scheduled"


async def _delete_asset(db: AsyncSession, platform: str, asset_id: int, block: bool) -> dict:
    from app.services.media_deletion import stage_media_deletion
    from app.services.task_file_cleanup import path_variants, platform_root

    model = XMediaAsset if platform == "x" else PlatformMediaAsset
    asset = await db.get(model, asset_id, with_for_update=True)
    if asset is None or (model is PlatformMediaAsset and asset.platform != platform):
        raise MediaRemovalError("媒体不存在", 404)
    path = asset.file_path
    shared = await db.scalar(
        select(model.id).where(model.id != asset.id, model.file_path.in_(path_variants(path))).limit(1)
    )
    if shared:
        # 文件仍被其他记录使用：只移除这条记录，不动文件，也无法登记阻止。
        await db.delete(asset)
        await db.flush()
        await refresh_media_counts(db, platform, task_id=asset.task_id, x_author_id=getattr(asset, "x_author_id", None))
        return {"removed": 0, "redownload": "unknown", "shared": True}
    redownload = "blocked" if block else "allowed"
    release = None
    if platform == "instagram":
        media_key = asset.external_media_id
        if block and media_key:
            await _add_block(
                db, platform=platform, media_key=media_key, work_id=asset.work_id, filename=asset.filename,
                title=asset.title, author_name=asset.author_name,
            )
        elif block:
            redownload = "unknown"
        else:
            if media_key:
                await db.execute(delete(MediaRedownloadBlock).where(
                    MediaRedownloadBlock.platform == platform, MediaRedownloadBlock.media_key == media_key,
                ))
            redownload = await _queue_instagram_redownload(db, asset)
    else:
        # 先读元数据定位归档条目（文件和元数据随后会被移走）。
        release = await asyncio.to_thread(plan_archive_release, path)
        if block:
            # 引擎会凭归档跳过；登记下来供“排除项目”维护时恢复下载。
            await _add_block(
                db, platform=platform,
                media_key=release.key if release else f"file:{asset.filename}",
                archive_path=str(release.archive) if release else None,
                filename=asset.filename, title=asset.title,
                author_name=asset.author_name or await _x_username(db, asset),
            )
            release = None
        else:
            if release is not None and release.kind == "text" and await _active_folder(db, release.archive.parent):
                release = None
            if release is None:
                redownload = "unknown"
    try:
        removed = await db.run_sync(
            lambda session: stage_media_deletion(session, [path], root=platform_root(platform))
        )
    except (ValueError, OSError) as exc:
        raise MediaRemovalError(f"文件未删除：{exc}") from exc
    if release is not None and not await asyncio.to_thread(apply_archive_release, release):
        redownload = "unknown"
    await db.delete(asset)
    await db.flush()
    await refresh_media_counts(db, platform, task_id=asset.task_id, x_author_id=getattr(asset, "x_author_id", None))
    return {"removed": removed, "redownload": redownload, "shared": False}


async def _x_username(db: AsyncSession, asset) -> str | None:
    if not isinstance(asset, XMediaAsset):
        return None
    return await db.scalar(select(XDownloadTask.username).where(XDownloadTask.id == asset.task_id))


async def _add_block(db: AsyncSession, **fields) -> None:
    """登记阻止重新下载；并发删除同一媒体时另一请求已登记，跳过即可。"""
    try:
        async with db.begin_nested():
            if not await db.scalar(select(MediaRedownloadBlock.id).where(
                MediaRedownloadBlock.platform == fields["platform"],
                MediaRedownloadBlock.media_key == fields["media_key"],
            )):
                db.add(MediaRedownloadBlock(**fields))
                await db.flush()
    except IntegrityError:
        pass


async def refresh_media_counts(db: AsyncSession, platform: str, *, task_id: int | None, x_author_id: int | None = None) -> None:
    """删除媒体后按实际剩余媒体重算任务与作者计数，不等下一次运行。"""
    model, task_model = (XMediaAsset, XDownloadTask) if platform == "x" else (PlatformMediaAsset, PlatformDownloadTask)
    # Instagram 子任务的 file_count 表示作品应有媒体数，媒体可能挂在父任务上，不按剩余资产重算。
    if task_id and platform != "instagram":
        task = await db.get(task_model, task_id)
        if task is not None:
            remaining = int(await db.scalar(select(func.count(model.id)).where(model.task_id == task_id)) or 0)
            task.file_count = task.downloaded_media_count = remaining
    if platform == "x" and x_author_id:
        author = await db.get(XAuthor, x_author_id)
        if author is not None:
            author.total_downloads = int(await db.scalar(
                select(func.count(XMediaAsset.id)).where(XMediaAsset.x_author_id == x_author_id)
            ) or 0)


async def delete_indexed_media(db: AsyncSession, platform: str, resource_id: int, *, block_redownload: bool) -> dict:
    """调用方负责提交；失败时回滚会恢复已隔离的文件。"""
    from app.services.task_file_cleanup import path_variants

    path = await db.scalar(
        select(DownloadTask.file_path).where(DownloadTask.id == resource_id) if platform == "douyin"
        else select((XMediaAsset if platform == "x" else PlatformMediaAsset).file_path)
        .where((XMediaAsset if platform == "x" else PlatformMediaAsset).id == resource_id)
    )
    if platform == "douyin":
        result = await _delete_douyin(db, resource_id, block_redownload)
    else:
        result = await _delete_asset(db, platform, resource_id, block_redownload)
    if path and not result.get("shared"):
        # 同一文件若还登记为无索引媒体，文件删除后那条登记也一并移除。
        await db.execute(delete(OrphanMediaFile).where(OrphanMediaFile.file_path.in_(path_variants(path))))
    return result


def removal_message(result: dict) -> str:
    if result.get("shared"):
        return "已移除这条媒体记录；文件仍被其他下载记录使用，未删除"
    head = "已删除文件" if result.get("removed") else "文件已不存在，已移除媒体记录"
    tail = {
        "allowed": "已移除下载归档记录，作者处于订阅状态时，下一次检查会重新下载",
        "scheduled": "已安排在下一次订阅检查时重新下载",
        "manual": "作者未订阅，不会自动重新下载；可在作品页手动重新下载",
        "blocked": "已阻止订阅重新下载",
        "unknown": "未能定位下载归档或媒体标识，订阅是否会重新下载无法保证",
    }[result.get("redownload", "allowed")]
    return f"{head}；{tail}"
