"""下载目录变更时的媒体记录路径迁移与兼容解析。"""

import stat
from pathlib import Path
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    DownloadHistory,
    DownloadTask,
    MediaRedownloadBlock,
    OrphanMediaFile,
    PlatformDownloadTask,
    PlatformMediaAsset,
    XDownloadTask,
    XMediaAsset,
)


def _resolved_path(value: str) -> Path:
    """先统一 Windows/POSIX 分隔符，再做非严格绝对化。"""
    return Path(str(value).replace("\\", "/")).expanduser().resolve(strict=False)


def _stat_if_present(path: Path):
    try:
        return path.stat()
    except FileNotFoundError:
        return None


def _rebase_path_result(
    value: Optional[str],
    old_root: str,
    new_root: str,
    *,
    directory: bool | None = False,
    drop_missing: bool = False,
) -> tuple[Optional[str], bool]:
    """返回迁移后路径，以及该非空路径是否未能重定位。"""
    if not value or not old_root or not new_root or old_root == new_root:
        return value, False
    try:
        old = _resolved_path(old_root)
        target_root = _resolved_path(new_root)
        stored = str(value).replace("\\", "/")
        path = Path(stored).expanduser()
        if path.is_absolute() and not path.resolve().is_relative_to(old):
            source = candidate = path
        else:
            source = resolve_media_path(stored, str(old))
            candidate = target_root / source.relative_to(old)
        resolved = candidate.resolve()
        if candidate.is_symlink() or not resolved.is_relative_to(target_root):
            return value, True
        target_stat = _stat_if_present(candidate)
        source_stat = _stat_if_present(source)
        if target_stat is None:
            if drop_missing and source_stat is None:
                return None, False
            return value, True
        is_dir = stat.S_ISDIR(target_stat.st_mode)
        if not (is_dir or stat.S_ISREG(target_stat.st_mode)) or (directory is not None and directory != is_dir):
            return value, True
        if source_stat and (
            stat.S_IFMT(source_stat.st_mode) != stat.S_IFMT(target_stat.st_mode)
            or (not is_dir and source_stat.st_size != target_stat.st_size)
        ):
            return value, True
        return str(resolved), False
    except (OSError, RuntimeError, ValueError):
        return value, True


def rebase_path(value: Optional[str], old_root: str, new_root: str) -> Optional[str]:
    """保留相对结构，将旧下载根目录替换为新根目录。"""
    return _rebase_path_result(value, old_root, new_root, directory=None)[0]


