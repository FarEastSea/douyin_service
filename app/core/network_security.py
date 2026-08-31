"""网络目标校验，集中阻断 SSRF 常见入口。"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit


DOUYIN_ALLOWED_DOMAINS = ("douyin.com", "iesdouyin.com")
DOUYIN_MEDIA_ALLOWED_DOMAINS = (
    "douyinpic.com",
    "douyincdn.com",
    "byteimg.com",
    "ibytedtos.com",
    "snssdk.com",
)
_CLOUD_METADATA_ADDRESSES = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("100.100.100.200"),
    ipaddress.ip_address("fd00:ec2::254"),
}


def _normalized_hostname(hostname: str) -> str:
    return str(hostname or "").strip().strip("[]").rstrip(".").lower()


def _resolved_addresses(hostname: str, port: int) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    host = _normalized_hostname(hostname)
    if not host:
        raise ValueError("目标主机不能为空")

    try:
        return {ipaddress.ip_address(host)}
    except ValueError:
        pass

    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"无法解析目标主机：{host}") from exc

    addresses = {ipaddress.ip_address(record[4][0].split("%", 1)[0]) for record in records}
    if not addresses:
        raise ValueError(f"目标主机没有可用地址：{host}")
    return addresses


def is_allowed_douyin_hostname(hostname: str) -> bool:
    """只接受官方根域及其真正的子域，避免 evildouyin.com 一类后缀绕过。"""
    host = _normalized_hostname(hostname)
    return any(host == domain or host.endswith(f".{domain}") for domain in DOUYIN_ALLOWED_DOMAINS)


def validate_douyin_media_url(url: str) -> str:
    """只允许访问抖音官方图片 CDN，并拒绝私网解析和跨域重定向入口。"""
    try:
        parsed = urlsplit(str(url or "").strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError("抖音图片地址格式不正确") from exc

    hostname = _normalized_hostname(parsed.hostname or "")
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("抖音图片地址必须是无账号信息的 HTTPS 地址")
    if not any(hostname == domain or hostname.endswith(f".{domain}") for domain in DOUYIN_MEDIA_ALLOWED_DOMAINS):
        raise ValueError("图片源地址不属于受信任的抖音图片域名")
    if (port or 443) != 443:
        raise ValueError("抖音图片地址只能使用 HTTPS 标准端口")
    if any(not address.is_global for address in _resolved_addresses(hostname, 443)):
        raise ValueError("抖音图片地址解析到了非公网地址，已拒绝访问")
    return parsed.geturl()


def get_douyin_media_response(session, url: str, *, timeout: int, max_redirects: int = 3):
    """流式获取图片，并逐跳验证重定向仍位于受信任 CDN。"""
    current_url = validate_douyin_media_url(url)
    for _ in range(max_redirects + 1):
        response = session.get(
            current_url, allow_redirects=False, timeout=timeout, stream=True,
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response, current_url
        location = response.headers.get("location")
        if not location:
            return response, current_url
        next_url = urljoin(current_url, location)
        response.close()
        current_url = validate_douyin_media_url(next_url)
    raise ValueError("抖音图片重定向次数过多，已停止访问")


def validate_douyin_url(url: str) -> str:
    """校验抖音 URL 的协议、域名、端口和当前 DNS 解析结果。"""
    try:
        parsed = urlsplit(str(url or "").strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError("抖音链接格式不正确") from exc

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("抖音链接只允许使用 http 或 https 协议")
    if parsed.username or parsed.password:
        raise ValueError("抖音链接不能包含账号信息")
    if not is_allowed_douyin_hostname(parsed.hostname or ""):
        raise ValueError("仅允许访问 douyin.com、iesdouyin.com 及其子域名")

    effective_port = port or (443 if parsed.scheme == "https" else 80)
    if effective_port not in {80, 443}:
        raise ValueError("抖音链接只能使用标准 HTTP/HTTPS 端口")

    addresses = _resolved_addresses(parsed.hostname or "", effective_port)
    if any(not address.is_global for address in addresses):
        raise ValueError("抖音链接解析到了私有、回环或其他非公网地址，已拒绝访问")
    return parsed.geturl()


def get_douyin_response(session, url: str, *, timeout: int, max_redirects: int = 5):
    """逐跳校验重定向，禁止跳出受信任的抖音域名集合。"""
    current_url = validate_douyin_url(url)
    for _ in range(max_redirects + 1):
        response = session.get(current_url, allow_redirects=False, timeout=timeout)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response, current_url

        location = response.headers.get("location")
        if not location:
            return response, current_url
        next_url = urljoin(current_url, location)
        validate_douyin_url(next_url)
        current_url = next_url

    raise ValueError("抖音链接重定向次数过多，已停止访问")


def validate_database_test_target(hostname: str, port: int) -> None:
    """数据库测试允许内网数据库，但拒绝回环和云元数据目标。"""
    addresses = _resolved_addresses(hostname, port)
    blocked = [
        address
        for address in addresses
        if address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address in _CLOUD_METADATA_ADDRESSES
    ]
    if blocked:
        raise ValueError("数据库测试不允许访问回环地址或云元数据地址")
