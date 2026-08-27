"""抖音媒体文件执行层；不负责解析抖音业务响应。"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from app.services.downloader import DouyinDownloader


class DouyinMediaEngine(Protocol):
    def build_file_path(
        self,
        author_name: str,
        desc: str,
        aweme_id: str,
        index: int | None = None,
        is_video: bool = True,
        is_live_photo: bool = False,
    ) -> str: ...

    def probe_status(self, url: str, *, timeout: int = 10) -> int: ...

    def download(
        self,
        url: str,
        file_path: str,
        *,
        task_id: int | None = None,
        progress_callback: Callable[[int, int, float], None] | None = None,
        check_pause: Callable[[], bool] | None = None,
    ) -> dict[str, Any]: ...


class RequestsDouyinMediaEngine:
    """复用现有可靠传输实现，同时把业务层与下载器细节隔离。"""

    def __init__(self, downloader: DouyinDownloader):
        self._downloader = downloader

    def build_file_path(
        self,
        author_name: str,
        desc: str,
        aweme_id: str,
        index: int | None = None,
        is_video: bool = True,
        is_live_photo: bool = False,
    ) -> str:
        return self._downloader.build_file_path(
            author_name,
            desc,
            aweme_id,
            index=index,
            is_video=is_video,
            is_live_photo=is_live_photo,
        )

    def probe_status(self, url: str, *, timeout: int = 10) -> int:
        response = self._downloader.session.head(
            url, timeout=timeout, allow_redirects=True
        )
        try:
            return int(response.status_code)
        finally:
            response.close()

    def download(
        self,
        url: str,
        file_path: str,
        *,
        task_id: int | None = None,
        progress_callback: Callable[[int, int, float], None] | None = None,
        check_pause: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        return self._downloader.download_file_with_resume(
            url=url,
            file_path=file_path,
            task_id=task_id,
            progress_callback=progress_callback,
            check_pause=check_pause,
        )


def build_douyin_media_engine(
    cookie: str,
    filepath: str | None = None,
    runtime_config: dict[str, Any] | None = None,
    request_context: Any = None,
) -> DouyinMediaEngine:
    return RequestsDouyinMediaEngine(
        DouyinDownloader(
            cookie, filepath, runtime_config=runtime_config,
            request_context=request_context,
        )
    )
