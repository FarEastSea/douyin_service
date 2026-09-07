"""抖音 Web API 请求签名。"""

from __future__ import annotations

from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

from app.services.vendor.douyin_abogus import ABogus, BrowserFingerprintGenerator


def add_douyin_api_signature(url: str, user_agent: str) -> str:
    """为抖音业务 API 的最终查询串生成 a_bogus。"""
    parsed = urlsplit(url)
    if not parsed.path.startswith("/aweme/"):
        return url

    if any(key.casefold() == "a_bogus" and value for key, value in parse_qsl(parsed.query)):
        return url
    if not parsed.query:
        raise ValueError("抖音业务 API 缺少可签名的查询参数")

    fingerprint = BrowserFingerprintGenerator.generate_fingerprint("Edge")
    signature = ABogus(
        fp=fingerprint,
        user_agent=str(user_agent or ""),
    ).generate_abogus(parsed.query)[1]
    if not signature:
        raise ValueError("抖音 a_bogus 签名生成结果为空")

    # 签名器针对 parsed.query 的原始字节生成摘要，因此保留原查询串，
    # 不能在生成签名后再用 urlencode 重排或二次编码。
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        f"{parsed.query}&a_bogus={quote(signature, safe='')}",
        parsed.fragment,
    ))
