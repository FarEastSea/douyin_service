"""可复用的主页媒体下载适配层；首个实现为 TikTok + gallery-dl。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Callable, Optional

from app.core.config import settings
from app.services.platform_credentials import get_platform_cookie_sync
from app.services.x_downloader import (
    convert_cookie_header_to_netscape,
    list_media_files,
)


@dataclass(frozen=True, slots=True)
class ProfilePlatformSpec:
    id: str
    name: str
    cookie_domain: str
    cookie_env_key: str
    cookie_file_env_key: str
    engine_env_key: str

    def download_root(self) -> str:
        current = settings.snapshot()
        return str(getattr(current, f"{self.id.upper()}_DOWNLOAD_DIR"))


@dataclass(slots=True)
class ProfileDownloadResult:
    success: bool
    file_count: int
    return_code: int
    files: list[str] = field(default_factory=list)
    error_message: Optional[str] = None
    error_code: Optional[str] = None


PROFILE_PLATFORM_SPECS = {
    "tiktok": ProfilePlatformSpec(
        id="tiktok",
        name="TikTok",
        cookie_domain=".tiktok.com",
        cookie_env_key="TIKTOK_COOKIE",
        cookie_file_env_key="TIKTOK_COOKIE_FILE",
        engine_env_key="TIKTOK_DOWNLOAD_ENGINE",
    ),
}


def get_profile_platform_spec(platform: str) -> ProfilePlatformSpec:
    normalized = str(platform or "").strip().lower()
    try:
        return PROFILE_PLATFORM_SPECS[normalized]
    except KeyError as exc:
        raise ValueError(f"平台暂不支持主页下载: {normalized or platform}") from exc


def resolve_profile_input(platform: str, raw_input: str) -> tuple[str, str]:
    spec = get_profile_platform_spec(platform)
    value = str(raw_input or "").strip()
    if not value:
        raise ValueError(f"请输入 {spec.name} 用户主页或用户名")
    if spec.id != "tiktok":
        raise ValueError(f"平台解析器尚未实现: {spec.id}")

    direct = re.fullmatch(r"@?([A-Za-z0-9._]{1,64})", value)
    if direct:
        username = direct.group(1)
    else:
        candidate = value if re.match(r"^https?://", value, re.I) else f"https://{value}"
        match = re.match(
            r"^https?://(?:www\.)?tiktok\.com/@([A-Za-z0-9._]{1,64})(?:[/?#]|$)",
            candidate,
            re.I,
        )
        if not match:
            raise ValueError("无法从输入中识别 TikTok 用户名，仅支持用户主页或 @用户名")
        username = match.group(1)
    username = username.lower()
    return username, f"https://www.tiktok.com/@{username}"


def get_platform_cookie_value(db, spec: ProfilePlatformSpec) -> Optional[str]:
    return get_platform_cookie_sync(db, spec.id, spec.cookie_env_key)


def materialize_platform_cookie_file(
    db, spec: ProfilePlatformSpec, task_id: int
) -> tuple[Optional[str], bool]:
    current = settings.snapshot()
    cookie = get_platform_cookie_value(db, spec)
    if not cookie:
        configured_file = getattr(current, spec.cookie_file_env_key, None)
        if configured_file and os.path.isfile(configured_file):
            return str(configured_file), False
        return None, False

    cookie_dir = Path(spec.download_root()) / ".tmp"
    cookie_dir.mkdir(parents=True, exist_ok=True)
    fd, cookie_path = tempfile.mkstemp(
        suffix=".txt", prefix=f"{spec.id}_cookie_{task_id}_", dir=cookie_dir
    )
    with os.fdopen(fd, "w", encoding="utf-8") as cookie_file:
        cookie_file.write(convert_cookie_header_to_netscape(cookie, spec.cookie_domain))
    return cookie_path, True


def cleanup_platform_cookie_file(cookie_path: Optional[str], managed: bool) -> None:
    if managed and cookie_path:
        try:
            os.remove(cookie_path)
        except OSError:
            pass


class GalleryDlProfileDownloadEngine:
    name = "gallery-dl"

    def download_profile(
        self,
        *,
        spec: ProfilePlatformSpec,
        source_url: str,
        source_key: str,
        destination: str,
        cookie_file: Optional[str] = None,
        on_line: Optional[Callable[[str], None]] = None,
        on_process: Optional[Callable[[int], None]] = None,
    ) -> ProfileDownloadResult:
        if importlib.util.find_spec("gallery_dl") is None:
            return ProfileDownloadResult(
                False, 0, -1, error_code="engine_unavailable",
                error_message="gallery-dl 未安装，请重新安装 requirements.txt 依赖",
            )

        user_folder = Path(destination).expanduser() / source_key
        user_folder.mkdir(parents=True, exist_ok=True)
        archive = user_folder / ".download-archive.sqlite3"
        command = [
            sys.executable, "-m", "gallery_dl", source_url,
            "--destination", str(user_folder), "--directory", "",
            "--download-archive", str(archive),
            "--sleep-request", "2-5", "--sleep", "1-3",
            "--sleep-429", "120", "--retries", "3",
        ]
        if cookie_file and os.path.isfile(cookie_file):
            command.extend(["--cookies", os.path.abspath(cookie_file)])

        captured: deque[str] = deque(maxlen=80)
        sink = on_line or (lambda _line: None)

        def log(line: str) -> None:
            captured.append(line)
            sink(line)

        log(f"[{spec.name}] 用户: @{source_key}")
        log(f"[{spec.name}] 目标目录: {user_folder}")
        log(f"[{spec.name}] 已启用请求间隔、限流冷却和下载归档")
        before = set(list_media_files(user_folder))
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            if on_process:
                on_process(process.pid)
            if process.stdout:
                for line in process.stdout:
                    log(line.rstrip())
            return_code = process.wait()
            files = list_media_files(user_folder)
            if return_code == 0:
                log(f"[{spec.name}] 完成：本次新增 {len(set(files) - before)}，目录共 {len(files)} 个媒体")
                return ProfileDownloadResult(True, len(files), 0, files=files)
            code, message = _interpret_error(spec, return_code, captured)
            return ProfileDownloadResult(
                False, len(files), return_code, files=files,
                error_code=code, error_message=message,
            )
        except Exception as exc:
            return ProfileDownloadResult(
                False, 0, -1, error_code="engine_exception",
                error_message=f"{type(exc).__name__}: {exc}",
            )


def build_profile_download_engine(platform: str, engine_name: Optional[str] = None):
    spec = get_profile_platform_spec(platform)
    current = settings.snapshot()
    normalized = str(engine_name or getattr(current, spec.engine_env_key, "gallery-dl")).strip().lower()
    if normalized != "gallery-dl":
        raise ValueError(f"{spec.name} 不支持下载引擎: {normalized}")
    return GalleryDlProfileDownloadEngine()


def _interpret_error(
    spec: ProfilePlatformSpec, return_code: int, output: list[str] | deque[str]
) -> tuple[str, str]:
    """仅按 gallery-dl 已知状态与日志证据分类，未知情况保持通用错误。"""
    evidence = "\n".join(output).lower()
    if return_code == 64:
        return "invalid_url", "用户主页链接格式无效或当前引擎不支持"
    if return_code == 16 or "authenticationerror" in evidence:
        return "auth_required", f"{spec.name} 要求登录，请在设置中心更新有效 Cookie"
    if "429" in evidence or "too many requests" in evidence:
        return "rate_limited", f"{spec.name} 请求受限，请稍后重试"
    if "404 not found" in evidence or "does not exist" in evidence:
        return "not_found", "用户不存在、已删除或当前账号无权访问"
    if return_code == 4:
        return "request_failed", f"{spec.name} 请求失败，请查看任务日志中的 HTTP 错误"
    return "engine_error", f"gallery-dl 执行失败（退出码 {return_code}），请查看任务日志"
