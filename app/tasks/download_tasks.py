"""
Celery 下载任务

为什么这样设计：
1. download_single_file: 单个文件下载任务，支持暂停/恢复
2. download_author_works: 批量下载作者所有作品
3. check_subscriptions: 定时检查订阅作者的新作品
4. 任务执行时更新数据库状态,便于查询
5. 使用日志记录详细的错误信息,便于调试
"""

from celery import shared_task, current_task
from celery.exceptions import SoftTimeLimitExceeded
from datetime import datetime, timedelta
from sqlalchemy import select, update
from sqlalchemy.orm import Session
import time
import logging
import traceback
import json
import random
from uuid import uuid4

from app.tasks.celery_app import celery_app
from app.models.database import get_sync_db
from app.models.models import (
    Author, Work, DownloadTask, DownloadHistory, SystemConfig,
    SubscriptionCheckReport,
    AuthorProfileHistory,
)
from app.services.downloader import (
    latest_video_url,
    payload_image_urls,
    payload_live_photo_urls,
    prefer_avatar_url,
    _classify_author_account_status,
)
from app.core import redis_client
from app.core.config import settings
from app.core.runtime_config import get_runtime_config_sync
from app.core.traffic_control import global_download_slot
from app.services.download_task_factory import ensure_download_task_sync
from app.services.work_manager import recalc_author_counts_sync, refresh_work_download_state_sync
from app.services.work_metadata import apply_work_payload
from app.services.douyin_account import get_request_context_sync
from app.services.douyin_errors import DouyinRequestError
from app.services.douyin_source import (
    DouyinSource,
    DouyinTraversalLimitError,
    build_author_profile_url,
    build_douyin_source,
)
from app.services.douyin_media import build_douyin_media_engine
from app.services.archive_rules import (
    archive_size_limits,
    build_archive_file_path,
    deserialize_archive_rules,
    get_archive_rules_sync,
    serialize_archive_rules,
    work_matches_archive_rules,
    write_metadata_sidecars,
)

# 配置日志
import os
logs_dir = 'logs'
if not os.path.exists(logs_dir):
    os.makedirs(logs_dir)

