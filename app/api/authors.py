"""
作者管理 API 路由

为什么这样设计：
1. 支持作者添加、订阅管理
2. 支持查看作者的作品列表
3. 支持手动触发订阅检查
"""

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import case, select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from typing import List, Literal, Optional
from datetime import datetime, timedelta

from app.models.database import get_async_db
from app.models.models import (
    Author, Work, DownloadTask, SubscriptionCheckReport, AuthorProfileHistory, SystemConfig,
)
from app.models.schemas import (
    AuthorCreate, AuthorUpdate, AuthorResponse, WorkResponse, WorkFileItem, MessageResponse,
    PaginatedAuthorsResponse
)
from app.services.downloader import DouyinDownloader, author_profile_has_identity, prefer_avatar_url
from app.services.avatar_cache import ensure_author_avatar_cached, find_cached_author_avatar
from app.tasks.download_tasks import (
    AUTHOR_ACCOUNT_STATUS_MARKER_PREFIX,
    SUBSCRIPTION_CYCLE_STATE_KEY,
    TERMINAL_AUTHOR_ACCOUNT_STATUSES,
    build_author_account_status_marker,
    check_subscriptions,
    download_author_works,
    sync_author_profile,
)

# 账号状态筛选支持的取值：abnormal=仅异常, normal=仅正常, 或具体状态码
_ACCOUNT_STATUS_MARKER_LIKE_PREFIX = f"{AUTHOR_ACCOUNT_STATUS_MARKER_PREFIX}|"
_ACCOUNT_STATUS_FILTER_CODES = {"deleted", "banned", "restricted", "unavailable"}


def _apply_account_status_filter(query, account_status: Optional[str]):
    """按账号状态筛选作者。账号状态以结构化标记存储在 last_error 中。"""
    if not account_status or account_status == "all":
        return query

    marker_cond = Author.last_error.startswith(
        _ACCOUNT_STATUS_MARKER_LIKE_PREFIX, autoescape=True
    )

    if account_status == "abnormal":
        return query.where(marker_cond)
    if account_status == "normal":
        return query.where(or_(Author.last_error.is_(None), ~marker_cond))
    if account_status in _ACCOUNT_STATUS_FILTER_CODES:
        return query.where(
            Author.last_error.startswith(
                f"{_ACCOUNT_STATUS_MARKER_LIKE_PREFIX}{account_status}", autoescape=True
            )
        )
    return query
from app.core import redis_client
from app.core.config import settings
from app.core.runtime_config import get_runtime_config
from app.services.work_manager import delete_author_hard
from app.services.douyin_errors import (
    DouyinCooldownError,
    DouyinRequestError,
    http_status_for_douyin_error,
)

router = APIRouter(prefix="/authors", tags=["作者管理"])


async def _raise_if_douyin_cooling() -> None:
    state = await asyncio.to_thread(redis_client.get_douyin_risk_state)
    if state.get("active"):
        exc = DouyinCooldownError(
            retry_after=int(state.get("retry_after") or 1),
            reason=state.get("error_type") or "argus_blocked",
        )
        raise HTTPException(status_code=429, detail=exc.as_dict())


def _normalize_author_profile_url(author: Author) -> bool:
    stable_url = DouyinDownloader.build_author_profile_url(author.sec_uid)
    if not stable_url or author.share_url == stable_url:
        return False

    author.share_url = stable_url
    return True


async def _normalize_author_profile_links(db: AsyncSession) -> int:
    result = await db.execute(select(Author))
    authors = result.scalars().all()

    updated = 0
    for author in authors:
        if _normalize_author_profile_url(author):
            updated += 1

    if updated:
        await db.commit()

    return updated


def _apply_author_profile_status(author: Author, author_info: dict) -> None:
    account_status = author_info.get("account_status", "active")
    if account_status in TERMINAL_AUTHOR_ACCOUNT_STATUSES:
        author.last_error = build_author_account_status_marker(
            account_status,
            author_info.get("account_status_label", "状态异常"),
            author_info.get("account_status_detail"),
        )
    elif account_status == "active":
        author.last_error = None


def _get_work_download_status(work: Work) -> str:
    """聚合作品关联任务状态，供作者预览使用。"""
    tasks = work.download_tasks or []
    if not tasks:
        return "completed" if work.is_downloaded else "not_started"

    statuses = {task.status for task in tasks}
    completed_task_count = sum(1 for task in tasks if task.status == "completed")

    if completed_task_count == len(tasks):
        return "completed"
    if "downloading" in statuses:
        return "downloading"
    if "paused" in statuses:
        return "paused"
    if completed_task_count > 0:
        return "partial"
    if "pending" in statuses:
        return "pending"
    if "failed" in statuses:
        return "failed"
    if "cancelled" in statuses:
        return "cancelled"
    return "not_started"


