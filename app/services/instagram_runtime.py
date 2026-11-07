"""Instagram 会话、持久预算与跨进程单通道；无共享协调时失败关闭。"""

import json
import math
import random
import re
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from hashlib import sha256
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select

from app.core.config import settings
from app.models.models import (
    InstagramAccountState,
    InstagramRequest,
)
from app.services.platform_credentials import (
    _decrypt,
    _encrypt,
    get_platform_cookie_sync,
)


TERMINAL_SCAN_ERRORS = frozenset(
    {
        "not_found",
        "unsafe_url",
        "engine_unavailable",
        "access_denied",
        "parse_error",
        "profile_response_invalid",
        "engine_contract_error",
        "engine_dependency_error",
        "session_endpoint_redirect",
    }
)
SESSION_RECOVERY_ERRORS = frozenset(
    {"auth_required", "checkpoint", "session_changed", "disabled"}
)


class InstagramStop(BaseException):
    """不可被引擎的普通重试捕获的控制信号。"""

    def __init__(self, code, message, until=None):
        self.code, self.message, self.until = code, message, until
        super().__init__(message)


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


def account_state(db):
    row = db.get(InstagramAccountState, 1)
    if row is None:
        # 所有调用者持有 Instagram Redis 通道或配置锁；唯一主键兜底竞争。
        from sqlalchemy.exc import IntegrityError

        try:
            with db.begin_nested():
                row = InstagramAccountState(id=1)
                db.add(row)
                db.flush()
        except IntegrityError:
            row = db.get(InstagramAccountState, 1)
    return row


def normalize_cookie(raw):
    """仅接受 Instagram 的 Cookie Header、Netscape 或浏览器 JSON 导出。"""
    if len(raw.encode("utf-8")) > 131072:
        raise ValueError("Cookie 文件不能超过128KB")
    raw = raw.strip().lstrip("\ufeff")
    cookies = {}
    if raw.startswith(("[", "{")):
        try:
            items = json.loads(raw)
            if isinstance(items, dict):
                items = items.get("cookies", [])
            for item in items:
                domain = str(item.get("domain", "")).lstrip(".").lower()
                if domain == "instagram.com" or domain.endswith(".instagram.com"):
                    if (
                        not item.get("expirationDate")
                        or float(item["expirationDate"]) > time.time()
                    ):
                        cookies[str(item["name"])] = str(item["value"])
        except (AttributeError, TypeError, ValueError, KeyError):
            raise ValueError("Cookie JSON 格式不正确") from None
    elif "\t" in raw:
        for line in raw.splitlines():
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_") :]
            elif line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) != 7:
                continue
            domain = fields[0].lstrip(".").lower()
            if domain == "instagram.com" or domain.endswith(".instagram.com"):
                if (
                    fields[4] == "0"
                    or fields[4].isdigit()
                    and int(fields[4]) > time.time()
                ):
                    cookies[fields[5]] = fields[6]
    else:
        raw = re.sub(r"^cookie\s*:\s*", "", raw, flags=re.IGNORECASE)
        for part in raw.split(";"):
            key, sep, value = part.strip().partition("=")
            if sep:
                cookies[key] = value
    if not cookies.get("sessionid") or not re.fullmatch(
        r"\d+", cookies.get("ds_user_id", "")
    ):
        raise ValueError(
            "Cookie 必须包含有效的 sessionid 和数字 ds_user_id，请从已登录的 Instagram 浏览器导入"
        )
    if any(
        not re.fullmatch(r"[A-Za-z0-9_-]+", key) or re.search(r"[\r\n\x00;]", value)
        for key, value in cookies.items()
    ):
        raise ValueError("Cookie 包含无效字符")
    return "; ".join(f"{key}={value}" for key, value in sorted(cookies.items()))


def connection(db):
    cookie = get_platform_cookie_sync(db, "instagram", "INSTAGRAM_COOKIE")
    if cookie:
        # 首次启动值可能直接来自 .env，与网页保存值走同一规范化，无效值视为未配置。
        try:
            cookie = normalize_cookie(cookie)
        except ValueError:
            cookie = None
    row = account_state(db)
    proxy = _decrypt(row.encrypted_proxy) if row.encrypted_proxy else ""
    fingerprint = sha256(f"{cookie or ''}\n{proxy}".encode()).hexdigest()
    return cookie, proxy, fingerprint


def validate_proxy(value):
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or not parsed.port
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError()
    except ValueError:
        raise ValueError("固定代理必须是包含端口的 HTTP/HTTPS 地址") from None
    if any(char.isspace() for char in value):
        raise ValueError("代理地址不能包含空白字符")
    return value


def set_proxy(db, value):
    row = account_state(db)
    row.encrypted_proxy = _encrypt(validate_proxy(value)) if value else None
    row.status, row.validated_fingerprint = "unchecked", None
    row.last_error = "出口已变更，请检测会话"
    db.commit()


