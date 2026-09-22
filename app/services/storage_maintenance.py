"""可恢复的存储维护：重新校验后隔离文件并标记缺失任务。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    DownloadHistory,
    DownloadTask,
    PlatformDownloadTask,
    PlatformMediaAsset,
    XDownloadTask,
    XMediaAsset,
)


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
                path, relative = _resolved_under(root, raw_path)
                item["relative_path"] = str(relative)
                if not path.is_file() or path.is_symlink() or path.stat().st_size != 0:
                    raise ValueError("文件已变化或不再是空文件")
                item.update(eligible=True, action="quarantine_and_mark_task_failed")
            elif issue_type == "partial_file":
                if not path.is_file() or path.is_symlink() or path.suffix.lower() not in PARTIAL_SUFFIXES:
                    raise ValueError("文件已变化或不是临时文件")
                if now - path.stat().st_mtime < partial_stale_seconds:
                    raise ValueError("临时文件仍在安全观察期内")
                item.update(eligible=True, action="quarantine_stale_partial")
            else:
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
    task.status = "failed"
    if hasattr(task, "phase"):
        task.phase = "failed"
    if hasattr(task, "error_code"):
        task.error_code = "storage_missing"
    task.error_message = STORAGE_MISSING_ERROR
    task.completed_at = datetime.now().replace(tzinfo=None)
    return f"{getattr(task, 'platform', 'douyin')}:{task.id}"


async def _relink_record(
    db: AsyncSession,
    record_kind: str | None,
    record_id: int | None,
    old_path: str,
    new_path: str,
) -> dict[str, Any]:
    record = await _load_record(db, record_kind or "", record_id)
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


async def apply_storage_repair_plan(
    db: AsyncSession,
    root: Path,
    targets: list[dict[str, Any]],
) -> dict[str, Any]:
    plan = await build_storage_repair_plan(db, root, targets)
    eligible = [item for item in plan if item["eligible"]]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    quarantine_root = root / ".quarantine" / "storage-maintenance" / timestamp
    moved: list[dict[str, str]] = []
    relinked: list[dict[str, Any]] = []
    marked_tasks: list[str] = []
    apply_errors: list[dict[str, str]] = []
    moved_sources: set[str] = set()
    for item in eligible:
        try:
            if str(item["action"]).startswith("quarantine"):
                source = Path(item["path"]).expanduser().resolve(strict=False)
                source_key = str(source)
                if source_key not in moved_sources:
                    destination = quarantine_root / str(item["relative_path"])
                    moved_path = await asyncio.to_thread(_quarantine, source, destination)
                    moved.append({"source": source_key, "quarantine": str(moved_path)})
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
        except (OSError, RuntimeError, ValueError) as exc:
            apply_errors.append({"path": str(item["path"]), "reason": str(exc)})
    if eligible:
        quarantine_root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "root": str(root),
            "moved": moved,
            "relinked": relinked,
            "marked_tasks": sorted(set(marked_tasks)),
            "apply_errors": apply_errors,
            "plan": eligible,
        }
        await asyncio.to_thread(
            (quarantine_root / "manifest.json").write_text,
            json.dumps(manifest, ensure_ascii=False, indent=2),
            "utf-8",
        )
        await db.commit()
    return {
        "planned": len(plan),
        "applied": len(eligible) - len(apply_errors),
        "skipped": [item for item in plan if not item["eligible"]],
        "moved": moved,
        "relinked": relinked,
        "marked_tasks": sorted(set(marked_tasks)),
        "apply_errors": apply_errors,
        "quarantine_root": str(quarantine_root) if eligible else None,
        "recoverable": True,
    }
