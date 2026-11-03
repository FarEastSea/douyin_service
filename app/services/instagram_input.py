"""Instagram 链接规范化；shortcode 区分大小写，识别阶段不联网。"""

import re
from urllib.parse import urlsplit

USERNAME = r"[A-Za-z0-9._]{1,30}"
RESERVED = {
    "accounts",
    "about",
    "explore",
    "direct",
    "developer",
    "legal",
    "privacy",
    "reels",
    "stories",
    "p",
    "reel",
    "tv",
}


def resolve_instagram_input(value: str):
    from app.services.platform_profile_download import ResolvedPlatformInput
    from app.services.platform_registry import normalize_share_input

    value = normalize_share_input(value).strip()
    if re.fullmatch(r"@?" + USERNAME, value) and (
        value.startswith("@")
        or value.lower()
        not in {"instagram.com", "www.instagram.com", "m.instagram.com"}
    ):
        username = value.lstrip("@").lower()
        if username in RESERVED or username in {".", ".."}:
            raise ValueError("请输入作者用户名或作品链接")
        return ResolvedPlatformInput(
            f"user-{username}", f"https://www.instagram.com/{username}/", "profile"
        )
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname
        not in {"instagram.com", "www.instagram.com", "m.instagram.com"}
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 80, 443}
    ):
        raise ValueError(
            "仅支持 Instagram 官方作者、帖文、Reels、Stories 和 Highlights 链接"
        )
    path = parsed.path
    match = re.fullmatch(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]{1,32})/?", path)
    if match:
        shortcode = match[1]
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        post_id = 0
        for character in shortcode:
            post_id = post_id * 64 + alphabet.index(character)
        return ResolvedPlatformInput(
            f"post-{post_id}", f"https://www.instagram.com/p/{shortcode}/", "work"
        )
    match = re.fullmatch(r"/stories/highlights/(\d+)/?", path)
    if match:
        return ResolvedPlatformInput(
            f"highlight-{match[1]}",
            f"https://www.instagram.com/stories/highlights/{match[1]}/",
            "collection",
        )
    match = re.fullmatch(r"/stories/(" + USERNAME + r")(?:/(\d+))?/?", path)
    if match:
        user, story = match.groups()
        return ResolvedPlatformInput(
            f"story-{story}" if story else f"stories-{user.lower()}",
            f"https://www.instagram.com/stories/{user.lower()}/"
            + (f"{story}/" if story else ""),
            "work" if story else "stories",
        )
    match = re.fullmatch(r"/(" + USERNAME + r")/?", path)
    if match and match[1].lower() not in RESERVED:
        return resolve_instagram_input("@" + match[1])
    raise ValueError(
        "该 Instagram 链接类型暂不支持，请使用作品、作者、Story 或 Highlight 链接"
    )