def _build_work_preview_payload(work: Work) -> tuple[Optional[str], List[str], Optional[str]]:
    """为作者作品预览优先选择本地已下载媒体地址。"""
    tasks = work.download_tasks or []
    completed_tasks = [
        task for task in tasks if task.status == "completed" and task.file_path
    ]

    if work.work_type == "video":
        completed_video_task = next(iter(completed_tasks), None)
        video_url = f"/api/tasks/{completed_video_task.id}/preview" if completed_video_task else None
        return video_url, [], video_url

    original_image_urls = work.image_urls or []
    local_image_urls = [
        (task.file_index, f"/api/tasks/{task.id}/preview")
        for task in completed_tasks
    ]
    local_image_urls.sort(key=lambda item: item[0])

    image_urls = original_image_urls
    if not any(work.live_photo_urls or []) and local_image_urls and (
        work.is_downloaded or len(local_image_urls) == len(original_image_urls) or not original_image_urls
    ):
        image_urls = [url for _, url in local_image_urls]

    primary_preview_url = image_urls[0] if image_urls else None
    return None, image_urls, primary_preview_url


def _build_work_files(work: Work) -> List[WorkFileItem]:
    """构造作品内文件（任务）列表，供图集单文件管理使用。"""
    files: List[WorkFileItem] = []
    live_photo_urls = work.live_photo_urls or []
    for task in sorted(work.download_tasks or [], key=lambda t: (t.file_index or 0)):
        local_available = task.status == "completed" and bool(task.file_path)
        file_index = task.file_index or 0
        files.append(WorkFileItem(
            task_id=task.id,
            file_index=file_index,
            status=task.status,
            file_name=task.file_name,
            preview_url=f"/api/tasks/{task.id}/preview" if local_available else None,
            media_type=(
                "video"
                if file_index < len(live_photo_urls) and live_photo_urls[file_index]
                else "image"
            ),
            local_available=local_available,
        ))
    return files


def _serialize_work_response(work: Work) -> WorkResponse:
    """构造作者作品预览响应。"""
    total_task_count = len(work.download_tasks or [])
    completed_task_count = sum(
        1 for task in (work.download_tasks or []) if task.status == "completed"
    )
    video_url, image_urls, primary_preview_url = _build_work_preview_payload(work)

    return WorkResponse(
        id=work.id,
        aweme_id=work.aweme_id,
        author_id=work.author_id,
        title=work.title,
        work_type=work.work_type,
        image_count=work.image_count,
        is_downloaded=work.is_downloaded,
        discovered_at=work.discovered_at,
        published_at=work.published_at,
        video_url=video_url,
        image_urls=image_urls,
        primary_preview_url=primary_preview_url,
        download_status=_get_work_download_status(work),
        completed_task_count=completed_task_count,
        total_task_count=total_task_count,
        is_excluded=bool(getattr(work, "is_excluded", False)),
        excluded_file_indices=work.excluded_file_indices,
        files=_build_work_files(work),
    )


