"""删除任务时的文件处理：完整文件默认保留，损坏或未完成的文件随任务清理。"""
from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import (
    Author,
    DownloadHistory,
    DownloadTask,
    OrphanMediaFile,
    PlatformAuthor,
    PlatformDownloadTask,
    PlatformMediaAsset,
    PlatformTaskWork,
    PlatformWork,
    Work,
    XDownloadTask,
    XMediaAsset,
)
from app.services.media_deletion import stage_media_deletion

ACTIVE_STATUSES = ("pending", "downloading", "paused")


def file_state(path_value: str | None, expected_size: int | None = None) -> str:
    """返回 missing / intact / broken；大小与记录不符或为空文件视为不完整。"""
    if not path_value:
        return "missing"
    path = Path(path_value)
    if path.is_symlink() or not path.is_file():
        return "missing"
    size = path.stat().st_size
    if size <= 0 or (expected_size and expected_size > 0 and size != expected_size):
        return "broken"
    return "intact"


# 刚被写过的分片可能属于尚未退出的执行（取消后进程仍在收尾），不能移走。
RECENT_WRITE_SECONDS = 120


def _partial_files(folder: Path, pattern: str, exclude_tokens: set[str]) -> list[str]:
    """只扫描任务目录本层；子目录可能属于同一作者的其他任务（如 X 单条推文）。"""
    if not folder.is_dir() or folder.is_symlink():
        return []
    fresh = time.time() - RECENT_WRITE_SECONDS
    return [
        str(path)
        for path in folder.glob(pattern)
        if path.is_file()
        and not path.is_symlink()
        and path.stat().st_mtime < fresh
        and not any(token in path.name for token in exclude_tokens)
    ]


def _douyin_plan(db: Session, task: DownloadTask) -> tuple[list[str], list[str]]:
    intact, broken = [], []
    if task.temp_file_path and file_state(task.temp_file_path) != "missing":
        broken.append(task.temp_file_path)
    paths = {task.file_path} | set(
        db.scalars(select(DownloadHistory.file_path).where(DownloadHistory.task_id == task.id))
    )
    for path in filter(None, paths):
        # 同一路径仍被其他任务或历史引用时，文件属于它们，任何情况下都不动。
        shared = db.scalar(
            select(DownloadTask.id).where(DownloadTask.id != task.id, DownloadTask.file_path == path).limit(1)
        ) or db.scalar(
            select(DownloadHistory.id).where(DownloadHistory.task_id != task.id, DownloadHistory.file_path == path).limit(1)
        )
        if shared:
            continue
        # 大小未知时按完整处理：同一路径可能是上次删除任务时保留下来的完整文件。
        state = file_state(path, task.total_bytes)
        if state != "missing":
            (intact if state == "intact" else broken).append(path)
    return intact, broken


def path_variants(path: str) -> set[str]:
    """同一文件可能以原始、展开或解析后的写法登记，比较引用时全部覆盖。"""
    values = {path}
    try:
        expanded = Path(path).expanduser()
        values.update({str(expanded), str(expanded.resolve(strict=False))})
    except (OSError, RuntimeError, ValueError):
        pass
    return values


def _asset_plan(db: Session, model, task_id: int, assets) -> tuple[list[str], list[str]]:
    intact, broken = [], []
    for asset in assets:
        # 同一路径可能被同主页的后续任务再次登记；文件属于它们，任何情况下都不动。
        if db.scalar(
            select(model.id)
            .where(model.task_id != task_id, model.file_path.in_(path_variants(asset.file_path)))
            .limit(1)
        ):
            continue
        state = file_state(asset.file_path, asset.size_bytes)
        if state != "missing":
            (intact if state == "intact" else broken).append(asset.file_path)
    return intact, broken


def _shared_download_dir(db: Session, model, task) -> bool:
    return bool(
        task.download_dir
        and db.scalar(
            select(model.id)
            .where(model.id != task.id, model.download_dir == task.download_dir, model.status.in_(ACTIVE_STATUSES))
            .limit(1)
        )
    )


def _x_plan(db: Session, task: XDownloadTask) -> tuple[list[str], list[str]]:
    intact, broken = _asset_plan(
        db, XMediaAsset, task.id, list(db.scalars(select(XMediaAsset).where(XMediaAsset.task_id == task.id)))
    )
    if task.download_dir and not _shared_download_dir(db, XDownloadTask, task):
        broken += _partial_files(Path(task.download_dir), "*.part", set())
    return intact, broken