logger = logging.getLogger(__name__)
if not logger.handlers:  # 避免重复添加handler
    logger.setLevel(logging.INFO)
    # 文件handler
    file_handler = logging.FileHandler('logs/download_tasks.log', encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    # 控制台handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    # 格式化
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    # 添加handlers
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


SUBSCRIPTION_CHECK_STATE_KEY = "runtime:last_subscription_check_at"
SUBSCRIPTION_CHECK_LOCK_KEY = "douyin:subscription_check_lock"
SUBSCRIPTION_CYCLE_STATE_KEY = "subscription_check_cycle_state"
AUTHOR_ACCOUNT_STATUS_MARKER_PREFIX = "__ACCOUNT_STATUS__"
TERMINAL_AUTHOR_ACCOUNT_STATUSES = {"deleted", "banned", "restricted"}


def _notify_event(event: str, title: str, body: str, *, level: str, dedupe_key: str) -> None:
    try:
        celery_app.send_task(
            "app.tasks.notification_tasks.deliver_notification",
            kwargs={
                "event": event,
                "title": title,
                "body": body,
                "level": level,
                "dedupe_key": dedupe_key,
            },
        )
    except Exception as exc:
        logger.warning(f"通知任务入队失败，不影响业务任务: {type(exc).__name__}: {str(exc)[:200]}")


def build_author_account_status_marker(status_code: str, status_label: str, detail: str | None = None) -> str:
    clean_detail = " ".join(str(detail or "").split())[:200]
    payload = [AUTHOR_ACCOUNT_STATUS_MARKER_PREFIX, status_code, status_label]
    if clean_detail:
        payload.append(clean_detail)
    return "|".join(payload)


def parse_author_account_status_marker(value: str | None) -> dict | None:
    if not value or not value.startswith(f"{AUTHOR_ACCOUNT_STATUS_MARKER_PREFIX}|"):
        return None

    parts = value.split("|", 3)
    return {
        "status_code": parts[1] if len(parts) > 1 else "unavailable",
        "status_label": parts[2] if len(parts) > 2 else "状态异常",
        "status_detail": parts[3] if len(parts) > 3 else None,
    }


def _parse_datetime(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _load_subscription_cycle_state(db: Session) -> dict:
    row = db.execute(
        select(SystemConfig).where(SystemConfig.key == SUBSCRIPTION_CYCLE_STATE_KEY)
    ).scalar_one_or_none()
    if not row or not row.value:
        return {}
    try:
        state = json.loads(row.value)
        return state if isinstance(state, dict) else {}
    except (TypeError, ValueError):
        return {}


def _save_subscription_cycle_state(db: Session, state: dict) -> None:
    row = db.execute(
        select(SystemConfig).where(SystemConfig.key == SUBSCRIPTION_CYCLE_STATE_KEY)
    ).scalar_one_or_none()
    value = json.dumps(state, ensure_ascii=False)
    if row:
        row.value = value
    else:
        db.add(SystemConfig(key=SUBSCRIPTION_CYCLE_STATE_KEY, value=value))


def _clear_subscription_cycle_state(db: Session, cycle_id: str | None = None) -> None:
    row = db.execute(
        select(SystemConfig).where(SystemConfig.key == SUBSCRIPTION_CYCLE_STATE_KEY)
    ).scalar_one_or_none()
    if not row:
        return
    if cycle_id:
        try:
            current = json.loads(row.value or "{}")
        except (TypeError, ValueError):
            current = {}
        if current.get("cycle_id") not in {None, cycle_id}:
            return
    db.delete(row)


def _release_subscription_lock(token: str) -> None:
    try:
        redis_client.redis_client.eval(
            "if redis.call('GET', KEYS[1]) == ARGV[1] then "
            "return redis.call('DEL', KEYS[1]) else return 0 end",
            1,
            SUBSCRIPTION_CHECK_LOCK_KEY,
            token,
        )
    except Exception:
        pass


def _is_probable_rate_limit_error(value: str | None) -> bool:
    """识别抖音常见的限流、验证码和网关伪成功响应。"""
    text = str(value or "").lower()
    return any(token in text for token in (
        "限流", "风控", "反爬", "验证码", "验证失败", "安全校验", "argussecurityplugin", "错误状态码", "非 json", "空响应", "服务异常",
        "too many requests", "rate limit", "status 429", "http 429",
        "status 403", "http 403", "captcha", "verify",
    ))


def _work_create_time(item: dict) -> int:
    """取作品发布时间戳，缺失时返回 0。"""
    try:
        return int(item.get("create_time") or 0)
    except (TypeError, ValueError):
        return 0


def _select_latest_work(work_list: list) -> dict | None:
    """
    选出"真正最新"的作品作为增量游标。

    抖音主页接口会把置顶(is_top)作品排在最前，单纯取 work_list[0] 会把游标
    永久卡在一个旧的置顶作品上，导致后续新作品永远检测不到。这里优先按发布
    时间 create_time 取最大；当接口未返回时间时退回到列表首个作品。
    """
    if not work_list:
        return None
    if any(_work_create_time(item) > 0 for item in work_list):
        return max(work_list, key=_work_create_time)
    return work_list[0]


def _detect_new_works(db: Session, author_id: int, work_list: list) -> list:
    """
    基于数据库已存在的作品判断哪些是新作品。

    不再依赖 last_aweme_id 的位置匹配（会被置顶作品破坏），而是直接对比
    数据库里该作者已记录的 aweme_id 集合，凡是没入库的都算新作品。
    """
    if not work_list:
        return []

    existing_ids = set(
        db.execute(
            select(Work.aweme_id).where(Work.author_id == author_id)
        ).scalars().all()
    )
    return [item for item in work_list if str(item["aweme_id"]) not in existing_ids]


def _known_work_ids(db: Session, author_id: int) -> set[str]:
    return {
        str(value)
        for value in db.execute(
            select(Work.aweme_id).where(Work.author_id == author_id)
        ).scalars().all()
        if value is not None
    }


def _collect_author_works(source, author: Author, db: Session, runtime_config: dict, *, incremental: bool) -> dict:
    known_ids = _known_work_ids(db, author.id)
    if not incremental or not known_ids:
        return source.scan_all_works(author.sec_uid, known_ids)
    return source.scan_incremental_works(
        author.sec_uid,
        known_ids,
        known_streak=int(runtime_config.get("subscription_known_streak", settings.SUBSCRIPTION_KNOWN_STREAK)),
        max_pages=int(runtime_config.get("subscription_max_pages", settings.SUBSCRIPTION_MAX_PAGES)),
        safe_lookback_pages=int(runtime_config.get(
            "subscription_safe_lookback_pages", settings.SUBSCRIPTION_SAFE_LOOKBACK_PAGES
        )),
    )


def _full_reconcile_due(author: Author, runtime_config: dict, *, forced: bool = False) -> bool:
    if forced:
        return True
    interval = int(runtime_config.get(
        "subscription_full_reconcile_interval",
        settings.SUBSCRIPTION_FULL_RECONCILE_INTERVAL,
    ))
    last_reconcile = author.last_full_reconcile_at
    return last_reconcile is None or (datetime.now() - last_reconcile).total_seconds() >= interval


def _scan_audit_fields(scan_result: dict) -> dict:
    metrics = scan_result.get("metrics") or {}
    return {
        "scan_mode": metrics.get("mode"),
        "pages_requested": int(metrics.get("pages_requested") or 0),
        "stop_reason": metrics.get("stop_reason"),
        "known_hits": int(metrics.get("known_hits") or 0),
    }


def _mark_author_account_anomaly(
    author: Author,
    status_code: str,
    status_label: str,
    detail: str | None = None,
) -> None:
    """
    标注"作者账号异常"（禁言/封号/注销/不可访问等）：

    - 用结构化标记写入 last_error，前端据此显示状态徽章，方便人工在作者管理里筛查
    - 仅标注，不自动取消订阅：账号状态可能存在误判，若因此退订正常作者会很麻烦，
      是否退订交由用户手动决定
    - 头像、昵称、作品等历史数据一律保留

    注意：这类情况是"作者账号本身有问题"，不是我方被抖音限流，调用方不应
    将其计入限流中断逻辑。
    """
    author.last_error = build_author_account_status_marker(status_code, status_label, detail)


def _author_is_being_deleted(author_id: int, phase: str) -> bool:
    """删除作者时，终止后续任务派生和执行。"""
    if not redis_client.is_author_deleting(author_id):
        return False

    logger.warning(f"作者 {author_id} 正在删除，停止任务执行: phase={phase}")
    try:
        redis_client.append_activity_log(
            "warning",
            "task",
            "作者删除进行中，已停止相关下载任务",
            f"author_id={author_id}, phase={phase}",
        )
    except Exception:
        pass
    return True


def _get_last_subscription_check_time(db: Session):
    config = db.execute(
        select(SystemConfig).where(SystemConfig.key == SUBSCRIPTION_CHECK_STATE_KEY)
    ).scalar_one_or_none()
    return _parse_datetime(config.value) if config else None


def _mark_subscription_check_started(db: Session):
    now_text = datetime.now().isoformat(timespec="seconds")
    config = db.execute(
        select(SystemConfig).where(SystemConfig.key == SUBSCRIPTION_CHECK_STATE_KEY)
    ).scalar_one_or_none()
    if config:
        config.value = now_text
    else:
        db.add(SystemConfig(key=SUBSCRIPTION_CHECK_STATE_KEY, value=now_text))
    db.commit()


def _reset_subscription_check_cooldown(db: Session):
    """
    清除订阅检查的全局冷却标记与分布式锁。

    当一轮检查因接近 Celery 超时被迫提前结束（作者太多、单轮跑不完）时调用，
    这样下一次 Beat 触发能立即继续检查"尚未轮到"的作者，而不是被全局间隔
    (默认 6 小时) 挡在门外，导致靠后的订阅作者长期得不到检查。
    """
    try:
        config = db.execute(
            select(SystemConfig).where(SystemConfig.key == SUBSCRIPTION_CHECK_STATE_KEY)
        ).scalar_one_or_none()
        if config:
            config.value = datetime.fromtimestamp(0).isoformat(timespec="seconds")
            db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    try:
        redis_client.redis_client.delete(SUBSCRIPTION_CHECK_LOCK_KEY)
    except Exception:
        pass


def _queue_scanned_new_works(
    db: Session,
    author: Author,
    new_works: list[dict],
) -> dict:
    """直接持久化本轮已验证的新作品，避免再次扫描导致历史缺口被停止边界挡住。"""
    task_ids: list[int] = []
    persisted_works = 0
    filtered_works = 0
    archive_rules = get_archive_rules_sync(db)
    archive_snapshot = serialize_archive_rules(archive_rules)
    for item in new_works:
        aweme_id = str(item.get("aweme_id") or "")
        if not aweme_id:
            raise ValueError("扫描结果缺少 aweme_id")
        existing = db.execute(
            select(Work).where(Work.aweme_id == aweme_id)
        ).scalar_one_or_none()
        if existing:
            continue

        work = Work(
            aweme_id=aweme_id,
            author_id=author.id,
            title=item.get("desc", ""),
            work_type="video",
        )
        apply_work_payload(db, work, item)
        db.add(work)
        db.flush()
        persisted_works += 1

        matches, _ = work_matches_archive_rules(work, archive_rules)
        if not matches:
            filtered_works += 1
            continue

        file_indices = [0] if work.work_type == "video" else list(range(work.image_count))
        for file_index in file_indices:
            task, action = ensure_download_task_sync(
                db, work.id, file_index, archive_rule_snapshot=archive_snapshot,
            )
            if action in {"created", "reused"}:
                task_ids.append(task.id)
                work.is_downloaded = False

    recalc_author_counts_sync(db, author)
    db.commit()

    celery_task_ids: list[str] = []
    for task_id in task_ids:
        queued = download_single_file.delay(task_id)
        celery_task_ids.append(queued.id)
        db.execute(
            update(DownloadTask)
            .where(DownloadTask.id == task_id, DownloadTask.status == "pending")
            .values(celery_task_id=queued.id)
        )
    db.commit()
    return {
        "persisted_works": persisted_works,
        "file_tasks": len(task_ids),
        "filtered_works": filtered_works,
        "celery_task_ids": celery_task_ids,
    }


def _refresh_scanned_works(db: Session, author_id: int, work_list: list[dict]) -> int:
    """用本轮已取回的数据批量刷新已入库作品，避免逐条查询和覆盖统计历史。"""
    payloads = {
        str(item.get("aweme_id")): item
        for item in work_list
        if item.get("aweme_id")
    }
    if not payloads:
        return 0
    works = db.execute(
        select(Work).where(
            Work.author_id == author_id,
            Work.aweme_id.in_(list(payloads)),
        )
    ).scalars().all()
    changed_stats = 0
    for work in works:
        if apply_work_payload(
            db, work, payloads[str(work.aweme_id)], preserve_existing=True
        ):
            changed_stats += 1
    return changed_stats


def sync_author_profile(author: Author, source: DouyinSource) -> dict:
    """同步作者基础资料，和订阅检查共用同一轮风控节奏。"""
    profile = source.fetch_profile(author.sec_uid)
    old_nickname = author.nickname
    old_avatar = author.avatar_url
    old_share_url = author.share_url
    old_last_error = author.last_error

    nickname = profile.get("nickname")
    avatar_url = profile.get("avatar_url")
    profile_url = profile.get("profile_url") or build_author_profile_url(author.sec_uid)
    account_status = profile.get("account_status", "active")
    account_status_label = profile.get("account_status_label", "正常")
    account_status_detail = profile.get("account_status_detail")

    if nickname and account_status == "active":
        author.nickname = nickname
    elif nickname and not author.nickname:
        author.nickname = nickname

    if avatar_url and account_status == "active":
        author.avatar_url = prefer_avatar_url(author.avatar_url, avatar_url)
        try:
            source.cache_author_avatar(author.id, author.avatar_url)
        except Exception as exc:
            logger.warning("缓存作者头像失败 author_id=%s: %s", author.id, exc)
    if profile_url:
        author.share_url = profile_url

    if account_status in TERMINAL_AUTHOR_ACCOUNT_STATUSES:
        author.last_error = build_author_account_status_marker(
            account_status,
            account_status_label,
            account_status_detail,
        )
    elif account_status == "active":
        author.last_error = None

    changed = (
        (old_nickname != author.nickname)
        or (old_avatar != author.avatar_url)
        or (old_share_url != author.share_url)
        or (old_last_error != author.last_error)
    )
    return {
        "changed": changed,
        "nickname": author.nickname,
        "avatar_url": author.avatar_url,
        "old_nickname": old_nickname,
        "old_avatar_url": old_avatar,
        "account_status": account_status,
        "account_status_label": account_status_label,
        "account_status_detail": account_status_detail,
    }


def record_author_profile_history(db: Session, author: Author, profile_result: dict) -> int:
    """把本次真实发生的昵称/头像变化追加到历史，不覆盖旧记录。"""
    changes = 0
    pairs = (
        ("nickname", profile_result.get("old_nickname"), author.nickname),
        ("avatar", profile_result.get("old_avatar_url"), author.avatar_url),
    )
    for field_name, old_value, new_value in pairs:
        if old_value and new_value and old_value != new_value:
            db.add(AuthorProfileHistory(
                author_id=author.id,
                field_name=field_name,
                old_value=old_value,
                new_value=new_value,
            ))
            changes += 1
    return changes


@celery_app.task(bind=True, name="app.tasks.download_tasks.download_single_file")
def download_single_file(self, task_id: int, risk_retry_attempt: int = 0):
    """
    下载单个文件的 Celery 任务
    
    Args:
        task_id: 数据库中的 DownloadTask ID
    """
    # ---- 顶层安全防护：任何异常都不能让 Worker 进程崩溃 ----
    try:
        with global_download_slot(getattr(self.request, "id", None) or task_id):
            _download_single_file_impl(self, task_id, risk_retry_attempt=risk_retry_attempt)
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)[:300]}"
        logger.error(f"任务 {task_id} 顶层异常: {error_msg}\n{traceback.format_exc()}")
        try:
            redis_client.append_activity_log("error", "task",
                f"❌ 任务异常: task_id={task_id}", error_msg)
        except Exception:
            pass
        # 尝试标记数据库中任务为 failed
        try:
            db = get_sync_db()
            task_obj = db.execute(
                select(DownloadTask).where(DownloadTask.id == task_id)
            ).scalar_one_or_none()
            if task_obj and task_obj.status not in ("completed", "failed"):
                task_obj.status = "failed"
                task_obj.error_message = error_msg
                db.commit()
            db.close()
        except Exception:
            pass


