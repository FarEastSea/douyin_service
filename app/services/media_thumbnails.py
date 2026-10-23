"""Versioned thumbnails generated exclusively from validated local media."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import threading
from uuid import uuid4
import warnings

from app.services.media_paths import resolve_media_path


class ThumbnailUnavailable(ValueError):
    pass


_generation_slots = threading.BoundedSemaphore(2)
_resource_locks = [threading.Lock() for _ in range(64)]


def ensure_media_thumbnail(path_value: str, root_value: str, platform: str, resource_id: int, media_type: str) -> Path:
    source = resolve_media_path(path_value, root_value)
    if not source.is_file():
        raise FileNotFoundError("媒体文件不存在")
    root = Path(root_value).expanduser().resolve()
    cache = (root / ".thumbnails").resolve()
    cache.relative_to(root)
    version = source.stat()
    fingerprint = hashlib.sha256(
        f"v1\0{source}\0{version.st_mtime_ns}\0{version.st_size}".encode("utf-8")
    ).hexdigest()[:24]
    prefix = f"{platform}-{resource_id}-"
    target = cache / f"{prefix}{fingerprint}.jpg"
    lock_index = int(hashlib.sha256(prefix.encode()).hexdigest()[:8], 16) % len(_resource_locks)
    with _resource_locks[lock_index]:
        if target.is_file() and target.stat().st_size:
            return target
        if not _generation_slots.acquire(timeout=3):
            raise ThumbnailUnavailable("缩略图正在生成，请稍后重试")
        temporary = cache / f".{prefix}{uuid4().hex}.jpg"
        try:
            cache.mkdir(parents=True, exist_ok=True)
            if media_type == "image":
                _image_thumbnail(source, temporary)
            elif media_type in {"video", "live", "live_photo"}:
                _video_thumbnail(source, temporary)
            else:
                raise ThumbnailUnavailable("该媒体类型没有本地缩略图")
            latest = source.stat()
            if (latest.st_mtime_ns, latest.st_size) != (version.st_mtime_ns, version.st_size):
                raise ThumbnailUnavailable("媒体文件正在更新，请稍后重试")
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise ThumbnailUnavailable("无法生成缩略图")
            os.replace(temporary, target)
            for previous in cache.glob(f"{prefix}*.jpg"):
                if previous != target and not previous.is_symlink():
                    previous.unlink(missing_ok=True)
            return target
        finally:
            temporary.unlink(missing_ok=True)
            _generation_slots.release()


def _image_thumbnail(source: Path, target: Path) -> None:
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                if image.width * image.height > 40_000_000:
                    raise ThumbnailUnavailable("图片尺寸超出缩略图处理范围")
                image.draft("RGB", (480, 480))
                thumbnail = ImageOps.exif_transpose(image)
                thumbnail.thumbnail((480, 480), Image.Resampling.LANCZOS)
                if thumbnail.mode != "RGB":
                    thumbnail = thumbnail.convert("RGB")
                thumbnail.save(target, "JPEG", quality=82, optimize=True)
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ThumbnailUnavailable("图片无法安全生成缩略图") from exc


def _video_thumbnail(source: Path, target: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ThumbnailUnavailable("FFmpeg 未安装，暂不能生成视频缩略图")
    try:
        result = subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-protocol_whitelist", "file,pipe", "-i", str(source), "-frames:v", "1",
             "-an", "-vf", "scale=480:480:force_original_aspect_ratio=decrease", "-threads", "1", str(target)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15, check=False,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
        )
    except subprocess.TimeoutExpired as exc:
        raise ThumbnailUnavailable("视频缩略图生成超时") from exc
    if result.returncode:
        raise ThumbnailUnavailable("视频无法生成缩略图")
