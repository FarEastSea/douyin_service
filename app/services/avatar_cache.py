"""Author avatar cache stored under the current Douyin download directory."""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import requests


_CACHE_DIR_NAME = "_author_avatars"
_MAX_AVATAR_BYTES = 8 * 1024 * 1024
_CONTENT_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_ALLOWED_AVATAR_HOST_SUFFIXES = (
    ".douyinpic.com",
    ".douyincdn.com",
    ".byteimg.com",
    ".ibytedtos.com",
    ".snssdk.com",
)
_locks_guard = threading.Lock()
_author_locks: dict[int, threading.Lock] = {}


def _author_lock(author_id: int) -> threading.Lock:
    with _locks_guard:
        return _author_locks.setdefault(author_id, threading.Lock())


def _source_digest(source_url: str) -> str:
    return hashlib.sha256(source_url.strip().encode("utf-8")).hexdigest()


def _cache_dir(download_dir: str) -> Path:
    path = Path(download_dir).expanduser().resolve() / _CACHE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _source_path(cache_dir: Path, author_id: int) -> Path:
    return cache_dir / f"{author_id}.source"


def find_cached_author_avatar(
    author_id: int,
    download_dir: str,
    source_url: Optional[str] = None,
) -> Optional[Path]:
    cache_dir = _cache_dir(download_dir)
    if source_url:
        source_path = _source_path(cache_dir, author_id)
        try:
            if not source_path.is_file() or source_path.read_text(encoding="ascii").strip() != _source_digest(source_url):
                return None
        except OSError:
            return None
    for extension in _CONTENT_EXTENSIONS.values():
        candidate = cache_dir / f"{author_id}{extension}"
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def ensure_author_avatar_cached(
    author_id: int,
    source_url: Optional[str],
    download_dir: str,
    session: requests.Session,
    timeout: int = 20,
) -> Optional[Path]:
    """Download only when the local file is absent or its source URL changed."""
    if not source_url:
        return find_cached_author_avatar(author_id, download_dir)

    parsed_source = urlsplit(source_url)
    hostname = (parsed_source.hostname or "").lower()
    if parsed_source.scheme != "https" or not any(hostname.endswith(suffix) for suffix in _ALLOWED_AVATAR_HOST_SUFFIXES):
        raise ValueError("头像源地址不属于受信任的抖音图片域名")

    with _author_lock(author_id):
        cache_dir = _cache_dir(download_dir)
        digest = _source_digest(source_url)
        source_path = _source_path(cache_dir, author_id)
        cached_path = find_cached_author_avatar(author_id, download_dir)
        if cached_path and source_path.is_file():
            try:
                if source_path.read_text(encoding="ascii").strip() == digest:
                    return cached_path
            except OSError:
                pass

        response = session.get(source_url, timeout=timeout, stream=True)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        extension = _CONTENT_EXTENSIONS.get(content_type)
        if not extension:
            raise ValueError(f"头像响应不是受支持的图片格式: {content_type or 'unknown'}")

        target_path = cache_dir / f"{author_id}{extension}"
        temp_path = cache_dir / f".{author_id}.{threading.get_ident()}.tmp"
        total = 0
        try:
            with temp_path.open("wb") as output:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > _MAX_AVATAR_BYTES:
                        raise ValueError("头像文件超过 8MB 限制")
                    output.write(chunk)
            if total == 0:
                raise ValueError("头像响应为空")
            os.replace(temp_path, target_path)
            source_path.write_text(digest, encoding="ascii")
            for old_extension in _CONTENT_EXTENSIONS.values():
                old_path = cache_dir / f"{author_id}{old_extension}"
                if old_path != target_path and old_path.is_file():
                    old_path.unlink()
            return target_path
        finally:
            response.close()
            if temp_path.exists():
                temp_path.unlink()
