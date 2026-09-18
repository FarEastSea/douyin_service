"""抖音 Web API 请求签名。"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from app.services.douyin_cookie import get_cookie_value
from app.services.vendor.douyin_abogus import ABogus, BrowserFingerprintGenerator
from app.services.vendor.douyin_websign import sign as sign_web_request


DOUYIN_SIGNATURE_CONTRACT = "web-a_bogus-websign-v4"
DOUYIN_WEB_SIGNED_PATHS = frozenset({
    "/aweme/v1/web/aweme/detail/",
    "/aweme/v1/web/aweme/post/",
    "/aweme/v1/web/aweme/favorite/",
    "/aweme/v1/web/aweme/listcollection/",
    "/aweme/v1/web/mix/aweme/",
    "/aweme/v1/web/tab/feed/",
    "/aweme/v1/web/mix/list/",
    "/aweme/v1/web/music/aweme/",
    "/aweme/v1/web/music/list/",
    "/aweme/v1/web/mix/detail/",
    "/aweme/v1/web/mix/listcollection/",
    "/aweme/v1/web/music/detail/",
    "/aweme/v1/web/collects/list/",
    "/aweme/v1/web/collects/video/list/",
})


def douyin_browser_name(user_agent: str) -> str:
    """返回与 User-Agent 一致的抖音 Web 浏览器名称。"""
    normalized = str(user_agent or "")
    if "Edg/" in normalized:
        return "Edge"
    if "Firefox/" in normalized:
        return "Firefox"
    if "Safari/" in normalized and "Chrome/" not in normalized:
        return "Safari"
    return "Chrome"


def douyin_browser_version(user_agent: str) -> str:
    """Extract the browser version that is actually bound to the saved User-Agent."""
    normalized = str(user_agent or "")
    browser = douyin_browser_name(normalized)
    token = {"Edge": "Edg", "Firefox": "Firefox", "Safari": "Version"}.get(browser, "Chrome")
    match = re.search(rf"{re.escape(token)}/([0-9.]+)", normalized)
    return match.group(1) if match else "unknown"


def build_douyin_user_post_url(
    sec_user_id: str,
    max_cursor: int,
    count: int,
    *,
    user_agent: str,
) -> str:
    """Build the browser request contract before signing the user-post API.

    ``msToken`` is not an account Cookie requirement. It is obtained separately
    in the same browser/proxy context and appended before generating a_bogus.
    """
    browser_name = douyin_browser_name(user_agent)
    browser_version = douyin_browser_version(user_agent)
    if browser_version == "unknown":
        raise ValueError("保存的 User-Agent 无法识别浏览器版本，请复制浏览器的完整 User-Agent")
    params = [
        ("device_platform", "webapp"),
        ("aid", "6383"),
        ("channel", "channel_pc_web"),
        ("sec_user_id", str(sec_user_id)),
        ("max_cursor", str(max_cursor)),
        ("locate_query", "false"),
        ("show_live_replay_strategy", "1"),
        ("need_time_list", "1"),
        ("time_list_query", "0"),
        ("whale_cut_token", ""),
        ("cut_version", "1"),
        ("count", str(count)),
        ("publish_video_strategy_type", "2"),
        ("from_user_page", "1"),
        ("update_version_code", "170400"),
        ("pc_client_type", "1"),
        ("pc_libra_divert", "Windows"),
        ("support_h265", "1"),
        ("support_dash", "0"),
        ("version_code", "290100"),
        ("version_name", "29.1.0"),
        ("cookie_enabled", "true"),
        ("screen_width", "1920"),
        ("screen_height", "1080"),
        ("browser_language", "zh-CN"),
        ("browser_platform", "Win32"),
        ("browser_name", browser_name),
        ("browser_version", browser_version),
        ("browser_online", "true"),
        ("engine_name", "Blink"),
        ("engine_version", browser_version),
        ("os_name", "Windows"),
        ("os_version", "10"),
        ("cpu_core_num", "12"),
        ("device_memory", "8"),
        ("platform", "PC"),
        ("downlink", "10"),
        ("effective_type", "4g"),
        ("round_trip_time", "50"),
    ]
    return "https://www.douyin.com/aweme/v1/web/aweme/post/?" + urlencode(params)


def add_douyin_ms_token(url: str, ms_token: str) -> str:
    """把自动获取的 msToken 加入业务参数，随后才能计算 a_bogus。"""
    parsed = urlsplit(url)
    if not parsed.path.startswith("/aweme/"):
        return url
    normalized = str(ms_token or "").strip()
    if not normalized:
        raise ValueError("抖音业务请求缺少自动生成的 msToken")
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() != "mstoken"
    ]
    query.append(("msToken", normalized))
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        urlencode(query),
        parsed.fragment,
    ))


def douyin_signature_diagnostics(
    cookie: str,
    user_agent: str,
    *,
    ms_token_state: str = "not_requested",
) -> dict[str, object]:
    """Return a copy-safe request summary; never expose Cookie or identity values."""
    return {
        "contract": DOUYIN_SIGNATURE_CONTRACT,
        "algorithm": "a_bogus+x-secsdk-web-signature",
        "browser_name": douyin_browser_name(user_agent),
        "browser_version": douyin_browser_version(user_agent),
        "has_uifid": bool(get_cookie_value(cookie, "UIFID")),
        "has_verify_fp": bool(get_cookie_value(cookie, "s_v_web_id")),
        "ms_token_strategy": "automatic_refresh",
        "ms_token_state": str(ms_token_state or "unknown"),
        "web_signature_state": "generated_per_attempt",
        "user_agent_configured": bool(str(user_agent or "").strip()),
    }


def add_douyin_api_signature(
    url: str,
    user_agent: str,
    cookie: str = "",
) -> tuple[str, dict[str, str]]:
    """Generate A-Bogus and the protected-path Web signature as one contract."""
    parsed = urlsplit(url)
    if not parsed.path.startswith("/aweme/"):
        return url, {}

    if any(key.casefold() == "a_bogus" and value for key, value in parse_qsl(parsed.query)):
        raise ValueError("抖音业务请求不能复用旧签名")
    if not parsed.query:
        raise ValueError("抖音业务 API 缺少可签名的查询参数")

    # Cookie、User-Agent、查询参数和 a_bogus 必须描述同一个浏览器环境。
    # 不能固定使用 Edge 指纹，否则 Chrome Cookie 会被 Argus 间歇拒绝。
    fingerprint = BrowserFingerprintGenerator.generate_fingerprint(
        douyin_browser_name(user_agent)
    )
    signature = ABogus(
        fp=fingerprint,
        user_agent=str(user_agent or ""),
    ).generate_abogus(parsed.query)[1]
    if not signature:
        raise ValueError("抖音 a_bogus 签名生成结果为空")

    # 签名器针对 parsed.query 的原始字节生成摘要，因此保留原查询串，
    # 不能在生成签名后再用 urlencode 重排或二次编码。
    a_bogus_query = f"{parsed.query}&a_bogus={quote(signature, safe='')}"
    normalized_path = parsed.path if parsed.path.endswith("/") else f"{parsed.path}/"
    if normalized_path not in DOUYIN_WEB_SIGNED_PATHS:
        return urlunsplit((
            parsed.scheme, parsed.netloc, parsed.path, a_bogus_query, parsed.fragment,
        )), {}

    uifid = get_cookie_value(cookie, "UIFID")
    if not uifid:
        raise ValueError("抖音签名保护接口缺少 Cookie 中的 UIFID")
    pairs = parse_qsl(a_bogus_query, keep_blank_values=True)
    verify_fp = get_cookie_value(cookie, "s_v_web_id")
    if verify_fp:
        pairs.extend((("verifyFp", verify_fp), ("fp", verify_fp)))
    signed_query, _, signature_headers = sign_web_request(pairs, uifid)
    return urlunsplit((
        parsed.scheme, parsed.netloc, parsed.path, signed_query, parsed.fragment,
    )), signature_headers
