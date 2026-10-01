"""下载目录变更时的媒体记录路径迁移与兼容解析。"""

from pathlib import Path
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    DownloadHistory,
    DownloadTask,
    PlatformDownloadTask,
    PlatformMediaAsset,
    XDownloadTask,
    XMediaAsset,
)


def _resolved_path(value: str) -> Path:
    """先统一 Windows/POSIX 分隔符，再做非严格绝对化。"""
    return Path(str(value).replace("\\", "/")).expanduser().resolve(strict=False)


def _rebase_path_result(
    value: Optional[str],
    old_root: str,
    new_root: str,
) -> tuple[Optional[str], bool]:
    """返回迁移后路径，以及该非空路径是否未能重定位。"""
    if not value or not old_root or not new_root or old_root == new_root:
        return value, False
    try:
        source = _resolved_path(value)
        old = _resolved_path(old_root)
        target_root = _resolved_path(new_root)
    except (OSError, RuntimeError, ValueError):
        return value, True

    try:
        relative = source.relative_to(old)
        candidate = target_root / relative
        if candidate.is_symlink() or not candidate.exists():
            return value, True
        if source.is_file() and (not candidate.is_file() or source.stat().st_size != candidate.stat().st_size):
            return value, True
        return str(candidate), False
    except ValueError:
        # 已位于新目录时只规范化路径，不重复拼接。
        try:
            source.relative_to(target_root)
            return (str(source), False) if source.exists() else (value, True)
        except ValueError:
            pass

        # 兼容网页目录已先行变更、旧根目录信息已经丢失的记录。
        # 不猜测仅名称相同的文件就是原媒体；修复必须保持明确的相对路径。
        return value, True


def rebase_path(value: Optional[str], old_root: str, new_root: str) -> Optional[str]:
    """保留相对结构，将旧下载根目录替换为新根目录。"""
    return _rebase_path_result(value, old_root, new_root)[0]


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
    changed = {"tasks": 0, "history": 0, "x_tasks": 0, "x_media": 0, "platform_tasks": 0, "platform_media": 0}
    unresolved = {"tasks": 0, "history": 0, "x_tasks": 0, "x_media": 0, "platform_tasks": 0, "platform_media": 0}
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
                    task.download_dir, old_root, new_root
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
            download_dir, path_unresolved = _rebase_path_result(task.download_dir, old_x_download_dir, new_x_download_dir)
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

    await db.flush()
    return {
        **changed,
        "unresolved": unresolved,
        "unresolved_total": sum(unresolved.values()),
    }


def resolve_media_path(stored_path: str, download_root: str) -> Path:
    """仅允许解析网页当前下载根目录内的媒体文件。"""
    path = Path(stored_path).expanduser().resolve()
    root = Path(download_root).expanduser().resolve()
    try:
        path.relative_to(root)
        return path
    except ValueError:
        candidate = (root / path.parent.name / path.name).resolve()
        candidate.relative_to(root)
        if candidate.is_file():
            return candidate
        direct_candidate = (root / path.name).resolve()
        direct_candidate.relative_to(root)
        if direct_candidate.is_file():
            return direct_candidate
        raise
