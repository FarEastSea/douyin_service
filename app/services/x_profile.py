"""X/Twitter 用户名与主页 URL 归一化工具。"""

from __future__ import annotations

import re


_RESERVED_X_PATHS = {
    "compose",
    "explore",
    "home",
    "i",
    "intent",
    "login",
    "messages",
    "notifications",
    "search",
    "settings",
    "share",
    "signup",
}

_USERNAME_PATTERN = re.compile(r"^@?([a-zA-Z0-9_]{1,15})$")
_PROFILE_PATTERN = re.compile(r"^(?:https?://)?(?:www\.)?(?:x\.com|twitter\.com)/([^/?#]+)")


def parse_x_username(raw_value: str) -> str:
    """从输入值中提取 X 用户名。"""
    value = (raw_value or "").strip()
    if not value:
        raise ValueError("请输入 X/Twitter 用户主页链接或用户名")

    direct_match = _USERNAME_PATTERN.fullmatch(value)
    if direct_match:
        return direct_match.group(1)

    profile_match = _PROFILE_PATTERN.match(value)
    if profile_match:
        username = profile_match.group(1).strip()
        if username.lower() in _RESERVED_X_PATHS:
            raise ValueError(f"无效的 X/Twitter 用户路径: {username}")

        normalized_match = _USERNAME_PATTERN.fullmatch(username)
        if normalized_match:
            return normalized_match.group(1)

    if value.startswith(("x.com/", "twitter.com/", "www.x.com/", "www.twitter.com/")):
        return parse_x_username(f"https://{value}")

    raise ValueError(f"无法从输入中提取 X/Twitter 用户名: {value}")


def normalize_x_profile_url(raw_value: str, username: str | None = None) -> str:
    """将输入值归一化为标准的 X 用户主页 URL。"""
    normalized_username = username or parse_x_username(raw_value)
    return f"https://x.com/{normalized_username}"