def _download_single_file_impl(self_task, task_id: int, risk_retry_attempt: int = 0):
    """download_single_file 的实际实现"""
    redis_client.append_activity_log("info", "task",
        f"⭐ download_single_file 启动", f"task_id={task_id}")
    db = get_sync_db()
    try:
        # 原子认领任务。队列消息被重复投递时，只有第一个进程能把 pending
        # 改为 downloading，其余消息直接结束，避免同一文件并发下载。
        claimed = db.execute(
            update(DownloadTask)
            .where(DownloadTask.id == task_id, DownloadTask.status == "pending")
            .values(
                status="downloading",
                celery_task_id=self_task.request.id,
                started_at=datetime.now(),
            )
        )
        if claimed.rowcount != 1:
            db.rollback()
            current_status = db.execute(
                select(DownloadTask.status).where(DownloadTask.id == task_id)
            ).scalar_one_or_none()
            if current_status is None:
                logger.error(f"任务 {task_id} 不存在")
                redis_client.append_activity_log("error", "task", f"任务不存在: task_id={task_id}")
                return {"success": False, "error": "任务不存在"}
            logger.info(f"任务 {task_id} 当前状态为 {current_status}，跳过重复队列消息")
            return {"success": True, "skipped": True, "status": current_status}
        db.commit()

        task = db.execute(
            select(DownloadTask).where(DownloadTask.id == task_id)
        ).scalar_one()

        if not task:
            logger.error(f"任务 {task_id} 不存在")
            redis_client.append_activity_log("error", "task", f"任务不存在: task_id={task_id}")
            return {"success": False, "error": "任务不存在"}

        logger.info(f"开始下载任务 {task_id}")
        redis_client.append_activity_log("info", "task", f"开始下载任务 {task_id}", f"work_id={task.work_id}, file_index={task.file_index}, status={task.status}")
        
        # 获取作品信息
        work = db.execute(
            select(Work).where(Work.id == task.work_id)
        ).scalar_one_or_none()

        if not work:
            logger.error(f"任务 {task_id} 关联的作品 {task.work_id} 不存在")
            task.status = "failed"
            task.error_message = "关联的作品不存在"
            db.commit()
            return {"success": False, "error": "作品不存在"}
        
        # 获取作者信息
        author = db.execute(
            select(Author).where(Author.id == work.author_id)
        ).scalar_one_or_none()

        if not author:
            logger.error(f"任务 {task_id} 关联的作者 {work.author_id} 不存在")
            task.status = "failed"
            task.error_message = "关联的作者不存在"
            db.commit()
            return {"success": False, "error": "作者不存在"}

        if _author_is_being_deleted(author.id, "download_single_file"):
            task.status = "cancelled"
            task.error_message = "作者正在删除"
            db.commit()
            return {"success": False, "deleted": True, "error": "作者正在删除"}

        if task.archive_rule_snapshot:
            archive_rules = deserialize_archive_rules(task.archive_rule_snapshot)
        else:
            # 兼容升级前已存在的任务：首次执行时固化当时的网页规则。
            archive_rules = get_archive_rules_sync(db)
            task.archive_rule_snapshot = serialize_archive_rules(archive_rules)
            db.commit()
        min_file_size, max_file_size = archive_size_limits(archive_rules)
        
        runtime_config = get_runtime_config_sync(db)

        # 采集和文件执行使用独立契约，任务不再直接操作下载器会话。
        request_context = get_request_context_sync(db)
        cookie = request_context.cookie
        source = build_douyin_source(
            cookie, settings.DOWNLOAD_DIR, runtime_config=runtime_config,
            request_context=request_context,
        )
        media = build_douyin_media_engine(
            cookie, settings.DOWNLOAD_DIR, runtime_config=runtime_config,
            request_context=request_context,
        )
        
        # 确定下载 URL 和文件路径
        if work.work_type == "video":
            url = work.video_url
            file_path = build_archive_file_path(
                settings.DOWNLOAD_DIR, archive_rules, author, work, task.file_index,
            )
        else:
            # 图集
            image_urls = work.image_urls
            live_photo_urls = work.live_photo_urls
            # 旧数据没有保存实况元数据。首次重下时主动刷新一次，避免静态封面 URL
            # 仍有效而跳过后面的“过期 URL 刷新”，继续误下成 JPG。
            if len(live_photo_urls) != len(image_urls):
                try:
                    fresh = source.refresh_assets(work.aweme_id)
                    refreshed_image_urls = payload_image_urls(fresh)
                    refreshed_live_photo_urls = payload_live_photo_urls(fresh)
                    if refreshed_image_urls:
                        image_urls = refreshed_image_urls
                        live_photo_urls = refreshed_live_photo_urls
                        work.image_urls = image_urls
                        work.image_count = len(image_urls)
                        work.live_photo_urls = live_photo_urls
                        db.commit()
                except Exception as exc:
                    logger.warning(
                        f"任务 {task_id} - 补全实况图片元数据失败，继续使用已有图片地址: {exc}"
                    )

            if task.file_index >= len(image_urls):
                task.status = "failed"
                task.error_message = "图片索引超出范围"
                db.commit()
                return {"success": False, "error": "图片索引超出范围"}
            
            live_photo_url = (
                live_photo_urls[task.file_index]
                if task.file_index < len(live_photo_urls)
                else None
            )
            url = live_photo_url or image_urls[task.file_index]
            file_path = build_archive_file_path(
                settings.DOWNLOAD_DIR,
                archive_rules,
                author,
                work,
                task.file_index,
                is_live_photo=bool(live_photo_url),
            )
        
        # URL 有效性检测：抖音 URL 会过期，重试任务必须刷新
        try:
            probe_status = media.probe_status(url, timeout=10)
            if probe_status in (403, 404, 410):
                logger.info(f"任务 {task_id} - URL 已过期(HTTP {probe_status})，刷新中...")
                fresh = source.refresh_assets(work.aweme_id)
                if work.work_type == "video":
                    refreshed_video_url = latest_video_url(fresh)
                    if refreshed_video_url:
                        url = refreshed_video_url
                        work.video_url = refreshed_video_url
                else:
                    image_urls = payload_image_urls(fresh)
                    live_photo_urls = payload_live_photo_urls(fresh)
                    if image_urls and task.file_index < len(image_urls):
                        live_photo_url = (
                            live_photo_urls[task.file_index]
                            if task.file_index < len(live_photo_urls)
                            else None
                        )
                        url = live_photo_url or image_urls[task.file_index]
                        work.image_urls = image_urls
                        work.image_count = len(image_urls)
                        work.live_photo_urls = live_photo_urls
                        file_path = build_archive_file_path(
                            settings.DOWNLOAD_DIR,
                            archive_rules,
                            author,
                            work,
                            task.file_index,
                            is_live_photo=bool(live_photo_url),
                        )
                db.commit()
                logger.info(f"任务 {task_id} - URL 已刷新")
        except DouyinRequestError:
            # 已确认直链失效且刷新接口命中风控时，交给外层统一延期；不能继续
            # 使用已失效地址，否则会把可恢复的风控错误降级成普通 403 失败。
            raise
        except Exception as e:
            logger.warning(f"任务 {task_id} - URL 有效性检测失败: {e}，使用原 URL 继续")

        task.file_path = file_path
        task.file_name = file_path.split("/")[-1].split("\\")[-1]
        db.commit()

        logger.info(f"任务 {task_id} - 下载文件: {task.file_name}, URL: {url[:100]}...")
        
        # 定义进度回调（节流：每 5 秒才写一次数据库，减少数据库压力）
        _last_db_commit = [time.time()]
        def progress_callback(downloaded, total, speed):
            task.downloaded_bytes = downloaded
            task.total_bytes = total
            task.download_speed = speed
            now = time.time()
            if now - _last_db_commit[0] >= 5:
                try:
                    db.commit()
                except Exception as commit_err:
                    logger.warning(f"任务 {task_id} 进度写入数据库失败: {commit_err}")
                    try:
                        db.rollback()
                    except Exception:
                        pass
                _last_db_commit[0] = now
        
        # 定义暂停检查
        def check_pause():
            return redis_client.is_task_paused(task_id)
        
        # 执行下载
        result = media.download(
            url=url,
            file_path=file_path,
            task_id=task_id,
            progress_callback=progress_callback,
            check_pause=check_pause,
            min_file_size=min_file_size,
            max_file_size=max_file_size,
        )
        
        if result.get("paused"):
            task.status = "paused"
            task.temp_file_path = result.get("temp_path")
            db.commit()
            logger.info(f"任务 {task_id} 已暂停, 已下载: {task.downloaded_bytes}/{task.total_bytes}")
            return {"success": False, "paused": True}

        if result.get("filtered"):
            task.status = "skipped"
            task.error_message = result.get("error") or "文件大小不符合归档规则"
            task.completed_at = datetime.now()
            task.downloaded_bytes = 0
            task.total_bytes = result.get("total_bytes", 0)
            task.temp_file_path = None
            refresh_work_download_state_sync(db, work)
            recalc_author_counts_sync(db, author)
            db.commit()
            redis_client.delete_progress(task_id)
            redis_client.append_activity_log(
                "info", "task", f"归档规则已跳过: {task.file_name}",
                f"task_id={task_id}, reason={task.error_message}",
            )
            return {"success": True, "skipped": True, "reason": task.error_message}

        if result.get("success"):
            write_metadata_sidecars(
                file_path, archive_rules, author, work, task.file_index,
            )
            task.status = "completed"
            task.completed_at = datetime.now()
            task.downloaded_bytes = result["downloaded_bytes"]
            task.total_bytes = result["total_bytes"]

            # 创建历史记录
            history = DownloadHistory(
                task_id=task.id,
                work_id=work.id,
                author_nickname=author.nickname,
                work_title=work.title,
                file_path=file_path,
                file_size=result["total_bytes"],
                download_duration=result.get("duration", 0)
            )
            db.add(history)

            # 只有作品的全部文件任务完成后才标记作品完成；作者计数按
            # 完整作品数重算，重试和多图下载都不会重复累加。
            refresh_work_download_state_sync(db, work)
            recalc_author_counts_sync(db, author)

            db.commit()

            # 清理 Redis 进度
            redis_client.delete_progress(task_id)

            logger.info(f"任务 {task_id} 下载成功: {task.file_name}, 大小: {result['total_bytes']} bytes")
            redis_client.append_activity_log("info", "task",
                f"下载成功: {task.file_name}",
                f"task_id={task_id}, size={result['total_bytes']} bytes")
            return {"success": True, "file_path": file_path}
        else:
            error_msg = result.get("error", "未知错误")
            task.status = "failed"
            task.error_message = error_msg
            task.retry_count = (task.retry_count or 0) + 1
            db.commit()
            logger.error(f"任务 {task_id} 下载失败: {error_msg}, 重试次数: {task.retry_count}")
            redis_client.append_activity_log("error", "task",
                f"下载失败: task_id={task_id}",
                f"error={error_msg}, retry={task.retry_count}")
            _notify_event(
                "download_failure",
                "作品下载失败",
                f"任务 #{task_id} 下载失败：{str(error_msg)[:500]}",
                level="error",
                dedupe_key=f"download:{task_id}:{error_msg}",
            )
            return {"success": False, "error": error_msg}

    except DouyinRequestError as e:
        db.rollback()
        runtime_config = get_runtime_config_sync(db)
        auto_retry = bool(runtime_config.get("douyin_risk_auto_retry", settings.DOUYIN_RISK_AUTO_RETRY))
        retry_after = int(e.retry_after or runtime_config.get(
            "douyin_risk_cooldown_seconds", settings.DOUYIN_RISK_COOLDOWN_SECONDS
        )) + random.randint(3, 25)
        task = db.execute(select(DownloadTask).where(DownloadTask.id == task_id)).scalar_one_or_none()
        if task and e.code in {"argus_blocked", "rate_limited"} and auto_retry and risk_retry_attempt < 1:
            task.status = "pending"
            task.error_message = f"{e.user_message} 系统将在约 {retry_after} 秒后自动恢复一次。"
            db.commit()
            queued = download_single_file.apply_async(
                args=[task_id], kwargs={"risk_retry_attempt": 1}, countdown=retry_after,
            )
            task.celery_task_id = queued.id
            db.commit()
            redis_client.append_activity_log(
                "warning", "task", "下载任务因抖音风控延期",
                f"task_id={task_id}, code={e.code}, retry_after={retry_after}",
            )
            return {"success": False, "deferred": True, "retry_after": retry_after, "error": e.user_message}
        if task:
            task.status = "failed"
            task.error_message = f"{e.user_message} {e.action}"
            task.retry_count = (task.retry_count or 0) + 1
            db.commit()
        redis_client.append_activity_log(
            "error", "task", "抖音请求失败，已停止自动恢复",
            f"task_id={task_id}, code={e.code}, action={e.action}",
        )
        _notify_event(
            "douyin_risk",
            "抖音请求保护已触发",
            f"任务 #{task_id}：{e.user_message} {e.action}",
            level="warning",
            dedupe_key=f"risk:{e.code}:{task_id}",
        )
        return {"success": False, "error": e.user_message, "error_code": e.code}
    except Exception as e:
        # 记录详细的错误信息和堆栈跟踪
        error_trace = traceback.format_exc()
        logger.error(f"任务 {task_id} 发生异常:\n{error_trace}")
        redis_client.append_activity_log("error", "task",
            f"❌ 任务异常: task_id={task_id}",
            f"{type(e).__name__}: {str(e)[:200]}")

        # 更新任务状态为失败
        try:
            db.rollback()  # 先回滚可能存在的脏事务
            task = db.execute(
                select(DownloadTask).where(DownloadTask.id == task_id)
            ).scalar_one_or_none()
            if task:
                task.status = "failed"
                task.error_message = f"{type(e).__name__}: {str(e)[:200]}"
                task.retry_count = (task.retry_count or 0) + 1
                db.commit()
        except Exception as db_error:
            logger.error(f"更新任务 {task_id} 失败状态时出错: {db_error}")
        _notify_event(
            "download_failure",
            "下载任务异常",
            f"任务 #{task_id}：{type(e).__name__}: {str(e)[:500]}",
            level="error",
            dedupe_key=f"download-exception:{task_id}:{type(e).__name__}:{str(e)[:100]}",
        )
    finally:
        try:
            db.close()
        except Exception:
            pass