@router.post("/", response_model=AuthorResponse)
async def add_author(
    request: AuthorCreate,
    db: AsyncSession = Depends(get_async_db)
):
    """
    添加作者
    
    - 输入作者分享链接
    - 自动获取作者信息（昵称、头像等）
    - 可选择是否订阅
    """
    try:
        await _raise_if_douyin_cooling()
        # 获取 Cookie
        current_settings = await asyncio.to_thread(settings.snapshot)
        cookie = await asyncio.to_thread(redis_client.get_cookie) or current_settings.DOUYIN_COOKIE
        if not cookie:
            raise HTTPException(status_code=400, detail="请先配置抖音 Cookie")
        
        runtime_config = await get_runtime_config(db)
        downloader = DouyinDownloader(cookie, current_settings.DOWNLOAD_DIR, runtime_config=runtime_config)
        
        # 抖音请求是同步 I/O，放到线程池避免阻塞 FastAPI 事件循环导致网关超时。
        sec_uid = await asyncio.to_thread(downloader.get_sec_uid, request.share_url)
        result = await db.execute(
            select(Author).where(Author.sec_uid == sec_uid)
        )
        existing = result.scalar_one_or_none()

        author_info = await asyncio.to_thread(downloader.get_author_info, sec_uid)
        stable_share_url = author_info.get("profile_url") or DouyinDownloader.build_author_profile_url(sec_uid) or request.share_url
        
        if existing:
            if stable_share_url and existing.share_url != stable_share_url:
                existing.share_url = stable_share_url
            latest_nickname = author_info.get("nickname")
            if latest_nickname:
                existing.nickname = latest_nickname
            latest_avatar_url = author_info.get("avatar_url")
            if latest_avatar_url:
                existing.avatar_url = prefer_avatar_url(existing.avatar_url, latest_avatar_url)
            _apply_author_profile_status(existing, author_info)
            await db.commit()
            await db.refresh(existing)
            # 计算该作者在列表中的位置(0-based)，供前端跳转
            pos_result = await db.execute(
                select(func.count(Author.id)).where(Author.created_at > existing.created_at)
            )
            position = pos_result.scalar() or 0
            # 返回已存在的作者信息，而不是报错
            return {
                "id": existing.id,
                "sec_uid": existing.sec_uid,
                "nickname": existing.nickname,
                "avatar_url": existing.avatar_url,
                "share_url": existing.share_url,
                "is_subscribed": existing.is_subscribed,
                "total_works": existing.total_works,
                "downloaded_works": existing.downloaded_works,
                "check_interval": existing.check_interval,
                "last_check_time": existing.last_check_time,
                "last_error": existing.last_error,
                "created_at": existing.created_at,
                "already_exists": True,
                "position": position
            }

        if not author_profile_has_identity(author_info):
            detail = author_info.get("account_status_detail") or author_info.get("account_status_label") or "抖音未返回可用的作者资料"
            raise HTTPException(status_code=503, detail=f"无法获取作者资料，未创建作者记录：{detail}")
        
        # 创建作者
        author = Author(
            sec_uid=sec_uid,
            nickname=author_info.get("nickname"),
            share_url=stable_share_url,
            avatar_url=author_info.get("avatar_url"),
            is_subscribed=request.is_subscribed,
            check_interval=request.check_interval
        )
        _apply_author_profile_status(author, author_info)
        db.add(author)
        await db.commit()
        await db.refresh(author)
        
        return author
    
    except DouyinRequestError as e:
        raise HTTPException(status_code=http_status_for_douyin_error(e), detail=e.as_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"添加作者失败: {str(e)}")


@router.get("/search")
async def search_authors(
    q: str = Query(..., min_length=1, description="搜索关键词（昵称或抖音ID）"),
    db: AsyncSession = Depends(get_async_db)
):
    """
    搜索作者并返回位置信息
    
    - 搜索范围：昵称(nickname)、抖音ID(sec_uid)
    - 返回匹配的作者列表以及在完整列表中的位置索引
    """
    # 先对完整作者列表计算稳定位置，再在外层搜索。
    # 这样只需一条 SQL，不再对每个命中作者单独 COUNT。
    ranked_authors = select(
        Author.id.label("id"),
        Author.nickname.label("nickname"),
        Author.sec_uid.label("sec_uid"),
        Author.avatar_url.label("avatar_url"),
        Author.is_subscribed.label("is_subscribed"),
        (
            func.row_number().over(
                order_by=(Author.created_at.desc(), Author.id.desc())
            ) - 1
        ).label("position"),
    ).subquery()
    search_query = select(ranked_authors).where(
        (ranked_authors.c.nickname.ilike(f"%{q}%"))
        | (ranked_authors.c.sec_uid.ilike(f"%{q}%"))
    ).order_by(ranked_authors.c.position)

    matched_authors = (await db.execute(search_query)).mappings().all()

    if not matched_authors:
        return {"items": [], "message": "未找到匹配的作者"}

    return {"items": [dict(item) for item in matched_authors], "total": len(matched_authors)}


