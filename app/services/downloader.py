"""
抖音下载核心服务

为什么这样设计：
1. 基于原有 douyin 类改造，保留核心下载逻辑
2. 增加 HTTP Range 断点续传支持
3. 增加进度回调机制，实时更新到 Redis
4. 增加暂停信号检测，支持任务暂停/恢复
5. 使用临时文件下载，完成后再重命名，避免下载失败留下损坏文件
6. 增强错误处理和日志记录
"""

import requests
import json
import re
import os
import time
import logging
from pathlib import Path
from typing import Optional, Callable, List, Dict, Any
from datetime import datetime
from urllib.parse import quote, unquote, urlsplit

from app.core.config import settings
from app.core import redis_client
from app.core.network_security import get_douyin_response, validate_douyin_url
from app.core.runtime_config import get_cached_runtime_config

# 配置日志
logger = logging.getLogger(__name__)
if not logger.handlers:
    logger.setLevel(logging.INFO)
    # 确保logs目录存在
    if not os.path.exists('logs'):
        os.makedirs('logs')
    file_handler = logging.FileHandler('logs/downloader.log', encoding='utf-8')
    console_handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


def _extract_play_urls(video_payload: Any) -> List[str]:
    if not isinstance(video_payload, dict):
        return []

    play_addr = video_payload.get('play_addr') or {}
    if not isinstance(play_addr, dict):
        return []

    url_list = play_addr.get('url_list')
    if not isinstance(url_list, list):
        return []

    return [url.strip() for url in url_list if isinstance(url, str) and url.strip()]


def _extract_image_entries(images_payload: Any) -> List[Dict[str, Any]]:
    if not isinstance(images_payload, list):
        return []

    normalized_entries: List[Dict[str, Any]] = []
    for image in images_payload:
        if not isinstance(image, dict):
            continue

        url_list = image.get('url_list')
        if not isinstance(url_list, list):
            continue

        normalized_url_list = [url.strip() for url in url_list if isinstance(url, str) and url.strip()]
        if not normalized_url_list:
            continue

        normalized_image = dict(image)
        normalized_image['url_list'] = normalized_url_list
        normalized_entries.append(normalized_image)

    return normalized_entries


def _extract_live_photo_url(image_payload: Any) -> Optional[str]:
    """提取单张图片关联的实况视频地址。"""
    if not isinstance(image_payload, dict):
        return None

    video_payload = image_payload.get('video')
    play_urls = _extract_play_urls(video_payload)
    if play_urls:
        return play_urls[-1]

    if not isinstance(video_payload, dict):
        return None
    play_addr = video_payload.get('play_addr')
    if not isinstance(play_addr, dict):
        return None

    uri = _normalize_optional_text(play_addr.get('uri'))
    if not uri:
        return None
    return f"https://aweme.snssdk.com/aweme/v1/play/?video_id={quote(uri, safe='')}&ratio=1080p&line=0"


def _normalize_optional_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None

    normalized_value = value.strip()
    return normalized_value or None


def _extract_primary_url(media_payload: Any) -> Optional[str]:
    if isinstance(media_payload, dict):
        url_list = media_payload.get('url_list')
        if not isinstance(url_list, list):
            return None

        return next(
            (url.strip() for url in url_list if isinstance(url, str) and url.strip()),
            None,
        )

    if isinstance(media_payload, list):
        for item in media_payload:
            url = _extract_primary_url(item)
            if url:
                return url

    return None


def _avatar_resolution_score(url: Optional[str], declared_size: int = 0) -> int:
    """Best-effort score without downloading the image just to inspect its pixels."""
    if not url:
        return 0
    dimensions = re.findall(r"(?<!\d)(\d{2,4})[xX_](\d{2,4})(?!\d)", url)
    encoded_score = max((int(width) * int(height) for width, height in dimensions), default=0)
    return max(encoded_score, declared_size * declared_size)


