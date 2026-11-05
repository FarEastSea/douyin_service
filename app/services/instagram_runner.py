"""隔离的 gallery-dl 运行器：stdout 只输出白名单 JSON 事件。

启动参数只有任务 ID 与执行 token；Cookie 和代理始终从受控存储读取。
不加载用户 gallery-dl 配置，不启动引擎自带的无界重试或外部下载进程。
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import requests
from sqlalchemy import func, select

from app.core.config import settings
from app.core.runtime_config import get_runtime_config_sync
from app.models.database import get_sync_db
from app.models.models import (
    PlatformAuthor,
    PlatformDownloadTask,
    PlatformMediaAsset,
    PlatformSubscriptionRun,
    PlatformSubscriptionState,
    PlatformTaskWork,
    PlatformWork,
    SystemConfig,
)
from app.services.download_lifecycle import (
    StaleDownloadAttempt,
    bind_download_attempt,
    lock_download_attempt,
)
from app.services.instagram_runtime import (
    InstagramStop,
    TERMINAL_SCAN_ERRORS,
    SESSION_RECOVERY_ERRORS,
    account_state,
    block_account,
    connection,
    instagram_channel,
    reserve_request,
    utcnow,
)
from app.services.instagram_store import (
    configure_subscription,
    ensure_work_task,
    interval_for,
    persist_work,
    upsert_author,
    work_complete,
)


def emit(message, **values):
    print(json.dumps({"message": message, **values}, ensure_ascii=False), flush=True)


def public_query_info(value):
    """Only persist the public doc ID and Relay provider names, never page tokens."""
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return None
    docid, providers = value
    if not isinstance(docid, str) or not docid.isdigit() or len(docid) > 64:
        return None
    if providers is None:
        providers = []
    if not isinstance(providers, (tuple, list)) or len(providers) > 256:
        return None
    if any(
        not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_]{1,256}", name) is None
        for name in providers
    ):
        return None
    return docid, list(providers)


def validate_profile_response(payload):
    """Do not let upstream turn an invalid GraphQL response into a missing user."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or "user" not in data or payload.get("errors"):
        raise InstagramStop(
            "profile_response_invalid",
            "Instagram 未返回有效的作者资料，扫描未完成并保留断点；请稍后重试，持续出现时需更新解析器",
        )
    user = data["user"]
    if user is None:
        raise InstagramStop("not_found", "作者不存在或当前会话无法访问")
    if (
        not isinstance(user, dict)
        or not str(user.get("id") or user.get("pk") or "").isdigit()
        or not isinstance(user.get("username"), str)
        or not user["username"].strip()
    ):
        raise InstagramStop(
            "profile_response_invalid",
            "Instagram 作者资料结构不完整，扫描未完成并保留断点",
        )


def verify_mp4(path):
    """验证完整容器及视频轨道；不能把任意带 ftyp 的响应当成视频。"""
    with open(path, "rb") as handle:

        def boxes(start, end):
            offset = start
            while offset < end:
                handle.seek(offset)
                header = handle.read(8)
                if len(header) != 8:
                    raise ValueError("MP4 容器截断")
                size, kind = int.from_bytes(header[:4], "big"), header[4:]
                length = 8
                if size == 1:
                    large = handle.read(8)
                    if len(large) != 8:
                        raise ValueError("MP4 容器截断")
                    size, length = int.from_bytes(large, "big"), 16
                elif size == 0:
                    size = end - offset
                if size < length or offset + size > end:
                    raise ValueError("MP4 容器长度无效")
                yield kind, offset + length, offset + size
                offset += size

        top = list(boxes(0, path.stat().st_size))
        if not any(
            kind == b"ftyp" and end - start >= 8 for kind, start, end in top
        ) or not any(kind == b"mdat" and end > start for kind, start, end in top):
            raise ValueError("MP4 缺少文件标识或媒体数据")
        for kind, start, end in top:
            if kind != b"moov":
                continue
            for track, a, b in boxes(start, end):
                if track != b"trak":
                    continue
                for media, c, d in boxes(a, b):
                    if media != b"mdia":
                        continue
                    for item, e, f in boxes(c, d):
                        if item == b"hdlr" and f - e >= 12:
                            handle.seek(e + 8)
                            if handle.read(4) == b"vide":
                                return
        raise ValueError("MP4 没有可用视频轨道")


