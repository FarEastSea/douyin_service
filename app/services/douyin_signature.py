"""抖音 Web API 请求签名。"""

from __future__ import annotations

from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

from app.services.vendor.douyin_abogus import ABogus


def add_douyin_api_signature(url: str, user_agent: str) -> str:
    """为抖音业务 API 的最终查询串生成 a_bogus。"""
    # 保留参数是为了让调用契约与请求上下文绑定；当前上游算法内部使用固定的
    # 浏览器特征，服务端真实验证表明它可与本项目保存的 User-Agent 配合。
    del user_agent
    parsed = urlsplit(url)
    if not parsed.path.startswith("/aweme/"):
        return url

    if any(key.casefold() == "a_bogus" and value for key, value in parse_qsl(parsed.query)):
        return url
    if not parsed.query:
        raise ValueError("抖音业务 API 缺少可签名的查询参数")

    signature = ABogus(platform="Win32").get_value(parsed.query)
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
