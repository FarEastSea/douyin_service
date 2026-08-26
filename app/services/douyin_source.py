"""抖音采集适配层。

业务任务只依赖这里的采集契约；网页接口字段变化时，修复集中在适配器，
不再让订阅、作者同步和下载编排各自理解上游响应。
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Protocol

from app.services.downloader import DouyinDownloader


class DouyinTraversalLimitError(RuntimeError):
    """扫描尚未到达可信边界就触及安全上限。"""


class DouyinSource(Protocol):
    def get_author_info(self, sec_uid: str) -> dict[str, Any]: ...
    def get_all_works(self, share_url: str, sec_uid: str | None = None) -> list[dict[str, Any]]: ...
    def get_incremental_works(
        self,
        sec_uid: str,
        known_aweme_ids: Iterable[str],
        *,
        known_streak: int,
        max_pages: int,
    ) -> list[dict[str, Any]]: ...


class DouyinWebSource:
    """当前抖音 Web 数据源实现。"""

    def __init__(self, downloader: DouyinDownloader):
        self.downloader = downloader

    @property
    def filepath(self) -> str:
        return self.downloader.filepath

    @property
    def session(self):
        return self.downloader.session

    @property
    def download_timeout(self) -> int:
        return self.downloader.download_timeout

    def get_author_info(self, sec_uid: str) -> dict[str, Any]:
        return self.downloader.get_author_info(sec_uid)

    def get_all_works(self, share_url: str, sec_uid: str | None = None) -> list[dict[str, Any]]:
        return self.downloader.get_all_works(share_url, sec_uid=sec_uid)

    def get_incremental_works(
        self,
        sec_uid: str,
        known_aweme_ids: Iterable[str],
        *,
        known_streak: int,
        max_pages: int,
    ) -> list[dict[str, Any]]:
        """从最新页向后扫描，跨过置顶项，在连续已知作品处安全停止。

        如果上游声称还有更多数据，但游标不前进或页数触及上限，则失败关闭，
        避免把不完整结果误判为“没有新作品”。
        """
        known_ids = {str(value) for value in known_aweme_ids if value is not None}
        if not known_ids:
            raise ValueError("增量扫描需要至少一个已知作品 ID")

        required_streak = max(3, int(known_streak))
        page_limit = max(1, int(max_pages))
        cursor = 0
        consecutive_known = 0
        collected: list[dict[str, Any]] = []
        collected_ids: set[str] = set()

        for page_number in range(1, page_limit + 1):
            data = self.downloader.get_work_list(sec_uid, cursor)
            raw_items = data.get("aweme_list") or []
            if not isinstance(raw_items, list):
                raise ValueError(f"抖音作品列表返回异常类型: {type(raw_items).__name__}")

            for raw_item in raw_items:
                normalized = self.downloader._normalize_work_item(raw_item, sec_uid)
                aweme_id = str(normalized.get("aweme_id") or "")
                if not aweme_id:
                    raise ValueError("抖音作品列表缺少 aweme_id")
                if aweme_id not in collected_ids:
                    collected.append(normalized)
                    collected_ids.add(aweme_id)

                # 置顶作品可能来自很早以前，不能参与连续已知边界计算。
                if normalized.get("is_top"):
                    continue
                if aweme_id in known_ids:
                    consecutive_known += 1
                else:
                    consecutive_known = 0

            has_more = bool(data.get("has_more", False))
            if consecutive_known >= required_streak or not has_more:
                return collected

            next_cursor = data.get("max_cursor")
            if next_cursor is None or str(next_cursor) == str(cursor):
                raise DouyinTraversalLimitError("抖音作品分页游标未前进，已停止本次增量扫描")
            cursor = next_cursor
            time.sleep(self.downloader.request_delay)

        raise DouyinTraversalLimitError(
            f"增量扫描达到 {page_limit} 页仍未找到连续 {required_streak} 个已知作品，已停止并保留现状"
        )


def build_douyin_source(
    cookie: str,
    filepath: str | None = None,
    runtime_config: dict[str, Any] | None = None,
) -> DouyinWebSource:
    return DouyinWebSource(DouyinDownloader(cookie, filepath, runtime_config=runtime_config))