class Runtime:
    def __init__(self, task_id, token, lease):
        self.db = get_sync_db()
        self.gate_db = get_sync_db()
        self.task_id, self.token, self.lease = task_id, token, lease
        self.task = self.db.get(PlatformDownloadTask, task_id)
        bind_download_attempt(self.db, task_id, token, PlatformDownloadTask)
        self.cookie, self.proxy, self.fingerprint = connection(self.gate_db)
        self.gate_db.commit()
        self.testing = self.task.source_type == "session"
        self.deadline = time.monotonic() + settings.snapshot().INSTAGRAM_BATCH_SECONDS
        self.last_heartbeat = 0
        self.requests_session = None

    def check(self):
        from app.services.external_download_coordination import check_external_execution

        check_external_execution("instagram", self.task_id, self.token)
        if not self.lease.owned():
            raise InstagramStop(
                "coordination_unavailable",
                "Instagram 执行租约已失效",
                utcnow() + timedelta(seconds=60),
            )
        self.gate_db.expire_all()
        options = json.loads(self.task.instagram_options or "{}")
        if (
            self.testing
            and options.get("fingerprint", self.fingerprint) != self.fingerprint
        ):
            raise InstagramStop(
                "session_changed", "该检测对应旧会话，请查看新会话检测任务"
            )
        state = (
            self.gate_db.get(PlatformSubscriptionState, options["state_id"])
            if options.get("state_id")
            else None
        )
        if state:
            author = self.gate_db.get(PlatformAuthor, state.author_id)
            if (
                not state.enabled
                or not state.manual
                and (
                    not author.is_subscribed
                    or not settings.snapshot().AUTO_CHECK_ENABLED
                )
            ):
                raise InstagramStop(
                    "subscription_paused", "该内容的订阅或历史补采已暂停"
                )
        self.gate_db.rollback()
        if time.monotonic() >= self.deadline:
            raise InstagramStop(
                "time_budget",
                "本片时间已用完，已保存进度",
                utcnow() + timedelta(seconds=60),
            )
        if time.monotonic() - self.last_heartbeat >= 5:
            emit("Instagram 正在执行", event="heartbeat")
            self.last_heartbeat = time.monotonic()

    def send(self, original, request, **kwargs):
        self.check()
        parsed = urlsplit(request.url)
        host = (parsed.hostname or "").lower()
        allowed = any(
            host == domain or host.endswith("." + domain)
            for domain in ("instagram.com", "cdninstagram.com", "fbcdn.net")
        )
        if (
            not allowed
            or parsed.scheme != "https"
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
        ):
            raise InstagramStop("unsafe_url", "引擎返回了不受信任的网络目标，已停止")
        api = host == "instagram.com" or host.endswith(".instagram.com")
        if api and any(
            part in parsed.path.lower()
            for part in ("/accounts/login", "/challenge", "/checkpoint")
        ):
            block_account(
                self.gate_db,
                "checkpoint"
                if "/accounts/login" not in parsed.path.lower()
                else "auth_required",
            )
        # 每个 Session.send 都经过门禁，因此重试与重定向也占接口预算。
        self.gate_db.expire_all()
        _, proxy, latest = connection(self.gate_db)
        self.gate_db.commit()
        if latest != self.fingerprint:
            raise InstagramStop("session_changed", "会话或出口已变更，请重新检测会话")
        if api or not kwargs.get("stream", False):
            while True:
                wait = reserve_request(
                    self.gate_db, fingerprint=self.fingerprint, testing=self.testing
                )
                if not wait:
                    break
                for _ in range(max(1, int(wait * 4))):
                    self.check()
                    time.sleep(min(0.25, wait))
        else:
            # CDN 不计接口预算，但仍检查会话、开关、熔断与协调状态。
            row = account_state(self.gate_db)
            if not settings.snapshot().INSTAGRAM_ENABLED and not self.testing:
                raise InstagramStop("disabled", "Instagram 采集已暂停")
            if (
                row.status != "ready"
                or row.cooldown_until
                and row.cooldown_until > utcnow()
            ):
                raise InstagramStop(
                    "auth_required", "会话不可用，已停止媒体请求", row.cooldown_until
                )
            self.gate_db.rollback()
        kwargs["proxies"] = {"http": proxy, "https": proxy} if proxy else {}
        kwargs["timeout"] = max(1, min(30, self.deadline - time.monotonic()))
        try:
            response = original(request, **kwargs)
        except requests.RequestException:
            raise InstagramStop(
                "network_error",
                "Instagram 网络连接未完成，稍后重试",
                utcnow() + timedelta(minutes=5),
            ) from None
        location = response.headers.get("location", "").lower()
        if api and (response.status_code == 401 or "/accounts/login" in location):
            response.close()
            block_account(self.gate_db, "auth_required")
        if api and any(part in location for part in ("/challenge", "/checkpoint")):
            response.close()
            block_account(self.gate_db, "checkpoint")
        if response.status_code == 429:
            after = response.headers.get("Retry-After")
            response.close()
            block_account(self.gate_db, "rate_limited", after)
        if api and (
            response.status_code in {400, 403}
            or "json" in response.headers.get("content-type", "")
            or parsed.path in {"/api/graphql", "/graphql/query"}
        ):
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            # 只分析错误字段，作品正文中出现这些词不能触发账号熔断。
            evidence = " ".join(
                str(payload.get(key) or "")
                for key in (
                    "message",
                    "error_type",
                    "checkpoint_url",
                    "challenge",
                    "errors",
                )
            ).lower()
            if (
                payload.get("challenge")
                or payload.get("checkpoint_url")
                or any(
                    word in evidence
                    for word in (
                        "challenge_required",
                        "checkpoint_required",
                        "consent_required",
                        "feedback_required",
                    )
                )
            ):
                response.close()
                block_account(self.gate_db, "checkpoint")
            if "login_required" in evidence or "not logged in" in evidence:
                response.close()
                block_account(self.gate_db, "auth_required")
            if any(
                word in evidence
                for word in (
                    "rate_limit_error",
                    "please wait a few minutes",
                    "too many requests",
                )
            ):
                response.close()
                block_account(self.gate_db, "rate_limited")
            if (
                parsed.path == "/api/graphql"
                and response.status_code == 200
                and request.headers.get("X-FB-Friendly-Name")
                == "PolarisProfilePageContentQuery"
            ):
                try:
                    validate_profile_response(payload)
                except InstagramStop:
                    response.close()
                    raise
            if parsed.path == "/graphql/query" and response.status_code == 200:
                values = payload.get("data")
                candidates = []
                if isinstance(values, dict):
                    for value in values.values():
                        if isinstance(value, dict):
                            candidates.append(value)
                            candidates.extend(
                                child
                                for child in value.values()
                                if isinstance(child, dict)
                            )
                page = next(
                    (
                        value
                        for value in candidates
                        if isinstance(value.get("edges"), list)
                    ),
                    None,
                )
                info = page.get("page_info") if page else None
                if (
                    not isinstance(info, dict)
                    or not isinstance(info.get("has_next_page"), bool)
                    or info["has_next_page"]
                    and not info.get("end_cursor")
                ):
                    response.close()
                    raise InstagramStop(
                        "parse_error", "上游分页结构未通过验证，保留断点等待修复"
                    )
        if response.status_code >= 400:
            if not api and kwargs.get("stream", False):
                response.close()
                raise ValueError("媒体文件已失效或当前会话不可访问")
            code = (
                "not_found"
                if response.status_code == 404
                else "access_denied"
                if response.status_code == 403
                else "upstream_error"
            )
            response.close()
            raise InstagramStop(
                code,
                "内容已失效、不可访问或上游请求失败，请核对账号权限和链接",
                utcnow() + timedelta(minutes=5) if code == "upstream_error" else None,
            )
        return response

    def extractor(self, url, cursor=None):
        from gallery_dl import config, exception, extractor, output

        config.clear()
        for key, value in {
            "cookies": dict(
                part.strip().split("=", 1) for part in self.cookie.split(";")
            ),
            "retries": 0,
            "sleep-request": 0,
            "sleep-429": 0,
            "user-cache": "memory",
            "videos": "merged",
            "audio": False,
            "previews": False,
            "cursor": cursor or True,
        }.items():
            config.set(("extractor", "instagram"), key, value)
        extr = extractor.find(url)
        if extr is None or extr.category != "instagram":
            raise InstagramStop("engine_unavailable", "固定版 gallery-dl 不支持此链接")
        # gallery-dl normally installs this adapter in Job, which this controlled
        # runner intentionally does not construct. Metadata error paths call
        # log.traceback(), unavailable on the standard logging.Logger.
        # Keep all engine logging private: response bodies and tokens may appear
        # even in warning/error records. Only emit our own structured events.
        engine_log = logging.Logger("instagram.engine.private")
        engine_log.disabled = True
        extr.log = output.LoggerAdapter(engine_log, SimpleNamespace(_logger_extra={}))
        extr.initialize()
        memory = {}

        def cache_key(func, args):
            return func.__name__, repr(args)

        def cache_update(func, key=..., value=None, _exp=0, _mem=False):
            identity = cache_key(func, () if key is ... else (key,))
            if value is None:
                memory.pop(identity, None)
                return
            memory[identity] = value
            # 1.32.19 caches (doc_id, Relay provider names), not just doc_id.
            # Use a new namespace so pre-upgrade ID-only entries cannot be read.
            info = public_query_info(value)
            if (
                func.__name__ == "_extract_opname_info"
                and key is not ...
                and info is not None
            ):
                name = "instagram:query:v2:" + str(key)
                row = self.gate_db.scalar(
                    select(SystemConfig).where(SystemConfig.key == name)
                )
                payload = json.dumps(
                    {"info": info, "expires": time.time() + (_exp or 86400)}
                )
                if row:
                    row.value = payload
                else:
                    self.gate_db.add(SystemConfig(key=name, value=payload))
                self.gate_db.commit()

        def cached(func, *args, _key=0, _exp=0, _mem=True):
            identity = cache_key(func, () if _key is None else args[_key : _key + 1])
            if identity in memory:
                return memory[identity]
            if func.__name__ == "_extract_opname_info" and args:
                row = self.gate_db.scalar(
                    select(SystemConfig).where(
                        SystemConfig.key == "instagram:query:v2:" + str(args[0])
                    )
                )
                if row:
                    try:
                        value = json.loads(row.value)
                        info = (
                            public_query_info(value.get("info"))
                            if isinstance(value, dict)
                            else None
                        )
                        valid = (
                            isinstance(value, dict)
                            and float(value.get("expires", 0)) > time.time()
                            and info is not None
                        )
                    except (ValueError, TypeError):
                        valid = False
                    self.gate_db.rollback()
                    if valid:
                        memory[identity] = info
                        return info
            try:
                value = func(*args)
            except exception.NotFoundError:
                raise
            except Exception as exc:
                # Upstream catches ordinary errors during profile lookup and
                # may misreport them as missing users. Keep the real diagnostic
                # and stop through the runner's non-retryable control signal.
                code, message = engine_failure(exc)
                raise InstagramStop(code, message) from None
            memory[identity] = value
            return value

        extr.cache, extr.cache_update = cached, cache_update
        extr.session.trust_env = False
        original = extr.session.send
        extr.session.send = lambda request, **kwargs: self.send(
            original, request, **kwargs
        )
        extr._interval_request = None
        extr.videos_dash = False
        extr._cursor = cursor
        extr.login()
        return extr

    def download(self, extr, file, work, task):
        media_id = str(file["media_id"]).split("_")[0]
        if not media_id.isdigit():
            raise ValueError("媒体 ID 无效")
        asset = self.db.scalar(
            select(PlatformMediaAsset).where(
                PlatformMediaAsset.platform == "instagram",
                PlatformMediaAsset.external_media_id == media_id,
            )
        )
        if (
            asset
            and Path(asset.file_path).is_file()
            and Path(asset.file_path).stat().st_size == asset.size_bytes
            and asset.size_bytes > 0
        ):
            return False
        if task.status == "cancelled":
            return False
        root = Path(settings.snapshot().INSTAGRAM_DOWNLOAD_DIR).expanduser().resolve()
        author = self.db.get(PlatformAuthor, work.author_id)
        folder = (
            root / author.external_user_id / work.external_work_id.replace(":", "-")
        )
        if any(
            parent.is_symlink()
            for parent in (root, root / author.external_user_id, folder)
        ):
            raise ValueError("下载目录不能是符号链接")
        folder.mkdir(parents=True, exist_ok=True)
        video = bool(file.get("video_url"))
        url = file.get("video_url") or file.get("display_url")
        extension = ".mp4" if video else Path(urlsplit(url).path).suffix.lower()
        if extension not in {
            ".mp4",
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".avif",
            ".heic",
            ".gif",
        }:
            extension = ".jpg"
        target = folder / f"{int(file.get('num') or 1):03d}_{media_id}{extension}"
        temporary = folder / f".{media_id}.{self.token}.part"
        if target.is_symlink() or temporary.is_symlink():
            raise ValueError("媒体路径不能是符号链接")
        response = extr.session.get(url, stream=True, timeout=30)
        try:
            mime = response.headers.get("Content-Type", "").split(";")[0]
            if not mime.startswith(("image/", "video/", "application/octet-stream")):
                raise ValueError("媒体响应不是图片或视频")
            expected_length = int(response.headers.get("Content-Length") or 0)
            size = 0
            with open(temporary, "wb") as handle:
                for block in response.iter_content(262144):
                    self.check()
                    if block:
                        handle.write(block)
                        size += len(block)
                handle.flush()
                os.fsync(handle.fileno())
            if not size or expected_length and size != expected_length:
                raise ValueError("媒体文件不完整")
            if not video:
                from PIL import Image

                with Image.open(temporary) as image:
                    image.verify()
            else:
                verify_mp4(temporary)
                if probe := shutil.which("ffprobe"):
                    result = subprocess.run(
                        [
                            probe,
                            "-v",
                            "error",
                            "-show_entries",
                            "stream=codec_type",
                            "-of",
                            "json",
                            str(temporary),
                        ],
                        capture_output=True,
                        timeout=15,
                        check=True,
                    )
                    if not any(
                        stream.get("codec_type") == "video"
                        for stream in json.loads(result.stdout).get("streams", [])
                    ):
                        raise ValueError("MP4 文件没有可用视频流")
            lock_download_attempt(
                self.db, self.task_id, self.token, PlatformDownloadTask
            )
            owned = self.db.scalar(
                select(PlatformDownloadTask)
                .where(PlatformDownloadTask.id == task.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if owned.status == "cancelled":
                return False
            os.replace(temporary, target)
            if asset is None:
                asset = PlatformMediaAsset(
                    task_id=task.id, platform="instagram", external_media_id=media_id
                )
                self.db.add(asset)
            asset.work_id, asset.media_index = work.id, int(file.get("num") or 1)
            asset.file_path, asset.filename, asset.size_bytes, asset.mime_type = (
                str(target),
                target.name,
                size,
                mime,
            )
            asset.media_type = "video" if video else "image"
            asset.title, asset.author_name, asset.published_at = (
                work.title,
                author.nickname or author.username,
                file.get("date") or work.published_at,
            )
            asset.width, asset.height = file.get("width"), file.get("height")
            task.download_dir = str(folder)
            self.db.commit()
            emit(
                "已保存一个媒体文件",
                event="media_saved",
                work_id=work.external_work_id,
                media_id=media_id,
                media_index=asset.media_index,
            )
            return True
        finally:
            response.close()
            temporary.unlink(missing_ok=True)

    def consume_post(self, extr, raw, *, stream_kind=None):
        # Stories/Highlights 的容器不是作品；每个 story 单独建稳定身份。
        if "items" in raw:
            posts = []
            for position, item in enumerate(raw["items"], 1):
                copy = {
                    **raw,
                    "collection_position": position,
                    "items": [item],
                    "seen": raw.get("seen")
                    or raw.get("latest_reel_media")
                    or item.get("taken_at")
                    or int(time.time()),
                }
                posts.append(copy)
        else:
            posts = [raw]
        for post in posts:
            data = extr._parse_post(post)
            files = [file for file in data.pop("_files") if not file.get("audio_url")]
            if not files:
                raise ValueError("上游作品缺少可下载媒体")
            user = post.get("user") or extr._user
            if not user:
                user = {
                    "pk": data["owner_id"],
                    "username": data["username"],
                    "full_name": data.get("fullname"),
                }
            author = upsert_author(self.db, user)
            is_story = data.get("type") in {"story", "highlight"}
            expected = (
                1
                if is_story
                else int(
                    post.get("carousel_media_count")
                    or len(post.get("carousel_media") or [post])
                )
            )
            merged = {**data, **files[0]}
            merged["collection_position"] = post.get("collection_position", 0)
            if data.get("type") == "story":
                item = post["items"][0]
                expiry = item.get("expiring_at") or int(item["taken_at"]) + 86400
                merged["expires"] = datetime.fromtimestamp(int(expiry), UTC).replace(
                    tzinfo=None
                )
            if stream_kind == "reels":
                merged["is_reel"] = True
            work, is_new = persist_work(self.db, author, merged, expected)
            child = ensure_work_task(self.db, work, self.task)
            if self.db.get(PlatformTaskWork, (self.task_id, work.id)) is None:
                self.db.add(PlatformTaskWork(task_id=self.task_id, work_id=work.id))
            self.db.commit()
            emit(
                "已发现并索引一个作品",
                event="work_discovered",
                work_id=work.external_work_id,
                is_new=is_new,
                expected_media=expected,
            )
            yield work, child, is_new, data, files

    def close(self):
        self.db.close()
        self.gate_db.close()


def scan(runtime):
    db, task = runtime.db, runtime.task
    options = json.loads(task.instagram_options or "{}")
    state = (
        db.get(PlatformSubscriptionState, options["state_id"])
        if options.get("state_id")
        else None
    )
    kind = (
        state.content_kind
        if state
        else options.get("kind", (options.get("kinds") or ["posts"])[0])
    )
    mode = state.scan_mode if state else "history"
    checkpoint = (
        json.loads(state.checkpoint or "{}") if state else options.get("checkpoint", {})
    )
    profile = task.source_type == "profile"
    username = task.source_url.rstrip("/").split("/")[-1]
    if state:
        author = db.get(PlatformAuthor, state.author_id)
        username = author.username
    url = (
        f"https://www.instagram.com/{username}/{kind}/"
        if profile and kind != "stories"
        else f"https://www.instagram.com/stories/{username}/"
        if profile
        else task.source_url
    )
    highlight_stream = profile and kind == "highlights"
    extr = runtime.extractor(
        url, None if highlight_stream else checkpoint.get("cursor")
    )
    if options.get("story_id"):
        extr.media_id = options["story_id"]
    if profile:
        handle = f"id:{author.external_user_id}" if state else username
        user = extr.api.user(handle)
        author = upsert_author(db, user)
        # 后续翻页直接以稳定用户 ID 获取当前用户名，避免旧用户名被他人占用。
        extr.item = f"id:{author.external_user_id}"
        if hasattr(extr, "user"):
            extr.user = extr.item
        if state is None:
            configure_subscription(
                db,
                author,
                subscribed=bool(options.get("subscribe") or author.is_subscribed),
                kinds=options.get("kinds"),
                backfill=True,
            )
            mode = "recent" if options.get("subscribe") else "history"
            kind = (options.get("kinds") or ["posts"])[0]
            state = db.scalar(
                select(PlatformSubscriptionState).where(
                    PlatformSubscriptionState.author_id == author.id,
                    PlatformSubscriptionState.content_kind == kind,
                    PlatformSubscriptionState.scan_mode == mode,
                )
            )
            # 首次添加允许只选择 Stories/Highlights，按选择的内容创建首片。
            if state is None and kind == "stories":
                mode = "recent"
                state = db.scalar(
                    select(PlatformSubscriptionState).where(
                        PlatformSubscriptionState.author_id == author.id,
                        PlatformSubscriptionState.content_kind == kind,
                        PlatformSubscriptionState.scan_mode == mode,
                    )
                )
            if not options.get("subscribe"):
                for history in db.scalars(
                    select(PlatformSubscriptionState).where(
                        PlatformSubscriptionState.author_id == author.id,
                        PlatformSubscriptionState.scan_mode == "history",
                        PlatformSubscriptionState.enabled.is_(True),
                    )
                ):
                    history.manual = True
            options["state_id"] = state.id
            task.instagram_options = json.dumps(options)
            state.task_id = task.id
        state.last_check_at = utcnow()
        if kind == "posts" and str(user.get("media_count", "")).isdigit():
            state.estimated_total = int(user["media_count"])
        author.last_check_time = utcnow()
        db.commit()
    run = PlatformSubscriptionRun(
        platform="instagram",
        author_id=author.id if profile else None,
        state_id=state.id if state else None,
        task_id=task.id,
        content_kind=kind if profile else task.source_type,
        scan_mode=mode,
    )
    task.phase = "scanning" if profile else "running"
    db.add(run)
    db.commit()
    count, known, discovered, saved = 0, 0, 0, 0
    boundary, partial = False, False
    page_seen = set(checkpoint.get("page_seen", []))
    current_cursor = checkpoint.get("cursor")
    try:

        def raw_posts():
            if not highlight_stream:
                yield from extr.posts()
                return
            ids = checkpoint.get("highlight_ids")
            if ids is None:
                ids = [str(item["id"]) for item in extr.api.highlights_tray(extr.item)]
                checkpoint["highlight_ids"] = ids
            for index in range(checkpoint.get("highlight_index", 0), len(ids)):
                checkpoint["highlight_index"] = index
                extr._cursor = checkpoint.get("highlight_cursor")
                # 每个集合独立查询；断点不会被误用于另一个集合的分页。
                for reel in extr.api.reels_media([ids[index]]):
                    checkpoint["highlight_cursor"] = extr._cursor
                    runtime.highlight_page_cursor = (
                        f"highlight:{index}:{extr._cursor or ''}"
                    )
                    yield reel
                checkpoint["highlight_index"] = index + 1
                checkpoint.pop("highlight_cursor", None)

        for raw in raw_posts():
            runtime.check()
            cursor = runtime.highlight_page_cursor if highlight_stream else extr._cursor
            if cursor != current_cursor:
                current_cursor, page_seen = cursor, set()
            for work, child, is_new, data, files in runtime.consume_post(
                extr, raw, stream_kind=kind
            ):
                identity = work.external_work_id
                if identity in page_seen and (
                    work_complete(db, work) or child.status == "cancelled"
                ):
                    continue
                runtime.check()
                count += 1
                discovered += int(is_new)
                had_all = work_complete(db, work)
                known = known if work.is_pinned else known + 1 if had_all else 0
                if child.status != "cancelled" and not had_all:
                    for file in files:
                        try:
                            saved += int(
                                runtime.download(extr, {**data, **file}, work, child)
                            )
                        except (OSError, ValueError, subprocess.SubprocessError):
                            partial = True
                            emit(
                                "一个媒体文件未通过下载或完整性校验，将保留完好文件并补缺",
                                event="media_failed",
                            )
                    if child.status == "cancelled":
                        pass
                    elif work_complete(db, work):
                        if child.id != task.id:
                            child.status, child.phase, child.completed_at = (
                                "completed",
                                "completed",
                                datetime.now(),
                            )
                            child.file_count = work.expected_media_count
                            child.downloaded_media_count = work.expected_media_count
                            child.progress_percent = 100
                            child.error_code = child.error_message = None
                    else:
                        partial = True
                        child.error_code, child.error_message = (
                            "partial_media",
                            "媒体不完整，等待补齐缺项",
                        )
                        if child.id != task.id:
                            child.status, child.phase = "pending", "queued"
                page_seen.add(identity)
                db.commit()
                checkpoint.update(cursor=current_cursor, page_seen=sorted(page_seen))
                if state:
                    state.checkpoint = json.dumps(checkpoint)
                else:
                    options["checkpoint"] = checkpoint
                    task.instagram_options = json.dumps(options)
                db.commit()
                emit(
                    f"已检查 {count} 条作品，新增 {discovered} 条，保存 {saved} 个文件",
                    event="progress",
                )
                if (
                    mode == "recent"
                    and kind in {"posts", "reels"}
                    and known
                    >= int(get_runtime_config_sync(db)["subscription_known_streak"])
                ):
                    boundary = True
                    break
                if count >= settings.snapshot().INSTAGRAM_BATCH_ITEMS:
                    raise InstagramStop(
                        "batch_limit",
                        "本片作品数已达上限，保存断点续跑",
                        utcnow() + timedelta(minutes=1),
                    )
            if boundary:
                break
        else:
            if getattr(extr, "status", 0):
                raise ValueError("引擎未完成完整扫描")
        if task.source_type == "work" and not count:
            raise InstagramStop("not_found", "作品已失效或当前账号无法访问")
        run.status = "partial" if partial else "completed"
        run.message = (
            "扫描完成；部分媒体等待补齐"
            if partial
            else "扫描完成；没有新增内容"
            if not discovered
            else "扫描完成"
        )
        if state:
            state.checkpoint = "{}"
            state.history_complete = mode == "history" and not partial
            if mode == "history" and not partial:
                state.manual = False
            state.last_success_at = utcnow()
            if mode == "recent":
                state.manual = False
            state.next_check_at = utcnow() + timedelta(seconds=interval_for(kind))
            state.deferred_reason = None
            author.last_success_at = utcnow()
            author.total_works = db.scalar(
                select(func.count(PlatformWork.id)).where(
                    PlatformWork.author_id == author.id
                )
            )
        task.status, task.phase, task.completed_at = (
            "completed",
            "completed",
            datetime.now(),
        )
        options.pop("checkpoint", None)
        options.pop("not_before", None)
        task.instagram_options = json.dumps(options)
        indexed = int(
            db.scalar(
                select(func.count(PlatformMediaAsset.id)).where(
                    PlatformMediaAsset.work_id.in_(
                        select(PlatformTaskWork.work_id).where(
                            PlatformTaskWork.task_id == task.id
                        )
                    )
                )
            )
            or 0
        )
        task.file_count, task.downloaded_media_count, task.progress_percent = (
            saved,
            saved,
            100,
        )
        task.error_code = task.error_message = None
        if task.source_type == "work":
            work = db.scalar(
                select(PlatformWork)
                .join(PlatformTaskWork)
                .where(PlatformTaskWork.task_id == task.id)
            )
            complete = work is not None and work_complete(db, work)
            task.status = task.phase = "completed" if complete else "failed"
            task.file_count = task.downloaded_media_count = indexed
            task.progress_percent = 100 if complete else 0
            if not complete:
                task.error_code, task.error_message = (
                    "partial_media",
                    "媒体不完整，请重试以补齐缺项",
                )
        task.output_log = (
            f"[Instagram] 已检查 {count} 条作品；新增 {discovered} 条；保存 {saved} 个文件。"
            + run.message
        )
    except InstagramStop as stop:
        db.rollback()
        run = db.get(PlatformSubscriptionRun, run.id)
        task = db.get(PlatformDownloadTask, task.id)
        run.status, run.message = "deferred", stop.message
        until = stop.until or utcnow() + timedelta(hours=1)
        # 当前页只标记已处理成功的 ID，失败项与未完成页会重读并按媒体 ID 补缺。
        checkpoint.update(cursor=current_cursor, page_seen=sorted(page_seen))
        if state:
            state = db.get(PlatformSubscriptionState, state.id)
            state.checkpoint = json.dumps(checkpoint)
            state.next_check_at, state.deferred_reason = until, stop.message
        options.update(checkpoint=checkpoint, not_before=until.isoformat())
        task.instagram_options = json.dumps(options)
        terminal = stop.code in TERMINAL_SCAN_ERRORS
        if terminal:
            run.status = "failed"
            if state:
                state.blocked_code, state.manual = stop.code, False
        task.status = "failed" if terminal else "pending"
        task.phase, task.error_code, task.error_message = (
            stop.code,
            stop.code,
            stop.message,
        )
        task.output_log = (
            f"[Instagram] 已检查 {count} 条作品；保存 {saved} 个文件。" + stop.message
        )
        task.file_count = task.downloaded_media_count = (
            saved
            if profile
            else int(
                db.scalar(
                    select(func.count(PlatformMediaAsset.id)).where(
                        PlatformMediaAsset.work_id.in_(
                            select(PlatformTaskWork.work_id).where(
                                PlatformTaskWork.task_id == task.id
                            )
                        )
                    )
                )
                or 0
            )
        )
        task.celery_task_id = None if not terminal else task.celery_task_id
    finally:
        run.discovered, run.saved, run.finished_at = discovered, saved, utcnow()
        db.commit()
    emit(
        run.message,
        event="scan_incomplete"
        if run.status in {"deferred", "failed"}
        else "scan_completed",
        checked=count,
        discovered=discovered,
        saved=saved,
        empty=not count,
        partial=partial,
        status=run.status,
    )


def record_unstarted_scan(db, task, started, status, message):
    """Keep setup failures visible even when no author or scan exists yet."""
    if task.source_type == "session":
        return None
    options = json.loads(task.instagram_options or "{}")
    state = (
        db.get(PlatformSubscriptionState, options["state_id"])
        if options.get("state_id")
        else None
    )
    existing = db.scalar(
        select(PlatformSubscriptionRun.id)
        .where(
            PlatformSubscriptionRun.task_id == task.id,
            PlatformSubscriptionRun.started_at >= started,
        )
        .limit(1)
    )
    if existing is None:
        db.add(
            PlatformSubscriptionRun(
                platform="instagram",
                author_id=state.author_id if state else None,
                state_id=state.id if state else None,
                task_id=task.id,
                content_kind=state.content_kind
                if state
                else options.get(
                    "kind", (options.get("kinds") or [task.source_type])[0]
                ),
                scan_mode=state.scan_mode
                if state
                else "recent"
                if options.get("subscribe")
                else "history",
                status=status,
                message=message,
                started_at=started,
                finished_at=utcnow(),
            )
        )
    return state


def validate_session(extr, expected_id):
    # current_user is a mobile endpoint; the web host may redirect it to home.
    # This endpoint returns the logged-in account's form, never a queried author.
    data = extr.api._call("/v1/accounts/edit/web_form_data/")
    form = data.get("form_data") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("status") not in {None, "ok"}
        or not isinstance(form, dict)
        or not isinstance(form.get("username"), str)
        or not form["username"].strip()
    ):
        raise InstagramStop(
            "session_response_invalid",
            "身份检测未返回已登录账号，请在浏览器确认登录状态后重新导入 Cookie",
        )
    identity = form.get("id") or form.get("pk") or form.get("user_id")
    if identity is not None and str(identity) != expected_id:
        raise InstagramStop(
            "session_identity_mismatch",
            "Cookie 中的账号标识与已登录会话不一致，请完整导出同一账号的 Cookie",
        )


def engine_failure(exc):
    """Return only fixed explanations and safe code locations, never exception text."""
    if isinstance(exc, ImportError):
        return (
            "engine_dependency_error",
            "Instagram 引擎依赖不可用，请检查服务端固定版本依赖",
        )
    from gallery_dl import exception

    if isinstance(exc, exception.AbortExtraction):
        text = str(exc)
        if "HTTP redirect to login page" in text:
            return (
                "auth_required",
                "Instagram 要求重新登录，请在浏览器登录后重新导入 Cookie",
            )
        if "HTTP redirect to challenge page" in text:
            return (
                "checkpoint",
                "Instagram 要求浏览器验证，请人工完成验证后重新导入 Cookie",
            )
        return (
            "session_endpoint_redirect",
            "Instagram 将请求重定向到其他页面，身份接口未完成验证；请核对出口与浏览器登录状态",
        )
    if isinstance(exc, (ImportError, exception.NoExtractorError)):
        return (
            "engine_dependency_error",
            "Instagram 引擎依赖不可用，请检查服务端固定版本依赖",
        )
    if isinstance(exc, exception.AuthenticationError):
        return (
            "auth_required",
            "Instagram 不接受当前登录会话，请在浏览器登录后重新导入 Cookie",
        )
    if isinstance(exc, exception.NotFoundError):
        return "not_found", "内容不存在或当前会话无法访问"
    location = ""
    trace = exc.__traceback__
    while trace:
        if Path(trace.tb_frame.f_code.co_filename).name == "instagram_runner.py":
            location = f"，运行器第 {trace.tb_lineno} 行"
        trace = trace.tb_next
    # The upstream source location is useful when the error crossed our cache
    # wrapper. Never include exception messages, locals, paths or response data.
    trace = exc.__traceback__
    parser_line = None
    while trace:
        filename = Path(trace.tb_frame.f_code.co_filename)
        if filename.name == "instagram.py" and filename.parent.name == "extractor":
            parser_line = trace.tb_lineno
        trace = trace.tb_next
    if parser_line is not None:
        location = f"{location}，解析器第 {parser_line} 行"
    return (
        "engine_contract_error",
        f"Instagram 引擎返回结构或处理逻辑异常（{type(exc).__name__}{location}），请反馈此诊断信息",
    )


def run(task_id, token):
    logging.disable(logging.CRITICAL)
    runtime = None
    started = utcnow()
    try:
        with instagram_channel() as lease:
            runtime = Runtime(task_id, token, lease)
            if not runtime.cookie:
                raise InstagramStop(
                    "auth_required", "请先在账号设置中导入 Instagram 会话"
                )
            if runtime.testing:
                own_id = dict(
                    part.strip().split("=", 1) for part in runtime.cookie.split(";")
                )["ds_user_id"]
                extr = runtime.extractor(
                    f"https://www.instagram.com/id:{own_id}/posts/"
                )
                validate_session(extr, own_id)
                row = account_state(runtime.gate_db)
                row.status, row.validated_fingerprint, row.validated_at = (
                    "ready",
                    runtime.fingerprint,
                    utcnow(),
                )
                row.last_error, row.cooldown_until, row.risk_count = None, None, 0
                runtime.gate_db.commit()
                for state in runtime.gate_db.scalars(
                    select(PlatformSubscriptionState)
                    .join(PlatformAuthor)
                    .where(
                        PlatformAuthor.platform == "instagram",
                        PlatformSubscriptionState.deferred_reason.is_not(None),
                        PlatformSubscriptionState.enabled.is_(True),
                        PlatformSubscriptionState.blocked_code.is_(None),
                        PlatformSubscriptionState.manual.is_(True)
                        | (
                            PlatformAuthor.is_subscribed.is_(True)
                            & bool(settings.snapshot().AUTO_CHECK_ENABLED)
                        ),
                        PlatformSubscriptionState.task_id.in_(
                            select(PlatformDownloadTask.id).where(
                                PlatformDownloadTask.error_code.in_(
                                    SESSION_RECOVERY_ERRORS
                                )
                            )
                        ),
                    )
                ):
                    state.next_check_at, state.deferred_reason = utcnow(), None
                for pending in runtime.gate_db.scalars(
                    select(PlatformDownloadTask).where(
                        PlatformDownloadTask.platform == "instagram",
                        PlatformDownloadTask.status == "pending",
                        PlatformDownloadTask.error_code.in_(
                            (
                                "auth_required",
                                "checkpoint",
                                "session_changed",
                                "disabled",
                            )
                        ),
                    )
                ):
                    options = json.loads(pending.instagram_options or "{}")
                    pending_state = (
                        runtime.gate_db.get(
                            PlatformSubscriptionState, options["state_id"]
                        )
                        if options.get("state_id")
                        else None
                    )
                    if pending_state:
                        pending_author = runtime.gate_db.get(
                            PlatformAuthor, pending_state.author_id
                        )
                        if (
                            not pending_state.enabled
                            or pending_state.blocked_code
                            or not (
                                pending_state.manual
                                or pending_author.is_subscribed
                                and settings.snapshot().AUTO_CHECK_ENABLED
                            )
                        ):
                            continue
                    options.pop("not_before", None)
                    pending.instagram_options = json.dumps(options)
                    pending.error_code, pending.error_message, pending.phase = (
                        None,
                        None,
                        "queued",
                    )
                runtime.gate_db.commit()
                runtime.task.status, runtime.task.phase, runtime.task.completed_at = (
                    "completed",
                    "completed",
                    datetime.now(),
                )
                runtime.task.error_code = runtime.task.error_message = None
                runtime.task.output_log = (
                    "Instagram 会话检测通过；内容类型仍需实际下载验收"
                )
                runtime.db.commit()
                emit(
                    "Instagram 会话检测通过；内容类型仍需实际下载验收",
                    event="session_ready",
                )
            else:
                scan(runtime)
            emit("本次 Instagram 执行已结束", event="finished")
            return 0
    except InstagramStop as stop:
        if runtime:
            runtime.db.rollback()
        db = get_sync_db()
        try:
            lock_download_attempt(db, task_id, token, PlatformDownloadTask)
            task = db.get(PlatformDownloadTask, task_id)
            until = stop.until or utcnow() + timedelta(minutes=5)
            options = json.loads(task.instagram_options or "{}")
            options["not_before"] = until.isoformat()
            task.instagram_options = json.dumps(options)
            terminal = (
                stop.code in TERMINAL_SCAN_ERRORS
                or task.source_type == "session"
                and stop.code
                in {
                    "auth_required",
                    "checkpoint",
                    "session_changed",
                    "session_response_invalid",
                    "session_identity_mismatch",
                    "network_error",
                }
            )
            state = record_unstarted_scan(
                db, task, started, "failed" if terminal else "deferred", stop.message
            )
            if state:
                if terminal:
                    state.blocked_code, state.manual = stop.code, False
                state.last_check_at, state.next_check_at, state.deferred_reason = (
                    started,
                    until,
                    stop.message,
                )
            task.status, task.phase, task.error_code, task.error_message = (
                "failed" if terminal else "pending",
                stop.code,
                stop.code,
                stop.message,
            )
            if not terminal:
                task.celery_task_id = None
            if task.source_type == "session":
                account = account_state(db)
                account.last_error = stop.message
                if terminal and stop.code not in {
                    "auth_required",
                    "checkpoint",
                    "session_changed",
                }:
                    account.status, account.validated_fingerprint = "unchecked", None
            db.commit()
        except StaleDownloadAttempt:
            db.rollback()
        finally:
            db.close()
        emit(stop.message, event="deferred", code=stop.code)
        return 0
    except StaleDownloadAttempt:
        emit("执行已取消或被替换", event="cancelled")
        return 0
    except Exception as exc:
        # 绝不输出上游异常原文，避免泄露带签名 URL、Cookie 或代理地址。
        code, message = engine_failure(exc)
        if runtime:
            runtime.db.rollback()
            try:
                lock_download_attempt(runtime.db, task_id, token, PlatformDownloadTask)
                for record in runtime.db.scalars(
                    select(PlatformSubscriptionRun).where(
                        PlatformSubscriptionRun.task_id == task_id,
                        PlatformSubscriptionRun.status == "running",
                    )
                ):
                    record.status, record.message, record.finished_at = (
                        "failed",
                        message,
                        utcnow(),
                    )
                task = runtime.db.get(PlatformDownloadTask, task_id)
                task.status, task.phase, task.error_code, task.error_message = (
                    "failed",
                    code,
                    code,
                    message,
                )
                state = record_unstarted_scan(
                    runtime.db, task, started, "failed", message
                )
                if state and code in TERMINAL_SCAN_ERRORS:
                    state.blocked_code, state.manual, state.deferred_reason = (
                        code,
                        False,
                        message,
                    )
                task.completed_at, task.output_log = datetime.now(), message
                if runtime.testing:
                    account = account_state(runtime.db)
                    account.last_error = message
                    if code in {"auth_required", "checkpoint"}:
                        account.status = code
                    else:
                        account.status, account.validated_fingerprint = (
                            "unchecked",
                            None,
                        )
                runtime.db.commit()
            except StaleDownloadAttempt:
                runtime.db.rollback()
        emit(message, event="error", code=code, error_type=type(exc).__name__)
        return 1
    finally:
        if runtime:
            try:
                from app.services.subscription_records import sync_session_runs

                # A completed execution may no longer flush its fenced Session.
                # Synchronize only this session check with a fresh transaction;
                # task ownership is rechecked under its row lock.
                runtime.db.rollback()
                if runtime.testing:
                    with get_sync_db() as record_db:
                        sync_session_runs(record_db, task_id=task_id, token=token)
                        record_db.commit()
            except Exception:
                emit("运行记录等待后台恢复", event="record_pending")
            finally:
                runtime.close()


if __name__ == "__main__":
    sys.exit(run(int(sys.argv[1]), sys.argv[2]))