@router.get("/", response_model=PaginatedAuthorsResponse)
async def list_authors(
    is_subscribed: Optional[bool] = Query(None, description="按订阅状态筛选"),
    account_status: Optional[str] = Query(
        None,
        description="按账号状态筛选：all/abnormal/normal 或具体状态码(deleted/banned/restricted/unavailable)",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_async_db)
):
    """获取作者列表（带分页），支持订阅状态与账号状态组合筛选"""
    # 构建基础查询条件
    base_query = select(Author)
    count_query = select(func.count(Author.id))
    
    if is_subscribed is not None:
        base_query = base_query.where(Author.is_subscribed == is_subscribed)
        count_query = count_query.where(Author.is_subscribed == is_subscribed)

    # 账号状态筛选（异常/正常/具体状态码），与订阅筛选组合生效
    base_query = _apply_account_status_filter(base_query, account_status)
    count_query = _apply_account_status_filter(count_query, account_status)
    
    # 当页数据与总数使用同一条窗口查询返回。
    query = base_query.add_columns(
        func.count(Author.id).over().label("_total")
    ).order_by(Author.created_at.desc(), Author.id.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    rows = result.all()
    authors = [row[0] for row in rows]
    if rows:
        total = int(rows[0][1] or 0)
    elif page > 1:
        total = int((await db.execute(count_query)).scalar() or 0)
    else:
        total = 0

    latest_report_result = await db.execute(
        select(SubscriptionCheckReport)
        .where(SubscriptionCheckReport.status != "running")
        .order_by(SubscriptionCheckReport.started_at.desc(), SubscriptionCheckReport.id.desc())
        .limit(1)
    )
    latest_report = latest_report_result.scalar_one_or_none()
    latest_details = []
    if latest_report:
        try:
            latest_details = json.loads(latest_report.details_json or "[]")
        except (TypeError, ValueError):
            latest_details = []
    detail_map = {item.get("author_id"): item for item in latest_details}
    breakpoint_id = next((item.get("author_id") for item in latest_details if item.get("status") == "deferred"), None)
    history_counts = {}
    if authors:
        history_result = await db.execute(
            select(AuthorProfileHistory.author_id, func.count(AuthorProfileHistory.id))
            .where(AuthorProfileHistory.author_id.in_([author.id for author in authors]))
            .group_by(AuthorProfileHistory.author_id)
        )
        history_counts = dict(history_result.all())
    runtime_config = await get_runtime_config(db)
    current_settings = await asyncio.to_thread(settings.snapshot)
    global_interval = int(runtime_config.get("subscription_check_interval", current_settings.DEFAULT_CHECK_INTERVAL))
    status_messages = {
        "new_works": "发现新作", "success": "暂未更新", "account_warning": "账号异常",
        "warning": "检查警告", "failed": "检查失败", "deferred": "限流后未执行",
        "not_due": "等待轮询",
    }
    for author in authors:
        detail = detail_map.get(author.id, {})
        status = detail.get("status")
        author.auto_update_status = status
        author.auto_update_message = status_messages.get(status, detail.get("message"))
        next_at = None
        if author.is_subscribed and author.last_auto_update_at:
            next_at = author.last_auto_update_at + timedelta(seconds=max(author.check_interval or 0, global_interval, current_settings.MIN_CHECK_INTERVAL))
        if author.is_subscribed and status == "deferred" and latest_report and latest_report.started_at:
            cycle_at = latest_report.started_at + timedelta(seconds=global_interval)
            next_at = max(next_at, cycle_at) if next_at else cycle_at
        author.expected_next_auto_update_at = next_at
        author.is_last_breakpoint = author.id == breakpoint_id
        author.profile_history_count = int(history_counts.get(author.id, 0))
    
    # 计算总页数
    pages = (total + page_size - 1) // page_size if total > 0 else 1
    
    return PaginatedAuthorsResponse(
        items=authors,
        total=total,
        page=page,
        page_size=page_size,
        pages=pages
    )


@router.post("/normalize-links", response_model=MessageResponse)
async def normalize_author_links(db: AsyncSession = Depends(get_async_db)):
    """批量修复历史作者记录中的主页链接。"""
    updated = await _normalize_author_profile_links(db)

    return MessageResponse(
        success=True,
        message=f"已修复 {updated} 位作者主页链接",
        data={"updated": updated},
    )


@router.get("/{author_id}", response_model=AuthorResponse)
async def get_author(author_id: int, db: AsyncSession = Depends(get_async_db)):
    """获取作者详情"""
    result = await db.execute(
        select(Author).where(Author.id == author_id)
    )
    author = result.scalar_one_or_none()
    
    if not author:
        raise HTTPException(status_code=404, detail="作者不存在")
    
    return author


@router.patch("/{author_id}", response_model=AuthorResponse)
async def update_author(
    author_id: int,
    request: AuthorUpdate,
    db: AsyncSession = Depends(get_async_db)
):
    """更新作者信息（订阅状态、检查间隔）"""
    result = await db.execute(
        select(Author).where(Author.id == author_id)
    )
    author = result.scalar_one_or_none()
    
    if not author:
        raise HTTPException(status_code=404, detail="作者不存在")
    
    if request.is_subscribed is not None:
        author.is_subscribed = request.is_subscribed
    
    if request.check_interval is not None:
        current_settings = await asyncio.to_thread(settings.snapshot)
        if request.check_interval < current_settings.MIN_CHECK_INTERVAL:
            raise HTTPException(
                status_code=400,
                detail=f"检查间隔不能小于 {current_settings.MIN_CHECK_INTERVAL} 秒"
            )
        author.check_interval = request.check_interval
    
    await db.commit()
    await db.refresh(author)
    
    return author


@router.post("/{author_id}/sync-avatar", response_model=AuthorResponse)
async def sync_author_avatar(author_id: int, db: AsyncSession = Depends(get_async_db)):
    """手动同步作者昵称和头像。"""
    result = await db.execute(
        select(Author).where(Author.id == author_id)
    )
    author = result.scalar_one_or_none()

    if not author:
        raise HTTPException(status_code=404, detail="作者不存在")

    current_settings = await asyncio.to_thread(settings.snapshot)
    cookie = await asyncio.to_thread(redis_client.get_cookie) or current_settings.DOUYIN_COOKIE
    if not cookie:
        raise HTTPException(status_code=400, detail="请先配置抖音 Cookie")

    try:
        import asyncio

        runtime_config = await get_runtime_config(db)

        def _sync():
            downloader = DouyinDownloader(cookie, current_settings.DOWNLOAD_DIR, runtime_config=runtime_config)
            return sync_author_profile(author, downloader)

        profile_result = await asyncio.to_thread(_sync)
        for field_name, old_value, new_value in (
            ("nickname", profile_result.get("old_nickname"), author.nickname),
            ("avatar", profile_result.get("old_avatar_url"), author.avatar_url),
        ):
            if old_value and new_value and old_value != new_value:
                db.add(AuthorProfileHistory(
                    author_id=author.id,
                    field_name=field_name,
                    old_value=old_value,
                    new_value=new_value,
                    source="manual_sync",
                ))
        await db.commit()
        await db.refresh(author)
        return author
    except Exception as e:
        author.last_error = str(e)[:1000]
        await db.commit()
        raise HTTPException(status_code=500, detail=f"同步作者头像失败: {str(e)}")


@router.get("/{author_id}/avatar")
async def get_author_avatar(author_id: int, db: AsyncSession = Depends(get_async_db)):
    """返回本地头像；仅在缓存缺失时访问一次头像源地址。"""
    result = await db.execute(select(Author).where(Author.id == author_id))
    author = result.scalar_one_or_none()
    if not author:
        raise HTTPException(status_code=404, detail="作者不存在")

    current_settings = await asyncio.to_thread(settings.snapshot)
    cached = await asyncio.to_thread(
        find_cached_author_avatar,
        author.id,
        current_settings.DOWNLOAD_DIR,
        author.avatar_url,
    )
    if cached:
        return FileResponse(cached, headers={"Cache-Control": "no-cache"})
    if not author.avatar_url:
        raise HTTPException(status_code=404, detail="作者暂无头像")

    cookie = await asyncio.to_thread(redis_client.get_cookie) or current_settings.DOUYIN_COOKIE
    runtime_config = await get_runtime_config(db)

    def _cache_avatar():
        downloader = DouyinDownloader(cookie or "", current_settings.DOWNLOAD_DIR, runtime_config=runtime_config)
        return ensure_author_avatar_cached(
            author.id,
            author.avatar_url,
            downloader.filepath,
            downloader.session,
            timeout=downloader.download_timeout,
        )

    try:
        cached = await asyncio.to_thread(_cache_avatar)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"缓存作者头像失败: {str(exc)}")
    if not cached:
        raise HTTPException(status_code=404, detail="作者暂无头像")
    return FileResponse(cached, headers={"Cache-Control": "no-cache"})


