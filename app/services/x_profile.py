"""X/Twitter 用户名与主页 URL 归一化工具。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


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


@dataclass(frozen=True, slots=True)
class ResolvedXInput:
    username: str
    source_url: str
    source_type: str
    work_id: str | None = None


def resolve_x_input(raw_value: str) -> ResolvedXInput:
    """识别 X 用户主页或单条动态，并生成稳定规范地址。"""
    value = str(raw_value or "").strip()
    direct_match = _USERNAME_PATTERN.fullmatch(value)
    if direct_match:
        username = direct_match.group(1)
        return ResolvedXInput(username, f"https://x.com/{username}", "profile")

    candidate = value if re.match(r"^https?://", value, re.I) else f"https://{value}"
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").lower()
    if host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        raise ValueError(f"无法识别 X/Twitter 链接: {value}")
    segments = [item for item in parsed.path.split("/") if item]
    if len(segments) >= 3 and segments[1].lower() == "status" and segments[2].isdigit():
        username = segments[0]
        if not _USERNAME_PATTERN.fullmatch(username):
            raise ValueError("X/Twitter 单条动态链接中的用户名无效")
        work_id = segments[2]
        return ResolvedXInput(username, f"https://x.com/{username}/status/{work_id}", "work", work_id)
    if len(segments) >= 4 and [item.lower() for item in segments[:3]] == ["i", "web", "status"] and segments[3].isdigit():
        work_id = segments[3]
        return ResolvedXInput("tweet", f"https://x.com/i/web/status/{work_id}", "work", work_id)
    if len(segments) == 1:
        username = parse_x_username(candidate)
        return ResolvedXInput(username, f"https://x.com/{username}", "profile")
    raise ValueError("仅支持 X/Twitter 用户主页或单条动态链接")


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