def readiness(db):
    cookie, proxy, fingerprint = connection(db)
    row = account_state(db)
    now = utcnow()
    count = lambda seconds: int(
        db.scalar(
            select(func.count(InstagramRequest.id)).where(
                InstagramRequest.requested_at > now - timedelta(seconds=seconds)
            )
        )
        or 0
    )
    return {
        "configured": bool(cookie),
        "proxy_configured": bool(proxy),
        "status": row.status
        if row.validated_fingerprint == fingerprint
        else "unchecked",
        "validated_at": row.validated_at,
        "cooldown_until": row.cooldown_until,
        "last_error": None
        if row.status == "ready" and row.cooldown_until and row.cooldown_until <= now
        else row.last_error,
        "cooling": bool(row.cooldown_until and row.cooldown_until > now),
        "hourly_used": count(3600),
        "daily_used": count(86400),
        "enabled": settings.snapshot().INSTAGRAM_ENABLED,
    }


@contextmanager
def instagram_channel():
    from redis.exceptions import RedisError

    from app.core.redis_client import redis_client

    lock = redis_client.lock(
        "instagram:execution", timeout=1200, blocking_timeout=0, thread_local=False
    )
    try:
        acquired = lock.acquire(blocking=False)
    except RedisError:
        raise InstagramStop(
            "coordination_unavailable",
            "Instagram 协调暂不可用，已暂停请求",
            utcnow() + timedelta(seconds=60),
        ) from None
    if not acquired:
        raise InstagramStop(
            "channel_busy",
            "另一项 Instagram 任务正在执行",
            utcnow() + timedelta(seconds=60),
        )
    try:
        yield lock
    finally:
        try:
            lock.release()
        except RedisError:
            pass


def reserve_request(db, *, fingerprint, testing=False):
    """预先持久记账；网络失败也占预算，Redis 重启不会重置额度。"""
    current = settings.snapshot()
    now = utcnow()
    row = db.scalar(
        select(InstagramAccountState)
        .where(InstagramAccountState.id == 1)
        .with_for_update()
    )
    if row is None:
        row = account_state(db)
    if not testing and not current.INSTAGRAM_ENABLED:
        raise InstagramStop("disabled", "Instagram 采集尚未启用")
    _, _, latest_fingerprint = connection(db)
    if latest_fingerprint != fingerprint:
        raise InstagramStop("session_changed", "会话或出口已变更，请检测新会话后继续")
    if row.cooldown_until and row.cooldown_until > now:
        raise InstagramStop("cooling", "账号正在保护性冷却", row.cooldown_until)
    if not testing and (
        row.status != "ready" or row.validated_fingerprint != fingerprint
    ):
        raise InstagramStop("auth_required", "请在 Instagram 账号设置中导入并检测会话")
    if until := budget_until(db, current, now):
        raise InstagramStop("waiting_budget", "接口预算耗尽，已保存进度", until)
    if row.next_request_at and row.next_request_at > now:
        wait = (row.next_request_at - now).total_seconds()
        db.rollback()
        return wait
    row.next_request_at = now + timedelta(
        seconds=random.uniform(
            current.INSTAGRAM_REQUEST_DELAY_MIN,
            max(
                current.INSTAGRAM_REQUEST_DELAY_MIN, current.INSTAGRAM_REQUEST_DELAY_MAX
            ),
        )
    )
    db.add(InstagramRequest(requested_at=now))
    db.execute(
        delete(InstagramRequest).where(
            InstagramRequest.requested_at < now - timedelta(days=2)
        )
    )
    db.commit()
    return 0


def budget_until(db, current=None, now=None):
    current, now = current or settings.snapshot(), now or utcnow()
    boundaries = []
    for seconds, limit in (
        (3600, current.INSTAGRAM_HOURLY_BUDGET),
        (86400, current.INSTAGRAM_DAILY_BUDGET),
    ):
        condition = InstagramRequest.requested_at > now - timedelta(seconds=seconds)
        if db.scalar(select(func.count(InstagramRequest.id)).where(condition)) >= limit:
            oldest = db.scalar(
                select(func.min(InstagramRequest.requested_at)).where(condition)
            )
            boundaries.append(oldest + timedelta(seconds=seconds + 1))
    return max(boundaries) if boundaries else None


def block_account(db, code, retry_after=None):
    db.rollback()
    row = account_state(db)
    if code == "rate_limited":
        row.risk_count += 1
        seconds = (3600, 21600, 86400)[min(row.risk_count - 1, 2)]
        try:
            seconds = max(seconds, float(retry_after))
        except (TypeError, ValueError):
            if retry_after:
                try:
                    seconds = max(
                        seconds,
                        parsedate_to_datetime(retry_after).timestamp() - time.time(),
                    )
                except (TypeError, ValueError, OverflowError):
                    pass
        if not math.isfinite(seconds):
            seconds = 86400
        row.cooldown_until = utcnow() + timedelta(seconds=min(seconds, 7 * 86400))
        # 限流不能证明会话有效：保留原状态，已通过检测的会话冷却后由单通道首个请求试探。
        message = "Instagram 请求受限，全部采集进入保护性冷却"
    else:
        row.status = code
        message = "Instagram 登录失效或要求验证，请在正常浏览器处理后重新导入并检测会话"
    row.last_error = message
    db.commit()
    raise InstagramStop(
        code, message, row.cooldown_until if code == "rate_limited" else None
    )
