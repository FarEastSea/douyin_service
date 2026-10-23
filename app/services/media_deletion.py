"""将媒体删除与数据库事务协调：同盘隔离，提交后清理，回滚后恢复。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.orm import Session

from app.core import env_config
from app.core.config import settings
from app.core.config_transaction import atomic_json
from app.models.models import Author, DownloadTask, SystemConfig, Work

logger = logging.getLogger(__name__)


def deletion_directory() -> Path:
    return env_config.ENV_PATH.parent / ".runtime" / "media-deletions"


def _identity(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_journal(path: Path, entry: dict) -> None:
    atomic_json(path, entry)
    _sync_directory(path.parent)


def _finish_journal(db, journal: Path, marker: str) -> None:
    journal.unlink()
    _sync_directory(journal.parent)
    # 清单已持久移除后才回收 marker；反向顺序会让重启误判为回滚。
    db.execute(delete(SystemConfig).where(SystemConfig.key == marker))


def _settle(db, journal: Path) -> None:
    entry = json.loads(journal.read_text(encoding="utf-8"))
    # 与状态写入使用相同顺序；已提交删除的记录可以不存在。
    for model, key in ((Author, "authors"), (Work, "works"), (DownloadTask, "tasks")):
        ids = entry.get(key) or []
        if ids:
            db.execute(select(model.id).where(model.id.in_(ids)).order_by(model.id).with_for_update()).all()
    committed = db.scalar(select(SystemConfig.value).where(SystemConfig.key == entry["marker"])) == "committed"
    if entry.get("operation") == "retire":
        if committed:
            for item in entry["files"]:
                path = Path(item["source"])
                if path.is_symlink():
                    raise RuntimeError("旧执行分片被替换为符号链接，保留清单")
                if path.is_file():
                    path.unlink()
                    _sync_directory(path.parent)
        _finish_journal(db, journal, entry["marker"])
        return
    if committed and entry.get("operation") == "handoff":
        # 已归属新执行的分片可能继续增长，不能再拿交接时的 stat 拒绝正常续传。
        _finish_journal(db, journal, entry["marker"])
        return
    for item in entry["files"]:
        source, staged = Path(item["source"]), Path(item["staged"])
        if not staged.exists():
            continue
        identity = _identity(staged)
        matches = identity[:2] == item["identity"][:2] if item.get("mutable_attempt") else identity == item["identity"]
        if staged.is_symlink() or not matches:
            raise RuntimeError("隔离媒体已被修改，保留删除清单等待处理")
        if committed:
            if entry.get("operation") != "handoff":
                staged.unlink()
        else:
            if source.exists():
                raise RuntimeError("原媒体路径已被占用，保留隔离文件而不覆盖")
            os.replace(staged, source)
        _sync_directory(source.parent)
        _sync_directory(staged.parent)
        try:
            staged.parent.rmdir()
        except OSError:
            pass
    _finish_journal(db, journal, entry["marker"])


def _recover_one(journal: Path) -> None:
    entry = json.loads(journal.read_text(encoding="utf-8"))
    args = {"connect_timeout": 5} if entry["database_url"].startswith(("postgresql", "mysql")) else {}
    engine = create_engine(entry["database_url"], connect_args=args)
    try:
        with Session(engine) as db:
            _settle(db, journal)
            db.commit()
    finally:
        engine.dispose()


def recover_media_deletions() -> None:
    for journal in deletion_directory().glob("*.json"):
        _recover_one(journal)


def _bind_settlement(db) -> None:
    if db.info.get("media_deletion_listener"):
        return
    db.info["media_deletion_listener"] = True

    def settle(session, transaction):
        # SAVEPOINT 的提交/回滚不会确定外层结果，也不会释放父记录锁。
        # 只在最外层事务及其连接释放后，以持久 marker 判定清理或恢复。
        if transaction.parent is not None:
            return
        for journal in session.info.pop("media_deletion_journals", []):
            try:
                _recover_one(journal)
            except Exception:
                # 数据库结果已经确定，文件恢复可在启动时继续，不伪报事务失败。
                logger.exception("媒体删除清单待恢复: %s", journal.name)

    event.listen(db, "after_transaction_end", settle)


def stage_media_deletion(db, paths, *, author_ids=(), work_ids=(), task_ids=()) -> int:
    """调用方必须先锁 Author→Work→Task；不提交事务。"""
    operation = uuid4().hex
    root = Path(settings.DOWNLOAD_DIR).expanduser().resolve()
    candidates = set()
    for value in paths:
        if not value:
            continue
        source = Path(value).expanduser()
        if source.is_symlink():
            raise ValueError("不允许删除符号链接媒体")
        source = source.resolve()
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise ValueError("删除文件不在当前下载目录内") from exc
        for suffix in ("", ".downloading", ".json", ".csv", ".json.tmp", ".csv.tmp"):
            candidate = source.with_name(source.name + suffix)
            if candidate.is_symlink():
                raise ValueError("不允许删除符号链接媒体")
            if candidate.is_file():
                candidates.add(candidate)
    if not candidates:
        return 0
    entry = {
        "marker": f"media-deletion:{operation}", "database_url": settings.effective_database_url,
        "authors": sorted(set(author_ids)), "works": sorted(set(work_ids)), "tasks": sorted(set(task_ids)),
        "files": [],
    }
    for source in sorted(candidates):
        staging = source.parent / f".media-deletion-{operation}"
        staged = staging / hashlib.sha256(source.name.encode()).hexdigest()
        entry["files"].append({"source": str(source), "staged": str(staged), "identity": _identity(source),
                               "mutable_attempt": source.name.endswith((".downloading", ".downloading.json"))})
    journal = deletion_directory() / f"{operation}.json"
    _write_journal(journal, entry)
    _bind_settlement(db)
    db.info.setdefault("media_deletion_journals", []).append(journal)
    db.add(SystemConfig(key=entry["marker"], value="committed"))
    try:
        for item in entry["files"]:
            staged = Path(item["staged"])
            staged.parent.mkdir(exist_ok=True)
            os.replace(item["source"], staged)
            _sync_directory(staged.parent)
            _sync_directory(Path(item["source"]).parent)
    except BaseException:
        # 调用方 rollback 后由事件恢复；不得在持有原事务行锁时另开连接等待自己。
        raise
    return len(candidates)


def stage_resume_handoff(db, task, destination: str) -> None:
    """在更新 temp_file_path 的同一事务中转移已停止执行的续传文件。"""
    if not task.temp_file_path or task.temp_file_path == destination:
        return
    source, target = Path(task.temp_file_path), Path(destination)
    if not source.exists():
        return
    if (source.is_symlink() or target.is_symlink() or not source.name.endswith(".downloading")
            or source.resolve().parent != target.resolve().parent):
        raise ValueError("续传文件路径或归属不合法")
    operation = uuid4().hex
    entry = {"operation": "handoff", "marker": f"media-handoff:{operation}",
             "database_url": settings.effective_database_url, "tasks": [task.id],
             "works": [task.work_id], "authors": [], "files": []}
    author_id = db.scalar(select(Work.author_id).where(Work.id == task.work_id))
    entry["authors"] = [author_id] if author_id is not None else []
    for old, new in ((source, target), (Path(str(source) + ".json"), Path(str(target) + ".json"))):
        if old.is_file():
            if old.is_symlink() or new.exists():
                raise ValueError("续传目标已存在或元数据为符号链接")
            entry["files"].append({"source": str(old), "staged": str(new), "identity": _identity(old)})
    journal = deletion_directory() / f"{operation}.json"
    _write_journal(journal, entry)
    _bind_settlement(db)
    db.info.setdefault("media_deletion_journals", []).append(journal)
    db.add(SystemConfig(key=entry["marker"], value="committed"))
    for item in entry["files"]:
        os.replace(item["source"], item["staged"])
    _sync_directory(target.parent)


def retire_attempt_artifacts(db, task) -> None:
    """撤销未知执行时放弃续传所有权，提交后回收其专有分片。

    旧执行可能仍持有打开的描述符，但它再写入前必须检查已失效的 token。
    Windows 暂不能移除打开文件时保留清单，在下次启动继续恢复。
    """
    if not task.temp_file_path:
        return
    source = Path(task.temp_file_path)
    root = Path(settings.DOWNLOAD_DIR).expanduser().resolve()
    source.resolve().relative_to(root)
    if source.is_symlink() or not source.name.endswith(".downloading"):
        raise ValueError("旧执行分片路径不合法")
    operation = uuid4().hex
    author_id = db.scalar(select(Work.author_id).where(Work.id == task.work_id))
    entry = {"operation": "retire", "marker": f"media-retire:{operation}",
             "database_url": settings.effective_database_url, "authors": [author_id] if author_id else [],
             "works": [task.work_id], "tasks": [task.id],
             "files": [{"source": str(path)} for path in (source, Path(str(source) + ".json"))]}
    journal = deletion_directory() / f"{operation}.json"
    _write_journal(journal, entry)
    _bind_settlement(db)
    db.info.setdefault("media_deletion_journals", []).append(journal)
    db.add(SystemConfig(key=entry["marker"], value="committed"))
    task.temp_file_path = None