@router.get("/{author_id}/profile-history")
async def get_author_profile_history(author_id: int, db: AsyncSession = Depends(get_async_db)):
    author_result = await db.execute(select(Author).where(Author.id == author_id))
    author = author_result.scalar_one_or_none()
    if not author:
        raise HTTPException(status_code=404, detail="作者不存在")
    history_result = await db.execute(
        select(AuthorProfileHistory)
        .where(AuthorProfileHistory.author_id == author_id)
        .order_by(AuthorProfileHistory.observed_at.desc(), AuthorProfileHistory.id.desc())
        .limit(200)
    )
    return {
        "author": {"id": author.id, "nickname": author.nickname, "avatar_url": author.avatar_url},
        "items": [{
            "id": item.id, "field_name": item.field_name, "old_value": item.old_value,
            "new_value": item.new_value, "source": item.source,
            "observed_at": item.observed_at.isoformat() if item.observed_at else None,
        } for item in history_result.scalars().all()],
    }


@router.get("/{author_id}/auto-update")
async def get_author_auto_update(author_id: int, db: AsyncSession = Depends(get_async_db)):
    """从集中报告中查询单个作者的自动更新轨迹，作者页不复制存储报告数据。"""
    author_result = await db.execute(select(Author).where(Author.id == author_id))
    author = author_result.scalar_one_or_none()
    if not author:
        raise HTTPException(status_code=404, detail="作者不存在")

    runtime_config = await get_runtime_config(db)
    current_settings = await asyncio.to_thread(settings.snapshot)
    global_interval = int(runtime_config.get("subscription_check_interval", current_settings.DEFAULT_CHECK_INTERVAL))
    report_result = await db.execute(
        select(SubscriptionCheckReport)
        .order_by(SubscriptionCheckReport.started_at.desc(), SubscriptionCheckReport.id.desc())
        .limit(50)
    )
    entries = []
    for report in report_result.scalars().all():
        try:
            details = json.loads(report.details_json or "[]")
        except (TypeError, ValueError):
            details = []
        item = next((detail for detail in details if detail.get("author_id") == author_id), None)
        if not item:
            continue
        report_breakpoint_id = next(
            (detail.get("author_id") for detail in details if detail.get("status") == "deferred"),
            None,
        )
        entries.append({
            "report_id": report.id,
            "trigger_type": report.trigger_type,
            "report_status": report.status,
            "status": item.get("status"),
            "message": item.get("message") or item.get("error") or item.get("warning"),
            "new_works": int(item.get("new_works", 0) or 0),
            "is_breakpoint": item.get("status") == "deferred" and author_id == report_breakpoint_id,
            "started_at": report.started_at.isoformat() if report.started_at else None,
            "finished_at": report.finished_at.isoformat() if report.finished_at else None,
        })

    expected_next_at = None
    if author.is_subscribed and author.last_auto_update_at:
        expected_next_at = author.last_auto_update_at + timedelta(
            seconds=max(author.check_interval or 0, global_interval, current_settings.MIN_CHECK_INTERVAL)
        )
    if entries and entries[0]["status"] == "deferred" and entries[0]["started_at"]:
        cycle_at = datetime.fromisoformat(entries[0]["started_at"]) + timedelta(seconds=global_interval)
        expected_next_at = max(expected_next_at, cycle_at) if expected_next_at else cycle_at

    return {
        "author": {
            "id": author.id, "nickname": author.nickname, "avatar_url": author.avatar_url,
            "is_subscribed": author.is_subscribed,
        },
        "last_auto_update_at": author.last_auto_update_at.isoformat() if author.last_auto_update_at else None,
        "expected_next_auto_update_at": expected_next_at.isoformat() if expected_next_at else None,
        "check_interval_seconds": max(author.check_interval or 0, global_interval, current_settings.MIN_CHECK_INTERVAL),
        "latest": entries[0] if entries else None,
        "history": entries,
    }


