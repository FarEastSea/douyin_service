"""可靠、低扰动的 X 媒体下载引擎。"""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from typing import Callable, Optional, Protocol


MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm", ".mov", ".m4v"}


@dataclass(slots=True)
class XDownloadRunResult:
    success: bool
    file_count: int
    return_code: int
    files: list[str] = field(default_factory=list)
    error_message: Optional[str] = None
    error_code: Optional[str] = None


class XDownloadEngine(Protocol):
    name: str

    def download_profile(self, *, profile_url: str, username: str, destination: str,
                         cookie_file: Optional[str] = None,
                         on_line: Optional[Callable[[str], None]] = None,
                         task_id: Optional[int] = None) -> XDownloadRunResult: ...


def convert_cookie_header_to_netscape(raw_cookie: str, domain: str = ".x.com") -> str:
    """将 Cookie 请求头转换为 gallery-dl 可读取的 Netscape 格式。"""
    raw_cookie = raw_cookie.strip()
    if not raw_cookie:
        return raw_cookie
    if "\t" in raw_cookie and (raw_cookie.startswith("#") or raw_cookie.startswith(".")):
        return raw_cookie
    lines = ["# Netscape HTTP Cookie File"]
    for part in raw_cookie.split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key:
            lines.append(f"{domain}\tTRUE\t/\tTRUE\t0\t{key.strip()}\t{value.strip()}")
    return "\n".join(lines) + "\n"


def list_media_files(folder: Path) -> list[str]:
    return sorted(str(path.resolve()) for path in folder.rglob("*")
                  if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS)


class GalleryDlXDownloadEngine:
    name = "gallery-dl"

    def download_profile(self, *, profile_url: str, username: str, destination: str,
                         cookie_file: Optional[str] = None,
                         on_line: Optional[Callable[[str], None]] = None,
                         task_id: Optional[int] = None) -> XDownloadRunResult:
        if importlib.util.find_spec("gallery_dl") is None:
            return XDownloadRunResult(False, 0, -1, error_code="engine_unavailable",
                                      error_message="gallery-dl 未安装，请重新安装 requirements.txt 依赖")

        user_folder = Path(destination).expanduser() / username
        user_folder.mkdir(parents=True, exist_ok=True)
        archive = user_folder / ".download-archive.sqlite3"
        media_url = f"https://x.com/{username}/media"
        command = [
            sys.executable, "-m", "gallery_dl", media_url,
            "--destination", str(user_folder), "--directory", "",
            "--download-archive", str(archive),
            "--sleep-request", "2-5", "--sleep", "1-3",
            "--sleep-429", "120", "--retries", "3",
        ]
        if cookie_file and os.path.isfile(cookie_file):
            command.extend(["--cookies", os.path.abspath(cookie_file)])

        log = on_line or (lambda _line: None)
        log(f"[X] 作者: @{username}")
        log(f"[X] 目标目录: {user_folder}")
        log("[X] 已启用请求间隔、限流冷却、有限重试和下载归档")
        before = set(list_media_files(user_folder))
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace", bufsize=1)
            if task_id is not None:
                try:
                    from app.core import redis_client
                    redis_client.set_x_task_pid(task_id, process.pid)
                except Exception:
                    pass
            if process.stdout:
                for line in process.stdout:
                    log(line.rstrip())
            return_code = process.wait()
            files = list_media_files(user_folder)
            if return_code == 0:
                log(f"[X] 完成：本次新增 {len(set(files) - before)}，目录共 {len(files)} 个媒体")
                return XDownloadRunResult(True, len(files), 0, files=files)
            code, message = interpret_gallery_dl_error(return_code)
            return XDownloadRunResult(False, len(files), return_code, files=files,
                                      error_code=code, error_message=message)
        except Exception as exc:
            return XDownloadRunResult(False, 0, -1, error_code="engine_exception",
                                      error_message=f"{type(exc).__name__}: {exc}")


def is_media_download_line(line: str) -> bool:
    return any(line.lower().strip().endswith(ext) for ext in MEDIA_EXTENSIONS)


def interpret_gallery_dl_error(code: int) -> tuple[str, str]:
    mapping = {
        4: ("not_found", "作者不存在、已删除或当前账号无权访问"),
        16: ("auth_required", "X 要求登录，请在设置中心更新有效 Cookie"),
        64: ("invalid_url", "作者链接格式无效"),
    }
    return mapping.get(code, ("engine_error", f"gallery-dl 执行失败（退出码 {code}），请查看任务日志"))


def build_x_download_engine(engine_name: Optional[str] = None) -> XDownloadEngine:
    normalized = (engine_name or "gallery-dl").strip().lower()
    if normalized != "gallery-dl":
        raise ValueError(f"不支持的 X 下载引擎: {normalized}")
    return GalleryDlXDownloadEngine()
