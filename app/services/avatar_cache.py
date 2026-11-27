"""Cache author images beneath the current platform download directory."""

from __future__ import annotations

import hashlib
import filecmp
import os
import threading
from tempfile import NamedTemporaryFile
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit
import requests

from app.core.network_security import DOUYIN_MEDIA_ALLOWED_DOMAINS, get_image_cdn_response


_CACHE_DIR_NAME = "_author_avatars"
_MAX_AVATAR_BYTES = 8 * 1024 * 1024
_CONTENT_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}
_locks_guard = threading.Lock()
_author_locks: dict[int, threading.Lock] = {}


def _author_lock(author_id: int) -> threading.Lock:
    with _locks_guard:
        return _author_locks.setdefault(author_id, threading.Lock())


def douyin_avatar_asset(url: Optional[str]) -> str:
    """Signature rotation does not change the image or its requested rendition."""
    if not url:
        return ""
    parts = urlsplit(url)
    params = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
              if key.lower() not in {"x-expires", "x-signature", "expires", "signature", "auth_key"}]
    return parts.path + "?" + urlencode(sorted(params))


def _source_digest(source_url: str, platform: str) -> str:
    identity = douyin_avatar_asset(source_url) if platform == "douyin" else source_url.strip()
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


_CDN_DOMAINS = {
    "douyin": DOUYIN_MEDIA_ALLOWED_DOMAINS,
    "instagram": ("cdninstagram.com", "fbcdn.net"),
    "x": ("pbs.twimg.com",),
}


def _cache_dir(download_dir: str, platform: str) -> Path:
    path = Path(download_dir).expanduser().resolve() / _CACHE_DIR_NAME
    if platform != "douyin":
        path = path / platform
    path.mkdir(parents=True, exist_ok=True)
    return path


def _source_path(cache_dir: Path, author_id: int) -> Path:
    return cache_dir / f"{author_id}.source"


def _matches_source(path: Path, url: str, platform: str) -> bool:
    try:
        stored = path.read_text(encoding="ascii").strip()
    except OSError:
        return False
    digest = _source_digest(url, platform)
    if stored == digest:
        return True
    legacy = hashlib.sha256(url.strip().encode("utf-8")).hexdigest()
    if platform != "douyin" or stored != legacy:
        return False
    # Upgrade the proven old URL before replacing it with its renewed signature.
    with NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temp = Path(output.name)
        output.write(digest.encode("ascii"))
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return True


def find_cached_author_avatar(
    author_id: int,
    download_dir: str,
    source_url: Optional[str] = None,
    *, platform: str = "douyin",
) -> Optional[Path]:
    cache_dir = _cache_dir(download_dir, platform)
    if source_url:
        source_path = _source_path(cache_dir, author_id)
        if not _matches_source(source_path, source_url, platform):
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
    *, platform: str = "douyin",
) -> Optional[Path]:
    """Refresh changed sources, but publish only changed image content."""
    if not source_url:
        return find_cached_author_avatar(author_id, download_dir, platform=platform)

    domains = _CDN_DOMAINS[platform]

    with _author_lock(author_id):
        cache_dir = _cache_dir(download_dir, platform)
        digest = _source_digest(source_url, platform)
        source_path = _source_path(cache_dir, author_id)
        cached_path = find_cached_author_avatar(author_id, download_dir, platform=platform)
        if cached_path and _matches_source(source_path, source_url, platform):
            return cached_path

        response, _ = get_image_cdn_response(
            session, source_url, domains, timeout=timeout,
        )
        temp_path: Path | None = None
        try:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            extension = _CONTENT_EXTENSIONS.get(content_type)
            if not extension:
                raise ValueError(f"头像响应不是受支持的图片格式: {content_type or 'unknown'}")

            target_path = cache_dir / f"{author_id}{extension}"
            temp_path = cache_dir / f".{author_id}.{threading.get_ident()}.tmp"
            total = 0
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
            # CDN hosts, signatures and image variants can change without changing
            # the avatar itself. Preserve the original inode and mtime in that case.
            if cached_path and filecmp.cmp(temp_path, cached_path, shallow=False):
                target_path = cached_path
            else:
                os.replace(temp_path, target_path)
            source_path.write_text(digest, encoding="ascii")
            for old_extension in _CONTENT_EXTENSIONS.values():
                old_path = cache_dir / f"{author_id}{old_extension}"
                if old_path != target_path and old_path.is_file():
                    old_path.unlink()
            return target_path
        finally:
            response.close()
            if temp_path and temp_path.exists():
                temp_path.unlink()


def cached_platform_avatar(author_id: int, source_url: Optional[str], download_dir: str, platform: str, proxy: str = "") -> Optional[Path]:
    """Read through a platform-scoped cache without forwarding account credentials to the CDN."""
    try:
        with requests.Session() as session:
            session.trust_env = False
            if proxy:
                session.proxies["https"] = proxy
            return ensure_author_avatar_cached(
                author_id, source_url, download_dir, session, timeout=8, platform=platform,
            )
    except (ValueError, OSError, requests.RequestException):
        return find_cached_author_avatar(author_id, download_dir, platform=platform)
