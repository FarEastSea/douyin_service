"""抖音采集适配层。

业务 API 与任务编排只依赖这里的契约。上游接口、签名或字段变化时，
修复集中在适配器，下载执行也不需要理解抖音响应结构。
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterable, Literal, Protocol, TypedDict
from urllib.parse import quote, unquote

from app.services.avatar_cache import ensure_author_avatar_cached
from app.services.douyin_cookie import require_douyin_uifid
from app.services.downloader import DouyinDownloader


class DouyinTraversalLimitError(RuntimeError):
    """扫描尚未到达可信边界就触及安全上限。"""

    def __init__(self, message: str, metrics: "DouyinScanMetrics | None" = None):
        super().__init__(message)
        self.metrics = metrics


class ResolvedDouyinInput(TypedDict):
    type: Literal["author", "work"]
    canonical_url: str
    sec_uid: str | None
    aweme_id: str | None


class DouyinWorkPage(TypedDict):
    items: list[dict[str, Any]]
    next_cursor: int | str | None
    has_more: bool


class DouyinSourceHealth(TypedDict):
    ok: bool
    reason: str | None


class DouyinScanMetrics(TypedDict):
    mode: Literal["incremental", "full"]
    pages_requested: int
    stop_reason: Literal["known_boundary", "end_of_list", "cursor_stalled", "max_pages"]
    known_hits: int


class DouyinScanResult(TypedDict):
    items: list[dict[str, Any]]
    metrics: DouyinScanMetrics


def build_author_profile_url(sec_uid: str | None) -> str | None:
    """生成稳定作者主页地址；不依赖具体采集实现。"""
    if not sec_uid:
        return None
    normalized = unquote(str(sec_uid).strip())
    if not normalized:
        return None
    return f"https://www.douyin.com/user/{quote(normalized, safe='')}"


class DouyinSource(Protocol):
    """抖音采集契约；不包含文件落盘和断点续传。"""

    def resolve_input(self, raw_input: str) -> ResolvedDouyinInput: ...
    def fetch_profile(self, sec_uid: str) -> dict[str, Any]: ...
    def list_works(
        self, sec_uid: str, *, cursor: int | str = 0, count: int = 42
    ) -> DouyinWorkPage: ...
    def fetch_work(self, canonical_url: str) -> dict[str, Any]: ...
    def refresh_assets(self, aweme_id: str) -> dict[str, Any]: ...
    def health_check(self) -> DouyinSourceHealth: ...
    def cache_author_avatar(self, author_id: int, source_url: str | None): ...
    def scan_all_works(
        self, sec_uid: str, known_aweme_ids: Iterable[str] = ()
    ) -> DouyinScanResult: ...
    def scan_incremental_works(
        self,
        sec_uid: str,
        known_aweme_ids: Iterable[str],
        *,
        known_streak: int,
        max_pages: int,
        safe_lookback_pages: int,
    ) -> DouyinScanResult: ...


class DouyinWebAdapter:
    """当前抖音 Web 采集实现。"""

    def __init__(self, downloader: DouyinDownloader):
        self._downloader = downloader

    def resolve_input(self, raw_input: str) -> ResolvedDouyinInput:
        resolved = self._downloader.detect_url_type(raw_input)
        input_type = resolved.get("type")
        canonical_url = str(resolved.get("redirect_url") or "")
        if input_type not in {"author", "work"} or not canonical_url:
            raise ValueError("抖音链接解析结果不完整")

        author_match = re.search(r"/user/([^/?#]+)", canonical_url)
        work_match = re.search(r"/(?:video|note)/(\d+)", canonical_url)
        sec_uid = unquote(author_match.group(1)) if author_match else None
        aweme_id = work_match.group(1) if work_match else None
        if input_type == "author" and not sec_uid:
            raise ValueError("无法从作者链接中提取 sec_uid")
        if input_type == "work" and not aweme_id:
            raise ValueError("无法从作品链接中提取 aweme_id")
        return {
            "type": input_type,
            "canonical_url": canonical_url,
            "sec_uid": sec_uid,
            "aweme_id": aweme_id,
        }

    def fetch_profile(self, sec_uid: str) -> dict[str, Any]:
        return self._downloader.get_author_info(sec_uid)

    def list_works(
        self, sec_uid: str, *, cursor: int | str = 0, count: int = 42
    ) -> DouyinWorkPage:
        data = self._downloader.get_work_list(sec_uid, cursor, count)
        raw_items = data.get("aweme_list")
        if not isinstance(raw_items, list):
            raise ValueError(f"抖音作品列表返回异常类型: {type(raw_items).__name__}")

        items: list[dict[str, Any]] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise ValueError("抖音作品列表包含非对象数据")
            items.append(self._downloader.normalize_work_item(raw_item, sec_uid))

        has_more = bool(data.get("has_more", False))
        next_cursor = data.get("max_cursor")
        return {"items": items, "next_cursor": next_cursor, "has_more": has_more}

    def fetch_work(self, canonical_url: str) -> dict[str, Any]:
        return self._downloader.get_single_work(canonical_url)

    def refresh_assets(self, aweme_id: str) -> dict[str, Any]:
        if not str(aweme_id or "").strip():
            raise ValueError("刷新作品资源需要 aweme_id")
        return self._downloader.refresh_work_urls(str(aweme_id))

    def health_check(self) -> DouyinSourceHealth:
        """只检查本地请求上下文，不主动访问抖音，避免健康检查放大风控。"""
        cookie = self._downloader.headers.get("cookie", "")
        if not cookie:
            return {"ok": False, "reason": "未配置抖音 Cookie"}
        try:
            require_douyin_uifid(cookie)
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}
        return {"ok": True, "reason": None}

    def cache_author_avatar(self, author_id: int, source_url: str | None):
        return ensure_author_avatar_cached(
            author_id,
            source_url,
            self._downloader.filepath,
            self._downloader.session,
            timeout=self._downloader.download_timeout,
        )

    def scan_all_works(
        self, sec_uid: str, known_aweme_ids: Iterable[str] = ()
    ) -> DouyinScanResult:
        known_ids = {str(value) for value in known_aweme_ids if value is not None}
        cursor: int | str = 0
        collected: list[dict[str, Any]] = []
        seen_cursors: set[str] = set()
        pages_requested = 0
        known_hits = 0
        while True:
            cursor_key = str(cursor)
            if cursor_key in seen_cursors:
                metrics: DouyinScanMetrics = {
                    "mode": "full",
                    "pages_requested": pages_requested,
                    "stop_reason": "cursor_stalled",
                    "known_hits": known_hits,
                }
                raise DouyinTraversalLimitError(
                    "抖音作品分页游标重复，已停止全量扫描", metrics
                )
            seen_cursors.add(cursor_key)

            page = self.list_works(sec_uid, cursor=cursor)
            pages_requested += 1
            collected.extend(page["items"])
            known_hits += sum(
                1 for item in page["items"]
                if str(item.get("aweme_id") or "") in known_ids
            )
            if not page["has_more"]:
                return {
                    "items": collected,
                    "metrics": {
                        "mode": "full",
                        "pages_requested": pages_requested,
                        "stop_reason": "end_of_list",
                        "known_hits": known_hits,
                    },
                }
            next_cursor = page["next_cursor"]
            if next_cursor is None or str(next_cursor) == str(cursor):
                metrics = {
                    "mode": "full",
                    "pages_requested": pages_requested,
                    "stop_reason": "cursor_stalled",
                    "known_hits": known_hits,
                }
                raise DouyinTraversalLimitError(
                    "抖音作品分页游标未前进，已停止全量扫描", metrics
                )
            cursor = next_cursor
            time.sleep(self._downloader.request_delay)

    def scan_incremental_works(
        self,
        sec_uid: str,
        known_aweme_ids: Iterable[str],
        *,
        known_streak: int,
        max_pages: int,
        safe_lookback_pages: int,
    ) -> DouyinScanResult:
        """从最新页向后扫描，跨过置顶项，在连续已知作品处安全停止。"""
        known_ids = {str(value) for value in known_aweme_ids if value is not None}
        if not known_ids:
            raise ValueError("增量扫描需要至少一个已知作品 ID")

        required_streak = max(3, int(known_streak))
        page_limit = max(1, int(max_pages))
        lookback_pages = max(1, min(int(safe_lookback_pages), page_limit))
        cursor: int | str = 0
        consecutive_known = 0
        known_hits = 0
        collected: list[dict[str, Any]] = []
        collected_ids: set[str] = set()

        for page_number in range(1, page_limit + 1):
            page = self.list_works(sec_uid, cursor=cursor)
            for item in page["items"]:
                aweme_id = str(item.get("aweme_id") or "")
                if not aweme_id:
                    raise ValueError("抖音作品列表缺少 aweme_id")
                if aweme_id not in collected_ids:
                    collected.append(item)
                    collected_ids.add(aweme_id)

                if item.get("is_top"):
                    if aweme_id in known_ids:
                        known_hits += 1
                    continue
                if aweme_id in known_ids:
                    known_hits += 1
                    consecutive_known += 1
                else:
                    consecutive_known = 0

            if not page["has_more"]:
                return {
                    "items": collected,
                    "metrics": {
                        "mode": "incremental",
                        "pages_requested": page_number,
                        "stop_reason": "end_of_list",
                        "known_hits": known_hits,
                    },
                }
            if page_number >= lookback_pages and consecutive_known >= required_streak:
                return {
                    "items": collected,
                    "metrics": {
                        "mode": "incremental",
                        "pages_requested": page_number,
                        "stop_reason": "known_boundary",
                        "known_hits": known_hits,
                    },
                }
            next_cursor = page["next_cursor"]
            if next_cursor is None or str(next_cursor) == str(cursor):
                metrics = {
                    "mode": "incremental",
                    "pages_requested": page_number,
                    "stop_reason": "cursor_stalled",
                    "known_hits": known_hits,
                }
                raise DouyinTraversalLimitError(
                    "抖音作品分页游标未前进，已停止本次增量扫描", metrics
                )
            cursor = next_cursor
            time.sleep(self._downloader.request_delay)

        metrics = {
            "mode": "incremental",
            "pages_requested": page_limit,
            "stop_reason": "max_pages",
            "known_hits": known_hits,
        }
        raise DouyinTraversalLimitError(
            f"增量扫描达到 {page_limit} 页仍未找到连续 {required_streak} 个已知作品，已停止并保留现状",
            metrics,
        )


def build_douyin_source(
    cookie: str,
    filepath: str | None = None,
    runtime_config: dict[str, Any] | None = None,
    request_context: Any = None,
) -> DouyinSource:
    return DouyinWebAdapter(DouyinDownloader(
        cookie, filepath, runtime_config=runtime_config,
        request_context=request_context,
    ))