@router.delete("/{author_id}", response_model=MessageResponse)
async def delete_author(author_id: int, db: AsyncSession = Depends(get_async_db)):
    """硬删除作者，恢复到添加作者之前的状态。"""
    try:
        stats = await delete_author_hard(db, author_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="作者不存在")

    return MessageResponse(
        success=True,
        message=(
            f"作者已删除（清理 {stats['removed_files']} 个文件、"
            f"{stats['removed_tasks']} 个任务、{stats['removed_history']} 条历史）"
        ),
        data=stats,
    )


@router.post("/{author_id}/subscribe", response_model=MessageResponse)
async def subscribe_author(author_id: int, db: AsyncSession = Depends(get_async_db)):
    """订阅作者"""
    result = await db.execute(
        select(Author).where(Author.id == author_id)
    )
    author = result.scalar_one_or_none()
    
    if not author:
        raise HTTPException(status_code=404, detail="作者不存在")
    
    author.is_subscribed = True
    await db.commit()
    
    return MessageResponse(success=True, message="已订阅")


@router.post("/{author_id}/unsubscribe", response_model=MessageResponse)
async def unsubscribe_author(author_id: int, db: AsyncSession = Depends(get_async_db)):
    """取消订阅作者"""
    result = await db.execute(
        select(Author).where(Author.id == author_id)
    )
    author = result.scalar_one_or_none()
    
    if not author:
        raise HTTPException(status_code=404, detail="作者不存在")
    
    author.is_subscribed = False
    await db.commit()
    
    return MessageResponse(success=True, message="已取消订阅")


