"""Release-time repair and guarded recovery of proven metadata CSV sidecars.

Run only while deployment owns the maintenance fence and all writers are stopped.
The recovery directory contains original bytes and a durable SHA-256 journal; a
restore uses those files exclusively and never needs settings or a database.
"""
from __future__ import annotations

import argparse
import csv
import errno
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Iterable

from app.services.csv_metadata import (
    METADATA_COLUMNS,
    metadata_csv_projection,
    spreadsheet_safe_cell,
)


POLICY_VERSION = 1
MAX_SIDECAR_BYTES = 4 * 1024 * 1024
NUMERIC_COLUMNS = (
    "duration_ms", "width", "height", "digg_count", "comment_count",
    "collect_count", "share_count", "play_count",
)
TEXT_COLUMNS = ("aweme_id", "author", "title", "music_title", "music_author")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_MUTATION_MARKER = b"CSV sidecar mutation may have started; manifest is required.\n"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _is_link(path: Path) -> bool:
    # Path.is_junction was added in 3.12; the production runtime also allows 3.11.
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _no_links(path: Path, *, missing: bool = False) -> None:
    """Check every component, including parents outside the download directory."""
    if hasattr(os, "O_PATH"):
        _no_links_by_descriptor(path, missing=missing)
        return
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if missing:
                return
            raise
        if stat.S_ISLNK(mode) or _is_link(current):
            raise RuntimeError("CSV recovery refuses symbolic links or junctions")