async def migrate_download_paths(
    db: AsyncSession,
    *,
    old_download_dir: str,
    new_download_dir: str,
    old_x_download_dir: str,
    new_x_download_dir: str,
    platform_download_dirs: dict[str, tuple[str, str]],
) -> dict:
    """同步迁移所有已存在任务、历史记录和各平台任务中的持久化路径。"""
    changed = {"tasks": 0, "history": 0, "x_tasks": 0, "x_media": 0, "platform_tasks": 0, "platform_media": 0, "orphan_media": 0}
    unresolved = {"tasks": 0, "history": 0, "x_tasks": 0, "x_media": 0, "platform_tasks": 0, "platform_media": 0, "orphan_media": 0}
    batch_size = 1000
    if any(old != new for old, new in [(old_download_dir, new_download_dir),
                                      (old_x_download_dir, new_x_download_dir),
                                      *platform_download_dirs.values()]):
        # 与任务的插入、领取和终态写入互斥，避免检查结束后新任务抢入。
        # PostgreSQL 是生产部署数据库；锁随事务提交/回滚自动释放。
        if db.get_bind().dialect.name == "postgresql":
            names = ", ".join(model.__tablename__ for model in
                              (DownloadTask, XDownloadTask, PlatformDownloadTask))
            await db.execute(text(f"LOCK TABLE {names} IN SHARE ROW EXCLUSIVE MODE"))
        for model in (DownloadTask, XDownloadTask, PlatformDownloadTask):
            active = await db.scalar(select(model.id).where(
                model.status.in_(("pending", "downloading", "paused"))).limit(1))
            if active:
                raise ValueError("存在排队、下载或暂停任务，不能切换下载目录；请先完成或取消这些任务")

    last_id = 0
    while True:
        tasks = (await db.execute(
            select(DownloadTask)
            .where(DownloadTask.id > last_id)
            .order_by(DownloadTask.id)
            .limit(batch_size)
        )).scalars().all()
        if not tasks:
            break
        for task in tasks:
            file_path, file_unresolved = _rebase_path_result(task.file_path, old_download_dir, new_download_dir)
            temp_path, temp_unresolved = _rebase_path_result(task.temp_file_path, old_download_dir, new_download_dir)
            if file_unresolved or temp_unresolved:
                unresolved["tasks"] += 1
            if file_path != task.file_path or temp_path != task.temp_file_path:
                task.file_path = file_path
                task.temp_file_path = temp_path
                changed["tasks"] += 1
        last_id = tasks[-1].id
        await db.flush()

    for platform, (old_root, new_root) in platform_download_dirs.items():
        if old_root == new_root:
            continue
        last_id = 0
        while True:
            platform_tasks = (await db.execute(
                select(PlatformDownloadTask)
                .where(
                    PlatformDownloadTask.platform == platform,
                    PlatformDownloadTask.id > last_id,
                )
                .order_by(PlatformDownloadTask.id)
                .limit(batch_size)
            )).scalars().all()
            if not platform_tasks:
                break
            for task in platform_tasks:
                download_dir, path_unresolved = _rebase_path_result(
                    task.download_dir, old_root, new_root, directory=True,
                )
                if path_unresolved:
                    unresolved["platform_tasks"] += 1
                if download_dir != task.download_dir:
                    task.download_dir = download_dir
                    changed["platform_tasks"] += 1
            last_id = platform_tasks[-1].id
            await db.flush()

        last_id = 0
        while True:
            platform_media = (await db.execute(
                select(PlatformMediaAsset)
                .where(
                    PlatformMediaAsset.platform == platform,
                    PlatformMediaAsset.id > last_id,
                )
                .order_by(PlatformMediaAsset.id)
                .limit(batch_size)
            )).scalars().all()
            if not platform_media:
                break
            for asset in platform_media:
                file_path, path_unresolved = _rebase_path_result(
                    asset.file_path, old_root, new_root
                )
                if path_unresolved:
                    unresolved["platform_media"] += 1
                if file_path != asset.file_path:
                    asset.file_path = file_path
                    changed["platform_media"] += 1
            last_id = platform_media[-1].id
            await db.flush()

    last_id = 0
    while True:
        history = (await db.execute(
            select(DownloadHistory)
            .where(DownloadHistory.id > last_id)
            .order_by(DownloadHistory.id)
            .limit(batch_size)
        )).scalars().all()
        if not history:
            break
        for row in history:
            file_path, path_unresolved = _rebase_path_result(row.file_path, old_download_dir, new_download_dir)
            if path_unresolved:
                unresolved["history"] += 1
            if file_path != row.file_path:
                row.file_path = file_path
                changed["history"] += 1
        last_id = history[-1].id
        await db.flush()

    last_id = 0
    while True:
        x_tasks = (await db.execute(
            select(XDownloadTask)
            .where(XDownloadTask.id > last_id)
            .order_by(XDownloadTask.id)
            .limit(batch_size)
        )).scalars().all()
        if not x_tasks:
            break
        for task in x_tasks:
            download_dir, path_unresolved = _rebase_path_result(
                task.download_dir, old_x_download_dir, new_x_download_dir, directory=True,
            )
            if path_unresolved:
                unresolved["x_tasks"] += 1
            if download_dir != task.download_dir:
                task.download_dir = download_dir
                changed["x_tasks"] += 1
        last_id = x_tasks[-1].id
        await db.flush()

    last_id = 0
    while True:
        x_media = (await db.execute(
            select(XMediaAsset)
            .where(XMediaAsset.id > last_id)
            .order_by(XMediaAsset.id)
            .limit(batch_size)
        )).scalars().all()
        if not x_media:
            break
        for asset in x_media:
            file_path, path_unresolved = _rebase_path_result(asset.file_path, old_x_download_dir, new_x_download_dir)
            if path_unresolved:
                unresolved["x_media"] += 1
            if file_path != asset.file_path:
                asset.file_path = file_path
                changed["x_media"] += 1
        last_id = x_media[-1].id
        await db.flush()

    # 无索引媒体按各自来源平台的目录迁移，保证切换目录后媒体库仍可预览与删除。
    roots = {
        "douyin": (old_download_dir, new_download_dir),
        "x": (old_x_download_dir, new_x_download_dir),
        **platform_download_dirs,
    }
    last_id = 0
    while True:
        orphans = (await db.execute(
            select(OrphanMediaFile)
            .where(OrphanMediaFile.id > last_id)
            .order_by(OrphanMediaFile.id)
            .limit(batch_size)
        )).scalars().all()
        if not orphans:
            break
        for orphan in orphans:
            old_root, new_root = roots.get(orphan.platform, (None, None))
            if not old_root or old_root == new_root:
                continue
            file_path, path_unresolved = _rebase_path_result(
                orphan.file_path, old_root, new_root, drop_missing=True,
            )
            if file_path is None and not path_unresolved:
                await db.delete(orphan)
                changed["orphan_media"] += 1
                continue
            if path_unresolved:
                unresolved["orphan_media"] += 1
            if file_path != orphan.file_path:
                orphan.file_path = file_path
                changed["orphan_media"] += 1
        last_id = orphans[-1].id
        await db.flush()

    # 排除记录中的下载归档路径同样随目录迁移；找不到时保留原值，恢复时会如实提示，不阻止目录切换。
    blocks = (await db.execute(
        select(MediaRedownloadBlock).where(MediaRedownloadBlock.archive_path.is_not(None))
    )).scalars().all()
    for block in blocks:
        old_root, new_root = roots.get(block.platform, (None, None))
        if not old_root or old_root == new_root:
            continue
        archive_path, path_unresolved = _rebase_path_result(block.archive_path, old_root, new_root)
        if not path_unresolved and archive_path != block.archive_path:
            block.archive_path = archive_path

    await db.flush()
    return {
        **changed,
        "unresolved": unresolved,
        "unresolved_total": sum(unresolved.values()),
    }


def resolve_media_path(stored_path: str, download_root: str) -> Path:
    """只解析当前根目录内的明确路径，绝不凭同名文件推测历史媒体。"""
    root = Path(download_root).expanduser().resolve()
    if not str(stored_path or "").strip():
        raise ValueError("媒体路径为空")
    path = Path(stored_path).expanduser()
    if not path.is_absolute():
        # 历史下载器可保存相对工作目录的完整路径；仅接受仍在当前根内
        # 的明确路径，不截断目录、不按存在性尝试同名替代品。
        absolute = path.resolve()
        try:
            absolute.relative_to(root)
        except ValueError:
            path = root / path
        else:
            path = absolute
    path = path.resolve()
    path.relative_to(root)
    return path
