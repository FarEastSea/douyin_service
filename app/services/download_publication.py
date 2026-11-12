"""正式媒体发布与数据库完成记录之间的可恢复协调。"""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path

from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.orm import Session

from app.core import env_config
from app.core.config import settings
from app.models.models import DownloadTask, SystemConfig
from app.services.download_lifecycle import lock_download_attempt
from app.services.download_paths import build_download_attempt_path
from app.services.filesystem_durability import sync_directory, write_journal


logger = logging.getLogger(__name__)
_MARKER_PREFIX = "media-publication:"


def publication_directory() -> Path:
    return env_config.ENV_PATH.parent / ".runtime" / "media-publications"


def _identity(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def _read_entry(journal: Path) -> dict | None:
    try:
        return json.loads(journal.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # 另一恢复进程可能已经在相同任务行锁保护下完成清理。
        return None


def _settle(db, journal: Path) -> None:
    entry = _read_entry(journal)
    if entry is None:
        return
    db.execute(select(DownloadTask.id).where(DownloadTask.id == entry["task_id"]).with_for_update())
    # 等行锁期间清单可能已被另一恢复者移除，不能拿旧快照再做一次回滚。
    entry = _read_entry(journal)
    if entry is None:
        return
    committed = db.scalar(select(SystemConfig.value).where(SystemConfig.key == entry["marker"])) == "committed"
    target, backup, temporary = (Path(entry[key]) for key in ("target", "backup", "temporary"))
    if backup.is_symlink() or (not committed and any(path.is_symlink() for path in (target, temporary))):
        raise RuntimeError("媒体发布路径被替换为符号链接，保留恢复清单")
    # 提交后目标已交回业务所有权，另一个合法删除/发布可以先取得行锁。
    # 结算器此时只回收自己的备份和日志，不再要求目标保留提交时的身份。
    if not committed:
        restored = bool(
            entry.get("backup_identity") and not backup.exists() and target.is_file()
            and _identity(target) == entry["backup_identity"]
        )
        if not temporary.exists() and target.exists():
            if not restored and entry.get("published_identity") != _identity(target):
                raise RuntimeError("媒体已被其他执行或用户修改，保留恢复清单，不覆盖当前文件")
        if backup.exists():
            if entry.get("backup_identity") and _identity(backup) != entry["backup_identity"]:
                raise RuntimeError("媒体发布备份被修改，保留恢复清单")
            os.replace(backup, target)
            sync_directory(target.parent)
        elif entry["existed"] and not temporary.exists() and not restored:
            # 旧协议/机器故障可能遗失 backup。绝不能把无法恢复的替换当作已恢复。
            raise RuntimeError("原媒体备份缺失，保留发布恢复清单")
        elif not entry["existed"] and not temporary.exists() and target.exists():
            target.unlink()
            sync_directory(target.parent)
    if backup.exists():
        backup.unlink()
        sync_directory(backup.parent)
    journal.unlink()
    sync_directory(journal.parent)
    # 清单已持久移除后才回收 marker；反向顺序会让重启误判为回滚。
    db.execute(delete(SystemConfig).where(SystemConfig.key == entry["marker"]))


def _recover_one(journal: Path, bind=None) -> None:
    entry = _read_entry(journal)
    if entry is None:
        return
    owned_engine = bind is None
    if owned_engine:
        args = {"connect_timeout": 5} if entry["database_url"].startswith(("postgresql", "mysql")) else {}
        bind = create_engine(entry["database_url"], connect_args=args)
    try:
        with Session(bind) as db:
            _settle(db, journal)
            db.commit()
    finally:
        if owned_engine:
            bind.dispose()


def _bind_settlement(db) -> None:
    if db.info.get("media_publication_listener"):
        return
    db.info["media_publication_listener"] = True

    def settle(session, transaction):
        if transaction.parent is not None:
            return
        # 与媒体删除一致：外层事务释放连接和行锁后，独立事务才读取提交证明。
        # 不在 publish_media.__exit__ 中提交调用方尚未结束的数据库事务。
        for journal in session.info.pop("media_publication_journals", []):
            try:
                _recover_one(journal, session.get_bind())
            except Exception:
                # 数据库结果已确定；恢复失败保留清单，不能伪报下载提交失败。
                logger.exception("媒体发布清单待恢复: %s", journal.name)

    event.listen(db, "after_transaction_end", settle)


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
    active = db.info.get("media_publication_journals", [])
    for pending in publication_directory().glob(f"{task_id}-*.json"):
        if pending in active:
            raise RuntimeError("本次执行已有未结束的媒体发布事务")
        _settle(db, pending)
    backup = Path(build_download_attempt_path(target, token, suffix=".previous"))
    if backup.exists() or backup.is_symlink():
        raise RuntimeError("媒体发布备份已存在，保留文件等待恢复")
    existed = target_path.exists()
    entry = {
        "task_id": task_id, "token": token, "temporary": temporary, "target": target,
        "published_identity": _identity(temp_path), "backup": str(backup), "existed": existed,
        "backup_identity": _identity(target_path) if existed else None,
        "marker": f"{_MARKER_PREFIX}{token}", "database_url": settings.effective_database_url,
    }
    write_journal(journal, entry)
    _bind_settlement(db)
    db.info.setdefault("media_publication_journals", []).append(journal)
    try:
        if existed:
            with open(target_path, "r+b") as handle:
                os.fsync(handle.fileno())
            os.link(target_path, backup)
            sync_directory(backup.parent)
        with open(temp_path, "r+b") as handle:
            os.fsync(handle.fileno())
        db.add(SystemConfig(key=entry["marker"], value="committed"))
        os.replace(temp_path, target_path)
        sync_directory(target_path.parent)
        yield
    except BaseException:
        db.rollback()
        raise


def _collect_orphan_markers(db) -> None:
    # 先快照已提交 marker，再扫描清单。新发布必须先持久化清单，最后才提交
    # marker，因此并发新发布不会落入这份快照；清单消失则表示文件已安全结算。
    markers = set(db.scalars(select(SystemConfig.key).where(SystemConfig.key.startswith(_MARKER_PREFIX))))
    if not markers:
        return
    directory = publication_directory()
    try:
        with os.scandir(directory) as entries:
            journals = [Path(item.path) for item in entries if item.name.endswith(".json")]
    except FileNotFoundError:
        journals = []
    for journal in journals:
        entry = _read_entry(journal)
        if entry is not None:
            markers.discard(entry["marker"])
    if directory.exists():
        # 即便上一恢复进程在 unlink 后、目录 fsync 前中断，也要先确认可见的
        # 清单缺失已持久化，再删除相应数据库提交证明。
        sync_directory(directory)
    keys = sorted(markers)
    for offset in range(0, len(keys), 500):
        db.execute(delete(SystemConfig).where(SystemConfig.key.in_(keys[offset:offset + 500])))


def recover_media_publications() -> None:
    for journal in publication_directory().glob("*.json"):
        _recover_one(journal)
    # 使用独立短事务清理历史 marker，绝不提交业务调用方的事务。
    from app.models.database import get_sync_db

    with get_sync_db() as db:
        _collect_orphan_markers(db)
        db.commit()