def _extract_best_avatar_url(author: Dict[str, Any]) -> Optional[str]:
    """Prefer Douyin's largest declared avatar variant and its best URL candidate."""
    variants = (
        ('avatar_720x720', 720),
        ('avatar_larger', 600),
        ('avatar_300x300', 300),
        ('avatar_medium', 240),
        ('avatar_168x168', 168),
        ('avatar_thumb', 100),
    )
    candidates = []
    for field_name, declared_size in variants:
        payload = author.get(field_name)
        urls = payload.get('url_list') if isinstance(payload, dict) else None
        if not isinstance(urls, list):
            continue
        for url in urls:
            normalized = _normalize_optional_text(url)
            if normalized:
                candidates.append((_avatar_resolution_score(normalized, declared_size), normalized))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def prefer_avatar_url(current_url: Optional[str], candidate_url: Optional[str]) -> Optional[str]:
    """Keep an unchanged avatar URL unless the new candidate is detectably sharper."""
    current = _normalize_optional_text(current_url)
    candidate = _normalize_optional_text(candidate_url)
    if not candidate:
        return current
    if not current or current == candidate:
        return candidate or current

    current_parts = urlsplit(current)
    candidate_parts = urlsplit(candidate)
    same_asset = current_parts.path == candidate_parts.path
    if same_asset and _avatar_resolution_score(candidate) <= _avatar_resolution_score(current):
        return current
    return candidate


def author_profile_has_identity(profile: Dict[str, Any]) -> bool:
    return bool(
        _normalize_optional_text(profile.get('nickname'))
        or _normalize_optional_text(profile.get('avatar_url'))
    )


def payload_image_urls(work_payload: Dict[str, Any]) -> List[str]:
    image_urls = work_payload.get('image_urls')
    if isinstance(image_urls, list):
        return [url.strip() for url in image_urls if isinstance(url, str) and url.strip()]

    images = work_payload.get('images')
    if not isinstance(images, list):
        return []

    normalized_urls: List[str] = []
    for image in images:
        if not isinstance(image, dict):
            continue

        url_list = image.get('url_list')
        if not isinstance(url_list, list):
            continue

        best_url = next(
            (url.strip() for url in reversed(url_list) if isinstance(url, str) and url.strip()),
            None,
        )
        if best_url:
            normalized_urls.append(best_url)

    return normalized_urls


def payload_live_photo_urls(work_payload: Dict[str, Any]) -> List[Optional[str]]:
    """
    返回与 image_urls 等长、按索引对齐的实况视频地址。

    普通图片对应 None；这样混合图集不会因过滤空值而导致图片与实况片段错位。
    """
    live_photo_urls = work_payload.get('live_photo_urls')
    if isinstance(live_photo_urls, list):
        return [_normalize_optional_text(url) for url in live_photo_urls]

    images = work_payload.get('images')
    if not isinstance(images, list):
        return []

    return [
        _extract_live_photo_url(image) if isinstance(image, dict) else None
        for image in images
    ]


def latest_video_url(work_payload: Dict[str, Any]) -> Optional[str]:
    video_urls = work_payload.get('video')
    if not isinstance(video_urls, list):
        return None

    return next(
        (url.strip() for url in reversed(video_urls) if isinstance(url, str) and url.strip()),
        None,
    )


def is_video_work_payload(work_payload: Dict[str, Any]) -> bool:
    work_type = work_payload.get('work_type')
    if work_type in {'video', 'images'}:
        return work_type == 'video'

    return bool(latest_video_url(work_payload)) and not payload_image_urls(work_payload)


def _classify_author_account_status(detail_text: str, status_code: Optional[int] = None) -> tuple[str, str]:
    text = detail_text or ""
    lower_text = text.lower()

    if status_code == 404 or any(token in text for token in ("注销", "已注销", "不存在", "未找到", "用户不存在", "账号不存在", "已删除")) or "not found" in lower_text:
        return "deleted", "已销号"

    # 禁言：账号仍在但被禁言，单独给出更准确的标签
    if "禁言" in text:
        return "banned", "已禁言"

    if any(token in text for token in ("封禁", "封号", "封停", "禁用", "处罚", "违规", "冻结")) or "banned" in lower_text:
        return "banned", "已封号"

    if any(token in text for token in ("私密", "不可见", "无法查看", "不可访问", "受限", "无权限")) or any(token in lower_text for token in ("private", "restricted")):
        return "restricted", "不可访问"

    return "unavailable", "状态异常"


