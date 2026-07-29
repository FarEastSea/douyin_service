"""下载目录变更时的媒体记录路径迁移与兼容解析。"""

from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import DownloadHistory, DownloadTask, XDownloadTask, XMediaAsset


def rebase_path(value: Optional[str], old_root: str, new_root: str) -> Optional[str]:
    """保留相对路径，将旧下载根目录替换为新根目录。"""
    if not value or not old_root or not new_root:
        return value
    source = Path(value).expanduser()
    old = Path(old_root).expanduser()
    try:
        relative = source.relative_to(old)
        return str(Path(new_root).expanduser() / relative)
    except ValueError:
        # 兼容网页目录已先行变更、旧根目录信息已经丢失的记录。
        target_root = Path(new_root).expanduser()
        candidate = target_root / source.parent.name / source.name
        if candidate.exists():
            return str(candidate)
        direct_candidate = target_root / source.name
        if direct_candidate.exists():
            return str(direct_candidate)
        return value


async def migrate_download_paths(
    db: AsyncSession,
    *,
    old_download_dir: str,
    new_download_dir: str,
    old_x_download_dir: str,
    new_x_download_dir: str,
) -> dict:
    """同步迁移所有已存在任务、历史记录和 X 任务中的持久化路径。"""
    changed = {"tasks": 0, "history": 0, "x_tasks": 0, "x_media": 0}

    tasks = (await db.execute(select(DownloadTask))).scalars().all()
    for task in tasks:
        file_path = rebase_path(task.file_path, old_download_dir, new_download_dir)
        temp_path = rebase_path(task.temp_file_path, old_download_dir, new_download_dir)
        if file_path != task.file_path or temp_path != task.temp_file_path:
            task.file_path = file_path
            task.temp_file_path = temp_path
            changed["tasks"] += 1

    history = (await db.execute(select(DownloadHistory))).scalars().all()
    for row in history:
        file_path = rebase_path(row.file_path, old_download_dir, new_download_dir)
        if file_path != row.file_path:
            row.file_path = file_path
            changed["history"] += 1

    x_tasks = (await db.execute(select(XDownloadTask))).scalars().all()
    for task in x_tasks:
        download_dir = rebase_path(task.download_dir, old_x_download_dir, new_x_download_dir)
        if download_dir != task.download_dir:
            task.download_dir = download_dir
            changed["x_tasks"] += 1

    x_media = (await db.execute(select(XMediaAsset))).scalars().all()
    for asset in x_media:
        file_path = rebase_path(asset.file_path, old_x_download_dir, new_x_download_dir)
        if file_path != asset.file_path:
            asset.file_path = file_path
            changed["x_media"] += 1

    await db.flush()
    return changed


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