def _no_links_by_descriptor(path: Path, *, missing: bool) -> None:
    # O_DIRECTORY | O_NOFOLLOW verifies directory traversal without requiring
    # ancestor attributes, which can be stale on an otherwise usable NFS mount.
    # The final entry still requires metadata and must never be a symbolic link.
    descriptor = os.open(path.anchor, os.O_PATH | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            try:
                child = os.open(part, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                if missing:
                    return
                raise
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise RuntimeError("CSV recovery refuses symbolic links or non-directory ancestors") from exc
                raise
            os.close(descriptor)
            descriptor = child
        if len(path.parts) > 1:
            try:
                mode = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False).st_mode
            except FileNotFoundError:
                if missing:
                    return
                raise
            if stat.S_ISLNK(mode):
                raise RuntimeError("CSV recovery refuses symbolic links or junctions")
    finally:
        os.close(descriptor)


def _inside(path: Path, directory: Path) -> None:
    try:
        path.relative_to(directory)
    except ValueError as exc:
        raise RuntimeError("CSV recovery path escapes its declared directory") from exc


def _regular(path: Path, directory: Path) -> os.stat_result:
    _inside(path, directory)
    _no_links(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("CSV recovery requires regular files")
    return info


def _sync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_bytes(path: Path, content: bytes, *, metadata: dict | None = None) -> None:
    _no_links(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=".csv-safety-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            if metadata:
                if hasattr(os, "fchmod"):
                    os.fchmod(output.fileno(), metadata["mode"])
                if hasattr(os, "fchown"):
                    actual = os.fstat(output.fileno())
                    if (actual.st_uid, actual.st_gid) != (metadata["uid"], metadata["gid"]):
                        os.fchown(output.fileno(), metadata["uid"], metadata["gid"])
            os.fsync(output.fileno())
        if metadata:
            os.utime(temporary, ns=(metadata["atime_ns"], metadata["mtime_ns"]))
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write_manifest(directory: Path, manifest: dict) -> None:
    _atomic_bytes(directory / "manifest.json", json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, indent=2,
    ).encode("utf-8"))


def _durable_directory(directory: Path) -> None:
    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    for child in reversed(missing):
        child.mkdir(mode=0o700)
        # Fsyncing the final directory alone cannot durably link new ancestors.
        _sync_directory(child)
        _sync_directory(child.parent)


def _recovery_directory(service_root: Path, backup_dir: str | Path) -> Path:
    directory = _absolute(backup_dir)
    recovery_root = service_root / ".runtime" / "deploy-recovery"
    _inside(directory, recovery_root)
    if directory == recovery_root:
        raise RuntimeError("A distinct CSV recovery subdirectory is required")
    _no_links(directory, missing=True)
    _durable_directory(directory)
    _no_links(directory)
    directory.chmod(0o700)
    _sync_directory(directory)
    return directory


def _parse_csv(content: bytes) -> list[str] | None:
    if len(content) > MAX_SIDECAR_BYTES:
        return None
    try:
        csv.field_size_limit(MAX_SIDECAR_BYTES)
        rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"), newline=""), strict=True))
    except (UnicodeError, csv.Error):
        return None
    if len(rows) != 2 or tuple(rows[0]) != METADATA_COLUMNS or len(rows[1]) != len(METADATA_COLUMNS):
        return None
    row = dict(zip(METADATA_COLUMNS, rows[1]))
    if not row["aweme_id"] or row["work_type"] not in {"video", "images"}:
        return None
    if not _INTEGER.fullmatch(row["file_index"]):
        return None
    if any(row[column] and not _INTEGER.fullmatch(row[column]) for column in NUMERIC_COLUMNS):
        return None
    if row["published_at"]:
        try:
            if datetime.fromisoformat(row["published_at"]).isoformat() != row["published_at"]:
                return None
        except ValueError:
            return None
    return rows[1]


def _safe_csv(content: bytes) -> bytes:
    row = _parse_csv(content)
    if row is None:
        raise RuntimeError("The recorded CSV no longer has the generated sidecar schema")
    safe = [spreadsheet_safe_cell(value) for value in row]
    if safe == row:
        return content
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(METADATA_COLUMNS)
    writer.writerow(safe)
    return output.getvalue().encode("utf-8-sig")


def _json_matches(path: Path, download_root: Path, row: list[str]) -> bool:
    adjacent = path.with_suffix(".json")
    try:
        info = _regular(adjacent, download_root)
    except FileNotFoundError:
        return False
    except RuntimeError:
        return False
    if info.st_size > MAX_SIDECAR_BYTES:
        return False
    def unique_object(pairs: list[tuple[str, Any]]) -> dict:
        value: dict = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate JSON metadata key")
            value[key] = item
        return value
    try:
        data = json.loads(adjacent.read_bytes().decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeError, ValueError):
        return False
    if not isinstance(data, dict) or set(data) != set(METADATA_COLUMNS):
        return False
    if any(data[key] is not None and not isinstance(data[key], str) for key in TEXT_COLUMNS):
        return False
    if (not isinstance(data["work_type"], str) or data["work_type"] not in {"video", "images"}
            or type(data["file_index"]) is not int or data["file_index"] < 0
            or any(data[key] is not None and (type(data[key]) is not int or data[key] < 0)
                   for key in NUMERIC_COLUMNS)
            or (data["published_at"] is not None and not isinstance(data["published_at"], str))
            or not isinstance(data["hashtags"], list)
            or any(not isinstance(tag, str) for tag in data["hashtags"])):
        return False
    return row in (metadata_csv_projection(data), metadata_csv_projection(data, safe=True))


def database_provenance(service_root: Path) -> tuple[Path, dict[str, set[tuple[str, int, str]]]]:
    """Read the actual service's persisted Web settings, then its task identities."""
    from app.core import env_config
    env_config.ENV_PATH = service_root / ".env"
    from app.core.config import settings
    settings.invalidate()
    from sqlalchemy import select
    from app.models.database import get_sync_engine
    from app.models.models import DownloadHistory, DownloadTask, Work

    download_root = _absolute(settings.snapshot().DOWNLOAD_DIR)
    provenance: dict[str, set[tuple[str, int, str]]] = {}
    statements = (
        select(DownloadTask.file_path, Work.aweme_id, DownloadTask.file_index, Work.work_type)
        .join(Work, Work.id == DownloadTask.work_id).where(DownloadTask.file_path.isnot(None)),
        select(DownloadHistory.file_path, Work.aweme_id, DownloadTask.file_index, Work.work_type)
        .join(Work, Work.id == DownloadHistory.work_id)
        .join(DownloadTask, DownloadTask.id == DownloadHistory.task_id)
        .where(DownloadHistory.file_path.isnot(None), DownloadTask.work_id == Work.id),
    )
    with get_sync_engine().connect() as connection:
        for statement in statements:
            for file_path, aweme_id, file_index, work_type in connection.execute(
                statement.execution_options(yield_per=1000)
            ):
                path = _absolute(file_path)
                try:
                    _inside(path, download_root)
                except RuntimeError:
                    continue
                sidecar = str(path.with_name(path.name + ".csv"))
                provenance.setdefault(os.path.normcase(sidecar), set()).add((
                    str(aweme_id), int(file_index), str(work_type),
                ))
    return download_root, provenance


def _db_matches(path: Path, row: list[str], provenance: dict[str, set[tuple[str, int, str]]]) -> bool:
    fields = dict(zip(METADATA_COLUMNS, row))
    return any(
        fields["aweme_id"] in (aweme_id, spreadsheet_safe_cell(aweme_id))
        and fields["file_index"] == str(index) and fields["work_type"] == work_type
        for aweme_id, index, work_type in provenance.get(os.path.normcase(str(path)), ())
    )


def _walk_csv(download_root: Path, counts: dict[str, int]) -> Iterable[Path]:
    def failed(error: OSError) -> None:
        raise error

    for directory, folders, files in os.walk(download_root, followlinks=False, onerror=failed):
        base = Path(directory)
        for name in list(folders):
            child = base / name
            if _is_link(child):
                folders.remove(name)
                counts["skipped_links"] += 1
        folders.sort()
        for name in sorted(files):
            if not name.endswith(".csv"):
                continue
            path = base / name
            if _is_link(path):
                counts["skipped_links"] += 1
                continue
            yield path


def _metadata(info: os.stat_result) -> dict[str, int]:
    return {"mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid,
            "atime_ns": info.st_atime_ns, "mtime_ns": info.st_mtime_ns}


def _plan(service_root: Path, download_root: Path, directory: Path, provenance: dict) -> dict:
    _no_links(download_root)
    if not download_root.is_dir():
        raise RuntimeError("CSV migration download directory is unavailable")
    counts = {key: 0 for key in ("scanned", "known", "unchanged", "unknown", "skipped_links", "changed")}
    manifest: dict[str, Any] = {
        "version": POLICY_VERSION, "service_root": str(service_root),
        "download_root": str(download_root), "status": "prepared", "counts": counts, "entries": [],
    }
    for path in _walk_csv(download_root, counts):
        info = _regular(path, download_root)
        counts["scanned"] += 1
        if info.st_size > MAX_SIDECAR_BYTES:
            counts["unknown"] += 1
            continue
        content = path.read_bytes()
        row = _parse_csv(content)
        source = "database" if row is not None and _db_matches(path, row, provenance) else None
        if source is None and row is not None and _json_matches(path, download_root, row):
            source = "json"
        if source is None:
            counts["unknown"] += 1
            continue
        counts["known"] += 1
        sanitized = _safe_csv(content)
        if sanitized == content:
            counts["unchanged"] += 1
            continue
        relative = path.relative_to(download_root).as_posix()
        backup = _digest(relative.encode("utf-8")) + ".original"
        backup_path = directory / backup
        if backup_path.exists() or backup_path.is_symlink():
            _regular(backup_path, directory)
            if backup_path.read_bytes() != content:
                raise RuntimeError("An incomplete CSV backup differs from the current original")
        else:
            _atomic_bytes(backup_path, content)
        manifest["entries"].append({
            "path": relative, "backup": backup, "source": source, "state": "prepared",
            "original_sha256": _digest(content), "sanitized_sha256": _digest(sanitized),
            "metadata": _metadata(info),
        })
        counts["changed"] += 1
    # No sidecar can change before every original and this journal are durable.
    _write_manifest(directory, manifest)
    return manifest


def _load_manifest(service_root: Path, directory: Path) -> dict | None:
    path = directory / "manifest.json"
    if not path.exists() and not path.is_symlink():
        if (directory / "mutation-started").exists() or (directory / "mutation-started").is_symlink():
            raise RuntimeError("CSV mutation marker exists but its recovery manifest is missing")
        return None
    _regular(path, directory)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("version") != POLICY_VERSION
            or manifest.get("service_root") != str(service_root)
            or manifest.get("status") not in {"prepared", "sanitizing", "complete", "restoring", "restored"}
            or not isinstance(manifest.get("entries"), list) or not isinstance(manifest.get("counts"), dict)):
        raise RuntimeError("CSV recovery manifest is invalid or belongs to another service")
    download_root = _absolute(manifest["download_root"])
    if str(download_root) != manifest["download_root"]:
        raise RuntimeError("CSV recovery download directory is not absolute")
    _no_links(download_root)
    seen: set[str] = set()
    for entry in manifest["entries"]:
        relative = Path(entry["path"])
        if (relative.is_absolute() or ".." in relative.parts or relative.suffix != ".csv"
                or relative.as_posix() != entry["path"] or entry["path"] in seen
                or entry["backup"] != _digest(entry["path"].encode("utf-8")) + ".original"
                or not _HASH.fullmatch(entry["original_sha256"])
                or not _HASH.fullmatch(entry["sanitized_sha256"])
                or entry["source"] not in {"database", "json"}
                or entry["state"] not in {"prepared", "sanitized", "restored"}):
            raise RuntimeError("CSV recovery manifest contains an invalid entry")
        metadata = entry["metadata"]
        if (set(metadata) != {"mode", "uid", "gid", "atime_ns", "mtime_ns"}
                or any(type(value) is not int or value < 0 for value in metadata.values())
                or metadata["mode"] > 0o7777):
            raise RuntimeError("CSV recovery metadata is invalid")
        seen.add(entry["path"])
    return manifest


def _entry_bytes(entry: dict, directory: Path) -> bytes:
    backup = directory / entry["backup"]
    _regular(backup, directory)
    content = backup.read_bytes()
    if _digest(content) != entry["original_sha256"]:
        raise RuntimeError("CSV recovery original backup failed its SHA-256 check")
    if _digest(_safe_csv(content)) != entry["sanitized_sha256"]:
        raise RuntimeError("CSV recovery sanitized hash disagrees with the recorded policy")
    return content


def _current_hash(path: Path, download_root: Path, entry: dict) -> str:
    _regular(path, download_root)
    digest = _digest(path.read_bytes())
    if digest not in {entry["original_sha256"], entry["sanitized_sha256"]}:
        raise RuntimeError("CSV recovery refuses to overwrite a file changed after migration")
    return digest


def sanitize(service_root: Path, backup_dir: str | Path, *, download_root: Path | None = None,
             provenance: dict | None = None) -> dict:
    """Plan once; resume only entries whose original or sanitized hashes still match."""
    directory = _recovery_directory(service_root, backup_dir)
    manifest = _load_manifest(service_root, directory)
    if manifest is None:
        if download_root is None or provenance is None:
            download_root, provenance = database_provenance(service_root)
        manifest = _plan(service_root, _absolute(download_root), directory, provenance)
    if manifest["status"] in {"restoring", "restored"}:
        raise RuntimeError("This CSV migration has been restored; use a new recovery directory")
    download_root = Path(manifest["download_root"])
    manifest["status"] = "sanitizing"
    _write_manifest(directory, manifest)
    _atomic_bytes(directory / "mutation-started", _MUTATION_MARKER)
    for entry in manifest["entries"]:
        original = _entry_bytes(entry, directory)
        path = download_root / entry["path"]
        if _current_hash(path, download_root, entry) == entry["original_sha256"]:
            _atomic_bytes(path, _safe_csv(original), metadata=entry["metadata"])
        entry["state"] = "sanitized"
        _write_manifest(directory, manifest)
    manifest["status"] = "complete"
    _write_manifest(directory, manifest)
    return manifest["counts"]


def restore(service_root: Path, backup_dir: str | Path) -> dict:
    """Restore exact bytes without reading configuration or opening a DB connection."""
    directory = _recovery_directory(service_root, backup_dir)
    manifest = _load_manifest(service_root, directory)
    if manifest is None:
        # Planning can leave backups, but mutation cannot start without a manifest.
        return {"restored": 0, "state": "not_started"}
    download_root = Path(manifest["download_root"])
    for entry in manifest["entries"]:
        _entry_bytes(entry, directory)
        _current_hash(download_root / entry["path"], download_root, entry)
    manifest["status"] = "restoring"
    _write_manifest(directory, manifest)
    for entry in reversed(manifest["entries"]):
        original = _entry_bytes(entry, directory)
        path = download_root / entry["path"]
        if _current_hash(path, download_root, entry) == entry["sanitized_sha256"]:
            _atomic_bytes(path, original, metadata=entry["metadata"])
        entry["state"] = "restored"
        _write_manifest(directory, manifest)
    manifest["status"] = "restored"
    _write_manifest(directory, manifest)
    return {"restored": len(manifest["entries"]), "state": "restored"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("sanitize", "restore"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--backup-dir", required=True)
    options = parser.parse_args()
    service_root = _absolute(options.root)
    _no_links(service_root)
    if service_root == Path(service_root.anchor) or not (service_root / "main.py").is_file():
        raise RuntimeError("CSV migration requires an application service directory")
    try:
        _regular(service_root / ".runtime" / "deployment-maintenance", service_root)
    except (FileNotFoundError, RuntimeError) as exc:
        raise RuntimeError("CSV migration requires the deployment maintenance fence") from exc
    # Relative persisted download paths are always interpreted in the real service.
    os.chdir(service_root)
    result = (sanitize if options.action == "sanitize" else restore)(service_root, options.backup_dir)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
