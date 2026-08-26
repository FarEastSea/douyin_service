"""可选通知渠道，发送时读取最新网页配置。"""

from __future__ import annotations

from email.message import EmailMessage
import hashlib
import hmac
import json
import smtplib
from typing import Any
from urllib.parse import urlparse

import requests

from app.core import redis_client
from app.core.config import settings


EVENT_SWITCHES = {
    "new_works": "NOTIFY_ON_NEW_WORKS",
    "download_failure": "NOTIFY_ON_DOWNLOAD_FAILURE",
    "douyin_risk": "NOTIFY_ON_RISK",
    "subscription_failure": "NOTIFY_ON_SUBSCRIPTION_FAILURE",
}


def _http_url(value: str, label: str) -> str:
    url = str(value or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label}必须是完整的 http/https 地址")
    return url


def _dedupe(dedupe_key: str, ttl: int) -> bool:
    if not dedupe_key or ttl <= 0:
        return False
    digest = hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()
    try:
        created = redis_client.redis_client.set(
            f"notification:dedupe:{digest}", "1", nx=True, ex=ttl
        )
        return not bool(created)
    except Exception:
        # Redis 短暂不可用不应吞掉重要通知。
        return False


def _send_webhook(config, payload: dict[str, Any]) -> None:
    url = _http_url(config.WEBHOOK_URL, "Webhook 地址")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if config.WEBHOOK_SECRET:
        headers["X-Signature-SHA256"] = hmac.new(
            config.WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
    response = requests.post(url, data=body, headers=headers, timeout=10)
    response.raise_for_status()


def _send_bark(config, payload: dict[str, Any]) -> None:
    server = _http_url(config.BARK_SERVER_URL, "Bark 服务地址")
    if not config.BARK_DEVICE_KEY:
        raise ValueError("Bark Device Key 未配置")
    response = requests.post(
        f"{server}/push",
        json={
            "device_key": config.BARK_DEVICE_KEY,
            "title": payload["title"],
            "body": payload["body"],
            "group": "媒体下载管理系统",
        },
        timeout=10,
    )
    response.raise_for_status()


def _send_email(config, payload: dict[str, Any]) -> None:
    recipients = [item.strip() for item in str(config.SMTP_TO).split(",") if item.strip()]
    sender = str(config.SMTP_FROM or config.SMTP_USERNAME).strip()
    if not config.SMTP_HOST or not sender or not recipients:
        raise ValueError("SMTP 主机、发件地址和收件地址必须配置")
    security = str(config.SMTP_SECURITY or "ssl").strip().lower()
    if security not in {"ssl", "starttls", "none"}:
        raise ValueError("SMTP 安全模式只能是 ssl、starttls 或 none")

    message = EmailMessage()
    message["Subject"] = payload["title"]
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(payload["body"])

    smtp_cls = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
    with smtp_cls(config.SMTP_HOST, int(config.SMTP_PORT), timeout=12) as client:
        if security == "starttls":
            client.starttls()
        if config.SMTP_USERNAME:
            client.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        client.send_message(message)


def _send_gotify(config, payload: dict[str, Any]) -> None:
    server = _http_url(config.GOTIFY_SERVER_URL, "Gotify 服务地址")
    if not config.GOTIFY_TOKEN:
        raise ValueError("Gotify 应用 Token 未配置")
    priority = 8 if payload["level"] == "error" else 5 if payload["level"] == "warning" else 2
    response = requests.post(
        f"{server}/message",
        params={"token": config.GOTIFY_TOKEN},
        json={"title": payload["title"], "message": payload["body"], "priority": priority},
        timeout=10,
    )
    response.raise_for_status()


CHANNELS = {
    "webhook": ("WEBHOOK_ENABLED", _send_webhook),
    "bark": ("BARK_ENABLED", _send_bark),
    "email": ("EMAIL_ENABLED", _send_email),
    "gotify": ("GOTIFY_ENABLED", _send_gotify),
}


def send_notification(
    event: str,
    title: str,
    body: str,
    *,
    level: str = "info",
    metadata: dict[str, Any] | None = None,
    channels: list[str] | None = None,
    dedupe_key: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """顺序发送通知；单渠道失败不会阻断其他渠道或业务任务。"""
    config = settings.snapshot()
    if not force:
        if not config.NOTIFY_ENABLED:
            return {"sent": 0, "skipped": True, "reason": "通知中心未启用", "channels": {}}
        switch = EVENT_SWITCHES.get(event)
        if switch and not bool(getattr(config, switch)):
            return {"sent": 0, "skipped": True, "reason": "该事件通知未启用", "channels": {}}
        if _dedupe(dedupe_key, max(0, int(config.NOTIFY_DEDUPE_SECONDS))):
            return {"sent": 0, "skipped": True, "reason": "重复通知已抑制", "channels": {}}

    selected = set(channels or CHANNELS)
    payload = {
        "event": event,
        "title": str(title)[:200],
        "body": str(body)[:4000],
        "level": level if level in {"info", "warning", "error"} else "info",
        "metadata": metadata or {},
    }
    results: dict[str, dict[str, Any]] = {}
    sent = 0
    for name, (enabled_key, sender) in CHANNELS.items():
        if name not in selected:
            continue
        if not bool(getattr(config, enabled_key)):
            results[name] = {"success": False, "skipped": True, "message": "渠道未启用"}
            continue
        try:
            sender(config, payload)
            results[name] = {"success": True, "message": "发送成功"}
            sent += 1
        except Exception as exc:
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                message = f"远端返回 HTTP {exc.response.status_code}"
            elif isinstance(exc, requests.RequestException):
                message = f"网络请求失败（{type(exc).__name__}）"
            elif isinstance(exc, (smtplib.SMTPException, OSError)):
                message = f"连接或认证失败（{type(exc).__name__}）"
            else:
                message = str(exc)[:300]
            results[name] = {
                "success": False,
                "message": message,
            }
    return {"sent": sent, "skipped": False, "channels": results}