def _platform_plan(db: Session, task: PlatformDownloadTask) -> tuple[list[str], list[str]]:
    assets = list(db.scalars(select(PlatformMediaAsset).where(PlatformMediaAsset.task_id == task.id)))
    intact, broken = _asset_plan(db, PlatformMediaAsset, task.id, assets)
    if task.platform != "instagram":
        if task.download_dir and not _shared_download_dir(db, PlatformDownloadTask, task):
            broken += _partial_files(Path(task.download_dir), "*.part", set())
        return intact, broken
    # Instagram 分片命名为 .<媒体ID>.<执行令牌>.part；正在执行的令牌所写分片必须保留。
    active_tokens = set(
        filter(
            None,
            db.scalars(
                select(PlatformDownloadTask.celery_task_id).where(
                    PlatformDownloadTask.platform == "instagram",
                    PlatformDownloadTask.id != task.id,
                    PlatformDownloadTask.status.in_(ACTIVE_STATUSES),
                )
            ),
        )
    )
    root = Path(settings.snapshot().INSTAGRAM_DOWNLOAD_DIR).expanduser()
    folders = {Path(asset.file_path).parent for asset in assets}
    for work, author in db.execute(
        select(PlatformWork, PlatformAuthor)
        .join(PlatformTaskWork, PlatformTaskWork.work_id == PlatformWork.id)
        .join(PlatformAuthor, PlatformAuthor.id == PlatformWork.author_id)
        .where(PlatformTaskWork.task_id == task.id)
    ):
        folders.add(root / author.external_user_id / work.external_work_id.replace(":", "-"))
    for folder in folders:
        broken += _partial_files(folder, ".*.part", active_tokens)
    return intact, broken


def path_is_indexed(db: Session, path: str) -> bool:
    """路径是否仍被任何已索引的下载记录引用（覆盖不同写法）。"""
    variants = path_variants(path)
    return any(
        db.scalar(select(column).where(column.in_(variants)).limit(1)) is not None
        for column in (
            DownloadTask.file_path,
            DownloadHistory.file_path,
            XMediaAsset.file_path,
            PlatformMediaAsset.file_path,
        )
    )


def platform_root(platform: str) -> str:
    """按平台各自的下载目录校验，平台子目录可以是指向其他磁盘的链接。"""
    if platform == "douyin":
        return settings.DOWNLOAD_DIR
    current = settings.snapshot()
    return getattr(current, f"{platform.upper()}_DOWNLOAD_DIR", None) or current.DOWNLOAD_ROOT


def plan_task_files(db: Session, platform: str, task) -> tuple[list[str], list[str]]:
    if platform == "douyin":
        return _douyin_plan(db, task)
    if platform == "x":
        return _x_plan(db, task)
    return _platform_plan(db, task)


def _orphan_metadata(db: Session, platform: str, task, paths: list[str]) -> list[dict]:
    """从即将删除的任务与媒体记录中带走标题、作者等信息，供媒体库继续展示。"""
    if platform == "douyin":
        from app.services.media_library import douyin_media_type

        work = db.get(Work, task.work_id)
        author = db.get(Author, work.author_id) if work else None
        return [
            {
                "file_path": path,
                "media_type": douyin_media_type(work.work_type if work else "video", path),
                "title": work.title if work else None,
                "author_name": author.nickname if author else None,
                "published_at": work.published_at if work else None,
                "width": work.width if work else None,
                "height": work.height if work else None,
                "duration_ms": work.duration_ms if work else None,
            }
            for path in paths
        ]
    model = XMediaAsset if platform == "x" else PlatformMediaAsset
    assets = db.scalars(select(model).where(model.task_id == task.id, model.file_path.in_(paths)))
    return [
        {
            "file_path": asset.file_path,
            "media_type": asset.media_type,
            "title": asset.title,
            "author_name": asset.author_name or (getattr(task, "username", None) if platform == "x" else None),
            "published_at": asset.published_at,
            "width": asset.width,
            "height": asset.height,
            "duration_ms": asset.duration_ms,
        }
        for asset in assets
    ]


def adopt_orphans(db: Session, platform: str, entries: list[dict], *, origin: str, source_task_key: str | None) -> int:
    """登记为无索引媒体；同一路径已登记时不重复添加。"""
    adopted = 0
    for entry in entries:
        path = Path(entry["file_path"])
        if db.scalar(select(OrphanMediaFile.id).where(OrphanMediaFile.file_path.in_(path_variants(str(path))))):
            continue
        # 每条登记独立 savepoint：并发登记同一路径时只跳过这一条，不让整个删除失败。
        try:
            with db.begin_nested():
                db.add(
                    OrphanMediaFile(
                        platform=platform,
                        filename=path.name,
                        size_bytes=path.stat().st_size if path.is_file() else 0,
                        origin=origin,
                        source_task_key=source_task_key,
                        **entry,
                    )
                )
                db.flush()
        except IntegrityError:
            continue
        adopted += 1
    return adopted


def stage_task_files(db: Session, platform: str, task, *, delete_files: bool) -> dict:
    """在删除任务的同一事务中处理文件：损坏的隔离删除，保留的完整文件转为无索引媒体。"""
    intact, broken = plan_task_files(db, platform, task)
    if not delete_files and intact:
        # 删除任务记录不代表放弃文件管理：保留的文件进入媒体库的“无索引媒体”。
        adopt_orphans(
            db, platform, _orphan_metadata(db, platform, task, intact),
            origin="task_deleted", source_task_key=f"{platform}:{task.id}",
        )
    targets = sorted(set(broken + (intact if delete_files else [])))
    removed = stage_media_deletion(
        db, targets, root=platform_root(platform), task_ids=[task.id] if platform == "douyin" else (),
    ) if targets else 0
    if delete_files and intact:
        # 删除完整文件时，同一路径若之前已登记为无索引媒体也一并移除。
        db.execute(OrphanMediaFile.__table__.delete().where(OrphanMediaFile.file_path.in_(intact)))
    return {"removed": removed, "kept": 0 if delete_files else len(intact)}