@router.get("/{author_id}/works", response_model=List[WorkResponse])
async def list_author_works(
    author_id: int,
    is_downloaded: Optional[bool] = Query(None, description="按下载状态筛选"),
    include_excluded: bool = Query(False, description="是否包含已删除（排除）的作品"),
    sort_by: Literal[
        "published_desc",
        "published_asc",
        "discovered_desc",
        "discovered_asc",
    ] = Query("published_desc", description="排序方式"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_async_db)
):
    """获取作者的作品列表"""
    # 检查作者是否存在
    result = await db.execute(
        select(Author).where(Author.id == author_id)
    )
    author = result.scalar_one_or_none()
    
    if not author:
        raise HTTPException(status_code=404, detail="作者不存在")
    
    query = select(Work).options(selectinload(Work.download_tasks)).where(Work.author_id == author_id)
    
    if not include_excluded:
        query = query.where(Work.is_excluded == False)  # noqa: E712
    
    if is_downloaded is not None:
        query = query.where(Work.is_downloaded == is_downloaded)
    
    if sort_by.startswith("published_"):
        # 旧数据可能没有平台发布时间。将这些记录稳定地排在有发布时间的
        # 作品之后，避免把发现时间伪装成作品发布时间参与排序。
        published_at_missing = case((Work.published_at.is_(None), 1), else_=0)
        published_order = Work.published_at.desc() if sort_by == "published_desc" else Work.published_at.asc()
        discovered_order = Work.discovered_at.desc() if sort_by == "published_desc" else Work.discovered_at.asc()
        id_order = Work.id.desc() if sort_by == "published_desc" else Work.id.asc()
        query = query.order_by(published_at_missing.asc(), published_order, discovered_order, id_order)
    else:
        discovered_order = Work.discovered_at.desc() if sort_by == "discovered_desc" else Work.discovered_at.asc()
        id_order = Work.id.desc() if sort_by == "discovered_desc" else Work.id.asc()
        query = query.order_by(discovered_order, id_order)
    query = query.offset((page - 1) * page_size).limit(page_size)
    
    result = await db.execute(query)
    works = result.scalars().all()
    
    return [_serialize_work_response(work) for work in works]


@router.post("/{author_id}/download", response_model=MessageResponse)
async def download_author(
    author_id: int,
    start_index: int = Query(1, ge=1, description="起始作品序号"),
    db: AsyncSession = Depends(get_async_db)
):
    """下载作者所有作品"""
    await _raise_if_douyin_cooling()
    result = await db.execute(
        select(Author).where(Author.id == author_id)
    )
    author = result.scalar_one_or_none()
    
    if not author:
        raise HTTPException(status_code=404, detail="作者不存在")

    if await asyncio.to_thread(redis_client.is_author_deleting, author_id):
        raise HTTPException(status_code=409, detail="作者正在删除，请稍后刷新列表")
    
    try:
        await asyncio.to_thread(
            redis_client.append_activity_log,
            "info", "api", f"触发下载: {author.nickname}(ID:{author_id})",
            f"sec_uid={author.sec_uid}, share_url={author.share_url}, start_index={start_index}",
        )
    except Exception:
        pass
    
    # 检查 Celery Worker 是否在线（通过 broker ping）
    try:
        def _ping_worker():
            from app.tasks.celery_app import celery_app
            return celery_app.control.ping(timeout=2.0)
        ping_result = await asyncio.to_thread(_ping_worker)
        if not ping_result:
            raise HTTPException(
                status_code=503,
                detail="Celery Worker 未运行，无法执行下载任务。请在服务器上运行: celery -A app.tasks.celery_app worker --loglevel=info"
            )
    except HTTPException:
        raise
    except Exception:
        pass  # ping 失败不阻塞，可能是 Redis 慢

    # 提交下载任务到 Celery 队列
    try:
        celery_result = await asyncio.to_thread(download_author_works.delay, author_id, start_index)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"无法提交任务到队列: {e}")
    
    try:
        await asyncio.to_thread(
            redis_client.append_activity_log,
            "info", "api", f"任务已提交到队列: {author.nickname}",
            f"celery_task_id={celery_result.id}",
        )
    except Exception:
        pass
    
    return MessageResponse(
        success=True,
        message="下载任务已提交",
        data={"celery_task_id": celery_result.id}
    )