@celery_app.task(bind=True, name="app.tasks.download_tasks.download_author_works")
def download_author_works(self, author_id: int, start_index: int = 1,
                          download_new_only: bool = False, risk_retry_attempt: int = 0):
    """
    下载作者所有作品
    
    Args:
        author_id: 作者ID
        start_index: 起始作品序号
        download_new_only: 是否只下载新作品
    """
    db = get_sync_db()
    
    try:
        # 获取作者信息
        author = db.execute(
            select(Author).where(Author.id == author_id)
        ).scalar_one_or_none()
        
        if not author:
            logger.error(f"下载作者作品失败: 作者ID {author_id} 不存在")
            redis_client.append_activity_log("error", "task", f"作者不存在: author_id={author_id}")
            return {"success": False, "error": "作者不存在"}

        if _author_is_being_deleted(author_id, "download_author_works:start"):
            return {"success": False, "deleted": True, "error": "作者正在删除"}
        
        logger.info(f"开始获取作者 {author.nickname}(ID:{author_id}) 的作品列表")
        redis_client.append_activity_log("info", "task",
            f"⭐ download_author_works 启动: {author.nickname}",
            f"author_id={author_id}, sec_uid={author.sec_uid}, start_index={start_index}, download_new_only={download_new_only}")
        
        runtime_config = get_runtime_config_sync(db)

        # 获取 Cookie 并创建下载器
        request_context = get_request_context_sync(db)
        cookie = request_context.cookie
        redis_client.append_activity_log("debug", "task",
            f"Cookie 状态: {'\u5df2配置(' + str(len(cookie)) + '字符)' if cookie else '\u672a配置'}")
        source = build_douyin_source(
            cookie, settings.DOWNLOAD_DIR, runtime_config=runtime_config,
            request_context=request_context,
        )

        try:
            profile_result = sync_author_profile(author, source)
            record_author_profile_history(db, author, profile_result)
            if profile_result.get("changed"):
                redis_client.append_activity_log(
                    "info",
                    "task",
                    f"作者资料已同步: {author.nickname}",
                    f"author_id={author_id}",
                )

            if profile_result.get("account_status") in TERMINAL_AUTHOR_ACCOUNT_STATUSES:
                # 账号异常：仅标注(marker 已由 sync_author_profile 写入)，保留订阅状态与
                # 头像/昵称/作品等历史数据，是否退订由用户手动决定。
                author.last_check_time = datetime.now()
                db.commit()
                redis_client.append_activity_log(
                    "warning",
                    "task",
                    f"作者账号状态异常，已跳过拉取（仅标注、未退订）: {author.nickname or f'作者{author_id}'}",
                    f"author_id={author_id}, status={profile_result.get('account_status_label')}, detail={profile_result.get('account_status_detail') or ''}",
                )
                return {
                    "success": True,
                    "skipped": True,
                    "reason": profile_result.get("account_status"),
                    "status_label": profile_result.get("account_status_label"),
                }
        except Exception as profile_error:
            logger.warning(f"作者 {author_id} 资料同步失败，继续作品拉取: {profile_error}")
        
        # 获取作品列表（直接传入 sec_uid，避免冗余请求触发限流）
        redis_client.append_activity_log("info", "task",
            f"正在调用抖音 API 获取作品列表...",
            f"sec_uid={author.sec_uid}")
        scan_result = _collect_author_works(
            source,
            author,
            db,
            runtime_config,
            incremental=download_new_only,
        )
        work_list = scan_result["items"]
        scan_metrics = scan_result["metrics"]
        redis_client.append_activity_log("info", "task",
            f"获取到 {len(work_list)} 个作品: {author.nickname}",
            f"author_id={author_id}, mode={scan_metrics['mode']}, "
            f"pages={scan_metrics['pages_requested']}, stop={scan_metrics['stop_reason']}, "
            f"known_hits={scan_metrics['known_hits']}")

        if _author_is_being_deleted(author_id, "download_author_works:before_create"):
            db.rollback()
            return {"success": False, "deleted": True, "error": "作者正在删除"}
        
        if not work_list:
            logger.warning(f"作者 {author.nickname}(ID:{author_id}) 获取到的作品列表为空，可能是 Cookie 过期或被限流")
            redis_client.append_activity_log("warning", "task",
                f"作品列表为空: {author.nickname}",
                "可能原因: Cookie过期、被限流、或作者无作品")
        
        # 更新作者信息
        if work_list:
            author.last_check_time = datetime.now()
            if scan_metrics["mode"] == "full":
                author.last_full_reconcile_at = author.last_check_time
            latest_work = _select_latest_work(work_list)
            if latest_work:
                author.last_aweme_id = latest_work.get("aweme_id")
        
        # 清除之前的错误信息（终止态标记已在资料同步阶段处理）
        if not parse_author_account_status_marker(author.last_error):
            author.last_error = None
        
        created_tasks = []
        reused_tasks = []
        filtered_works = 0
        archive_rules = get_archive_rules_sync(db)
        archive_snapshot = serialize_archive_rules(archive_rules)
        
        # 处理每个作品
        for idx, item in enumerate(work_list[start_index - 1:], start=start_index):
            if _author_is_being_deleted(author_id, "download_author_works:building_tasks"):
                db.rollback()
                return {"success": False, "deleted": True, "error": "作者正在删除"}

            aweme_id = item["aweme_id"]
            
            # 检查作品是否已存在
            existing_work = db.execute(
                select(Work).where(Work.aweme_id == aweme_id)
            ).scalar_one_or_none()
            
            if existing_work:
                # 已被用户删除（排除）的作品：跳过，避免重新下载
                if getattr(existing_work, "is_excluded", False):
                    continue
                work = existing_work
                # 刷新 URL、元数据及统计快照；增量模式只跳过后续下载任务创建。
                apply_work_payload(db, work, item, preserve_existing=True)
                if download_new_only:
                    continue
            else:
                # 创建新作品记录
                work = Work(
                    aweme_id=aweme_id,
                    author_id=author_id,
                    title=item.get("desc", ""),
                    work_type="video",
                )
                apply_work_payload(db, work, item)
                
                db.add(work)
                db.flush()

            matches, reason = work_matches_archive_rules(work, archive_rules)
            if not matches:
                filtered_works += 1
                logger.info(f"作品 {work.aweme_id} 被归档规则跳过: {reason}")
                continue
            
            # 创建或重用下载任务
            if work.work_type == "video":
                task, action = ensure_download_task_sync(
                    db, work.id, 0, archive_rule_snapshot=archive_snapshot,
                )
                if action == "created":
                    created_tasks.append(task.id)
                elif action == "reused":
                    reused_tasks.append(task.id)
                if action != "existing":
                    work.is_downloaded = False
            else:
                # 图集：每张图片一个任务
                excluded_indices = set(work.excluded_file_indices)
                for img_idx in range(work.image_count):
                    # 跳过用户单独删除的图集文件，避免重新下载
                    if img_idx in excluded_indices:
                        continue
                    task, action = ensure_download_task_sync(
                        db, work.id, img_idx, archive_rule_snapshot=archive_snapshot,
                    )
                    if action == "created":
                        created_tasks.append(task.id)
                    elif action == "reused":
                        reused_tasks.append(task.id)
                    if action != "existing":
                        work.is_downloaded = False
            
            # 本地建任务轻微节流，避免一次性写入过猛；不使用反限流请求间隔。
            time.sleep(min(float(runtime_config.get("douyin_request_delay", settings.REQUEST_DELAY)), 1.0))

        if _author_is_being_deleted(author_id, "download_author_works:before_commit"):
            db.rollback()
            return {"success": False, "deleted": True, "error": "作者正在删除"}

        recalc_author_counts_sync(db, author)
        db.commit()
        
        # 触发所有下载任务（新建的 + 重用的）
        all_task_ids = created_tasks + reused_tasks
        redis_client.append_activity_log("info", "task",
            f"准备分发 {len(all_task_ids)} 个文件下载任务: {author.nickname}",
            f"task_ids={all_task_ids[:20]}{'...' if len(all_task_ids) > 20 else ''}")

        if _author_is_being_deleted(author_id, "download_author_works:before_dispatch"):
            return {"success": False, "deleted": True, "error": "作者正在删除"}

        for tid in all_task_ids:
            queued = download_single_file.delay(tid)
            db.execute(
                update(DownloadTask)
                .where(DownloadTask.id == tid, DownloadTask.status == "pending")
                .values(celery_task_id=queued.id)
            )
        db.commit()
        
        logger.info(f"作者 {author.nickname}(ID:{author_id}) 作品处理完成: "
                     f"总作品 {len(work_list)}, 新建任务 {len(created_tasks)}, "
                     f"重用任务 {len(reused_tasks)}, 规则过滤 {filtered_works}")
        redis_client.append_activity_log("info", "task",
            f"✅ 作品处理完成: {author.nickname}",
            f"总作品={len(work_list)}, 新建={len(created_tasks)}, 重用={len(reused_tasks)}, "
            f"规则过滤={filtered_works}, 下载分发={len(all_task_ids)}")
        
        return {
            "success": True,
            "total_works": len(work_list),
            "created_tasks": len(created_tasks),
            "reused_tasks": len(reused_tasks),
            "filtered_works": filtered_works,
            "task_ids": all_task_ids
        }

    except DouyinRequestError as e:
        db.rollback()
        runtime_config = get_runtime_config_sync(db)
        auto_retry = bool(runtime_config.get("douyin_risk_auto_retry", settings.DOUYIN_RISK_AUTO_RETRY))
        retry_after = int(e.retry_after or runtime_config.get(
            "douyin_risk_cooldown_seconds", settings.DOUYIN_RISK_COOLDOWN_SECONDS
        )) + random.randint(3, 25)
        author = db.execute(select(Author).where(Author.id == author_id)).scalar_one_or_none()
        if e.code in {"argus_blocked", "rate_limited"} and auto_retry and risk_retry_attempt < 1:
            if author:
                author.last_error = f"{e.user_message} 系统将在约 {retry_after} 秒后自动恢复一次。"
                db.commit()
            download_author_works.apply_async(
                args=[author_id, start_index, download_new_only],
                kwargs={"risk_retry_attempt": 1}, countdown=retry_after,
            )
            redis_client.append_activity_log(
                "warning", "task", "作者作品拉取因抖音风控延期",
                f"author_id={author_id}, code={e.code}, retry_after={retry_after}",
            )
            return {"success": False, "deferred": True, "retry_after": retry_after, "error": e.user_message}
        if author:
            author.last_error = f"{e.user_message} {e.action}"
            db.commit()
        redis_client.append_activity_log(
            "error", "task", "作者作品拉取已停止自动恢复",
            f"author_id={author_id}, code={e.code}, action={e.action}",
        )
        return {"success": False, "error": e.user_message, "error_code": e.code}
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)[:200]}"
        logger.error(f"下载作者 {author_id} 作品时出错: {error_msg}\n{traceback.format_exc()}")
        redis_client.append_activity_log("error", "task",
            f"下载作者作品失败: author_id={author_id}",
            error_msg)
        # 将错误记录到 Author，以便前端展示
        try:
            author = db.execute(
                select(Author).where(Author.id == author_id)
            ).scalar_one_or_none()
            if author:
                author.last_error = error_msg
                db.commit()
            else:
                db.rollback()
        except Exception:
            db.rollback()
        return {"success": False, "error": error_msg}
    finally:
        db.close()


