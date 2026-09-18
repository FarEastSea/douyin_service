"""Bounded, read-only storage audit shared by Celery and the operations API."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.models import (
    DownloadHistory,
    DownloadTask,
    PlatformMediaAsset,
    XMediaAsset,
)
from app.services.storage_maintenance import find_rebase_candidate


ProgressCallback = Callable[[dict[str, Any]], None]


def _safe_stat(path_value: str | None) -> tuple[bool, int]:
    if not path_value:
        return False, 0
    try:
        path = Path(path_value).expanduser()
        return path.is_file(), path.stat().st_size if path.is_file() else 0
    except (OSError, RuntimeError, ValueError):
        return False, 0


def _scan_storage_files(
    root: Path,
    known: set[str],
    max_files: int,
    allow_orphans: bool,
    progress: ProgressCallback | None,
    sample_limit: int,
) -> tuple[list[dict[str, Any]], list[str], int, int, int]:
    partials: list[dict[str, Any]] = []
    orphan_files: list[str] = []
    scanned_files = 0
    partial_count = 0
    orphan_count = 0
    if not root.is_dir():
        return partials, orphan_files, scanned_files, partial_count, orphan_count
    for path in root.rglob("*"):
        if scanned_files >= max_files:
            break
        try:
            if not path.is_file():
                continue
            try:
                if path.relative_to(root).parts[0] == ".quarantine":
                    continue
            except (IndexError, ValueError):
                continue
            scanned_files += 1
            if progress and scanned_files % 1000 == 0:
                progress({"phase": "files", "scanned_files": scanned_files})
            resolved = str(path.resolve(strict=False))
            if path.suffix.lower() in {".part", ".tmp", ".downloading"}:
                stat = path.stat()
                age_seconds = max(0, datetime.now().timestamp() - stat.st_mtime)
                if age_seconds < 6 * 3600:
                    continue
                partial_count += 1
                if len(partials) < sample_limit:
                    partials.append({
                        "path": resolved,
                        "size_bytes": stat.st_size,
                        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "stale_seconds": int(age_seconds),
                    })
            elif (
                allow_orphans
                and path.suffix.lower() in {
                    ".jpg", ".jpeg", ".png", ".gif", ".webp",
                    ".mp4", ".webm", ".mov", ".m4v",
                }
                and resolved not in known
            ):
                orphan_count += 1
                if len(orphan_files) < sample_limit:
                    orphan_files.append(resolved)
        except OSError:
            continue
    return partials, orphan_files, scanned_files, partial_count, orphan_count


def run_storage_audit(
    db: Session,
    root_value: str,
    *,
    max_records: int = 200_000,
    max_files: int = 50_000,
    sample_limit: int = 200,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Scan registered media and disk files without mutating either source."""
    root = Path(root_value).expanduser().resolve(strict=False)
    known: set[str] = set()
    relinkable: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    zero_byte: list[dict[str, Any]] = []
    relinkable_count = 0
    missing_count = 0
    zero_byte_count = 0
    scanned_records = 0
    total_records = 0
    for count_statement in (
        select(func.count(DownloadTask.id)).where(
            DownloadTask.status == "completed", DownloadTask.file_path.is_not(None),
        ),
        select(func.count(DownloadHistory.id)).where(DownloadHistory.file_path.is_not(None)),
        select(func.count(XMediaAsset.id)),
        select(func.count(PlatformMediaAsset.id)),
    ):
        total_records += int(db.scalar(count_statement) or 0)

    if progress:
        progress({"phase": "records", "total_records": total_records, "scanned_records": 0})

    def consume(kind: str, statement) -> None:
        nonlocal scanned_records, relinkable_count, missing_count, zero_byte_count
        remaining = max_records - scanned_records
        if remaining <= 0:
            return
        rows = db.execute(statement.limit(remaining)).all()
        for record_id, path_value in rows:
            if not path_value:
                continue
            scanned_records += 1
            try:
                candidate = Path(str(path_value)).expanduser()
                if not candidate.is_absolute():
                    candidate = root / candidate
                normalized = str(candidate.resolve(strict=False))
            except (OSError, RuntimeError, ValueError):
                normalized = str(path_value)
            known.add(normalized)
            exists, size = _safe_stat(normalized)
            if not exists:
                rebased = find_rebase_candidate(root, normalized)
                if rebased is not None:
                    relinkable_count += 1
                    rebased_path = str(rebased)
                    known.add(rebased_path)
                    if len(relinkable) + len(missing) < sample_limit:
                        relinkable.append({
                            "kind": kind,
                            "id": int(record_id),
                            "path": normalized,
                            "suggested_path": rebased_path,
                        })
                else:
                    missing_count += 1
                    if len(relinkable) + len(missing) < sample_limit:
                        missing.append({"kind": kind, "id": int(record_id), "path": normalized})
            elif size == 0:
                zero_byte_count += 1
                if len(zero_byte) < sample_limit:
                    zero_byte.append({"kind": kind, "id": int(record_id), "path": normalized})
            if progress and scanned_records % 1000 == 0:
                progress({
                    "phase": "records",
                    "total_records": total_records,
                    "scanned_records": scanned_records,
                })

    consume("download_task", select(DownloadTask.id, DownloadTask.file_path).where(
        DownloadTask.status == "completed", DownloadTask.file_path.is_not(None),
    ))
    consume("download_history", select(DownloadHistory.id, DownloadHistory.file_path).where(
        DownloadHistory.file_path.is_not(None),
    ))
    consume("x_media", select(XMediaAsset.id, XMediaAsset.file_path))
    consume("platform_media", select(PlatformMediaAsset.id, PlatformMediaAsset.file_path))

    records_truncated = total_records > scanned_records
    if progress:
        progress({
            "phase": "files",
            "total_records": total_records,
            "scanned_records": scanned_records,
            "scanned_files": 0,
        })
    partials, orphan_files, scanned_files, partial_count, orphan_count = _scan_storage_files(
        root, known, max_files, not records_truncated, progress, sample_limit,
    )
    disk_target = root
    while not disk_target.exists() and disk_target != disk_target.parent:
        disk_target = disk_target.parent
    disk = shutil.disk_usage(disk_target)
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "root": str(root),
        "scanned_records": scanned_records,
        "scanned_files": scanned_files,
        "total_records": total_records,
        "records_truncated": records_truncated,
        "files_truncated": scanned_files >= max_files,
        "orphan_scan_reliable": not records_truncated,
        "sample_limit": sample_limit,
        "issue_counts": {
            "relinkable_records": relinkable_count,
            "missing_records": missing_count,
            "zero_byte_files": zero_byte_count,
            "partial_files": partial_count,
            "orphan_files": orphan_count,
        },
        "relinkable_records": relinkable,
        "missing_records": missing,
        "zero_byte_files": zero_byte,
        "partial_files": partials,
        "orphan_files": orphan_files,
        "disk": {
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
            "used_percent": round(disk.used / disk.total * 100, 1) if disk.total else 0,
        },
        "note": "结果仅用于核对；旧根目录记录可在预演确认后回填，文件不会移动。其他问题也不会自动处理。",
    }


def storage_repair_targets(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert one trusted audit result into revalidated maintenance targets."""
    targets: list[dict[str, Any]] = []
    for item in report.get("relinkable_records") or []:
        targets.append({
            "issue_type": "stale_record_path",
            "record_kind": item.get("kind"),
            "record_id": item.get("id"),
            "path": item.get("path"),
        })
    for item in report.get("missing_records") or []:
        targets.append({
            "issue_type": "missing_record",
            "record_kind": item.get("kind"),
            "record_id": item.get("id"),
            "path": item.get("path"),
        })
    for item in report.get("zero_byte_files") or []:
        targets.append({
            "issue_type": "zero_byte_file",
            "record_kind": item.get("kind"),
            "record_id": item.get("id"),
            "path": item.get("path"),
        })
    targets.extend(
        {"issue_type": "partial_file", "path": item.get("path")}
        for item in report.get("partial_files") or []
    )
    targets.extend(
        {"issue_type": "orphan_file", "path": path}
        for path in report.get("orphan_files") or []
    )
    return targets
