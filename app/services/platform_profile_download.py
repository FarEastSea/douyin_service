"""可复用的平台媒体下载适配层；支持主页与单条作品。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Optional
from urllib.parse import parse_qs, quote, unquote, urlsplit

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
    default_engine: str

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
        default_engine="gallery-dl",
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
        default_engine="gallery-dl",
    ),
    "bilibili": ProfilePlatformSpec(
        id="bilibili",
        name="哔哩哔哩",
        cookie_domain=".bilibili.com",
        cookie_env_key="BILIBILI_COOKIE",
        cookie_file_env_key="BILIBILI_COOKIE_FILE",
        engine_env_key="BILIBILI_DOWNLOAD_ENGINE",
        download_subdir_env_key="BILIBILI_DOWNLOAD_SUBDIR",
        default_download_subdir="Bilibili",
        default_engine="yt-dlp",
    ),
    "xhs": ProfilePlatformSpec(
        id="xhs",
        name="小红书",
        cookie_domain=".xiaohongshu.com",
        cookie_env_key="XHS_COOKIE",
        cookie_file_env_key="XHS_COOKIE_FILE",
        engine_env_key="XHS_DOWNLOAD_ENGINE",
        download_subdir_env_key="XHS_DOWNLOAD_SUBDIR",
        default_download_subdir="Xiaohongshu",
        default_engine="xhs-api",
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
    if spec.id == "bilibili":
        return _resolve_bilibili_input(value)
    if spec.id == "xhs":
        return _resolve_xhs_input(value)
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


def _resolve_bilibili_input(value: str) -> ResolvedPlatformInput:
    """接受 UP 主 UID/空间、BV/av 视频、分P链接和包含视频的动态链接。"""
    direct_video = re.fullmatch(r"(BV[0-9A-Za-z]{10}|av\d+)", value, re.I)
    if direct_video:
        video_id = _normalize_bilibili_video_id(direct_video.group(1))
        return ResolvedPlatformInput(
            video_id, f"https://www.bilibili.com/video/{video_id}", "work"
        )
    if value.isdecimal():
        return ResolvedPlatformInput(
            value, f"https://space.bilibili.com/{value}/video", "profile"
        )

    candidate = value if re.match(r"^https?://", value, re.I) else f"https://{value}"
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").lower()
    is_bilibili = host == "bilibili.com" or host.endswith(".bilibili.com")
    if not is_bilibili and host != "b23.tv":
        raise ValueError("无法识别 B站链接或 UP 主 UID")

    if host == "b23.tv":
        if not parsed.path.strip("/"):
            raise ValueError("B站短链接缺少有效标识")
        short_url = f"https://b23.tv/{parsed.path.strip('/')}"
        digest = sha256(short_url.encode("utf-8")).hexdigest()[:16]
        return ResolvedPlatformInput(f"share-{digest}", short_url, "work")

    parts = [part for part in parsed.path.split("/") if part]
    if host == "space.bilibili.com":
        if not parts or not parts[0].isdecimal():
            raise ValueError("B站 UP 空间链接缺少数字 UID")
        if parts[1:] not in ([], ["video"], ["upload", "video"]):
            raise ValueError("仅支持 B站 UP 空间主页或投稿视频页")
        uid = parts[0]
        return ResolvedPlatformInput(
            uid, f"https://space.bilibili.com/{uid}/video", "profile"
        )

    video_match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10}|av\d+)(?:/)?", parsed.path, re.I)
    if video_match:
        video_id = _normalize_bilibili_video_id(video_match.group(1))
        query = parse_qs(parsed.query, keep_blank_values=True)
        page_values = query.get("p", [])
        if page_values and (len(page_values) != 1 or not page_values[0].isdigit() or int(page_values[0]) < 1):
            raise ValueError("B站分P参数 p 必须是正整数")
        page = page_values[0] if page_values else None
        suffix = f"?p={int(page)}" if page and int(page) > 0 else ""
        source_key = f"{video_id}-p{int(page)}" if suffix else video_id
        return ResolvedPlatformInput(
            source_key, f"https://www.bilibili.com/video/{video_id}{suffix}", "work"
        )

    dynamic_match = re.fullmatch(r"/(\d+)(?:/)?", parsed.path)
    if host == "t.bilibili.com" and dynamic_match:
        dynamic_id = dynamic_match.group(1)
        return ResolvedPlatformInput(
            f"dynamic-{dynamic_id}", f"https://t.bilibili.com/{dynamic_id}", "work"
        )
    opus_match = re.fullmatch(r"/opus/(\d+)(?:/)?", parsed.path, re.I)
    if opus_match:
        dynamic_id = opus_match.group(1)
        return ResolvedPlatformInput(
            f"dynamic-{dynamic_id}", f"https://www.bilibili.com/opus/{dynamic_id}", "work"
        )
    raise ValueError("仅支持 B站 UP 空间、BV/av 视频、分P或包含视频的动态链接")


def _normalize_bilibili_video_id(value: str) -> str:
    return f"BV{value[2:]}" if value[:2].lower() == "bv" else f"av{value[2:]}"


def _resolve_xhs_input(value: str) -> ResolvedPlatformInput:
    """接受小红书分享文本、作者主页、短链及官方单笔记链接。"""
    match = re.search(r"https?://[^\s<>]+", value, re.I)
    candidate = (match.group(0) if match else value).rstrip("，。！？；;,.!?)）]】")
    candidate = candidate if re.match(r"^https?://", candidate, re.I) else f"https://{candidate}"
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").lower()
    if host == "xhslink.com" or host.endswith(".xhslink.com"):
        if not parsed.path.strip("/"):
            raise ValueError("小红书短链接缺少分享标识")
        short_url = f"https://{host}/{parsed.path.strip('/')}"
        digest = sha256(short_url.encode("utf-8")).hexdigest()[:16]
        return ResolvedPlatformInput(f"share-{digest}", short_url, "work")
    if not (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")):
        raise ValueError("无法识别小红书笔记链接")

    patterns = (
        r"/(?:explore|discovery/item)/([0-9A-Za-z]+)(?:/)?",
        r"/user/profile/[0-9A-Za-z]+/([0-9A-Za-z]+)(?:/)?",
    )
    work_id = next(
        (matched.group(1) for pattern in patterns if (matched := re.fullmatch(pattern, parsed.path, re.I))),
        None,
    )
    if not work_id:
        profile_match = re.fullmatch(
            r"/user/profile/([0-9A-Za-z._-]{1,128})(?:/)?", parsed.path, re.I,
        )
        if profile_match:
            user_id = profile_match.group(1)
            allowed_query = parse_qs(parsed.query, keep_blank_values=False)
            query_items = []
            for key in ("xsec_token", "xsec_source"):
                for item in allowed_query.get(key, [])[:1]:
                    query_items.append(f"{quote(key)}={quote(item, safe='._~-')}")
            canonical = f"https://www.xiaohongshu.com/user/profile/{quote(user_id, safe='._~-')}"
            if query_items:
                canonical = f"{canonical}?{'&'.join(query_items)}"
            return ResolvedPlatformInput(f"user-{user_id}", canonical, "profile")
        raise ValueError("仅支持小红书 explore、discovery/item、带作品 ID 的用户链接或 xhslink 短链")

    allowed_query = parse_qs(parsed.query, keep_blank_values=False)
    query_items = []
    for key in ("xsec_token", "xsec_source"):
        for item in allowed_query.get(key, [])[:1]:
            query_items.append(f"{quote(key)}={quote(item, safe='._~-')}")
    query = "&".join(query_items)
    canonical = f"https://www.xiaohongshu.com/explore/{work_id}"
    if query:
        canonical = f"{canonical}?{query}"
    return ResolvedPlatformInput(f"note-{work_id}", canonical, "work")


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


class YtDlpProfileDownloadEngine:
    name = "yt-dlp"

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
        if importlib.util.find_spec("yt_dlp") is None:
            return ProfileDownloadResult(
                False, 0, -1, error_code="engine_unavailable",
                error_message="yt-dlp 未安装，请重新安装 requirements.txt 依赖",
            )

        user_folder = Path(destination).expanduser() / profile_storage_key(source_key)
        user_folder.mkdir(parents=True, exist_ok=True)
        archive = user_folder / ".download-archive.txt"
        has_ffmpeg = shutil.which("ffmpeg") is not None
        command = [
            sys.executable, "-m", "yt_dlp", source_url,
            "--paths", str(user_folder),
            "--output", "%(upload_date>%Y-%m-%d)s_%(title).180B_[%(id)s].%(ext)s",
            "--download-archive", str(archive),
            "--continue", "--no-overwrites",
            "--retries", "3", "--fragment-retries", "3",
            "--retry-sleep", "http:linear=5::30",
            "--retry-sleep", "fragment:linear=5::30",
            "--sleep-requests", "3", "--sleep-interval", "2",
            "--max-sleep-interval", "5", "--concurrent-fragments", "1",
            "--socket-timeout", "30",
            "--format", "bestvideo*+bestaudio/best" if has_ffmpeg else "best[ext=mp4]/best[ext=webm]",
            "--write-thumbnail", "--write-info-json", "--no-write-playlist-metafiles",
            "--newline",
        ]
        if has_ffmpeg:
            command.extend(["--merge-output-format", "mp4"])
        if cookie_file and os.path.isfile(cookie_file):
            command.extend(["--cookies", os.path.abspath(cookie_file)])

        captured: deque[str] = deque(maxlen=80)
        sink = on_line or (lambda _line: None)

        def log(line: str) -> None:
            captured.append(line)
            sink(line)

        log(f"[{spec.name}] {'单条视频/动态' if source_type == 'work' else 'UP 主空间'}: {source_key}")
        log(f"[{spec.name}] 目标目录: {user_folder}")
        log(f"[{spec.name}] 已启用单并发分片、请求间隔、有限重试和下载归档")
        if not has_ffmpeg:
            log(f"[{spec.name}] 未检测到 FFmpeg，将下载可直接播放的单文件格式，最高画质可能受限")
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
            code, message = _interpret_yt_dlp_error(spec, return_code, captured)
            return ProfileDownloadResult(
                False, len(files), return_code, files=files,
                error_code=code, error_message=message,
            )
        except Exception as exc:
            return ProfileDownloadResult(
                False, 0, -1, error_code="engine_exception",
                error_message=f"{type(exc).__name__}: {exc}",
            )


def get_configured_profile_engine_name(spec: ProfilePlatformSpec) -> str:
    current = settings.snapshot()
    return str(getattr(current, spec.engine_env_key, spec.default_engine)).strip().lower()


def build_profile_download_engine(platform: str, engine_name: Optional[str] = None):
    spec = get_profile_platform_spec(platform)
    normalized = str(engine_name or get_configured_profile_engine_name(spec)).strip().lower()
    if normalized == "gallery-dl" and spec.default_engine == "gallery-dl":
        return GalleryDlProfileDownloadEngine()
    if normalized == "yt-dlp" and spec.default_engine == "yt-dlp":
        return YtDlpProfileDownloadEngine()
    if normalized == "xhs-api" and spec.default_engine == "xhs-api":
        from app.services.xhs_download import XhsApiDownloadEngine

        return XhsApiDownloadEngine()
    raise ValueError(f"{spec.name} 不支持下载引擎: {normalized}")


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


def _interpret_yt_dlp_error(
    spec: ProfilePlatformSpec, return_code: int, output: list[str] | deque[str]
) -> tuple[str, str]:
    """依据 yt-dlp 明确错误证据分类，未知错误保留原始任务日志供排查。"""
    evidence = "\n".join(output).lower()
    if "ffmpeg" in evidence and any(marker in evidence for marker in ("not found", "not installed")):
        return "dependency_missing", "B站视频合并需要 FFmpeg，请在服务器安装后重试"
    if "no valid video url found" in evidence:
        return "no_video", "该 B站动态不包含可下载视频；图片或纯文字动态暂不支持"
    if any(marker in evidence for marker in (
        "request is blocked by server (352)", "request is blocked by server (412)",
        "http error 412", "http error 429", "too many requests", "exceeded rate limit",
    )):
        return "rate_limited", "B站请求受限，请等待后重试；不要连续提交任务"
    if any(marker in evidence for marker in (
        "login required", "you need to login", "sign in to confirm", "cookies are no longer valid",
    )):
        return "auth_required", "B站要求登录，请在设置中心更新有效 Cookie"
    if "http error 403" in evidence or "403 forbidden" in evidence:
        return "access_denied", "B站拒绝访问，请检查 Cookie、账号权限或稍后重试"
    if "unsupported url" in evidence:
        return "invalid_url", "B站链接格式无效，或该页面不包含可下载视频"
    if any(marker in evidence for marker in (
        "video is no longer available", "video has been deleted", "http error 404",
    )):
        return "not_found", "B站视频不存在、已删除，或当前账号无权访问"
    return "engine_error", f"yt-dlp 执行失败（退出码 {return_code}），请查看任务日志"