@router.post("/{author_id}/check", response_model=MessageResponse)
async def check_author_updates(author_id: int, db: AsyncSession = Depends(get_async_db)):
    """手动检查作者是否有新作品"""
    await _raise_if_douyin_cooling()
    result = await db.execute(
        select(Author).where(Author.id == author_id)
    )
    author = result.scalar_one_or_none()
    
    if not author:
        raise HTTPException(status_code=404, detail="作者不存在")

    if await asyncio.to_thread(redis_client.is_author_deleting, author_id):
        raise HTTPException(status_code=409, detail="作者正在删除，请稍后刷新列表")
    
    # 触发后台检查任务（只下载新作品）
    celery_result = await asyncio.to_thread(
        download_author_works.delay,
        author_id,
        start_index=1,
        download_new_only=True,
    )
    
    return MessageResponse(
        success=True,
        message="检查任务已提交",
        data={"celery_task_id": celery_result.id}
    )


@router.post("/check-all", response_model=MessageResponse)
async def check_all_subscriptions(db: AsyncSession = Depends(get_async_db)):
    """手动触发检查所有订阅"""
    await _raise_if_douyin_cooling()
    celery_result = await asyncio.to_thread(check_subscriptions.delay, True)
    
    return MessageResponse(
        success=True,
        message="订阅检查任务已提交",
        data={"celery_task_id": celery_result.id}
    )


@router.get("/reports/subscriptions")
async def get_subscription_reports(
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_async_db),
):
    """获取最近的订阅检查报告，包含每位作者的明确结果。"""
    runtime_config = await get_runtime_config(db)
    current_settings = await asyncio.to_thread(settings.snapshot)
    global_interval = int(runtime_config.get("subscription_check_interval", current_settings.DEFAULT_CHECK_INTERVAL))
    result = await db.execute(
        select(SubscriptionCheckReport)
        .order_by(SubscriptionCheckReport.started_at.desc(), SubscriptionCheckReport.id.desc())
        .limit(limit)
    )
    reports = []
    for report in result.scalars().all():
        try:
            details = json.loads(report.details_json or "[]")
        except (TypeError, ValueError):
            details = []
        display_status = report.status
        display_summary = report.summary
        if display_status == "running" and report.started_at and (datetime.now() - report.started_at).total_seconds() > 2100:
            display_status = "interrupted"
            display_summary = "任务超过 35 分钟仍未结束，可能被 Worker 中断；下一轮会继续检查未处理作者"
        reports.append({
            "id": report.id,
            "celery_task_id": report.celery_task_id,
            "trigger_type": report.trigger_type,
            "status": display_status,
            "total_authors": report.total_authors or 0,
            "due_authors": report.due_authors or 0,
            "checked_authors": report.checked_authors or 0,
            "success_authors": report.success_authors or 0,
            "new_works": report.new_works or 0,
            "warning_authors": report.warning_authors or 0,
            "failed_authors": report.failed_authors or 0,
            "skipped_authors": report.skipped_authors or 0,
            "remaining_authors": report.remaining_authors or 0,
            "summary": display_summary,
            "details": details,
            "started_at": report.started_at.isoformat() if report.started_at else None,
            "finished_at": report.finished_at.isoformat() if report.finished_at else None,
            "global_interval_seconds": global_interval,
            "expected_next_cycle_at": (
                (report.started_at + timedelta(seconds=global_interval)).isoformat()
                if report.started_at and report.trigger_type == "auto" else None
            ),
        })
    cycle_result = await db.execute(
        select(SystemConfig).where(SystemConfig.key == SUBSCRIPTION_CYCLE_STATE_KEY)
    )
    cycle_row = cycle_result.scalar_one_or_none()
    try:
        cycle = json.loads(cycle_row.value or "{}") if cycle_row else {}
    except (TypeError, ValueError):
        cycle = {}
    if not cycle and reports:
        latest = reports[0]
        cycle = {
            "active": latest["status"] == "running",
            "total_authors": latest["total_authors"],
            "checked_authors": latest["checked_authors"],
            "remaining_authors": latest["remaining_authors"],
            "new_works": latest["new_works"],
        }
    return {"items": reports, "cycle": cycle}