@celery_app.task(bind=True, name="app.tasks.download_tasks.check_subscriptions")
def check_subscriptions(self, force: bool = False, risk_retry_attempt: int = 0,
                        author_ids: list[int] | None = None,
                        cycle_id: str | None = None,
                        cycle_total: int = 0,
                        cycle_checked: int = 0,
                        cycle_new_works: int = 0,
                        full_reconcile: bool = False):
    """
    检查所有订阅的作者是否有新作品
    
    定时任务，由 Celery Beat 调度执行
    """
    db = get_sync_db()
    report = None
    lock_token = str(getattr(getattr(self, "request", None), "id", None) or uuid4().hex)
    lock_acquired = False
    
    try:
        runtime_config = get_runtime_config_sync(db)
        if not force and not runtime_config.get("auto_check_enabled", True):
            redis_client.append_activity_log(
                "info",
                "task",
                "订阅自动检查已跳过",
                "设置页已关闭自动检查",
            )
            return {"success": True, "skipped": True, "reason": "auto_check_disabled"}

        global_interval = int(runtime_config.get("subscription_check_interval", settings.DEFAULT_CHECK_INTERVAL))
        author_delay = float(runtime_config.get("author_check_delay", settings.AUTHOR_CHECK_DELAY))

        # 风控或超时留下的周期由下一次自动调度或手动触发从断点继续，不新开一轮。
        if not author_ids and not full_reconcile:
            saved_cycle = _load_subscription_cycle_state(db)
            saved_remaining = saved_cycle.get("remaining_author_ids") or []
            if saved_cycle.get("active") and saved_remaining:
                author_ids = [int(author_id) for author_id in saved_remaining]
                cycle_id = str(saved_cycle.get("cycle_id") or cycle_id or uuid4().hex)
                cycle_total = int(saved_cycle.get("total_authors") or cycle_total or len(author_ids))
                cycle_checked = int(saved_cycle.get("checked_authors") or cycle_checked or 0)
                cycle_new_works = int(saved_cycle.get("new_works") or cycle_new_works or 0)
                full_reconcile = bool(saved_cycle.get("full_reconcile", full_reconcile))

        if not force and not author_ids:
            last_check_time = _get_last_subscription_check_time(db)
            if last_check_time:
                elapsed = (datetime.now() - last_check_time).total_seconds()
                if elapsed < global_interval:
                    return {
                        "success": True,
                        "skipped": True,
                        "reason": "global_interval",
                        "next_check_seconds": int(global_interval - elapsed),
                    }

        # 手动检查、自动检查和断点续检共用同一互斥锁。过去 force=True
        # 会完全绕过锁，多次点击可同时扫描全部作者并迅速触发风控。
        try:
            lock_acquired = bool(redis_client.redis_client.set(
                SUBSCRIPTION_CHECK_LOCK_KEY,
                lock_token,
                nx=True,
                ex=2100,
            ))
        except Exception:
            # Redis 故障时由 Celery worker 并发提供退化保护。
            lock_acquired = True
        if not lock_acquired:
            return {
                "success": True,
                "skipped": True,
                "reason": "global_lock",
            }

        if not force and not author_ids:
            # 在真正请求抖音前就标记本轮已开始，失败也进入全局冷却。
            _mark_subscription_check_started(db)

        report = SubscriptionCheckReport(
            celery_task_id=getattr(getattr(current_task, "request", None), "id", None),
            trigger_type="reconcile" if full_reconcile else ("manual" if force else "auto"),
            status="running",
        )
        db.add(report)
        db.commit()

        # 获取所有订阅的作者：按"最久未检查优先"排序（从未检查过的排最前）。
        # 这样即使本轮因超时/限流提前结束，靠后的作者也会在下一轮优先被检查，
        # 避免固定顺序导致列表末尾的作者长期被漏检。
        authors_query = select(Author).where(Author.is_subscribed == True)
        if author_ids:
            authors_query = authors_query.where(Author.id.in_(author_ids))
        authors = db.execute(
            authors_query
            .order_by(
                Author.last_check_time.is_(None).desc(),
                Author.last_check_time.asc(),
                Author.id.asc(),
            )
        ).scalars().all()
        cycle_id = cycle_id or uuid4().hex
        cycle_total = max(int(cycle_total or 0), len(authors))
        cycle_checked = max(0, int(cycle_checked or 0))
        cycle_new_works = max(0, int(cycle_new_works or 0))
        report.total_authors = len(authors)
        _save_subscription_cycle_state(db, {
            "active": True,
            "cycle_id": cycle_id,
            "total_authors": cycle_total,
            "checked_authors": cycle_checked,
            "remaining_authors": max(0, cycle_total - cycle_checked),
            "new_works": cycle_new_works,
            "full_reconcile": full_reconcile,
            "remaining_author_ids": [author.id for author in authors],
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })
        db.commit()
        
        results = []
        skipped_count = 0
        checked_count = 0
        consecutive_rate_limited = 0
        stopped_for_timeout = False
        stopped_for_rate_limit = False
        risk_author_id = None
        risk_error_code = None
        RATE_LIMIT_STOP_THRESHOLD = 3

        def _result_sets() -> tuple[set[int], set[int]]:
            settled_ids: set[int] = set()
            completed_ids: set[int] = set()
            for item in results:
                author_id = item.get("author_id")
                if not author_id:
                    continue
                status = item.get("status")
                risk_failure = item.get("error_code") in {
                    "account_isolated", "browser_identity_missing", "argus_blocked", "rate_limited",
                    "suspected_rate_limit",
                }
                if status != "deferred" and not risk_failure:
                    settled_ids.add(int(author_id))
                if status not in {"deferred", "not_due"} and not risk_failure:
                    completed_ids.add(int(author_id))
            return settled_ids, completed_ids

        def _update_running_progress() -> None:
            settled_ids, completed_ids = _result_sets()
            status_counts: dict[str, int] = {}
            for item in results:
                status = item.get("status", "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1
            round_checked = len(completed_ids)
            round_remaining_ids = [author.id for author in authors if author.id not in settled_ids]
            current_new_works = sum(int(item.get("new_works", 0) or 0) for item in results)
            requested_pages = sum(int(item.get("pages_requested", 0) or 0) for item in results)
            full_scans = sum(1 for item in results if item.get("scan_mode") == "full")
            cumulative_checked = min(cycle_total, cycle_checked + round_checked)
            report.checked_authors = round_checked
            report.success_authors = status_counts.get("success", 0) + status_counts.get("new_works", 0)
            report.new_works = current_new_works
            report.warning_authors = status_counts.get("warning", 0) + status_counts.get("account_warning", 0)
            report.failed_authors = status_counts.get("failed", 0)
            report.skipped_authors = status_counts.get("not_due", 0)
            report.remaining_authors = len(round_remaining_ids)
            report.summary = (
                f"正在检查：本轮已检查 {round_checked} 位，"
                f"累计已检查 {cumulative_checked}/{cycle_total} 位，"
                f"请求作品页 {requested_pages} 页，全量对账 {full_scans} 位"
            )
            report.details_json = json.dumps(results, ensure_ascii=False, default=str)
            _save_subscription_cycle_state(db, {
                "active": True,
                "cycle_id": cycle_id,
                "total_authors": cycle_total,
                "checked_authors": cumulative_checked,
                "remaining_authors": len(round_remaining_ids),
                "new_works": cycle_new_works + current_new_works,
                "full_reconcile": full_reconcile,
                "remaining_author_ids": round_remaining_ids,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
            db.commit()
        
        for author in authors:
            # 检查是否到达检查时间
            if not force and author.last_check_time:
                elapsed = (datetime.now() - author.last_check_time).total_seconds()
                effective_interval = max(int(author.check_interval or 0), global_interval, settings.MIN_CHECK_INTERVAL)
                if elapsed < effective_interval:
                    skipped_count += 1
                    results.append({
                        "author_id": author.id,
                        "nickname": author.nickname,
                        "status": "not_due",
                        "message": "未到作者检查间隔",
                        "next_check_seconds": int(effective_interval - elapsed),
                    })
                    _update_running_progress()
                    continue
            
            checked_count += 1
            try:
                # 获取 Cookie 并创建下载器
                request_context = get_request_context_sync(db)
                source = build_douyin_source(
                    request_context.cookie, settings.DOWNLOAD_DIR,
                    runtime_config=runtime_config,
                    request_context=request_context,
                )

                profile_result = sync_author_profile(author, source)
                record_author_profile_history(db, author, profile_result)

                if profile_result.get("account_status") in TERMINAL_AUTHOR_ACCOUNT_STATUSES:
                    # 成功拿到账号状态，说明与抖音通信正常，不是我方被限流
                    consecutive_rate_limited = 0
                    # 账号异常：sync_author_profile 已写入结构化标记（前端可据此筛选）。
                    # 仅标注、保留订阅状态与历史数据，是否退订由用户手动决定。
                    author.last_check_time = datetime.now()
                    author.last_auto_update_at = author.last_check_time
                    db.commit()
                    redis_client.append_activity_log(
                        "warning",
                        "task",
                        f"作者账号状态异常，已标注（未退订）: {author.nickname or author.id}",
                        f"状态={profile_result.get('account_status_label')}, "
                        f"detail={profile_result.get('account_status_detail') or ''}",
                    )
                    results.append({
                        "author_id": author.id,
                        "nickname": author.nickname,
                        "account_status": profile_result.get("account_status"),
                        "status_label": profile_result.get("account_status_label"),
                        "skipped": True,
                        "status": "account_warning",
                        "message": profile_result.get("account_status_label"),
                    })
                    if author_delay > 0:
                        time.sleep(author_delay)
                    continue
                
                # 日常使用有界增量扫描；到达网页设置的周期或人工指定时执行全量对账。
                run_full_reconcile = _full_reconcile_due(
                    author, runtime_config, forced=full_reconcile
                )
                scan_result = _collect_author_works(
                    source,
                    author,
                    db,
                    runtime_config,
                    incremental=not run_full_reconcile,
                )
                work_list = scan_result["items"]
                scan_audit = _scan_audit_fields(scan_result)
                redis_client.append_activity_log(
                    "info",
                    "task",
                    f"订阅作品扫描完成: {author.nickname or author.id}",
                    f"mode={scan_audit['scan_mode']}, pages={scan_audit['pages_requested']}, "
                    f"stop={scan_audit['stop_reason']}, known_hits={scan_audit['known_hits']}",
                )

                # 空列表无法区分“确实无作品”和风控伪成功。短暂退避后复查一次，
                # 两次都为空才记录警告，避免一次瞬时空响应造成整轮漏更。
                if not work_list:
                    time.sleep(max(3.0, min(author_delay, 10.0)))
                    scan_result = _collect_author_works(
                        source,
                        author,
                        db,
                        runtime_config,
                        incremental=not run_full_reconcile,
                    )
                    work_list = scan_result["items"]
                    scan_audit = _scan_audit_fields(scan_result)
                
                if not work_list:
                    author.last_check_time = datetime.now()
                    author.last_auto_update_at = author.last_check_time
                    author.last_error = "作品列表为空，可能是 Cookie 过期、被限流或作者暂无作品"
                    db.commit()
                    results.append({
                        "author_id": author.id,
                        "nickname": author.nickname,
                        "warning": author.last_error,
                        "status": "warning",
                        "message": author.last_error,
                        **scan_audit,
                    })
                    if author_delay > 0:
                        time.sleep(author_delay)
                    continue
                
                # 同一批响应先刷新已知作品的元数据与统计历史，再识别新作品。
                _refresh_scanned_works(db, author.id, work_list)

                # 检查是否有新作品：以数据库已入库作品为基准，避免置顶作品卡死增量游标
                new_works = _detect_new_works(db, author.id, work_list)

                if new_works:
                    queue_result = _queue_scanned_new_works(db, author, new_works)
                    results.append({
                        "author_id": author.id,
                        "nickname": author.nickname,
                        "new_works": queue_result["persisted_works"],
                        "file_tasks": queue_result["file_tasks"],
                        "task_ids": queue_result["celery_task_ids"][:20],
                        "status": "new_works",
                        "message": (
                            f"发现 {queue_result['persisted_works']} 个新作品，"
                            f"已提交 {queue_result['file_tasks']} 个文件任务"
                        ),
                        **scan_audit,
                    })
                elif profile_result.get("changed"):
                    results.append({
                        "author_id": author.id,
                        "nickname": author.nickname,
                        "avatar_synced": True,
                        "status": "success",
                        "message": "无新作品，作者资料已更新",
                        **scan_audit,
                    })
                else:
                    results.append({
                        "author_id": author.id,
                        "nickname": author.nickname,
                        "status": "success",
                        "message": "检查成功，无新作品",
                        **scan_audit,
                    })
                
                # 本作者检查成功，重置连续限流计数
                consecutive_rate_limited = 0

                # 更新检查时间
                author.last_check_time = datetime.now()
                author.last_auto_update_at = author.last_check_time
                if run_full_reconcile:
                    author.last_full_reconcile_at = author.last_check_time
                recalc_author_counts_sync(db, author)
                latest_work = _select_latest_work(work_list)
                if latest_work:
                    author.last_aweme_id = latest_work["aweme_id"]
                author.last_error = None
                db.commit()
                if new_works:
                    _notify_event(
                        "new_works",
                        f"{author.nickname or f'作者 {author.id}'} 发布了新作品",
                        f"发现 {queue_result['persisted_works']} 个新作品，下载任务已提交。",
                        level="info",
                        dedupe_key=f"new-works:{author.id}:" + ",".join(
                            sorted(str(item["aweme_id"]) for item in new_works)
                        ),
                    )
                
            except SoftTimeLimitExceeded:
                # 接近 Celery 软超时：优雅退出。已检查作者的进度都已逐个提交，
                # 未检查的作者会在下一轮（最久未检查优先）继续处理。
                stopped_for_timeout = True
                logger.warning("订阅检查接近执行超时，本轮提前结束")
                redis_client.append_activity_log(
                    "warning",
                    "task",
                    "订阅检查接近执行超时，本轮提前结束",
                    f"已检查={checked_count}，剩余作者将在下一轮优先继续",
                )
                break
            except Exception as e:
                error_msg = str(e)

                if isinstance(e, DouyinRequestError) and e.code in {
                    "account_isolated", "browser_identity_missing", "argus_blocked", "rate_limited",
                }:
                    author.last_error = f"{e.user_message} {e.action}"
                    db.commit()
                    stopped_for_rate_limit = True
                    risk_author_id = author.id
                    risk_error_code = e.code
                    results.append({
                        "author_id": author.id,
                        "nickname": author.nickname,
                        "status": "failed",
                        "error_code": e.code,
                        "message": e.user_message,
                    })
                    redis_client.append_activity_log(
                        "warning", "task", "抖音请求保护已触发，本轮订阅检查立即停止",
                        f"author_id={author.id}, 原因={e.user_message}, 后续操作={e.action}",
                    )
                    _notify_event(
                        "douyin_risk",
                        "订阅检查触发抖音请求保护",
                        f"作者 {author.nickname or author.id}：{e.user_message} {e.action}",
                        level="warning",
                        dedupe_key=f"subscription-risk:{e.code}:{author.id}",
                    )
                    break

                # 优先判断是否为"作者账号异常"（禁言/封号/注销/不可访问等）。
                # 这类是作者账号自身的问题，不是我方被限流：应打标记并自动取消订阅，
                # 保留历史数据，且不计入限流中断、不停止本轮其余作者的检查。
                anomaly_code, anomaly_label = _classify_author_account_status(error_msg)
                if anomaly_code in TERMINAL_AUTHOR_ACCOUNT_STATUSES:
                    # 仅标注（前端可据此筛选），保留订阅状态与历史数据，由用户手动决定是否退订
                    _mark_author_account_anomaly(author, anomaly_code, anomaly_label, error_msg)
                    author.last_check_time = datetime.now()
                    author.last_auto_update_at = author.last_check_time
                    db.commit()
                    consecutive_rate_limited = 0  # 账号异常不是限流，重置计数
                    logger.warning(
                        f"作者账号状态异常，已标注（未退订）: author_id={author.id}, "
                        f"nickname={author.nickname}, status={anomaly_label}"
                    )
                    redis_client.append_activity_log(
                        "warning",
                        "task",
                        f"作者账号状态异常，已标注（未退订）: {author.nickname or author.id}",
                        f"状态={anomaly_label}, detail={error_msg[:300]}",
                    )
                    results.append({
                        "author_id": author.id,
                        "nickname": author.nickname,
                        "account_status": anomaly_code,
                        "status_label": anomaly_label,
                        "status": "account_warning",
                        "message": anomaly_label,
                    })
                    if author_delay > 0:
                        time.sleep(author_delay)
                    continue

                probable_rate_limit = _is_probable_rate_limit_error(error_msg)
                incomplete_scan = isinstance(e, DouyinTraversalLimitError)
                if not probable_rate_limit and not incomplete_scan:
                    author.last_check_time = datetime.now()
                    author.last_auto_update_at = author.last_check_time
                author.last_error = error_msg[:1000]
                db.commit()
                logger.warning(
                    f"订阅检查失败: author_id={author.id}, nickname={author.nickname}, error={error_msg}"
                )
                redis_client.append_activity_log(
                    "warning",
                    "task",
                    f"订阅检查失败: {author.nickname or author.id}",
                    error_msg[:500],
                )
                results.append({
                    "author_id": author.id,
                    "nickname": author.nickname,
                    "error": error_msg,
                    "status": "failed",
                    "message": error_msg[:300],
                })
                if incomplete_scan:
                    results[-1]["error_code"] = "traversal_limit"
                    if e.metrics:
                        results[-1].update({
                            "scan_mode": e.metrics.get("mode"),
                            "pages_requested": int(e.metrics.get("pages_requested") or 0),
                            "stop_reason": e.metrics.get("stop_reason"),
                            "known_hits": int(e.metrics.get("known_hits") or 0),
                        })
                _notify_event(
                    "subscription_failure",
                    "作者订阅检查失败",
                    f"作者 {author.nickname or author.id}：{error_msg[:500]}",
                    level="warning",
                    dedupe_key=f"subscription:{author.id}:{type(e).__name__}:{error_msg[:100]}",
                )

                # 疑似限流：不再因单个作者的偶发失败中断整轮检查。
                # 只有连续多次疑似限流才提前停止本轮，避免持续请求扩大限制；
                # 疑似限流作者不推进 last_check_time，并保留在本周期续检集合中。
                if probable_rate_limit:
                    results[-1]["error_code"] = "suspected_rate_limit"
                    consecutive_rate_limited += 1
                    if consecutive_rate_limited >= RATE_LIMIT_STOP_THRESHOLD:
                        stopped_for_rate_limit = True
                        risk_author_id = author.id
                        risk_error_code = "suspected_rate_limit"
                        redis_client.append_activity_log(
                            "warning",
                            "task",
                            "连续多次疑似触发抖音限流，本轮订阅检查提前停止",
                            f"连续失败={consecutive_rate_limited}, 最近 author_id={author.id}, error={error_msg[:200]}",
                        )
                        break
                    redis_client.append_activity_log(
                        "info",
                        "task",
                        "疑似限流，跳过当前作者继续检查其余作者",
                        f"连续失败={consecutive_rate_limited}/{RATE_LIMIT_STOP_THRESHOLD}, author_id={author.id}",
                    )
                else:
                    # 非限流类错误不累计限流计数
                    consecutive_rate_limited = 0
            finally:
                # 每位作者完成后即持久化报告和跨轮累计进度，页面无需等整轮结束。
                _update_running_progress()

            # 请求间隔
            if author_delay > 0:
                time.sleep(author_delay)
        
        # 若因接近超时被迫提前结束，清除全局冷却，让下一次 Beat 立即继续。
        if stopped_for_timeout and not force:
            _reset_subscription_check_cooldown(db)

        settled_author_ids, completed_author_ids = _result_sets()
        resume_author_ids = [author.id for author in authors if author.id not in settled_author_ids]
        if risk_author_id is not None and risk_author_id not in resume_author_ids:
            resume_author_ids.insert(0, risk_author_id)
        if stopped_for_timeout or stopped_for_rate_limit:
            if risk_error_code in {"account_isolated", "browser_identity_missing"}:
                deferred_reason = "抖音账号请求上下文不可用，重新保存账号档案后优先续检"
            else:
                deferred_reason = "疑似限流，等待下一轮优先续检" if stopped_for_rate_limit else "本轮接近超时，等待下一轮优先续检"
            for author in authors:
                if author.id in resume_author_ids:
                    results.append({
                        "author_id": author.id,
                        "nickname": author.nickname,
                        "status": "deferred",
                        "message": deferred_reason,
                    })

        round_checked = len(completed_author_ids)
        remaining_count = len(resume_author_ids) if (stopped_for_timeout or stopped_for_rate_limit) else 0
        due_count = round_checked + remaining_count
        status_counts = {}
        for item in results:
            key = item.get("status", "unknown")
            status_counts[key] = status_counts.get(key, 0) + 1
        current_new_works = sum(int(item.get("new_works", 0) or 0) for item in results)
        requested_pages = sum(int(item.get("pages_requested", 0) or 0) for item in results)
        full_scans = sum(1 for item in results if item.get("scan_mode") == "full")
        cumulative_checked = min(cycle_total, cycle_checked + round_checked)
        cumulative_new_works = cycle_new_works + current_new_works
        report.due_authors = due_count
        report.checked_authors = round_checked
        report.success_authors = status_counts.get("success", 0) + status_counts.get("new_works", 0)
        report.new_works = current_new_works
        report.warning_authors = status_counts.get("warning", 0) + status_counts.get("account_warning", 0)
        report.failed_authors = status_counts.get("failed", 0)
        report.skipped_authors = skipped_count
        report.remaining_authors = remaining_count
        report.status = (
            "partial_authentication" if risk_error_code in {"account_isolated", "browser_identity_missing"}
            else ("partial_rate_limited" if stopped_for_rate_limit
                  else ("partial_timeout" if stopped_for_timeout else "completed"))
        )
        report.summary = (
            f"本轮已检查 {round_checked} 位，累计已检查 {cumulative_checked}/{cycle_total} 位，"
            f"等待续检 {remaining_count} 位，发现新作品 {report.new_works} 个，"
            f"请求作品页 {requested_pages} 页，全量对账 {full_scans} 位"
        )
        report.details_json = json.dumps(results, ensure_ascii=False, default=str)
        report.finished_at = datetime.now()
        if remaining_count > 0:
            _save_subscription_cycle_state(db, {
                "active": True,
                "cycle_id": cycle_id,
                "total_authors": cycle_total,
                "checked_authors": cumulative_checked,
                "remaining_authors": remaining_count,
                "new_works": cumulative_new_works,
                "full_reconcile": full_reconcile,
                "remaining_author_ids": resume_author_ids,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
        else:
            _clear_subscription_cycle_state(db, cycle_id)
        db.commit()

        # 纯执行超时属于容量分片，可短延迟承接；风控中断则严格等待下一个
        # 设置中心定义的自动更新周期，避免过早重试加重账号风险。
        continuation_task_id = None
        if not force and stopped_for_timeout and remaining_count > 0:
            countdown = 30
            try:
                continuation = check_subscriptions.apply_async(kwargs={
                    "force": False,
                    "author_ids": resume_author_ids,
                    "cycle_id": cycle_id,
                    "cycle_total": cycle_total,
                    "cycle_checked": cumulative_checked,
                    "cycle_new_works": cumulative_new_works,
                    "full_reconcile": full_reconcile,
                }, countdown=countdown)
                continuation_task_id = continuation.id
                redis_client.append_activity_log(
                    "info", "task", "订阅检查已安排分批续检",
                    f"{countdown} 秒后继续，剩余={remaining_count}, celery_task_id={continuation_task_id}",
                )
            except Exception as continuation_error:
                logger.warning(f"订阅检查续检任务提交失败，将等待下一次 Beat: {continuation_error}")
        elif stopped_for_rate_limit and remaining_count > 0 and risk_error_code in {
            "argus_blocked", "rate_limited", "suspected_rate_limit",
        }:
            auto_retry = bool(runtime_config.get("douyin_risk_auto_retry", settings.DOUYIN_RISK_AUTO_RETRY))
            if auto_retry and risk_retry_attempt < 1:
                state = redis_client.get_douyin_risk_state()
                countdown = int(state.get("retry_after") or runtime_config.get(
                    "douyin_risk_cooldown_seconds", settings.DOUYIN_RISK_COOLDOWN_SECONDS
                )) + random.randint(3, 25)
                try:
                    continuation = check_subscriptions.apply_async(
                        kwargs={
                            "force": force,
                            "risk_retry_attempt": 1,
                            "author_ids": resume_author_ids,
                            "cycle_id": cycle_id,
                            "cycle_total": cycle_total,
                            "cycle_checked": cumulative_checked,
                            "cycle_new_works": cumulative_new_works,
                            "full_reconcile": full_reconcile,
                        },
                        countdown=countdown,
                    )
                    continuation_task_id = continuation.id
                    redis_client.append_activity_log(
                        "warning", "task", "订阅检查已安排一次风控恢复",
                        f"{countdown} 秒后继续，剩余={remaining_count}, celery_task_id={continuation_task_id}",
                    )
                except Exception as continuation_error:
                    logger.warning(f"订阅检查风控恢复任务提交失败，将等待下一次 Beat: {continuation_error}")
        
        return {
            "success": True,
            "checked": round_checked,
            "skipped": skipped_count,
            "stopped_for_timeout": stopped_for_timeout,
            "stopped_for_rate_limit": stopped_for_rate_limit,
            "report_id": report.id,
            "continuation_task_id": continuation_task_id,
            "results": results,
        }
    
    except Exception as e:
        db.rollback()
        error_summary = f"{type(e).__name__}: {str(e)[:500]}"
        logger.error(f"订阅检查任务异常终止: {error_summary}\n{traceback.format_exc()}")
        try:
            redis_client.append_activity_log("error", "task", "订阅检查任务异常终止", error_summary)
        except Exception:
            pass
        _notify_event(
            "subscription_failure",
            "订阅检查任务异常终止",
            error_summary,
            level="error",
            dedupe_key=f"subscription-fatal:{type(e).__name__}:{str(e)[:100]}",
        )
        if report:
            try:
                report.status = "failed"
                report.summary = f"订阅检查任务异常终止: {error_summary}"
                report.finished_at = datetime.now()
                db.commit()
            except Exception:
                db.rollback()
        return {"success": False, "error": str(e)}
    finally:
        if lock_acquired:
            _release_subscription_lock(lock_token)
        db.close()


@celery_app.task(name="app.tasks.download_tasks.detect_stuck_tasks")
def detect_stuck_tasks():
    """
    检测并恢复三类可恢复任务：
    1. downloading 长时间无进度；
    2. pending 长时间未被 Worker 认领；
    3. 因 fork 连接池污染产生的数据库瞬时失败。
    由 Celery Beat 每 5 分钟调度一次。
    """
    db = get_sync_db()
    try:
        downloading_tasks = db.execute(
            select(DownloadTask).where(DownloadTask.status == "downloading")
        ).scalars().all()

        runtime_config = get_runtime_config_sync(db)
        now = time.time()
        now_dt = datetime.now()
        timeout = int(runtime_config.get("stuck_task_timeout", settings.STUCK_TASK_TIMEOUT))
        retry_limit = int(runtime_config.get("download_retry_count", settings.DOWNLOAD_RETRY_COUNT))
        retry_delay = int(runtime_config.get("download_retry_delay", settings.DOWNLOAD_RETRY_DELAY))
        stuck_count = 0
        redispatch_ids = []

        for task in downloading_tasks:
            progress = redis_client.get_progress(task.id)
            last_updated = progress.get("last_updated", 0) if progress else 0

            if last_updated == 0 and task.started_at:
                last_updated = task.started_at.timestamp()

            if now - last_updated > timeout:
                elapsed_min = int((now - last_updated) / 60)
                task.retry_count = (task.retry_count or 0) + 1
                redis_client.delete_progress(task.id)
                redis_client.resume_task(task.id)  # 清除暂停标记
                stuck_count += 1
                
                # 如果未超过最大重试次数，自动重试
                if task.retry_count <= retry_limit:
                    task.status = "pending"
                    task.error_message = None
                    logger.warning(
                        f"任务 {task.id} 卡住 {elapsed_min} 分钟，自动重试 ({task.retry_count}/{retry_limit})"
                    )
                    db.commit()
                    redispatch_ids.append(task.id)
                else:
                    task.status = "failed"
                    task.error_message = f"下载超时：任务卡住超过 {elapsed_min} 分钟无进度变化，已达最大重试次数"
                    logger.warning(
                        f"任务 {task.id} 卡住 {elapsed_min} 分钟，已达最大重试次数，标记为失败"
                    )

        # pending 任务不产生进度，原逻辑永远检测不到。超过同一超时阈值
        # 仍未被认领就重新投递；原子认领可保证旧消息随后到达时不会重复下载。
        pending_cutoff = now_dt - timedelta(seconds=timeout)
        orphaned_pending = db.execute(
            select(DownloadTask).where(
                DownloadTask.status == "pending",
                DownloadTask.updated_at < pending_cutoff,
            )
        ).scalars().all()
        for task in orphaned_pending:
            task.updated_at = now_dt
            task.error_message = None
            redispatch_ids.append(task.id)

        # 连接池跨 fork 导致的失败属于基础设施瞬时故障，应在连接隔离后
        # 自动恢复；业务错误、风控和无效 URL 不在此处盲目重试。
        recoverable_failed = db.execute(
            select(DownloadTask).where(
                DownloadTask.status == "failed",
                DownloadTask.retry_count <= retry_limit,
                DownloadTask.error_message.isnot(None),
                (
                    DownloadTask.error_message.startswith("DatabaseError:")
                    | DownloadTask.error_message.startswith("ResourceClosedError:")
                ),
            )
        ).scalars().all()
        for task in recoverable_failed:
            task.status = "pending"
            task.error_message = None
            task.updated_at = now_dt
            redispatch_ids.append(task.id)

        db.commit()

        queued_ids = []
        for task_id in dict.fromkeys(redispatch_ids):
            if retry_delay > 0:
                queued = download_single_file.apply_async(args=[task_id], countdown=retry_delay)
            else:
                queued = download_single_file.delay(task_id)
            db.execute(
                update(DownloadTask)
                .where(DownloadTask.id == task_id, DownloadTask.status == "pending")
                .values(celery_task_id=queued.id, updated_at=datetime.now())
            )
            queued_ids.append(task_id)
        db.commit()

        if orphaned_pending or recoverable_failed:
            redis_client.append_activity_log(
                "warning", "task", "自动恢复遗留下载任务",
                f"待处理遗留={len(orphaned_pending)}, 数据库瞬时失败={len(recoverable_failed)}, 已重新分发={len(queued_ids)}",
            )
        logger.info(
            f"卡住任务检测完成: 下载中={len(downloading_tasks)}, 卡住={stuck_count}, "
            f"待处理遗留={len(orphaned_pending)}, 数据库瞬时失败={len(recoverable_failed)}"
        )
        return {
            "checked": len(downloading_tasks) + len(orphaned_pending) + len(recoverable_failed),
            "stuck": stuck_count,
            "orphaned_pending": len(orphaned_pending),
            "recoverable_failed": len(recoverable_failed),
            "redispatched": len(queued_ids),
        }
    except Exception as e:
        logger.error(f"检测卡住任务时出错: {e}")
        return {"error": str(e)}
    finally:
        db.close()


@celery_app.task(name="app.tasks.download_tasks.resume_task")
def resume_task(task_id: int):
    """
    恢复暂停的任务
    
    Args:
        task_id: 任务ID
    """
    # 从暂停集合中移除
    redis_client.resume_task(task_id)
    
    # 重新启动下载任务
    return download_single_file.delay(task_id)