class DouyinDownloader:
    """抖音下载器 - 支持断点续传和进度追踪"""

    @staticmethod
    def _extract_author_identity(author_payload: Any, fallback_sec_uid: str = '') -> Dict[str, Optional[str]]:
        author = author_payload if isinstance(author_payload, dict) else {}
        sec_uid = _normalize_optional_text(author.get('sec_uid')) or fallback_sec_uid

        return {
            'nickname': _normalize_optional_text(author.get('nickname')),
            'avatar_url': _extract_best_avatar_url(author),
            'sec_uid': sec_uid,
            'profile_url': DouyinDownloader.build_author_profile_url(sec_uid),
        }

    @staticmethod
    def build_author_profile_url(sec_uid: Optional[str]) -> Optional[str]:
        if not sec_uid:
            return None

        normalized_sec_uid = unquote(sec_uid.strip())
        if not normalized_sec_uid:
            return None

        return f"https://www.douyin.com/user/{quote(normalized_sec_uid, safe='')}"
    
    def __init__(self, cookie: str, filepath: str = None, runtime_config: dict = None):
        """
        初始化下载器
        
        Args:
            cookie: 抖音 Cookie
            filepath: 下载保存路径，默认使用配置中的路径
        """
        self.headers = {
            'accept': 'application/json, text/plain, */*',
            'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'cookie': cookie,
            'referer': 'https://www.douyin.com/',
            'sec-ch-ua': '"Not)A;Brand";v="99", "Microsoft Edge";v="127", "Chromium";v="127"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 Edg/127.0.0.0'
        }
        self.runtime_config = runtime_config or get_cached_runtime_config()
        self.download_timeout = int(self.runtime_config.get("download_timeout", settings.DOWNLOAD_TIMEOUT))
        self.download_retry_count = int(self.runtime_config.get("download_retry_count", settings.DOWNLOAD_RETRY_COUNT))
        self.request_delay = float(self.runtime_config.get("douyin_request_delay", settings.REQUEST_DELAY))
        self.filepath = filepath or settings.DOWNLOAD_DIR
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        # 设置重试
        adapter = requests.adapters.HTTPAdapter(
            max_retries=self.download_retry_count
        )
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)

    def _normalize_work_item(self, item: Dict[str, Any], fallback_sec_uid: str = '') -> Dict[str, Any]:
        author = item.get('author') or {}
        if not isinstance(author, dict):
            author = {}

        author_identity = self._extract_author_identity(author, fallback_sec_uid)

        raw_images = item.get('images')
        image_entries = _extract_image_entries(raw_images)
        image_urls = payload_image_urls({'images': image_entries})
        live_photo_urls = payload_live_photo_urls({'images': image_entries})
        video_urls = _extract_play_urls(item.get('video'))

        try:
            create_time = int(item.get('create_time') or 0)
        except (TypeError, ValueError):
            create_time = 0

        return {
            'aweme_id': item['aweme_id'],
            'desc': item.get('desc', ''),
            'images': image_entries,
            'image_urls': image_urls,
            'live_photo_urls': live_photo_urls,
            'video': video_urls,
            'work_type': 'images' if raw_images is not None else 'video',
            'author_name': author_identity.get('nickname') or '未知作者',
            'author_sec_uid': author_identity.get('sec_uid') or fallback_sec_uid,
            # 置顶标记与发布时间：用于订阅增量检测，避免置顶作品卡死游标
            'is_top': 1 if item.get('is_top') else 0,
            'create_time': create_time,
        }
    
    def detect_url_type(self, url: str) -> dict:
        """
        检测链接类型：作者主页 or 单个作品
        
        Returns:
            {"type": "author"|"work", "redirect_url": str}
        """
        url_match = re.search(r'(https?://[^\s]+)', url)
        if not url_match:
            raise ValueError("无效的分享链接")

        raw_url = url_match.group(1).strip()
        res, redirect_url = get_douyin_response(
            self.session,
            raw_url,
            timeout=self.download_timeout,
        )

        if re.search(r'/video/(\d+)', redirect_url) or re.search(r'/note/(\d+)', redirect_url):
            return {"type": "work", "redirect_url": redirect_url}
        elif re.search(r'user/([^/?]+)', redirect_url):
            return {"type": "author", "redirect_url": redirect_url}
        else:
            raise ValueError("无法识别链接类型，请检查链接是否正确")

    def get_single_work(self, redirect_url: str) -> Dict[str, Any]:
        """
        获取单个作品信息
        
        Args:
            redirect_url: 重定向后的真实 URL (含 /video/xxx 或 /note/xxx)
        
        Returns:
            作品信息字典
        """
        validate_douyin_url(redirect_url)
        aweme_id_match = re.search(r'/(?:video|note)/(\d+)', redirect_url)
        if not aweme_id_match:
            raise ValueError("无法从链接中提取作品ID")

        aweme_id = aweme_id_match.group(1)
        api_url = f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={aweme_id}&device_platform=webapp&aid=6383"

        res, _ = get_douyin_response(self.session, api_url, timeout=self.download_timeout)
        if res.status_code != 200:
            raise ValueError(f"抖音 API 请求失败 (HTTP {res.status_code})，请检查 Cookie 是否有效")
        try:
            data = res.json()
        except (json.JSONDecodeError, ValueError):
            body_preview = res.text[:200] if res.text else '(空响应)'
            raise ValueError(f"抖音 API 返回了非 JSON 内容，可能是 Cookie 过期或被限流。响应内容: {body_preview}")
        item = data.get('aweme_detail')
        if not item:
            raise ValueError("获取作品详情失败，可能作品已被删除或链接无效")

        author = item.get('author', {})
        sec_uid = author.get('sec_uid', '')

        work = {
            **self._normalize_work_item(item, sec_uid),
        }

        author_info = {
            **self._extract_author_identity(author, sec_uid),
            'account_status': 'active',
            'account_status_label': '正常',
            'account_status_detail': None,
        }

        return {"work": work, "author_info": author_info}

    def get_sec_uid(self, url: str) -> str:
        """
        从分享链接获取用户 sec_uid
        
        Args:
            url: 抖音分享链接
            
        Returns:
            用户的 sec_uid
        """
        # 提取URL
        url_match = re.search(r'(https?://[^\s]+)', url)
        if not url_match:
            raise ValueError("无效的分享链接")
        
        url = url_match.group(1).strip()
        
        # 逐跳校验重定向，禁止离开受信任抖音域名或解析到内网地址。
        res, redirects_url = get_douyin_response(
            self.session,
            url,
            timeout=self.download_timeout,
        )
        
        # 从URL中提取 sec_uid
        sec_uid_match = re.search(r'user/([^/?]+)', redirects_url)
        if not sec_uid_match:
            raise ValueError("无法获取用户ID")
        
        return sec_uid_match.group(1)
    
    def get_author_info(self, sec_uid: str) -> Dict[str, Any]:
        """
        获取作者信息
        
        Args:
            sec_uid: 用户 sec_uid
            
        Returns:
            包含昵称、头像等信息的字典
        """
        encoded_sec_uid = quote(str(sec_uid), safe='')
        url = f"https://www.douyin.com/aweme/v1/web/user/profile/other/?device_platform=webapp&aid=6383&sec_user_id={encoded_sec_uid}"
        profile_url = self.build_author_profile_url(sec_uid)

        def _build_author_info_response(
            nickname: Optional[str],
            avatar_url: Optional[str],
            account_status: str,
            account_status_label: str,
            account_status_detail: Optional[str],
        ) -> Dict[str, Any]:
            return {
                'nickname': nickname,
                'avatar_url': avatar_url,
                'sec_uid': sec_uid,
                'profile_url': profile_url,
                'account_status': account_status,
                'account_status_label': account_status_label,
                'account_status_detail': account_status_detail,
            }

        def _fallback_to_work_list(
            account_status: str,
            account_status_label: str,
            account_status_detail: Optional[str],
        ) -> Dict[str, Any]:
            try:
                data = self.get_work_list(sec_uid, count=3)
            except Exception as fallback_exc:
                if account_status_detail:
                    account_status_detail = f"{account_status_detail}; works_fallback={fallback_exc}"
                else:
                    account_status_detail = f"works_fallback={fallback_exc}"
                return _build_author_info_response(
                    None,
                    None,
                    account_status,
                    account_status_label,
                    account_status_detail,
                )

            aweme_list = data.get('aweme_list')
            if not isinstance(aweme_list, list):
                return _build_author_info_response(
                    None,
                    None,
                    account_status,
                    account_status_label,
                    account_status_detail,
                )

            for item in aweme_list:
                if not isinstance(item, dict):
                    continue

                fallback_author_info = self._extract_author_identity(item.get('author'), sec_uid)
                if author_profile_has_identity(fallback_author_info):
                    logger.info(
                        "作者资料接口不可用，已从作品列表回退作者资料: sec_uid=%s",
                        sec_uid,
                    )
                    return {
                        **fallback_author_info,
                        'account_status': 'active',
                        'account_status_label': '正常',
                        'account_status_detail': None,
                    }

            return _build_author_info_response(
                None,
                None,
                account_status,
                account_status_label,
                account_status_detail,
            )
        
        try:
            res, _ = get_douyin_response(self.session, url, timeout=self.download_timeout)
            if res.status_code != 200:
                body_preview = res.text[:200] if res.text else '(空响应)'
                status_code, status_label = _classify_author_account_status(body_preview, res.status_code)
                if status_code == "unavailable":
                    status_code, status_label = "transient_error", "资料获取失败"
                logger.warning(f"获取作者信息失败 HTTP {res.status_code}, sec_uid={sec_uid}, detail={body_preview}")
                return _fallback_to_work_list(status_code, status_label, body_preview)

            if not res.text.strip():
                logger.warning("获取作者信息返回空响应, sec_uid=%s", sec_uid)
                return _fallback_to_work_list('transient_error', '资料获取失败', '抖音资料接口返回空响应')

            try:
                data = res.json()
            except (json.JSONDecodeError, ValueError):
                body_preview = res.text[:200] if res.text else '(空响应)'
                logger.warning("获取作者信息返回非 JSON 内容, sec_uid=%s, detail=%s", sec_uid, body_preview)
                return _fallback_to_work_list('transient_error', '资料获取失败', body_preview)

            user = data.get('user') or {}
            status_text = ' '.join(
                str(part).strip()
                for part in (
                    data.get('status_msg'),
                    data.get('message'),
                    data.get('description'),
                )
                if part
            )

            if not user:
                status_code, status_label = _classify_author_account_status(status_text, data.get('status_code'))
                if status_code == "unavailable":
                    status_code, status_label = "transient_error", "资料获取失败"
                return _fallback_to_work_list(
                    status_code,
                    status_label,
                    status_text or '抖音未返回作者资料',
                )

            author_info = self._extract_author_identity(user, sec_uid)
            if author_profile_has_identity(author_info):
                return {
                    **author_info,
                    'account_status': 'active',
                    'account_status_label': '正常',
                    'account_status_detail': None,
                }

            return _fallback_to_work_list(
                'transient_error',
                '资料获取失败',
                '抖音返回的作者资料不完整',
            )
        except Exception as exc:
            return _fallback_to_work_list('transient_error', '资料获取失败', str(exc))
    
    def get_work_list(self, sec_uid: str, max_cursor: int = 0, count: int = 42) -> Dict[str, Any]:
        """
        获取作品列表（单页）
        
        Args:
            sec_uid: 用户 sec_uid
            max_cursor: 分页游标
            count: 每页数量
            
        Returns:
            包含 aweme_list, has_more, max_cursor 的字典
        """
        encoded_sec_uid = quote(str(sec_uid), safe='')
        url = f"https://www.douyin.com/aweme/v1/web/aweme/post/?device_platform=webapp&aid=6383&channel=channel_pc_web&sec_user_id={encoded_sec_uid}&max_cursor={max_cursor}&locate_query=false&show_live_replay_strategy=1&need_time_list=1&time_list_query=0&count={count}&publish_video_strategy_type=2&pc_client_type=1&cookie_enabled=true&browser_language=zh-CN&browser_platform=Win32&browser_name=Edge"
        
        res, _ = get_douyin_response(self.session, url, timeout=self.download_timeout)
        if res.status_code != 200:
            body_preview = res.text[:500] if res.text else '(空响应)'
            logger.error(
                f"获取作品列表失败 HTTP {res.status_code}, sec_uid={sec_uid}, "
                f"cursor={max_cursor}, 响应内容: {body_preview}"
            )
            raise ValueError(
                f"获取作品列表失败 (HTTP {res.status_code})，请检查 Cookie 是否有效。"
                f"响应内容: {body_preview}"
            )
        try:
            data = res.json()
        except (json.JSONDecodeError, ValueError):
            body_preview = res.text[:500] if res.text else '(空响应)'
            content_type = res.headers.get('Content-Type', '未知')
            logger.error(
                f"获取作品列表失败，抖音返回了非 JSON 内容, sec_uid={sec_uid}, "
                f"cursor={max_cursor}, Content-Type={content_type}, "
                f"HTTP {res.status_code}, 响应内容: {body_preview}"
            )
            raise ValueError(
                f"获取作品列表失败，抖音返回了非 JSON 内容，可能是 Cookie 过期或被限流。"
                f"HTTP 状态码: {res.status_code}, Content-Type: {content_type}, "
                f"响应内容: {body_preview}"
            )
        
        # 校验 API 业务状态码（非致命，部分接口非0也有数据）
        api_status = data.get('status_code', 0)
        if api_status != 0:
            logger.warning(f"抖音 API 返回非零状态码: {api_status}，可能触发了反爬限制")
            # 如果没有数据才报错
            if not data.get('aweme_list'):
                raise ValueError(f"抖音 API 返回错误状态码: {api_status}，且无作品数据，请检查 Cookie 是否有效")
        
        return data
    
    def get_all_works(self, share_url: str, sec_uid: str = None) -> List[Dict[str, Any]]:
        """
        获取用户所有作品
        
        Args:
            share_url: 分享链接
            sec_uid: 用户 sec_uid（如已知则直接使用，避免重复请求）
            
        Returns:
            作品列表
        """
        if not sec_uid:
            sec_uid = self.get_sec_uid(share_url)
        has_more = True
        max_cursor = 0
        work_list = []
        
        while has_more:
            data = self.get_work_list(sec_uid, max_cursor)
            has_more = data.get('has_more', False)
            max_cursor = data.get('max_cursor', 0)

            aweme_list = data.get('aweme_list') or []
            if not isinstance(aweme_list, list):
                raise ValueError(f"抖音作品列表返回异常类型: {type(aweme_list).__name__}")

            for item in aweme_list:
                work_list.append(self._normalize_work_item(item, sec_uid))
            
            # 请求间隔，避免频繁请求
            time.sleep(self.request_delay)
        
        return work_list
    
    def download_file_with_resume(
        self,
        url: str,
        file_path: str,
        task_id: int = None,
        progress_callback: Callable[[int, int, float], None] = None,
        check_pause: Callable[[], bool] = None
    ) -> Dict[str, Any]:
        """
        下载文件，支持断点续传
        
        Args:
            url: 下载URL
            file_path: 保存路径
            task_id: 任务ID（用于进度更新）
            progress_callback: 进度回调函数(downloaded, total, speed)
            check_pause: 检查暂停的函数，返回 True 表示需要暂停
            
        Returns:
            下载结果字典
        """
        temp_path = file_path + ".downloading"
        downloaded_bytes = 0
        
        # 检查是否有未完成的下载
        if os.path.exists(temp_path):
            downloaded_bytes = os.path.getsize(temp_path)
        
        # 构建 Range 请求头
        headers = self.headers.copy()
        if downloaded_bytes > 0:
            headers['Range'] = f'bytes={downloaded_bytes}-'
        
        try:
            logger.info(f"开始下载文件: {file_path}, URL: {url[:100]}...")

            # 发起请求
            res = self.session.get(
                url,
                headers=headers,
                stream=True,
                timeout=self.download_timeout
            )

            # 检查是否支持断点续传
            if res.status_code == 206:  # Partial Content
                # 解析 Content-Range: bytes 0-999/1000
                content_range = res.headers.get('Content-Range', '')
                match = re.search(r'/(\d+)', content_range)
                total_bytes = int(match.group(1)) if match else 0
                logger.info(f"支持断点续传, 从 {downloaded_bytes} 字节继续下载, 总大小: {total_bytes}")
            elif res.status_code == 200:
                # 不支持断点续传，从头开始
                total_bytes = int(res.headers.get('Content-Length', 0))
                downloaded_bytes = 0
                # 删除旧的临时文件
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                logger.info(f"开始全新下载, 文件大小: {total_bytes} 字节")
            else:
                error_msg = f"下载失败，HTTP状态码: {res.status_code}, URL: {url[:100]}"
                logger.error(error_msg)
                raise Exception(error_msg)
            
            # 确保目录存在
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            
            # 开始下载
            start_time = time.time()
            last_update_time = start_time
            last_pause_check_time = start_time - 0.5
            bytes_since_last_update = 0
            
            mode = 'ab' if downloaded_bytes > 0 else 'wb'
            with open(temp_path, mode) as f:
                for chunk in res.iter_content(chunk_size=settings.DOWNLOAD_CHUNK_SIZE):
                    current_time = time.time()
                    # 暂停检查与进度更新同频，避免每个 1MB chunk 都往返 Redis。
                    should_check_pause = (
                        check_pause
                        and current_time - last_pause_check_time >= 0.5
                    )
                    if should_check_pause:
                        last_pause_check_time = current_time
                    if should_check_pause and check_pause():
                        # 保存进度并退出
                        if task_id:
                            redis_client.update_progress(task_id, {
                                'status': 'paused',
                                'downloaded_bytes': downloaded_bytes,
                                'total_bytes': total_bytes
                            })
                        return {
                            'success': False,
                            'paused': True,
                            'downloaded_bytes': downloaded_bytes,
                            'total_bytes': total_bytes,
                            'temp_path': temp_path
                        }
                    
                    if chunk:
                        f.write(chunk)
                        chunk_size = len(chunk)
                        downloaded_bytes += chunk_size
                        bytes_since_last_update += chunk_size
                        
                        # 每0.5秒更新一次进度
                        current_time = time.time()
                        if current_time - last_update_time >= 0.5:
                            elapsed = current_time - last_update_time
                            speed = bytes_since_last_update / elapsed if elapsed > 0 else 0
                            
                            # 调用进度回调
                            if progress_callback:
                                progress_callback(downloaded_bytes, total_bytes, speed)
                            
                            # 更新 Redis 进度
                            if task_id:
                                redis_client.update_progress(task_id, {
                                    'status': 'downloading',
                                    'downloaded_bytes': downloaded_bytes,
                                    'total_bytes': total_bytes,
                                    'speed': speed,
                                    'progress_percent': round(downloaded_bytes / total_bytes * 100, 2) if total_bytes > 0 else 0
                                })
                            
                            last_update_time = current_time
                            bytes_since_last_update = 0
            
            # 下载完成，重命名临时文件
            if os.path.exists(file_path):
                os.remove(file_path)
            os.rename(temp_path, file_path)

            total_time = time.time() - start_time
            avg_speed = downloaded_bytes / total_time if total_time > 0 else 0
            logger.info(f"下载完成: {file_path}, 大小: {downloaded_bytes} 字节, 耗时: {total_time:.1f}秒, 平均速度: {avg_speed/1024:.1f} KB/s")

            return {
                'success': True,
                'paused': False,
                'downloaded_bytes': downloaded_bytes,
                'total_bytes': total_bytes,
                'file_path': file_path,
                'duration': int(total_time)
            }

        except requests.exceptions.Timeout as e:
            error_msg = f"下载超时: {str(e)}"
            logger.error(f"{error_msg}, URL: {url[:100]}")
            return {
                'success': False,
                'paused': False,
                'error': error_msg,
                'downloaded_bytes': downloaded_bytes,
                'temp_path': temp_path if os.path.exists(temp_path) else None
            }
        except requests.exceptions.ConnectionError as e:
            error_msg = f"网络连接错误: {str(e)}"
            logger.error(f"{error_msg}, URL: {url[:100]}")
            return {
                'success': False,
                'paused': False,
                'error': error_msg,
                'downloaded_bytes': downloaded_bytes,
                'temp_path': temp_path if os.path.exists(temp_path) else None
            }
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}"
            logger.error(f"下载失败: {error_msg}, URL: {url[:100]}")
            return {
                'success': False,
                'paused': False,
                'error': error_msg,
                'downloaded_bytes': downloaded_bytes,
                'temp_path': temp_path if os.path.exists(temp_path) else None
            }
    
    def refresh_work_urls(self, aweme_id: str) -> Dict[str, Any]:
        """
        通过作品ID重新获取最新的下载URL
        
        Args:
            aweme_id: 作品ID
            
        Returns:
            包含最新 video/images URL 的字典
        """
        api_url = f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={aweme_id}&device_platform=webapp&aid=6383"
        
        res, _ = get_douyin_response(self.session, api_url, timeout=self.download_timeout)
        if res.status_code != 200:
            raise ValueError(f"刷新URL失败 (HTTP {res.status_code})")
        try:
            data = res.json()
        except (json.JSONDecodeError, ValueError):
            raise ValueError("刷新URL失败，返回非JSON内容")
        
        item = data.get('aweme_detail')
        if not item:
            raise ValueError(f"作品 {aweme_id} 详情获取失败，可能已被删除")
        
        return {
            **self._normalize_work_item(item),
        }

    @staticmethod
    def sanitize_filename(name: str, max_length: int = 50) -> str:
        """
        清理文件名，移除非法字符
        
        Args:
            name: 原始文件名
            max_length: 最大长度
            
        Returns:
            清理后的文件名
        """
        # 移除非法字符
        invalid_pattern = r'[\n\\/:*?"<>|]'
        name = re.sub(invalid_pattern, ' ', name)
        # 移除多余空格
        name = ' '.join(name.split())
        # 限制长度
        if len(name) > max_length:
            name = name[:max_length]
        return name.strip() or "untitled"
    
    def build_file_path(
        self,
        author_name: str,
        desc: str,
        aweme_id: str,
        index: int = None,
        is_video: bool = True,
        is_live_photo: bool = False,
    ) -> str:
        """
        构建文件保存路径
        
        Args:
            author_name: 作者名称
            desc: 作品描述
            aweme_id: 作品ID
            index: 图集索引（图集时使用）
            is_video: 是否为视频
            is_live_photo: 是否为图集中的实况视频
            
        Returns:
            完整的文件路径
        """
        # 清理作者名和描述
        author_name = self.sanitize_filename(author_name, 30)
        desc = self.sanitize_filename(desc, 50)
        
        # 构建目录
        author_dir = os.path.join(self.filepath, author_name)
        
        # 构建文件名
        if is_video:
            filename = f"{desc}_{aweme_id}.mp4"
        elif is_live_photo:
            filename = f"{desc}_{aweme_id}_{index}_live.mp4"
        else:
            filename = f"{desc}_{aweme_id}_{index}.jpg"
        
        return os.path.join(author_dir, filename)
