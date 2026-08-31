"""把作品远程封面缓存到当前抖音下载目录。"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Optional

import requests

from app.core.network_security import get_douyin_media_response, validate_douyin_media_url


_CACHE_DIR_NAME = "_work_covers"
_MAX_COVER_BYTES = 12 * 1024 * 1024
_CONTENT_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/avif": ".avif",
}
_cache_lock = threading.Lock()


def _source_digest(source_url: str) -> str:
    return hashlib.sha256(source_url.strip().encode("utf-8")).hexdigest()


def _cache_dir(download_dir: str) -> Path:
    path = Path(download_dir).expanduser().resolve() / _CACHE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _source_path(cache_dir: Path, work_id: int) -> Path:
    return cache_dir / f"{work_id}.source"


def find_cached_work_cover(
    work_id: int,
    download_dir: str,
    source_url: Optional[str] = None,
) -> Optional[Path]:
    cache_dir = _cache_dir(download_dir)
    if source_url:
        source_path = _source_path(cache_dir, work_id)
        try:
            if not source_path.is_file() or source_path.read_text(encoding="ascii").strip() != _source_digest(source_url):
                return None
        except OSError:
            return None
    for extension in _CONTENT_EXTENSIONS.values():
        candidate = cache_dir / f"{work_id}{extension}"
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def ensure_work_cover_cached(
    work_id: int,
    source_url: Optional[str],
    download_dir: str,
    session: requests.Session,
    timeout: int = 20,
) -> Optional[Path]:
    if not source_url:
        return find_cached_work_cover(work_id, download_dir)
    validate_douyin_media_url(source_url)

    with _cache_lock:
        cache_dir = _cache_dir(download_dir)
        digest = _source_digest(source_url)
        source_path = _source_path(cache_dir, work_id)
        cached = find_cached_work_cover(work_id, download_dir, source_url)
        if cached:
            return cached

        response, _ = get_douyin_media_response(
            session, source_url, timeout=timeout,
        )
        temp_path: Path | None = None
        try:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            extension = _CONTENT_EXTENSIONS.get(content_type)
            if not extension:
                raise ValueError(f"作品封面不是受支持的图片格式: {content_type or 'unknown'}")

            target_path = cache_dir / f"{work_id}{extension}"
            temp_path = cache_dir / f".{work_id}.{threading.get_ident()}.tmp"
            total = 0
            with temp_path.open("wb") as output:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > _MAX_COVER_BYTES:
                        raise ValueError("作品封面超过 12MB 限制")
                    output.write(chunk)
            if total == 0:
                raise ValueError("作品封面响应为空")
            os.replace(temp_path, target_path)
            source_path.write_text(digest, encoding="ascii")
            for old_extension in _CONTENT_EXTENSIONS.values():
                old_path = cache_dir / f"{work_id}{old_extension}"
                if old_path != target_path and old_path.is_file():
                    old_path.unlink()
            return target_path
        finally:
            response.close()
            if temp_path and temp_path.exists():
                temp_path.unlink()
