"""自动获取并轮换抖音 Web API 使用的 msToken。"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import logging
import secrets
import time
from typing import Any

import requests

from app.core import redis_client
from app.services.douyin_cookie import get_cookie_value
from app.services.vendor.douyin_mstoken_bootstrap import (
    MSTOKEN_DATA_TYPE,
    MSTOKEN_ENDPOINT,
    MSTOKEN_MAGIC,
    MSTOKEN_STR_DATA,
    MSTOKEN_ULR,
    MSTOKEN_VERSION,
)


logger = logging.getLogger(__name__)

_CACHE_PREFIX = "douyin:mstoken:"
_REJECTED_COOKIE_PREFIX = "douyin:mstoken:rejected-cookie:"
_CACHE_TTL_SECONDS = 20 * 60
_REJECTED_COOKIE_TTL_SECONDS = 24 * 60 * 60
_VALID_TOKEN_LENGTHS = {164, 184}
_FALLBACK_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-"


class DouyinMsTokenError(RuntimeError):
    """msToken 端点未能返回可用 token。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class DouyinMsToken:
    value: str
    source: str
    generated_at: int


def build_ms_token_scope(*parts: Any) -> str:
    """生成不暴露 Cookie、代理或账号信息的缓存作用域。"""
    material = "\x00".join(str(part or "") for part in parts)
    return sha256(material.encode("utf-8")).hexdigest()


def _cache_key(scope: str) -> str:
    return f"{_CACHE_PREFIX}{scope}"


def _valid_token(value: Any) -> bool:
    normalized = str(value or "").strip()
    return (
        len(normalized) in _VALID_TOKEN_LENGTHS
        and not any(char.isspace() for char in normalized)
    )


def _account_cookie_rejected(scope: str) -> bool:
    try:
        return bool(redis_client.redis_client.get(f"{_REJECTED_COOKIE_PREFIX}{scope}"))
    except Exception as exc:
        logger.warning("读取抖音账号 Cookie token 拒绝状态失败: %s", exc)
        return False


def _read_cached(scope: str) -> DouyinMsToken | None:
    try:
        raw = redis_client.redis_client.get(_cache_key(scope))
    except Exception as exc:
        logger.warning("读取抖音 msToken 缓存失败，将直接重新获取: %s", exc)
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        invalidate_ms_token(scope)
        return None
    value = str(payload.get("value") or "")
    if not _valid_token(value):
        invalidate_ms_token(scope)
        return None
    return DouyinMsToken(
        value=value,
        source=f"cache_{str(payload.get('source') or 'unknown')}",
        generated_at=int(payload.get("generated_at") or 0),
    )


def _write_cached(scope: str, token: DouyinMsToken) -> None:
    payload = json.dumps(
        {
            "value": token.value,
            "source": token.source,
            "generated_at": token.generated_at,
        },
        ensure_ascii=False,
    )
    try:
        redis_client.redis_client.setex(
            _cache_key(scope), _CACHE_TTL_SECONDS, payload
        )
    except Exception as exc:
        logger.warning("写入抖音 msToken 缓存失败，本次请求仍继续: %s", exc)


def invalidate_ms_token(scope: str, *, reject_account_cookie: bool = False) -> None:
    try:
        redis_client.redis_client.delete(_cache_key(scope))
        if reject_account_cookie:
            redis_client.redis_client.setex(
                f"{_REJECTED_COOKIE_PREFIX}{scope}",
                _REJECTED_COOKIE_TTL_SECONDS,
                "1",
            )
    except Exception as exc:
        logger.warning("清理抖音 msToken 缓存失败: %s", exc)


def _generate_compatible_token() -> str:
    """生成 f2 同规格的短期请求 token；拒绝后会立即轮换。"""
    return "".join(secrets.choice(_FALLBACK_ALPHABET) for _ in range(182)) + "=="


def _request_endpoint_token(
    *,
    user_agent: str,
    proxies: dict[str, str] | None,
    timeout: float,
) -> str:
    payload = {
        "magic": MSTOKEN_MAGIC,
        "version": MSTOKEN_VERSION,
        "dataType": MSTOKEN_DATA_TYPE,
        "strData": MSTOKEN_STR_DATA,
        "ulr": MSTOKEN_ULR,
        "tspFromClient": int(time.time() * 1000),
    }
    try:
        response = requests.post(
            MSTOKEN_ENDPOINT,
            data=json.dumps(payload),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": str(user_agent or ""),
            },
            proxies=proxies or None,
            timeout=max(3.0, min(float(timeout or 10), 30.0)),
        )
    except requests.RequestException as exc:
        raise DouyinMsTokenError(
            f"访问 ByteDance msToken 端点失败: {type(exc).__name__}: {exc}"
        ) from exc

    try:
        if response.status_code >= 400:
            raise DouyinMsTokenError(
                f"ByteDance msToken 端点返回 HTTP {response.status_code}",
                status_code=response.status_code,
            )
        value = str(response.cookies.get("msToken") or "").strip()
        if not _valid_token(value):
            raise DouyinMsTokenError(
                "ByteDance msToken 端点未返回有效的 Set-Cookie: msToken",
                status_code=response.status_code,
            )
        return value
    finally:
        response.close()


def get_douyin_ms_token(
    *,
    scope: str,
    user_agent: str,
    cookie: str = "",
    proxies: dict[str, str] | None = None,
    timeout: float = 10,
    force_refresh: bool = False,
) -> DouyinMsToken:
    """获取 msToken；缓存失效或签名被拒绝时重新请求生成。

    账号 Cookie 继续用于 ``www.douyin.com`` 的业务请求。若 Cookie 自带
    msToken 则优先使用；否则在相同 User-Agent 与代理出口下请求 ByteDance
    token 端点。端点未下发 Cookie 时生成明确标记的短期兼容 token，签名
    一旦被拒绝便立即轮换，不能要求用户手工补充该字段。
    """
    if not force_refresh:
        cookie_token = get_cookie_value(cookie, "msToken")
        if _valid_token(cookie_token) and not _account_cookie_rejected(scope):
            return DouyinMsToken(
                value=cookie_token,
                source="account_cookie",
                generated_at=0,
            )
        cached = _read_cached(scope)
        if cached is not None:
            return cached
    else:
        invalidate_ms_token(scope)

    try:
        value = _request_endpoint_token(
            user_agent=user_agent,
            proxies=proxies or None,
            timeout=timeout,
        )
        source = "endpoint_refreshed" if force_refresh else "endpoint_generated"
    except DouyinMsTokenError as exc:
        # ByteDance 的旧 bootstrap 端点会按地区/版本返回二进制响应但不下发
        # Cookie。f2 同时提供相同格式的临时 token 生成方式；这里明确标记
        # fallback，且业务签名一旦被拒绝便立即废弃并重新生成，绝不伪装成
        # 已从端点取得的真实 Cookie。
        logger.warning("抖音 msToken 端点不可用，改用短期兼容 token: %s", exc)
        value = _generate_compatible_token()
        source = "fallback_refreshed" if force_refresh else "fallback_generated"

    token = DouyinMsToken(
        value=value,
        source=source,
        generated_at=int(time.time()),
    )
    _write_cached(scope, token)
    return token
