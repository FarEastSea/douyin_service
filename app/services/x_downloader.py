"""X/Twitter 下载引擎适配层。"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import subprocess
import sys
import time
from typing import Callable, Optional, Protocol


logger = logging.getLogger(__name__)

MEDIA_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".mp4",
    ".webm",
    ".mov",
    ".avi",
    ".mkv",
    ".ts",
)

RETURN_CODE_ERROR_MAP = {
    4: ("not_found", "用户未找到或无法访问"),
    16: ("auth_required", "需要有效的 Cookie 文件才能访问"),
    64: ("invalid_url", "URL 格式不正确或不被支持"),
}


@dataclass(slots=True)
class XDownloadRunResult:
    """单次 X 下载执行结果。"""

    success: bool
    file_count: int
    return_code: int
    error_message: Optional[str] = None
    error_code: Optional[str] = None


class XDownloadEngine(Protocol):
    """X 下载引擎接口。"""

    name: str

    def download_profile(
        self,
        *,
        profile_url: str,
        username: str,
        destination: str,
        cookie_file: Optional[str] = None,
        on_line: Optional[Callable[[str], None]] = None,
        task_id: Optional[int] = None,
    ) -> XDownloadRunResult:
        ...


def convert_cookie_header_to_netscape(raw_cookie: str, domain: str = ".x.com") -> str:
    """将浏览器 Cookie 头转换为 Netscape cookies.txt 格式。"""
    raw_cookie = raw_cookie.strip()
    if not raw_cookie:
        return raw_cookie

    if "\t" in raw_cookie and (raw_cookie.startswith("#") or raw_cookie.startswith(".")):
        return raw_cookie

    lines = ["# Netscape HTTP Cookie File"]
    now_epoch = str(int(time.time()) + 86400 * 365)

    for part in raw_cookie.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        lines.append(f"{domain}\tTRUE\t/\tTRUE\t{now_epoch}\t{key}\t{value}")

    return "\n".join(lines) + "\n"


class GalleryDlXDownloadEngine:
    """基于 gallery-dl 的 X 下载引擎。"""

    name = "gallery-dl"

    def download_profile(
        self,
        *,
        profile_url: str,
        username: str,
        destination: str,
        cookie_file: Optional[str] = None,
        on_line: Optional[Callable[[str], None]] = None,
        task_id: Optional[int] = None,
    ) -> XDownloadRunResult:
        user_folder = os.path.join(destination, username)
        os.makedirs(user_folder, exist_ok=True)

        command = [
            sys.executable,
            "-m",
            "gallery_dl",
            profile_url,
            "--destination",
            user_folder,
            "--directory",
            "",
        ]

        if cookie_file and os.path.isfile(cookie_file):
            command.extend(["--cookies", os.path.abspath(cookie_file)])

        def log(line: str) -> None:
            if on_line:
                on_line(line)

        log(f"[gallery-dl] URL: {profile_url}")
        log(f"[gallery-dl] 目标目录: {user_folder}")
        if cookie_file:
            log(f"[gallery-dl] Cookie 文件: {cookie_file}")

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )

            if task_id is not None:
                try:
                    from app.core import redis_client

                    redis_client.set_x_task_pid(task_id, process.pid)
                except Exception:
                    pass

            file_count = 0
            if process.stdout is not None:
                for line in process.stdout:
                    normalized_line = line.rstrip("\n")
                    log(normalized_line)
                    if is_media_download_line(normalized_line):
                        file_count += 1

            process.wait()
            return_code = process.returncode
            if return_code == 0:
                log(f"[gallery-dl] 下载完成，共 {file_count} 个文件")
                return XDownloadRunResult(
                    success=True,
                    file_count=file_count,
                    return_code=return_code,
                )

            error_code, error_message = interpret_gallery_dl_error(return_code)
            log(f"[gallery-dl] 下载失败 (code={return_code}): {error_message}")
            return XDownloadRunResult(
                success=False,
                file_count=file_count,
                return_code=return_code,
                error_message=error_message,
                error_code=error_code,
            )
        except FileNotFoundError:
            error_message = "gallery-dl 未安装，请运行: pip install gallery-dl"
            log(f"[gallery-dl] 错误: {error_message}")
            return XDownloadRunResult(
                success=False,
                file_count=0,
                return_code=-1,
                error_message=error_message,
                error_code="engine_unavailable",
            )
        except Exception as exc:
            error_message = f"{type(exc).__name__}: {exc}"
            log(f"[gallery-dl] 异常: {error_message}")
            return XDownloadRunResult(
                success=False,
                file_count=0,
                return_code=-1,
                error_message=error_message,
                error_code="engine_exception",
            )


def is_media_download_line(line: str) -> bool:
    """判断 gallery-dl 输出行是否代表实际媒体下载。"""
    if not line or line.startswith("#") or line.startswith("["):
        return False
    lower = line.lower().strip()
    return any(lower.endswith(ext) for ext in MEDIA_EXTENSIONS)


def interpret_gallery_dl_error(code: int) -> tuple[str, str]:
    """将 gallery-dl 返回码归一化为业务错误。"""
    if code in RETURN_CODE_ERROR_MAP:
        return RETURN_CODE_ERROR_MAP[code]
    return "engine_error", f"gallery-dl 返回错误码 {code}"


def build_x_download_engine(engine_name: Optional[str] = None) -> XDownloadEngine:
    """根据配置构建 X 下载引擎。"""
    normalized_name = (engine_name or "gallery-dl").strip().lower()
    if normalized_name == "gallery-dl":
        return GalleryDlXDownloadEngine()
    raise ValueError(f"不支持的 X 下载引擎: {normalized_name}")
