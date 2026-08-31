"""通用平台 Cookie 的加密保存与动态读取。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import os

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from app.core.config import settings
from app.models.models import PlatformCredential


KEY_PATH = Path(__file__).resolve().parents[2] / ".runtime" / "platform-credentials.key"


def _fernet(*, create: bool) -> Fernet:
    if not KEY_PATH.exists():
        if not create:
            raise RuntimeError("平台凭据密钥不存在，请在设置中心重新保存 Cookie")
        KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        try:
            fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(key)
    try:
        return Fernet(KEY_PATH.read_bytes().strip())
    except (OSError, ValueError) as exc:
        raise RuntimeError("平台凭据密钥不可用，请检查项目运行目录权限") from exc


def _encrypt(value: str) -> str:
    return _fernet(create=True).encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(value: str) -> str:
    try:
        return _fernet(create=False).decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("平台 Cookie 密文无法解密，请在设置中心重新保存") from exc


def _fingerprint(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:12]


def get_platform_cookie_sync(db, platform: str, env_key: str) -> str | None:
    normalized = str(platform).strip().lower()
    row = db.execute(
        select(PlatformCredential).where(PlatformCredential.platform == normalized)
    ).scalar_one_or_none()
    if row:
        return _decrypt(row.encrypted_cookie)
    initial = getattr(settings.snapshot(), env_key, None)
    return initial.strip() if initial and initial.strip() else None


async def get_platform_credential_status(db, platform: str, env_key: str) -> dict:
    normalized = str(platform).strip().lower()
    row = await db.scalar(
        select(PlatformCredential).where(PlatformCredential.platform == normalized)
    )
    if row:
        return {"configured": True, "cookie_fingerprint": row.cookie_fingerprint}
    initial = getattr(settings.snapshot(), env_key, None)
    return {
        "configured": bool(initial and initial.strip()),
        "cookie_fingerprint": _fingerprint(initial.strip()) if initial and initial.strip() else None,
    }


async def save_platform_cookie(db, platform: str, cookie: str) -> dict:
    normalized = str(platform).strip().lower()
    clean_cookie = str(cookie or "").strip()
    if not normalized or not clean_cookie:
        raise ValueError("平台和 Cookie 不能为空")
    row = await db.scalar(
        select(PlatformCredential).where(PlatformCredential.platform == normalized)
    )
    encrypted = _encrypt(clean_cookie)
    fingerprint = _fingerprint(clean_cookie)
    if row:
        row.encrypted_cookie = encrypted
        row.cookie_fingerprint = fingerprint
    else:
        db.add(PlatformCredential(
            platform=normalized,
            encrypted_cookie=encrypted,
            cookie_fingerprint=fingerprint,
        ))
    await db.commit()
    return {"configured": True, "cookie_fingerprint": fingerprint}

