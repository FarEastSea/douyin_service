"""Read explicit author restrictions without retaining profile payloads or links."""
import re
from typing import Any


def _enabled(value: Any) -> bool:
    return value is True or (type(value) is int and value == 1) or value == "1"


def _text(value: Any) -> str:
    if not isinstance(value, str) or "\ufffd" in value:
        return ""
    # Only human-readable explanation fields are retained, never linked URLs.
    value = re.sub(r"(?:https?://|aweme://)\S+", "", value)
    return " ".join(value.replace("|", " ").split())[:150]


def explicit_author_status(user: Any) -> dict | None:
    if not isinstance(user, dict):
        return None
    info = user.get("punish_remind_info")
    info = info if isinstance(info, dict) else {}
    banned = _enabled(user.get("is_ban"))
    punished = _enabled(info.get("is_punish"))
    if not (banned or punished):
        return None
    content = info.get("punish_content")
    content = content.get("content") if isinstance(content, dict) else content
    title, reason = _text(info.get("punish_title")), _text(content)
    detail = ["平台标记账号封禁" if banned else "平台标记账号受到处罚"]
    ban_type = info.get("ban_type")
    if type(ban_type) is int and ban_type >= 0:
        detail.append(f"处罚类型：{ban_type}")
    detail.extend(part for part in (title, reason) if part)
    return {
        "account_status": "banned" if banned else "restricted",
        "account_status_label": "已封禁" if banned else "账号受处罚",
        "account_status_detail": "；".join(detail)[:200],
    }


def stored_author_status(value: str | None) -> dict:
    if not value or not value.startswith("__ACCOUNT_STATUS__|"):
        return {}
    parts = value.split("|", 3)
    return dict(zip(("account_status", "account_status_label", "account_status_detail"), parts[1:]))
