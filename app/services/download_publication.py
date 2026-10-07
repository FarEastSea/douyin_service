"""正式媒体发布与数据库完成记录之间的可恢复协调。"""
from contextlib import contextmanager
import json
import os
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core import env_config
from app.core.config import settings
from app.core.config_transaction import atomic_json
from app.models.models import DownloadTask, SystemConfig
from app.services.download_lifecycle import lock_download_attempt
from app.services.download_paths import build_download_attempt_path


def publication_directory() -> Path:
    return env_config.ENV_PATH.parent / ".runtime" / "media-publications"


def _settle(db, journal: Path) -> None:
    entry = json.loads(journal.read_text(encoding="utf-8"))
    db.execute(select(DownloadTask.id).where(DownloadTask.id == entry["task_id"]).with_for_update())
    committed = db.scalar(select(SystemConfig.value).where(SystemConfig.key == entry["marker"]))
    target, backup = Path(entry["target"]), Path(entry["backup"])
    if committed != "committed":
        if not Path(entry["temporary"]).exists() and target.exists():
            stat = target.stat()
            if entry.get("published_identity") != [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]:
                raise RuntimeError("媒体已被其他执行或用户修改，保留恢复清单，不覆盖当前文件")
        if backup.exists():
            os.replace(backup, target)
        elif not entry["existed"] and not Path(entry["temporary"]).exists() and target.exists():
            target.unlink()
    if backup.exists():
        backup.unlink()
    journal.unlink()


@contextmanager
def publish_media(db, task_id: int, token: str, temporary: str, target: str):
    lock_download_attempt(db, task_id, token)
    target_path, temp_path = Path(target), Path(temporary)
    if target_path.is_symlink() or temp_path.is_symlink() or temp_path.stat().st_size <= 0:
        raise ValueError("发布文件为空或路径为符号链接")
    if target_path.parent.resolve() != temp_path.parent.resolve():
        raise ValueError("临时文件与目标文件不在同一目录")
    journal = publication_directory() / f"{task_id}-{token}.json"
    # 上一轮不确定提交必须先恢复，不能用新执行覆盖证据。
    for pending in publication_directory().glob(f"{task_id}-*.json"):
        _settle(db, pending)
    backup = Path(build_download_attempt_path(target, token, suffix=".previous"))
    temp_stat = temp_path.stat()
    entry = {"task_id": task_id, "token": token, "temporary": temporary, "target": target,
             "published_identity": [temp_stat.st_dev, temp_stat.st_ino, temp_stat.st_size, temp_stat.st_mtime_ns],
             "backup": str(backup), "existed": target_path.exists(),
             "marker": f"media-publication:{token}", "database_url": settings.effective_database_url}
    atomic_json(journal, entry)
    try:
        if target_path.exists():
            os.link(target_path, backup)
        with open(temp_path, "r+b") as handle:
            os.fsync(handle.fileno())
        db.add(SystemConfig(key=entry["marker"], value="committed"))
        os.replace(temp_path, target_path)
        yield
    except BaseException:
        db.rollback()
        _settle(db, journal)
        raise
    else:
        _settle(db, journal)


def recover_media_publications() -> None:
    for journal in publication_directory().glob("*.json"):
        entry = json.loads(journal.read_text(encoding="utf-8"))
        args = {"connect_timeout": 5} if entry["database_url"].startswith(("postgresql", "mysql")) else {}
        engine = create_engine(entry["database_url"], connect_args=args)
        try:
            with Session(engine) as db:
                # 恢复持有同一任务行锁，避免和已经启动的执行同时修改文件。
                db.execute(select(DownloadTask.id).where(DownloadTask.id == entry["task_id"]).with_for_update())
                _settle(db, journal)
        finally:
            engine.dispose()
