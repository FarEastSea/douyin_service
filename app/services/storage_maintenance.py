"""可恢复的存储维护：重新校验后隔离文件并标记缺失任务。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    DownloadHistory,
    DownloadTask,
    PlatformDownloadTask,
    PlatformMediaAsset,
    XDownloadTask,
    XMediaAsset,
    SystemConfig, Work, Author,
)
from app.core.config_transaction import atomic_json


PARTIAL_SUFFIXES = {".part", ".tmp", ".downloading"}
MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm", ".mov", ".m4v"}
STORAGE_MISSING_ERROR = "存储巡检确认本地文件缺失或为空，请重试任务恢复媒体"
RECORD_MODELS = {
    "download_task": (DownloadTask, DownloadTask.file_path),
    "download_history": (DownloadHistory, DownloadHistory.file_path),
    "x_media": (XMediaAsset, XMediaAsset.file_path),
    "platform_media": (PlatformMediaAsset, PlatformMediaAsset.file_path),
}


def _resolved_under(root: Path, value: str) -> tuple[Path, Path]:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        lexical_relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("路径不在下载根目录内") from exc
    current = root
    for part in lexical_relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("符号链接不进入自动维护")
    path = candidate.resolve(strict=False)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("路径解析后超出下载根目录") from exc
    if not relative.parts or relative.parts[0] == ".quarantine":
        raise ValueError("隔离目录不能再次进入维护计划")
    return path, relative


def _normalized_record_path(root: Path, value: str) -> Path:
    """Normalize a database path without assuming that its historical root is current."""
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve(strict=False)


def find_rebase_candidate(root: Path, value: str) -> Path | None:
    """Find one unambiguous existing file after replacing an obsolete absolute root."""
    original = Path(value).expanduser()
    if not original.is_absolute() or original.exists():
        return None
    matches: list[Path] = []
    parts = original.parts
    # Keep at least a directory and filename. Matching a bare filename is too ambiguous.
    for index in range(1, len(parts) - 1):
        try:
            candidate, _ = _resolved_under(root, str(root.joinpath(*parts[index:])))
            if candidate.is_file() and candidate.stat().st_size > 0:
                matches.append(candidate)
        except (OSError, RuntimeError, ValueError):
            continue
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


async def _load_record(db: AsyncSession, kind: str, record_id: int | None):
    definition = RECORD_MODELS.get(kind)
    if not definition or not record_id:
        return None
    model, _ = definition
    return await db.get(model, int(record_id))


def _record_path(record: Any) -> str | None:
    return str(getattr(record, "file_path", "") or "") or None


async def _is_referenced(db: AsyncSession, root: Path, raw_path: str, resolved_path: str) -> bool:
    values = {raw_path, resolved_path}
    try:
        relative = str(Path(resolved_path).relative_to(root))
        values.update({relative, relative.replace("\\", "/")})
    except ValueError:
        pass
    for model, column in RECORD_MODELS.values():
        if int((await db.scalar(select(func.count(model.id)).where(column.in_(values)))) or 0):
            return True
    return False


async def _active_path(db: AsyncSession, path: Path) -> bool:
    # 暂停下载的临时文件也是可恢复状态，不能当作垃圾隔离。
    active = bool(await db.scalar(select(DownloadTask.id).where(
        DownloadTask.status.in_(("pending", "downloading", "paused")),
        or_(DownloadTask.file_path == str(path), DownloadTask.temp_file_path == str(path)),
    ).limit(1)))
    if active:
        return True
    for model in (XDownloadTask, PlatformDownloadTask):
        directories = (await db.execute(select(model.download_dir).where(
            model.status.in_(("pending", "downloading", "paused"))))).scalars().all()
        for directory in directories:
            if directory and path.is_relative_to(Path(directory).resolve(strict=False)):
                return True
    return False


async def build_storage_repair_plan(
    db: AsyncSession,
    root: Path,
    targets: list[dict[str, Any]],
    *,
    partial_stale_seconds: int = 6 * 3600,
) -> list[dict[str, Any]]:
    """对客户端选择逐项重新校验，不能依赖旧扫描结果直接修改。"""
    now = datetime.now(timezone.utc).timestamp()
    plan: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int | None]] = set()
    for target in targets:
        issue_type = str(target.get("issue_type") or "")
        record_kind = str(target.get("record_kind") or "") or None
        record_id = target.get("record_id")
        raw_path = str(target.get("path") or "").strip()
        key = (issue_type, raw_path, int(record_id) if record_id else None)
        if key in seen:
            continue
        seen.add(key)
        item = {
            "issue_type": issue_type,
            "record_kind": record_kind,
            "record_id": int(record_id) if record_id else None,
            "path": raw_path,
            "eligible": False,
            "action": None,
            "reason": None,
        }
        try:
            if issue_type not in {
                "stale_record_path", "missing_record", "zero_byte_file",
                "partial_file", "orphan_file",
            }:
                raise ValueError("不支持的问题类型")
            record = None
            if issue_type in {"stale_record_path", "missing_record", "zero_byte_file"}:
                path = _normalized_record_path(root, raw_path)
                if record is None:
                    record = await _load_record(db, record_kind or "", item["record_id"])
                if record is None:
                    raise ValueError("关联记录不存在")
                recorded_path = _record_path(record)
                if not recorded_path:
                    raise ValueError("关联记录已不再指向文件")
                if _normalized_record_path(root, recorded_path) != path:
                    raise ValueError("关联记录路径已经变化")
            else:
                path, relative = _resolved_under(root, raw_path)
                item["relative_path"] = str(relative)
            if issue_type == "stale_record_path":
                if path.exists():
                    raise ValueError("原记录路径已经恢复，无需迁移")
                candidate = find_rebase_candidate(root, raw_path)
                if candidate is None:
                    raise ValueError("当前下载目录中没有唯一对应文件")
                if isinstance(record, XMediaAsset):
                    duplicate = await db.scalar(select(func.count(XMediaAsset.id)).where(
                        XMediaAsset.file_path == str(candidate),
                        XMediaAsset.id != record.id,
                    ))
                    if int(duplicate or 0):
                        raise ValueError("目标文件已经被其他 X 媒体记录引用")
                item.update(
                    eligible=True,
                    action="relink_record",
                    suggested_path=str(candidate),
                    relative_path=str(candidate.relative_to(root)),
                )
            elif issue_type == "missing_record":
                if path.exists():
                    raise ValueError("文件已经恢复，无需处理")
                if find_rebase_candidate(root, raw_path) is not None:
                    raise ValueError("文件可在当前下载目录中找回，应回填路径而不是标记失败")
                item.update(eligible=True, action="mark_task_failed")
            elif issue_type == "zero_byte_file":
                if await _active_path(db, path):
                    raise ValueError("文件属于活跃或暂停任务，跳过维护")
                path, relative = _resolved_under(root, raw_path)
                item["relative_path"] = str(relative)
                if not path.is_file() or path.is_symlink() or path.stat().st_size != 0:
                    raise ValueError("文件已变化或不再是空文件")
                item.update(eligible=True, action="quarantine_and_mark_task_failed")
            elif issue_type == "partial_file":
                if await _active_path(db, path):
                    raise ValueError("临时文件属于活跃或暂停任务，跳过维护")
                if not path.is_file() or path.is_symlink() or path.suffix.lower() not in PARTIAL_SUFFIXES:
                    raise ValueError("文件已变化或不是临时文件")
                if now - path.stat().st_mtime < partial_stale_seconds:
                    raise ValueError("临时文件仍在安全观察期内")
                item.update(eligible=True, action="quarantine_stale_partial")
            else:
                if await _active_path(db, path):
                    raise ValueError("文件属于活跃或暂停任务，跳过维护")
                if not path.is_file() or path.is_symlink() or path.suffix.lower() not in MEDIA_SUFFIXES:
                    raise ValueError("文件已变化或不是可识别媒体")
                if await _is_referenced(db, root, raw_path, str(path)):
                    raise ValueError("文件已经被数据库记录引用")
                item.update(eligible=True, action="quarantine_orphan")
        except (OSError, RuntimeError, ValueError) as exc:
            item["reason"] = str(exc)
        plan.append(item)
    return plan


async def _mark_related_task_failed(
    db: AsyncSession, record_kind: str | None, record_id: int | None,
) -> str | None:
    record = await _load_record(db, record_kind or "", record_id)
    if record is not None:
        await db.refresh(record, with_for_update=True)
    if isinstance(record, DownloadTask) and record.status in {"pending", "downloading", "paused"}:
        raise ValueError("任务正在使用该文件，跳过缺失标记")
    task = None
    if isinstance(record, DownloadTask):
        task = record
    elif isinstance(record, DownloadHistory) and record.task_id:
        task = await db.get(DownloadTask, record.task_id)
    elif isinstance(record, XMediaAsset):
        task = await db.get(XDownloadTask, record.task_id)
    elif isinstance(record, PlatformMediaAsset):
        task = await db.get(PlatformDownloadTask, record.task_id)
    if task is None:
        return None
    await db.refresh(task, with_for_update=True)
    if task.status in {"pending", "downloading", "paused"}:
        raise ValueError("任务已经进入活跃状态，未标记失败")
    task.status = "failed"
    if hasattr(task, "phase"):
        task.phase = "failed"
    if hasattr(task, "error_code"):
        task.error_code = "storage_missing"
    task.error_message = STORAGE_MISSING_ERROR
    task.completed_at = datetime.now().replace(tzinfo=None)
    if isinstance(task, DownloadTask):
        from app.services.work_manager import refresh_work_download_state, recalc_author_counts
        work = await db.get(Work, task.work_id)
        if work:
            await refresh_work_download_state(db, work)
            await recalc_author_counts(db, await db.get(Author, work.author_id))
    return f"{getattr(task, 'platform', 'douyin')}:{task.id}"


async def _relink_record(
    db: AsyncSession,
    record_kind: str | None,
    record_id: int | None,
    old_path: str,
    new_path: str,
) -> dict[str, Any]:
    record = await _load_record(db, record_kind or "", record_id)
    if record is not None:
        await db.refresh(record, with_for_update=True)
    if isinstance(record, DownloadTask) and record.status in {"pending", "downloading", "paused"}:
        raise ValueError("任务正在使用该文件，跳过路径回填")
    if record is None or _record_path(record) != old_path:
        raise ValueError("关联记录路径已经变化")
    record.file_path = new_path
    filename = Path(new_path).name
    if isinstance(record, DownloadTask):
        record.file_name = filename
    elif isinstance(record, (XMediaAsset, PlatformMediaAsset)):
        record.filename = filename
    return {
        "record_kind": str(record_kind),
        "record_id": int(record_id or 0),
        "old_path": old_path,
        "new_path": new_path,
    }


def _quarantine(path: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    candidate = destination
    index = 1
    while candidate.exists():
        candidate = destination.with_name(f"{destination.name}.{index}")
        index += 1
    return Path(shutil.move(str(path), str(candidate)))


@contextmanager
def maintenance_lock(root: Path):
    path = root / ".quarantine" / "storage-maintenance.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0"); handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


async def apply_storage_repair_plan(db: AsyncSession, root: Path, targets: list[dict[str, Any]]) -> dict:
    lock = maintenance_lock(root)
    await asyncio.to_thread(lock.__enter__)
    try:
        return await _apply_storage_repair_plan(db, root, targets)
    finally:
        await asyncio.to_thread(lock.__exit__, None, None, None)


async def _apply_storage_repair_plan(
    db: AsyncSession,
    root: Path,
    targets: list[dict[str, Any]],
) -> dict[str, Any]:
    await recover_storage_journals(db, root)
    plan = await build_storage_repair_plan(db, root, targets)
    eligible = [item for item in plan if item["eligible"]]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    quarantine_root = root / ".quarantine" / "storage-maintenance" / timestamp
    moved: list[dict[str, str]] = []
    relinked: list[dict[str, Any]] = []
    marked_tasks: list[str] = []
    apply_errors: list[dict[str, str]] = []
    moved_sources: set[str] = set()
    manifest = {
        "state": "prepared", "created_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root), "moved": moved, "relinked": relinked,
        "marked_tasks": marked_tasks, "apply_errors": apply_errors, "plan": plan,
    }
    manifest_path = quarantine_root / "manifest.json"
    if eligible:
        await asyncio.to_thread(atomic_json, manifest_path, manifest)
    for item in eligible:
        savepoint = await db.begin_nested()
        move_start, relink_start, mark_start = len(moved), len(relinked), len(marked_tasks)
        try:
            if str(item["action"]).startswith("quarantine"):
                source = Path(item["path"]).expanduser().resolve(strict=False)
                source_key = str(source)
                if source_key not in moved_sources:
                    destination = quarantine_root / str(item["relative_path"])
                    if await _active_path(db, source):
                        raise ValueError("文件已属于活跃任务，跳过维护")
                    moved.append({"source": source_key, "quarantine": str(destination), "state": "planned"})
                    await asyncio.to_thread(atomic_json, manifest_path, manifest)
                    moved_path = await asyncio.to_thread(_quarantine, source, destination)
                    moved[-1].update(quarantine=str(moved_path), state="moved")
                    await asyncio.to_thread(atomic_json, manifest_path, manifest)
                    moved_sources.add(source_key)
            if item["action"] in {"mark_task_failed", "quarantine_and_mark_task_failed"}:
                task_key = await _mark_related_task_failed(
                    db, item.get("record_kind"), item.get("record_id"),
                )
                if not task_key:
                    raise ValueError("关联任务不存在，无法标记文件缺失")
                marked_tasks.append(task_key)
            elif item["action"] == "relink_record":
                relinked.append(await _relink_record(
                    db,
                    item.get("record_kind"),
                    item.get("record_id"),
                    str(item["path"]),
                    str(item["suggested_path"]),
                ))
            await savepoint.commit()
        except (OSError, RuntimeError, ValueError, SQLAlchemyError) as exc:
            await savepoint.rollback()
            del relinked[relink_start:]
            del marked_tasks[mark_start:]
            apply_errors.append({"path": str(item["path"]), "reason": str(exc)})
            # 当前项失败不能留下已移动但未提交的文件。
            for move in moved[move_start:]:
                if move["source"] == str(Path(item["path"]).resolve(strict=False)) and move["state"] == "moved":
                    if not Path(move["source"]).exists():
                        os.link(move["quarantine"], move["source"])
                        Path(move["quarantine"]).unlink()
                        move["state"] = "compensated"
                        moved_sources.discard(move["source"])
    if eligible:
        await asyncio.to_thread(atomic_json, manifest_path, manifest)
        db.add(SystemConfig(key="storage-maintenance:" + timestamp, value="committed"))
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            # 与配置同样使用提交标记处理提交结果不确定的连接异常。
            committed = await db.scalar(select(SystemConfig.value).where(
                SystemConfig.key == "storage-maintenance:" + timestamp))
            if committed != "committed":
                for move in reversed(moved):
                    source, destination = Path(move["source"]), Path(move["quarantine"])
                    if destination.exists() and not source.exists():
                        os.link(destination, source)
                        destination.unlink()
                        move["state"] = "compensated"
                manifest["state"] = "compensated"
                await asyncio.to_thread(atomic_json, manifest_path, manifest)
            raise
        manifest["state"] = "partial" if apply_errors else "completed"
        await asyncio.to_thread(atomic_json, manifest_path, manifest)
    return {
        "planned": len(plan),
        "applied": len(eligible) - len(apply_errors),
        "skipped": [item for item in plan if not item["eligible"]],
        "moved": [move for move in moved if move["state"] == "moved"],
        "relinked": relinked,
        "marked_tasks": sorted(set(marked_tasks)),
        "apply_errors": apply_errors,
        "quarantine_root": str(quarantine_root) if eligible else None,
        "recoverable": True,
        "status": manifest["state"] if eligible else "skipped",
    }


async def recover_storage_journals(db: AsyncSession, root: Path) -> None:
    """没有提交标记的中断批次回退文件；已有提交标记只完成日志状态。"""
    entries = storage_journals(root, limit=None)
    unreadable = [entry for entry in entries if entry.get("read_error")]
    if unreadable:
        raise ValueError(f"{len(unreadable)} 份存储维护清单不可读，已停止维护以保护恢复记录；请在可恢复维护清单查看错误并检查共享存储读取权限")
    for entry in entries:
        if entry.get("state") != "prepared":
            continue
        committed = await db.scalar(select(SystemConfig.value).where(
            SystemConfig.key == "storage-maintenance:" + entry["id"]))
        if committed == "committed":
            entry["state"] = "partial" if entry.get("apply_errors") else "completed"
        else:
            blocked = False
            for move in reversed(entry.get("moved", [])):
                if move["state"] not in {"planned", "moved"}:
                    continue
                source, _ = _resolved_under(root, move["source"])
                destination = Path(move["quarantine"])
                destination.relative_to(root / ".quarantine" / "storage-maintenance" / entry["id"])
                if destination.is_symlink():
                    raise ValueError("隔离文件为符号链接，停止恢复")
                if destination.exists():
                    if source.exists() or await _active_path(db, source):
                        blocked = True
                    else:
                        source.parent.mkdir(parents=True, exist_ok=True)
                        os.link(destination, source)
                        destination.unlink()
                        move["state"] = "compensated"
            entry["state"] = "recovery_blocked" if blocked else "compensated"
        await asyncio.to_thread(atomic_json,
            root / ".quarantine" / "storage-maintenance" / entry["id"] / "manifest.json", entry)


def storage_journals(root: Path, *, limit: int | None = 100) -> list[dict]:
    directory = root / ".quarantine" / "storage-maintenance"
    if not directory.exists():
        return []
    entries = []
    for path in sorted(directory.glob("*/manifest.json"), reverse=True)[:limit]:
        if path.is_symlink() or path.parent.is_symlink():
            continue
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(entry, dict):
                raise ValueError("维护清单必须是对象")
        except (OSError, ValueError) as exc:
            entries.append({"id": path.parent.name, "state": "unreadable",
                            "read_error": type(exc).__name__,
                            "message": "历史维护清单不可读取，未恢复或改动任何文件；请检查共享存储权限或清单完整性",
                            "path": str(path), "moved": []})
            continue
        entries.append({"id": path.parent.name, **entry})
    return entries


async def restore_quarantined(db, root: Path, journal_id: str, *, dry_run: bool = True) -> dict:
    lock = maintenance_lock(root)
    await asyncio.to_thread(lock.__enter__)
    try:
        return await _restore_quarantined(db, root, journal_id, dry_run=dry_run)
    finally:
        await asyncio.to_thread(lock.__exit__, None, None, None)


async def _restore_quarantined(db, root: Path, journal_id: str, *, dry_run: bool = True) -> dict:
    if not journal_id.isalnum():
        raise ValueError("维护日志标识无效")
    path = root / ".quarantine" / "storage-maintenance" / journal_id / "manifest.json"
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise ValueError("维护日志不存在或路径不安全")
    entry = json.loads(path.read_text(encoding="utf-8"))
    plan = []
    for move in entry.get("moved", []):
        source, _ = _resolved_under(root, move["source"])
        destination = Path(move["quarantine"])
        relative = destination.relative_to(path.parent)
        for ancestor in [path.parent, *[path.parent.joinpath(*relative.parts[:i]) for i in range(1, len(relative.parts)+1)]]:
            if ancestor.is_symlink():
                raise ValueError("隔离路径包含符号链接，停止恢复")
        destination = destination.resolve()
        destination.relative_to(path.parent.resolve())
        eligible = (not source.exists() and destination.is_file() and not destination.is_symlink()
                    and not await _active_path(db, source))
        plan.append({**move, "eligible": eligible,
                     "reason": None if eligible else "源位置已存在、隔离文件缺失或任务活跃；禁止覆盖"})
    if not dry_run:
        entry["restore_result"] = plan
        await asyncio.to_thread(atomic_json, path, entry)
        for item in plan:
            if item["eligible"]:
                try:
                    source = Path(item["source"])
                    if await _active_path(db, source):
                        raise ValueError("任务已开始使用目标路径")
                    source.parent.mkdir(parents=True, exist_ok=True)
                    item["state"] = "restoring"
                    await asyncio.to_thread(atomic_json, path, entry)
                    os.link(item["quarantine"], item["source"])
                    Path(item["quarantine"]).unlink()
                    item["state"] = "restored"
                    for move in entry.get("moved", []):
                        if move["quarantine"] == item["quarantine"]:
                            move["state"] = "restored"
                except (OSError, ValueError) as exc:
                    item["state"] = "failed"
                    item["reason"] = str(exc)
                await asyncio.to_thread(atomic_json, path, entry)
    return {"dry_run": dry_run, "items": plan,
            "status": "partial" if any(item.get("state") == "failed" or
                        (not item["eligible"] and item.get("state") != "restored") for item in plan) else "completed"}
