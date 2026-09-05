"""抖音单账号请求上下文、密文存储与健康状态。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import asyncio
import os
from typing import Any
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core import redis_client
from app.core.env_config import write_env_updates
from app.models.database import get_sync_db
from app.models.models import DouyinAccountProfile, SystemConfig
from app.services.douyin_cookie import get_cookie_value
from app.services.douyin_errors import DouyinRequestError


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)
PROFILE_ID = 1
KEY_PATH = Path(__file__).resolve().parents[2] / ".runtime" / "douyin-account.key"
SIGNATURE_RECOVERY_MARKER = "douyin_signature_recovery_20260906"


@dataclass(frozen=True)
class DouyinRequestContext:
    profile_id: int
    context_version: str
    cookie: str
    user_agent: str
    proxy_url: str | None


def _fingerprint(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:12]


def _context_version(profile: DouyinAccountProfile) -> str:
    """标识一次密文上下文，避免旧请求结果覆盖新保存的账号状态。"""
    material = "\x00".join((
        profile.encrypted_cookie or "",
        profile.user_agent or "",
        "1" if profile.proxy_enabled else "0",
        profile.encrypted_proxy_url or "",
    ))
    return sha256(material.encode("utf-8")).hexdigest()


def _fernet(*, create: bool) -> Fernet:
    if not KEY_PATH.exists():
        if not create:
            raise RuntimeError("抖音账号密钥不存在，请在设置中心重新保存账号档案")
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
        raise RuntimeError("抖音账号密钥不可用，请检查项目根目录密钥文件权限") from exc


def _encrypt(value: str) -> str:
    return _fernet(create=True).encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(value: str | None) -> str:
    if not value:
        return ""
    try:
        return _fernet(create=False).decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("抖音账号密文无法解密，请在设置中心重新保存账号档案") from exc


def _normalize_user_agent(value: str | None) -> str:
    normalized = " ".join(str(value or DEFAULT_USER_AGENT).split())
    if len(normalized) < 20 or len(normalized) > 512:
        raise ValueError("User-Agent 长度必须在 20 到 512 个字符之间")
    return normalized


def _normalize_proxy_url(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("代理地址格式不正确") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("代理地址必须是包含端口的 http/https 地址")
    return normalized


def _profile_payload(profile: DouyinAccountProfile | None) -> dict[str, Any]:
    if profile is None:
        return {
            "configured": False,
            "name": "默认账号",
            "status": "unconfigured",
            "status_label": "未配置",
            "isolated": False,
            "user_agent": DEFAULT_USER_AGENT,
            "proxy_enabled": False,
            "proxy_label": None,
        }
    proxy_label = None
    if profile.proxy_enabled and profile.encrypted_proxy_url:
        try:
            parsed = urlsplit(_decrypt(profile.encrypted_proxy_url))
            proxy_label = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        except Exception:
            proxy_label = "已配置（无法解密）"
    labels = {
        "unknown": "等待首次请求",
        "healthy": "正常",
        "degraded": "异常",
        "isolated": "已隔离",
    }
    return {
        "configured": bool(profile.encrypted_cookie),
        "name": profile.name,
        "status": profile.status,
        "status_label": labels.get(profile.status, "未知"),
        "isolated": profile.status == "isolated",
        "isolation_reason": profile.isolation_reason,
        "cookie_fingerprint": profile.cookie_fingerprint,
        "has_uifid": bool(profile.uifid_fingerprint),
        "uifid_fingerprint": profile.uifid_fingerprint,
        "user_agent": profile.user_agent,
        "proxy_enabled": bool(profile.proxy_enabled),
        "proxy_label": proxy_label,
        "consecutive_failures": int(profile.consecutive_failures or 0),
        "last_success_at": profile.last_success_at,
        "last_failure_at": profile.last_failure_at,
        "last_failure_code": profile.last_failure_code,
        "last_checked_at": profile.last_checked_at,
        "updated_at": profile.updated_at,
    }


async def get_account_status(db) -> dict[str, Any]:
    profile = await db.scalar(
        select(DouyinAccountProfile).where(DouyinAccountProfile.id == PROFILE_ID)
    )
    return _profile_payload(profile)


async def save_account_profile(
    db,
    *,
    cookie: str | None,
    user_agent: str | None,
    proxy_enabled: bool | None,
    proxy_url: str | None,
) -> dict[str, Any]:
    profile = await db.scalar(
        select(DouyinAccountProfile).where(DouyinAccountProfile.id == PROFILE_ID)
    )
    clean_cookie = str(cookie or "").strip()
    if profile is None and not clean_cookie:
        raise ValueError("首次保存账号档案必须提供完整 Cookie")
    if clean_cookie and not get_cookie_value(clean_cookie, "UIFID"):
        raise ValueError("抖音 Cookie 缺少 UIFID，请从已登录页面复制完整 Cookie Header String")

    effective_proxy_enabled = (
        bool(profile.proxy_enabled) if proxy_enabled is None and profile else bool(proxy_enabled)
    )
    clean_proxy = _normalize_proxy_url(proxy_url) if effective_proxy_enabled and str(proxy_url or "").strip() else None
    clean_user_agent = _normalize_user_agent(
        user_agent if user_agent is not None else (profile.user_agent if profile else None)
    )
    if profile is not None and not clean_cookie:
        try:
            _decrypt(profile.encrypted_cookie)
        except RuntimeError as exc:
            raise ValueError("账号密钥不可用，请重新填写完整 Cookie") from exc
    if (
        profile is not None
        and effective_proxy_enabled
        and not clean_proxy
        and profile.encrypted_proxy_url
    ):
        try:
            _decrypt(profile.encrypted_proxy_url)
        except RuntimeError as exc:
            raise ValueError("代理密钥不可用，请重新填写代理地址") from exc
    if profile is None:
        profile = DouyinAccountProfile(
            id=PROFILE_ID,
            name="默认账号",
            encrypted_cookie=_encrypt(clean_cookie),
            cookie_fingerprint=_fingerprint(clean_cookie),
            uifid_fingerprint=_fingerprint(get_cookie_value(clean_cookie, "UIFID")),
            user_agent=clean_user_agent,
        )
        db.add(profile)
    elif clean_cookie:
        profile.encrypted_cookie = _encrypt(clean_cookie)
        profile.cookie_fingerprint = _fingerprint(clean_cookie)
        profile.uifid_fingerprint = _fingerprint(get_cookie_value(clean_cookie, "UIFID"))

    profile.user_agent = clean_user_agent
    profile.proxy_enabled = effective_proxy_enabled
    if effective_proxy_enabled:
        if clean_proxy:
            profile.encrypted_proxy_url = _encrypt(clean_proxy)
        elif not profile.encrypted_proxy_url:
            raise ValueError("启用代理时必须提供代理地址")
    else:
        profile.encrypted_proxy_url = None
    profile.status = "unknown"
    profile.isolation_reason = None
    profile.consecutive_failures = 0
    profile.last_failure_code = None
    await db.commit()
    await db.refresh(profile)
    await asyncio.to_thread(redis_client.clear_douyin_risk_state)
    return _profile_payload(profile)


def get_request_context_sync(db) -> DouyinRequestContext:
    profile = db.execute(
        select(DouyinAccountProfile).where(DouyinAccountProfile.id == PROFILE_ID)
    ).scalar_one_or_none()
    if profile is None or not profile.encrypted_cookie:
        raise DouyinRequestError("cookie_invalid", detail="douyin account profile is not configured")
    if profile.status == "isolated":
        try:
            redis_client.set_douyin_risk_state("account_isolated", profile.isolation_reason or "isolated", 0)
        except Exception:
            pass
        raise DouyinRequestError("account_isolated", detail=profile.isolation_reason or "isolated")
    try:
        return DouyinRequestContext(
            profile_id=profile.id,
            context_version=_context_version(profile),
            cookie=_decrypt(profile.encrypted_cookie),
            user_agent=_normalize_user_agent(profile.user_agent),
            proxy_url=_decrypt(profile.encrypted_proxy_url) if profile.proxy_enabled else None,
        )
    except (RuntimeError, ValueError) as exc:
        profile.status = "isolated"
        profile.isolation_reason = "credential_decryption_failed"
        profile.last_failure_code = "credential_decryption_failed"
        profile.last_failure_at = datetime.now()
        db.commit()
        try:
            redis_client.set_douyin_risk_state(
                "account_isolated", "credential_decryption_failed", 0
            )
        except Exception:
            pass
        raise DouyinRequestError("account_isolated", detail=str(exc)) from exc


async def get_request_context(db) -> DouyinRequestContext:
    profile = await db.scalar(
        select(DouyinAccountProfile).where(DouyinAccountProfile.id == PROFILE_ID)
    )
    if profile is None or not profile.encrypted_cookie:
        raise DouyinRequestError("cookie_invalid", detail="douyin account profile is not configured")
    if profile.status == "isolated":
        try:
            redis_client.set_douyin_risk_state("account_isolated", profile.isolation_reason or "isolated", 0)
        except Exception:
            pass
        raise DouyinRequestError("account_isolated", detail=profile.isolation_reason or "isolated")
    try:
        return DouyinRequestContext(
            profile_id=profile.id,
            context_version=_context_version(profile),
            cookie=_decrypt(profile.encrypted_cookie),
            user_agent=_normalize_user_agent(profile.user_agent),
            proxy_url=_decrypt(profile.encrypted_proxy_url) if profile.proxy_enabled else None,
        )
    except (RuntimeError, ValueError) as exc:
        profile.status = "isolated"
        profile.isolation_reason = "credential_decryption_failed"
        profile.last_failure_code = "credential_decryption_failed"
        profile.last_failure_at = datetime.now()
        await db.commit()
        try:
            redis_client.set_douyin_risk_state(
                "account_isolated", "credential_decryption_failed", 0
            )
        except Exception:
            pass
        raise DouyinRequestError("account_isolated", detail=str(exc)) from exc


def record_request_result(
    profile_id: int | None,
    context_version: str | None,
    error_code: str | None = None,
) -> bool:
    if not profile_id:
        return False
    db = get_sync_db()
    try:
        profile = db.execute(
            select(DouyinAccountProfile)
            .where(DouyinAccountProfile.id == profile_id)
            .with_for_update()
        ).scalar_one_or_none()
        if profile is None:
            return False
        if not context_version or _context_version(profile) != context_version:
            return profile.status == "isolated"
        now = datetime.now()
        profile.last_checked_at = now
        if profile.status == "isolated":
            if error_code is None:
                profile.last_success_at = now
            else:
                profile.last_failure_at = now
                profile.last_failure_code = error_code
            db.commit()
            return True
        if error_code is None:
            profile.status = "healthy"
            profile.isolation_reason = None
            profile.consecutive_failures = 0
            profile.last_success_at = now
            profile.last_failure_code = None
        else:
            profile.consecutive_failures = int(profile.consecutive_failures or 0) + 1
            profile.last_failure_at = now
            profile.last_failure_code = error_code
            isolate = error_code in {"browser_identity_missing", "cookie_invalid"} or (
                error_code in {"argus_blocked", "rate_limited"}
                and profile.consecutive_failures >= 3
            )
            profile.status = "isolated" if isolate else "degraded"
            profile.isolation_reason = error_code if isolate else None
            if isolate:
                try:
                    redis_client.set_douyin_risk_state(
                        "account_isolated", error_code, 0
                    )
                except Exception:
                    pass
        db.commit()
        return profile.status == "isolated"
    finally:
        db.close()


def migrate_legacy_account_sync() -> bool:
    """一次性把旧明文 Cookie 迁移为独立密文档案。"""
    db = get_sync_db()
    migrated = False
    try:
        profile = db.execute(
            select(DouyinAccountProfile).where(DouyinAccountProfile.id == PROFILE_ID)
        ).scalar_one_or_none()
        legacy_row = db.execute(
            select(SystemConfig).where(SystemConfig.key == "douyin_cookie")
        ).scalar_one_or_none()
        legacy_cookie = str(legacy_row.value or "").strip() if legacy_row else ""
        if not legacy_cookie:
            from app.core.config import settings
            legacy_cookie = str(settings.snapshot().DOUYIN_COOKIE or "").strip()
        if legacy_cookie:
            migrated = True
        if profile is None and legacy_cookie:
            uifid = get_cookie_value(legacy_cookie, "UIFID")
            profile = DouyinAccountProfile(
                id=PROFILE_ID,
                name="默认账号",
                encrypted_cookie=_encrypt(legacy_cookie),
                cookie_fingerprint=_fingerprint(legacy_cookie),
                uifid_fingerprint=_fingerprint(uifid) if uifid else None,
                user_agent=DEFAULT_USER_AGENT,
                status="unknown" if uifid else "isolated",
                isolation_reason=None if uifid else "browser_identity_missing",
            )
            db.add(profile)
        if legacy_row is not None:
            db.delete(legacy_row)
            migrated = True
        db.commit()
    finally:
        db.close()
    if migrated:
        write_env_updates({"DOUYIN_COOKIE": ""})
        try:
            redis_client.redis_client.delete(redis_client.COOKIE_KEY)
        except Exception:
            pass
    return migrated


def recover_legacy_signature_isolation_sync() -> bool:
    """一次性解除被旧版错误归类为账号风控的签名失败状态。"""
    db = get_sync_db()
    recovered = False
    try:
        marker = db.execute(
            select(SystemConfig).where(SystemConfig.key == SIGNATURE_RECOVERY_MARKER)
        ).scalar_one_or_none()
        if marker is not None:
            return False

        profile = db.execute(
            select(DouyinAccountProfile).where(DouyinAccountProfile.id == PROFILE_ID)
        ).scalar_one_or_none()
        if profile is not None and (
            profile.isolation_reason == "argus_blocked"
            or profile.last_failure_code == "argus_blocked"
        ):
            profile.status = "unknown"
            profile.isolation_reason = None
            profile.consecutive_failures = 0
            profile.last_failure_code = None
            recovered = True

        db.add(SystemConfig(key=SIGNATURE_RECOVERY_MARKER, value="completed"))
        db.commit()
    except IntegrityError:
        # 多 Web worker 同时启动时，只允许第一个进程执行一次性恢复。
        db.rollback()
        return False
    finally:
        db.close()

    if recovered:
        try:
            redis_client.clear_douyin_risk_state()
        except Exception:
            pass
    return recovered
