"""抖音浏览器 Cookie 解析与业务接口身份参数补全。"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def parse_cookie_header(cookie: str) -> dict[str, str]:
    """解析浏览器复制的 Cookie Header String，不接受 JSON/Netscape 格式。"""
    values: dict[str, str] = {}
    for item in str(cookie or "").split(";"):
        name, separator, value = item.strip().partition("=")
        if separator and name.strip():
            values[name.strip().casefold()] = value.strip()
    return values


def get_cookie_value(cookie: str, name: str) -> str:
    return parse_cookie_header(cookie).get(str(name).casefold(), "")


def require_douyin_uifid(cookie: str) -> str:
    """返回完整 Cookie 中的 UIFID；缺失时在本地失败，避免反复撞 Argus。"""
    uifid = get_cookie_value(cookie, "UIFID")
    if not uifid:
        raise ValueError(
            "抖音 Cookie 缺少 UIFID 浏览器身份标识。请在已登录抖音的浏览器中，"
            "从网络请求头复制完整 Cookie 后重新保存。"
        )
    return uifid


def add_uifid_to_douyin_api_url(url: str, cookie: str) -> str:
    """为抖音 Web 业务 API 补上与 Cookie 一致的 uifid 查询参数。"""
    parsed = urlsplit(url)
    if not parsed.path.startswith("/aweme/"):
        return url

    query = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key.casefold() == "uifid" and value for key, value in query):
        return url

    query.append(("uifid", require_douyin_uifid(cookie)))
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        urlencode(query),
        parsed.fragment,
    ))
