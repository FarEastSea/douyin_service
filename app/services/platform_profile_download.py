"""可复用的平台媒体下载适配层；支持主页与单条作品。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Callable, Optional
from urllib.parse import quote, unquote, urlsplit

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
    download_subdir_env_key: str
    default_download_subdir: str

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


@dataclass(frozen=True, slots=True)
class ResolvedPlatformInput:
    source_key: str
    source_url: str
    source_type: str


PROFILE_PLATFORM_SPECS = {
    "tiktok": ProfilePlatformSpec(
        id="tiktok",
        name="TikTok",
        cookie_domain=".tiktok.com",
        cookie_env_key="TIKTOK_COOKIE",
        cookie_file_env_key="TIKTOK_COOKIE_FILE",
        engine_env_key="TIKTOK_DOWNLOAD_ENGINE",
        download_subdir_env_key="TIKTOK_DOWNLOAD_SUBDIR",
        default_download_subdir="TikTok",
    ),
    "weibo": ProfilePlatformSpec(
        id="weibo",
        name="微博",
        cookie_domain=".weibo.com",
        cookie_env_key="WEIBO_COOKIE",
        cookie_file_env_key="WEIBO_COOKIE_FILE",
        engine_env_key="WEIBO_DOWNLOAD_ENGINE",
        download_subdir_env_key="WEIBO_DOWNLOAD_SUBDIR",
        default_download_subdir="Weibo",
    ),
}


def get_profile_platform_spec(platform: str) -> ProfilePlatformSpec:
    normalized = str(platform or "").strip().lower()
    try:
        return PROFILE_PLATFORM_SPECS[normalized]
    except KeyError as exc:
        raise ValueError(f"平台暂不支持媒体下载: {normalized or platform}") from exc


def resolve_platform_input(platform: str, raw_input: str) -> ResolvedPlatformInput:
    spec = get_profile_platform_spec(platform)
    value = str(raw_input or "").strip()
    if not value:
        raise ValueError(f"请输入 {spec.name} 用户主页、用户名或单条作品链接")
    if spec.id == "tiktok":
        return _resolve_tiktok_input(value)
    if spec.id == "weibo":
        return _resolve_weibo_input(value)
    raise ValueError(f"平台解析器尚未实现: {spec.id}")


def _resolve_tiktok_input(value: str) -> ResolvedPlatformInput:
    direct = re.fullmatch(r"@?([A-Za-z0-9._]{1,64})", value)
    if direct:
        username = direct.group(1).lower()
        return ResolvedPlatformInput(username, f"https://www.tiktok.com/@{username}", "profile")

    candidate = value if re.match(r"^https?://", value, re.I) else f"https://{value}"
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").lower()
    if not (host == "tiktok.com" or host.endswith(".tiktok.com")):
        raise ValueError("无法识别 TikTok 链接或用户名")
    work_match = re.fullmatch(
        r"/@([A-Za-z0-9._]{1,64})/(video|photo)/(\d+)(?:/)?",
        parsed.path,
        re.I,
    )
    if work_match:
        username, work_type, work_id = work_match.groups()
        username = username.lower()
        canonical = f"https://www.tiktok.com/@{username}/{work_type.lower()}/{work_id}"
        return ResolvedPlatformInput(f"{username}-{work_type.lower()}-{work_id}", canonical, "work")
    profile_match = re.fullmatch(r"/@([A-Za-z0-9._]{1,64})(?:/)?", parsed.path, re.I)
    if profile_match:
        username = profile_match.group(1).lower()
        return ResolvedPlatformInput(username, f"https://www.tiktok.com/@{username}", "profile")
    if host in {"vm.tiktok.com", "vt.tiktok.com"} and parsed.path.strip("/"):
        digest = sha256(candidate.encode("utf-8")).hexdigest()[:16]
        return ResolvedPlatformInput(f"share-{digest}", candidate, "work")
    raise ValueError("无法识别 TikTok 用户主页或单条视频/图文链接")


def _resolve_weibo_input(value: str) -> ResolvedPlatformInput:
    """接受微博 UID、昵称、官方主页 URL 与单条微博链接。"""
    direct = re.fullmatch(r"@?([^\s/?#]{1,64})", value)
    if direct and "." not in direct.group(1):
        identity = direct.group(1)
        prefix = "u" if identity.isdecimal() else "n"
    else:
        candidate = value if re.match(r"^https?://", value, re.I) else f"https://{value}"
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").lower()
        if host not in {
            "weibo.com", "www.weibo.com", "m.weibo.com",
            "weibo.cn", "www.weibo.cn", "m.weibo.cn",
        }:
            raise ValueError("无法识别微博主页，仅支持 UID、昵称或 weibo.com/weibo.cn 用户主页")
        parts = [unquote(item) for item in parsed.path.split("/") if item]
        if not parts:
            raise ValueError("微博主页缺少用户 UID 或昵称")
        if parts[0].lower() in {"detail", "status"}:
            if len(parts) < 2 or not re.fullmatch(r"[A-Za-z0-9]+", parts[1]):
                raise ValueError("微博单条动态链接缺少有效动态 ID")
            work_id = parts[1]
            return ResolvedPlatformInput(
                f"status-{work_id}", f"https://{host}/{parts[0].lower()}/{work_id}", "work"
            )
        if len(parts) > 1 and parts[0].isdecimal():
            if not re.fullmatch(r"[A-Za-z0-9]+", parts[1]):
                raise ValueError("微博单条动态 ID 格式无效")
            return ResolvedPlatformInput(
                f"{parts[0]}-status-{parts[1]}",
                f"https://weibo.com/{parts[0]}/{parts[1]}",
                "work",
            )
        if parts[0].lower() in {"u", "n"}:
            if len(parts) < 2:
                raise ValueError("微博主页缺少用户 UID 或昵称")
            prefix, identity = parts[0].lower(), parts[1]
        elif len(parts) >= 3 and parts[0].lower() == "p" and parts[1].lower() == "profile":
            prefix, identity = "p/profile", parts[2]
        else:
            prefix, identity = "", parts[0]

    identity = identity.strip()
    if not identity or identity in {".", ".."} or len(identity) > 64:
        raise ValueError("微博用户 UID 或昵称格式无效")
    encoded = quote(identity, safe="._-")
    path = f"{prefix}/{encoded}" if prefix else encoded
    return ResolvedPlatformInput(identity, f"https://weibo.com/{path}", "profile")


def profile_storage_key(source_key: str) -> str:
    """将平台用户标识转换为不会越过下载根目录的稳定文件夹名。"""
    raw = str(source_key or "").strip()
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = "profile"
    if cleaned != raw or len(cleaned) > 120:
        cleaned = f"{cleaned[:100]}_{sha256(raw.encode('utf-8')).hexdigest()[:12]}"
    return cleaned


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
        source_type: str,
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

        user_folder = Path(destination).expanduser() / profile_storage_key(source_key)
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

        log(f"[{spec.name}] {'单条作品' if source_type == 'work' else '用户主页'}: {source_key}")
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
        return "invalid_url", "主页或单条作品链接格式无效，或当前引擎不支持"
    if return_code == 16 or "authenticationerror" in evidence:
        return "auth_required", f"{spec.name} 要求登录，请在设置中心更新有效 Cookie"
    if "401 unauthorized" in evidence or "redirect to login" in evidence:
        return "auth_required", f"{spec.name} 登录状态无效，请在设置中心更新有效 Cookie"
    if "403 forbidden" in evidence:
        return "access_denied", f"{spec.name} 拒绝访问，请检查 Cookie、账号权限或稍后重试"
    if "429" in evidence or "too many requests" in evidence:
        return "rate_limited", f"{spec.name} 请求受限，请稍后重试"
    if any(marker in evidence for marker in (
        "404 not found", "does not exist", "notfounderror", "could not be found",
    )):
        return "not_found", "用户或作品不存在、已删除，或当前账号无权访问"
    if return_code == 4:
        return "request_failed", f"{spec.name} 请求失败，请查看任务日志中的 HTTP 错误"
    return "engine_error", f"gallery-dl 执行失败（退出码 {return_code}），请查看任务日志"
