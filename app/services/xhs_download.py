"""小红书单笔记下载引擎；解析服务与主应用依赖隔离。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

import requests

from app.services.x_downloader import list_media_files

_ALLOWED_MEDIA_DOMAINS = ("xhscdn.com", "xhsimg.com", "xiaohongshu.com")
_ALLOWED_SUFFIXES = {"avif", "gif", "heic", "jpeg", "jpg", "mov", "mp4", "png", "webp"}
XHS_INTERNAL_API_URL = "http://127.0.0.1:5556"


@dataclass(slots=True)
class XhsDownloadResult:
    success: bool
    file_count: int
    return_code: int
    files: list[str] = field(default_factory=list)
    error_message: Optional[str] = None
    error_code: Optional[str] = None
    author_profile: Optional[dict[str, Any]] = None
    discovered_works: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def _cookie_header(cookie_file: Optional[str]) -> str:
    if not cookie_file or not os.path.isfile(cookie_file):
        return ""
    content = Path(cookie_file).read_text(encoding="utf-8").strip()
    if "\t" not in content:
        return content if "=" in content and not content.startswith(("{", "[")) else ""
    cookies: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("#HttpOnly_"):
            line = line.removeprefix("#HttpOnly_")
        elif not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and parts[5].strip():
            cookies.append(f"{parts[5].strip()}={parts[6].strip()}")
    return "; ".join(cookies)


def _safe_segment(value: str, fallback: str) -> str:
    cleaned = re.sub(r'[^0-9A-Za-z._-]+', "_", str(value or "")).strip(" ._")
    return (cleaned[:100] or fallback)


def _media_url(value: str) -> str:
    raw = str(value or "").strip()
    if raw.startswith("//"):
        raw = f"https:{raw}"
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not any(
        host == domain or host.endswith(f".{domain}") for domain in _ALLOWED_MEDIA_DOMAINS
    ):
        raise ValueError("解析服务返回了非小红书媒体地址，已拒绝下载")
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))


def _response_error(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return str(detail or f"HTTP {response.status_code}")[:300]


class XhsApiDownloadEngine:
    """调用仅监听回环地址的隔离解析服务，再由主任务保存媒体。"""

    name = "xhs-api"

    def download_profile(
        self,
        *,
        spec,
        source_url: str,
        source_key: str,
        source_type: str,
        destination: str,
        cookie_file: Optional[str] = None,
        on_line: Optional[Callable[[str], None]] = None,
        on_process: Optional[Callable[[int], None]] = None,
        known_work_ids: Iterable[str] = (),
        max_items: int = 100,
        max_scrolls: int = 40,
        known_streak: int = 5,
        scroll_delay: float = 2.0,
    ) -> XhsDownloadResult:
        del on_process
        log = on_line or (lambda _line: None)
        if source_type == "profile":
            return self._download_author(
                spec=spec,
                source_url=source_url,
                destination=destination,
                cookie_file=cookie_file,
                log=log,
                known_work_ids=known_work_ids,
                max_items=max_items,
                max_scrolls=max_scrolls,
                known_streak=known_streak,
                scroll_delay=scroll_delay,
            )
        if source_type != "work":
            return XhsDownloadResult(False, 0, 64, error_code="invalid_url", error_message="不支持的小红书来源类型")

        cookie = _cookie_header(cookie_file)
        folder = Path(destination).expanduser() / _safe_segment(source_key, "note")
        folder.mkdir(parents=True, exist_ok=True)
        before = set(list_media_files(folder))
        log(f"[{spec.name}] 单条笔记: {source_key}")
        log(f"[{spec.name}] 目标目录: {folder}")
        log(f"[{spec.name}] 单条解析服务仅通过本机回环地址访问，媒体地址限制为小红书官方域名")

        session = requests.Session()
        session.trust_env = False
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
            ),
            "Referer": source_url,
        })
        if cookie:
            session.headers["Cookie"] = cookie

        try:
            response = session.post(
                f"{XHS_INTERNAL_API_URL}/xhs/detail",
                json={"url": source_url, "download": False, "cookie": cookie or None},
                timeout=(5, 60),
            )
            if response.status_code >= 400:
                code = {
                    401: "auth_required",
                    403: "access_denied",
                    429: "rate_limited",
                    502: "engine_unavailable",
                    503: "engine_unavailable",
                    504: "engine_unavailable",
                }.get(response.status_code, "parse_failed")
                return XhsDownloadResult(
                    False, len(before), response.status_code, files=sorted(before),
                    error_code=code,
                    error_message=f"小红书笔记解析失败：{_response_error(response)}",
                )
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            media = data.get("媒体") if isinstance(data, dict) else None
            work_id = data.get("作品ID") if isinstance(data, dict) else None
            if not isinstance(media, list) or not media:
                return XhsDownloadResult(
                    False, len(before), 2, files=sorted(before), error_code="no_media",
                    error_message="小红书解析服务未返回可下载媒体；链接可能已过期、作品不可见或 Cookie 权限不足",
                )

            stable_id = _safe_segment(str(work_id or source_key), "note")
            for position, item in enumerate(media, start=1):
                if not isinstance(item, dict):
                    raise ValueError("媒体列表结构不符合解析服务契约")
                index = int(item.get("序号") or position)
                suffix = str(item.get("扩展名") or "").lower().lstrip(".")
                if suffix == "":
                    suffix = "mp4" if item.get("类型") == "视频" else "jpg"
                if suffix not in _ALLOWED_SUFFIXES:
                    raise ValueError(f"采集服务返回了不支持的媒体格式：{suffix}")
                media_url = _media_url(str(item.get("地址") or ""))
                target = folder / f"{stable_id}_{index:03d}.{suffix}"
                if target.is_file() and target.stat().st_size > 0:
                    log(f"[{spec.name}] 已存在，跳过: {target.name}")
                    continue
                temporary = target.with_suffix(target.suffix + ".part")
                with session.get(media_url, stream=True, timeout=(10, 60)) as media_response:
                    media_response.raise_for_status()
                    content_type = media_response.headers.get("Content-Type", "").lower()
                    if "text/html" in content_type or "application/json" in content_type:
                        raise ValueError("媒体地址返回了页面或 JSON，而不是媒体文件")
                    with temporary.open("wb") as output:
                        for chunk in media_response.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                output.write(chunk)
                if not temporary.is_file() or temporary.stat().st_size == 0:
                    raise ValueError("媒体下载结果为空")
                os.replace(temporary, target)
                log(f"[{spec.name}] 已下载: {target.name}")

            files = list_media_files(folder)
            log(f"[{spec.name}] 完成：本次新增 {len(set(files) - before)}，目录共 {len(files)} 个媒体")
            author = data.get("作者") if isinstance(data.get("作者"), dict) else {}
            stats = data.get("互动") if isinstance(data.get("互动"), dict) else {}
            metadata = {
                "title": data.get("标题") or data.get("描述"),
                "author": {"name": author.get("昵称") or data.get("作者昵称")},
                "published_at": data.get("发布时间"),
                "cover_url": data.get("封面"),
                "like_count": stats.get("点赞") or data.get("点赞数"),
                "comment_count": stats.get("评论") or data.get("评论数"),
                "share_count": stats.get("分享") or data.get("分享数"),
            }
            return XhsDownloadResult(True, len(files), 0, files=files, metadata=metadata)
        except requests.ConnectionError:
            return XhsDownloadResult(
                False, len(before), -1, files=sorted(before), error_code="engine_unavailable",
                error_message="小红书单条下载解析服务未启动，请查看 Jenkins 部署日志和 xhs-api.log",
            )
        except requests.Timeout:
            return XhsDownloadResult(
                False, len(before), -1, files=sorted(before), error_code="request_timeout",
                error_message="小红书笔记解析或媒体下载超时，请稍后重试",
            )
        except (OSError, ValueError, requests.RequestException) as exc:
            return XhsDownloadResult(
                False, len(list_media_files(folder)), -1, files=list_media_files(folder),
                error_code="engine_error", error_message=f"小红书下载失败：{str(exc)[:300]}",
            )
        finally:
            session.close()

    def _download_author(
        self,
        *,
        spec,
        source_url: str,
        destination: str,
        cookie_file: Optional[str],
        log: Callable[[str], None],
        known_work_ids: Iterable[str],
        max_items: int,
        max_scrolls: int,
        known_streak: int,
        scroll_delay: float,
    ) -> XhsDownloadResult:
        from app.services.xhs_profile import XhsProfileError, collect_profile

        del destination, cookie_file

        log(f"[{spec.name}] 正在通过受管浏览器读取作者主页")
        try:
            collected = collect_profile(
                source_url,
                known_ids=known_work_ids,
                max_items=max_items,
                max_scrolls=max_scrolls,
                known_streak=known_streak,
                scroll_delay=scroll_delay,
            )
        except XhsProfileError as exc:
            return XhsDownloadResult(False, 0, -1, error_code=exc.code, error_message=str(exc))

        items = [item for item in collected.get("items", []) if isinstance(item, dict)]
        profile = collected.get("author") if isinstance(collected.get("author"), dict) else None
        if not profile:
            return XhsDownloadResult(
                False, 0, 2, error_code="invalid_profile_payload",
                error_message="小红书作者主页没有返回可验证身份",
            )
        if not items:
            log(f"[{spec.name}] 作者主页当前没有作品")
            return XhsDownloadResult(True, 0, 0, author_profile=profile)
        log(
            f"[{spec.name}] 已发现 {len(items)} 条作品，停止原因: "
            f"{collected.get('stop_reason') or 'unknown'}"
        )
        discovered: list[dict[str, Any]] = []
        for item in items:
            note_id = str(item.get("note_id") or "").strip()
            note_url = str(item.get("source_url") or "").strip()
            if not note_id or not note_url:
                continue
            discovered.append(dict(item))
        if not discovered:
            return XhsDownloadResult(
                False, 0, 2, error_code="invalid_profile_payload",
                error_message="小红书作者主页返回的作品摘要缺少可验证 ID 或地址",
            )
        return XhsDownloadResult(
            True,
            0,
            0,
            author_profile=profile,
            discovered_works=discovered,
        )
