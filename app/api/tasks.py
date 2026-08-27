"""
下载任务 API 路由

为什么这样设计：
1. RESTful 风格 API 设计
2. 支持任务创建、查询、暂停、恢复、取消
3. 使用依赖注入获取数据库会话
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select, func, or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer, selectinload
from typing import List, Optional
from datetime import datetime
import asyncio
import json
import mimetypes
from pathlib import Path

from app.models.database import get_async_db
from app.models.models import Author, Work, DownloadTask, DownloadHistory
from app.models.schemas import (
    DownloadTaskCreate, DownloadTaskResponse, TaskProgressResponse,
    BatchDownloadRequest, BatchDownloadResponse, MessageResponse,
    PaginatedTasksResponse
)
from app.tasks.download_tasks import download_single_file, download_author_works, resume_task
from app.core import redis_client
from app.services.downloader import (
    author_profile_has_identity,
    is_video_work_payload,
    latest_video_url,
    payload_image_urls,
    payload_live_photo_urls,
    prefer_avatar_url,
)
from app.core.config import settings
from app.core.runtime_config import get_runtime_config
from app.services.media_paths import resolve_media_path
from app.services.download_task_factory import ensure_download_task_async
from app.services.work_manager import recalc_author_counts
from app.services.douyin_errors import (
    DouyinCooldownError,
    DouyinRequestError,
    classify_stored_task_error,
    http_status_for_douyin_error,
)
from app.services.douyin_source import (
    DouyinSource,
    build_author_profile_url,
    build_douyin_source,
)

router = APIRouter(prefix="/tasks", tags=["下载任务"])

VIDEO_PREVIEW_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
IMAGE_PREVIEW_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
PREVIEWABLE_EXTENSIONS = VIDEO_PREVIEW_EXTENSIONS | IMAGE_PREVIEW_EXTENSIONS


def _parse_preview_range(value: str, file_size: int) -> tuple[int, int]:
    """解析单段 HTTP bytes Range；移动端视频预览依赖 206 响应。"""
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("unsupported range")
    start_text, separator, end_text = value[6:].partition("-")
    if not separator:
        raise ValueError("invalid range")
    if not start_text:
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise ValueError("invalid suffix")
        start = max(0, file_size - suffix_length)
        end = file_size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else file_size - 1
    if start < 0 or start >= file_size or end < start:
        raise ValueError("range outside file")
    return start, min(end, file_size - 1)


def _iter_preview_range(file_path: Path, start: int, end: int):
    remaining = end - start + 1
    with file_path.open("rb") as file:
        file.seek(start)
        while remaining > 0:
            chunk = file.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


async def _raise_if_douyin_cooling() -> None:
    state = await asyncio.to_thread(redis_client.get_douyin_risk_state)
    if state.get("active"):
        exc = DouyinCooldownError(
            retry_after=int(state.get("retry_after") or 1),
            reason=state.get("error_type") or "argus_blocked",
        )
        raise HTTPException(status_code=429, detail=exc.as_dict())


def _update_task_runtime(
    task_ids: List[int],
    *,
    pause: bool = False,
    resume: bool = False,
    clear_progress: bool = False,
) -> None:
    """批量执行同步 Redis 操作，供异步接口在线程中调用。"""
    for task_id in task_ids:
        if pause:
            redis_client.pause_task(task_id)
        if resume:
            redis_client.resume_task(task_id)
        if clear_progress:
            redis_client.delete_progress(task_id)


def _dispatch_download_tasks(task_ids: List[int]) -> None:
    """批量向 Celery 投递任务，避免阻塞 FastAPI 事件循环。"""
    for task_id in task_ids:
        download_single_file.delay(task_id)


def _apply_work_media_payload(work: Work, work_payload: dict, preserve_existing: bool = False) -> None:
    work_type = "video" if is_video_work_payload(work_payload) else "images"
    image_urls = payload_image_urls(work_payload)
    live_photo_urls = payload_live_photo_urls(work_payload)
    video_url = latest_video_url(work_payload)

    work.work_type = work_type

    try:
        create_time = int(work_payload.get("create_time") or 0)
    except (TypeError, ValueError):
        create_time = 0
    if create_time > 0:
        work.published_at = datetime.fromtimestamp(create_time)

    if work_type == "video":
        work.image_count = 0
        if video_url or not preserve_existing:
            work.video_url = video_url
        if not preserve_existing:
            work.image_urls = []
            work.live_photo_urls = []
        return

    if image_urls or not preserve_existing:
        work.image_urls = image_urls
        work.image_count = len(image_urls)
    if live_photo_urls or not preserve_existing:
        work.live_photo_urls = live_photo_urls
    work.video_url = None


def _build_task_preview_data(
    task: DownloadTask,
    work: Optional[Work],
    *,
    include_remote_preview: bool = True,
) -> dict:
    """构造任务预览元数据。"""
    if not work:
        return {
            "preview_media_type": None,
            "preview_url": None,
            "local_preview_available": False,
        }

    if work.work_type == "video":
        if task.status == "completed" and task.file_path:
            return {
                "preview_media_type": "video",
                "preview_url": f"/api/tasks/{task.id}/preview",
                "local_preview_available": True,
            }
        return {
            "preview_media_type": "video",
            "preview_url": None,
            "local_preview_available": False,
        }

    image_urls = (work.image_urls or []) if include_remote_preview else []
    live_photo_urls = (work.live_photo_urls or []) if include_remote_preview else []
    live_photo_url = (
        live_photo_urls[task.file_index]
        if 0 <= task.file_index < len(live_photo_urls)
        else None
    )
    preview_url = None
    if task.status == "completed" and task.file_path:
        preview_url = f"/api/tasks/{task.id}/preview"
    elif live_photo_url:
        preview_url = live_photo_url
    elif 0 <= task.file_index < len(image_urls):
        preview_url = image_urls[task.file_index]

    return {
        "preview_media_type": "video" if live_photo_url else "image",
        "preview_url": preview_url,
        "local_preview_available": task.status == "completed" and bool(task.file_path),
    }


def _serialize_download_task(
    task: DownloadTask,
    *,
    include_remote_preview: bool = True,
) -> DownloadTaskResponse:
    """将任务 ORM 对象转换为包含预览字段的响应对象。"""
    work = task.work
    author = work.author if work else None
    preview_data = _build_task_preview_data(
        task,
        work,
        include_remote_preview=include_remote_preview,
    )

    item = DownloadTaskResponse.model_validate(task)
    item.author_id = author.id if author else None
    item.author_nickname = author.nickname if author else None
    item.aweme_id = work.aweme_id if work else None
    item.work_title = work.title if work else None
    item.work_type = work.work_type if work else None
    item.published_at = work.published_at if work else None
    item.image_count = work.image_count if work else 0
    item.preview_media_type = preview_data["preview_media_type"]
    item.preview_url = preview_data["preview_url"]
    item.local_preview_available = preview_data["local_preview_available"]
    error_meta = classify_stored_task_error(task.error_message)
    item.error_code = error_meta["error_code"]
    item.error_category = error_meta["error_category"]
    item.error_action = error_meta["error_action"]
    if item.error_category == "risk_control":
        try:
            item.retry_after = int(redis_client.get_douyin_risk_state().get("retry_after") or 0)
        except Exception:
            item.retry_after = 0
    return item


@router.post("/download", response_model=BatchDownloadResponse)
async def create_download_task(
    request: BatchDownloadRequest,
    db: AsyncSession = Depends(get_async_db)
):
    """
    创建下载任务 - 自动识别作者主页链接或单个作品链接
    """
    try:
        await _raise_if_douyin_cooling()
        current_settings = await asyncio.to_thread(settings.snapshot)
        cookie = await asyncio.to_thread(redis_client.get_cookie) or current_settings.DOUYIN_COOKIE
        if not cookie:
            raise HTTPException(status_code=400, detail="请先配置抖音 Cookie")
        
        # 检查 Celery Worker 是否在线
        try:
            def _ping_worker():
                from app.tasks.celery_app import celery_app
                return celery_app.control.ping(timeout=2.0)
            ping_result = await asyncio.to_thread(_ping_worker)
            if not ping_result:
                raise HTTPException(
                    status_code=503,
                    detail="Celery Worker 未运行，无法执行下载任务。请在服务器上启动 Worker。"
                )
        except HTTPException:
            raise
        except Exception:
            pass

        runtime_config = await get_runtime_config(db)
        source = build_douyin_source(
            cookie, current_settings.DOWNLOAD_DIR, runtime_config=runtime_config
        )

        url_info = await asyncio.to_thread(source.resolve_input, request.share_url)

        if url_info["type"] == "work":
            return await _handle_single_work(source, url_info["canonical_url"], db)
        else:
            return await _handle_author_download(source, url_info["sec_uid"], db)
    
    except DouyinRequestError as e:
        raise HTTPException(status_code=http_status_for_douyin_error(e), detail=e.as_dict())
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建任务失败: {str(e)}")


async def _handle_author_download(
    source: DouyinSource, sec_uid: str, db: AsyncSession
) -> BatchDownloadResponse:
    """处理作者主页链接 - 原有逻辑"""
    result = await db.execute(select(Author).where(Author.sec_uid == sec_uid))
    author = result.scalar_one_or_none()
    author_exists = author is not None
    author_info = await asyncio.to_thread(source.fetch_profile, sec_uid)

    if not author and not author_profile_has_identity(author_info):
        detail = author_info.get("account_status_detail") or author_info.get("account_status_label") or "抖音未返回可用的作者资料"
        raise HTTPException(status_code=502, detail=f"无法获取作者资料，未创建作者记录：{detail}")

    if not author:
        author = Author(
            sec_uid=sec_uid,
            nickname=author_info.get("nickname"),
            share_url=author_info.get("profile_url") or build_author_profile_url(sec_uid),
            avatar_url=author_info.get("avatar_url")
        )
        db.add(author)
        await db.flush()
    else:
        author.nickname = author_info.get("nickname") or author.nickname
        author.avatar_url = prefer_avatar_url(author.avatar_url, author_info.get("avatar_url"))
        author.share_url = author_info.get("profile_url") or build_author_profile_url(sec_uid) or author.share_url

    author_created_at = author.created_at
    await recalc_author_counts(db, author)
    await db.commit()

    author_position = None
    if author_exists and author_created_at:
        pos_result = await db.execute(
            select(func.count(Author.id)).where(Author.created_at > author_created_at)
        )
        author_position = pos_result.scalar() or 0

    await asyncio.to_thread(download_author_works.delay, author.id, start_index=1)

    return BatchDownloadResponse(
        url_type="author",
        author_id=author.id,
        author_nickname=author.nickname,
        total_works=author.total_works or 0,
        created_tasks=0,
        task_ids=[],
        author_already_exists=author_exists,
        author_position=author_position
    )



async def _handle_single_work(
    source: DouyinSource, canonical_url: str, db: AsyncSession
) -> BatchDownloadResponse:
    """处理单个作品链接"""
    work_data = await asyncio.to_thread(source.fetch_work, canonical_url)
    work_info = work_data["work"]
    author_info = work_data["author_info"]
    sec_uid = author_info["sec_uid"]

    if not sec_uid:
        raise HTTPException(status_code=400, detail="无法获取作品作者信息")
    if not author_profile_has_identity(author_info):
        detail = author_info.get("account_status_detail") or author_info.get("account_status_label") or "抖音未返回可用的作者资料"
        raise HTTPException(status_code=502, detail=f"无法获取作者资料，未创建作者记录：{detail}")

    result = await db.execute(select(Author).where(Author.sec_uid == sec_uid))
    author = result.scalar_one_or_none()
    if not author:
        author = Author(
            sec_uid=sec_uid,
            nickname=author_info.get("nickname"),
            share_url=author_info.get("profile_url") or build_author_profile_url(sec_uid),
            avatar_url=author_info.get("avatar_url")
        )
        db.add(author)
        await db.flush()
    else:
        author.share_url = author_info.get("profile_url") or build_author_profile_url(sec_uid) or author.share_url

    aweme_id = work_info["aweme_id"]
    result = await db.execute(select(Work).where(Work.aweme_id == aweme_id))
    work = result.scalar_one_or_none()

    if not work:
        work = Work(
            aweme_id=aweme_id,
            author_id=author.id,
            title=work_info.get("desc", ""),
            work_type="video",
        )
        _apply_work_media_payload(work, work_info)
        db.add(work)
        await db.flush()
    else:
        # 更新已存在作品的 URL（抖音 URL 会过期）
        _apply_work_media_payload(work, work_info, preserve_existing=True)
        work.title = work_info.get("desc", "") or work.title

    created_task_ids = []
    reused_task_ids = []
    if work.work_type == "video":
        task, action = await ensure_download_task_async(db, work.id, 0)
        if action == "created":
            created_task_ids.append(task.id)
        elif action == "reused":
            reused_task_ids.append(task.id)
        if action != "existing":
            work.is_downloaded = False
    else:
        for img_idx in range(work.image_count):
            task, action = await ensure_download_task_async(db, work.id, img_idx)
            if action == "created":
                created_task_ids.append(task.id)
            elif action == "reused":
                reused_task_ids.append(task.id)
            if action != "existing":
                work.is_downloaded = False

    await recalc_author_counts(db, author)
    await db.commit()

    all_task_ids = created_task_ids + reused_task_ids
    await asyncio.to_thread(_dispatch_download_tasks, all_task_ids)

    return BatchDownloadResponse(
        url_type="work",
        author_id=author.id,
        author_nickname=author.nickname,
        total_works=1,
        created_tasks=len(all_task_ids),
        task_ids=all_task_ids,
    )


@router.get("/", response_model=PaginatedTasksResponse)
async def list_tasks(
    status: Optional[str] = Query(None, description="按状态筛选"),
    author_id: Optional[int] = Query(None, description="按作者筛选"),
    q: Optional[str] = Query(None, max_length=200, description="搜索任务、作品或作者"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_async_db)
):
    """获取下载任务列表（带分页）"""
    common_filters = []
    if author_id:
        common_filters.append(Work.author_id == author_id)

    search_text = (q or "").strip()
    if search_text:
        escaped = search_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        search_filters = [
            DownloadTask.file_name.ilike(pattern, escape="\\"),
            Work.title.ilike(pattern, escape="\\"),
            Work.aweme_id.ilike(pattern, escape="\\"),
            Author.nickname.ilike(pattern, escape="\\"),
        ]
        if search_text.isdigit():
            search_filters.append(DownloadTask.id == int(search_text))
        common_filters.append(or_(*search_filters))

    # 统一关联作品和作者，使分页、总数与状态汇总使用完全相同的筛选语义。
    base_query = select(DownloadTask).join(
        Work, DownloadTask.work_id == Work.id
    ).outerjoin(
        Author, Work.author_id == Author.id
    ).options(
        selectinload(DownloadTask.work).options(
            selectinload(Work.author),
            defer(Work._image_urls),
            defer(Work._live_photo_urls),
            defer(Work.video_url),
        )
    ).where(*common_filters)
    count_query = select(func.count(DownloadTask.id)).select_from(DownloadTask).join(
        Work, DownloadTask.work_id == Work.id
    ).outerjoin(
        Author, Work.author_id == Author.id
    ).where(*common_filters)

    status_count_query = select(
        DownloadTask.status,
        func.count(DownloadTask.id),
    ).select_from(DownloadTask).join(
        Work, DownloadTask.work_id == Work.id
    ).outerjoin(
        Author, Work.author_id == Author.id
    ).where(*common_filters).group_by(DownloadTask.status)
    
    if status:
        base_query = base_query.where(DownloadTask.status == status)
        count_query = count_query.where(DownloadTask.status == status)
    
    # 窗口计数与当页数据一次返回，避免高频轮询每次额外 COUNT。
    # 批量创建的任务可能共享同一个 created_at。必须用唯一主键打破并列，
    # 否则数据库可在每次分页查询时以不同顺序返回这些任务。
    query = base_query.add_columns(
        func.count(DownloadTask.id).over().label("_total")
    ).order_by(
        DownloadTask.created_at.desc(),
        DownloadTask.id.desc(),
    )
    query = query.offset((page - 1) * page_size).limit(page_size)
    
    result = await db.execute(query)
    rows = result.all()
    status_counts = {
        str(row[0]): int(row[1] or 0)
        for row in (await db.execute(status_count_query)).all()
    }
    tasks = [row[0] for row in rows]
    if rows:
        total = int(rows[0][1] or 0)
    elif page > 1:
        total = int((await db.execute(count_query)).scalar() or 0)
    else:
        total = 0
    
    # 计算总页数
    pages = (total + page_size - 1) // page_size if total > 0 else 1
    
    items = [
        _serialize_download_task(task, include_remote_preview=False)
        for task in tasks
    ]
    
    return PaginatedTasksResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
        status_counts=status_counts,
    )


@router.get("/failed/errors", response_model=MessageResponse)
async def get_failed_errors_for_toolbar(db: AsyncSession = Depends(get_async_db)):
    """静态路径别名，避免与 /{task_id} 动态路由发生匹配冲突。"""
    return await get_all_failed_errors(db)


@router.get("/{task_id}/preview")
async def preview_task_video(
    task_id: int,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
):
    """预览已完成的本地媒体文件。"""
    result = await db.execute(
        select(DownloadTask).where(DownloadTask.id == task_id)
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status != "completed":
        raise HTTPException(status_code=400, detail="仅支持预览已完成任务")
    if not task.file_path:
        raise HTTPException(status_code=404, detail="任务没有可预览文件")

    try:
        current_settings = await asyncio.to_thread(settings.snapshot)
        file_path = await asyncio.to_thread(
            resolve_media_path,
            task.file_path,
            current_settings.DOWNLOAD_DIR,
        )
        if str(file_path) != task.file_path:
            task.file_path = str(file_path)
            await db.commit()
    except ValueError:
        raise HTTPException(status_code=403, detail="文件不在下载目录内")

    if file_path.suffix.lower() not in PREVIEWABLE_EXTENSIONS:
        raise HTTPException(status_code=400, detail="仅支持预览图片或视频文件")
    if not await asyncio.to_thread(file_path.is_file):
        raise HTTPException(status_code=404, detail="预览文件不存在")

    media_type = mimetypes.guess_type(str(file_path))[0] or "video/mp4"
    if file_path.suffix.lower() in VIDEO_PREVIEW_EXTENSIONS:
        file_size = (await asyncio.to_thread(file_path.stat)).st_size
        range_header = request.headers.get("range")
        if range_header:
            try:
                start, end = _parse_preview_range(range_header, file_size)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=416,
                    detail="请求的视频范围无效",
                    headers={"Content-Range": f"bytes */{file_size}"},
                )
            return StreamingResponse(
                _iter_preview_range(file_path, start, end),
                status_code=206,
                media_type=media_type,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Content-Length": str(end - start + 1),
                    "Content-Disposition": "inline",
                    "Cache-Control": "private, max-age=60",
                },
            )
    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=file_path.name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=60", "Accept-Ranges": "bytes"},
    )


@router.get("/{task_id}", response_model=DownloadTaskResponse)
async def get_task(task_id: int, db: AsyncSession = Depends(get_async_db)):
    """获取单个任务详情"""
    result = await db.execute(
        select(DownloadTask)
        .options(selectinload(DownloadTask.work).selectinload(Work.author))
        .where(DownloadTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    return _serialize_download_task(task)


@router.get("/{task_id}/progress", response_model=TaskProgressResponse)
async def get_task_progress(task_id: int, db: AsyncSession = Depends(get_async_db)):
    """获取任务实时进度"""
    # 先从 Redis 获取实时进度
    progress = await asyncio.to_thread(redis_client.get_progress, task_id)
    
    if progress:
        eta = None
        if progress.get("speed", 0) > 0:
            remaining = progress.get("total_bytes", 0) - progress.get("downloaded_bytes", 0)
            eta = int(remaining / progress["speed"])
        
        return TaskProgressResponse(
            task_id=task_id,
            celery_task_id=progress.get("celery_task_id"),
            status=progress.get("status", "downloading"),
            file_name=progress.get("file_name"),
            total_bytes=progress.get("total_bytes", 0),
            downloaded_bytes=progress.get("downloaded_bytes", 0),
            progress_percent=progress.get("progress_percent", 0),
            download_speed=progress.get("speed", 0),
            eta_seconds=eta
        )
    
    # Redis 中没有，从数据库获取
    result = await db.execute(
        select(DownloadTask).where(DownloadTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    return TaskProgressResponse(
        task_id=task.id,
        celery_task_id=task.celery_task_id,
        status=task.status,
        file_name=task.file_name,
        total_bytes=task.total_bytes,
        downloaded_bytes=task.downloaded_bytes,
        progress_percent=task.progress_percent,
        download_speed=task.download_speed,
        eta_seconds=None
    )


@router.post("/{task_id}/pause", response_model=MessageResponse)
async def pause_task(task_id: int, db: AsyncSession = Depends(get_async_db)):
    """暂停下载任务"""
    result = await db.execute(
        select(DownloadTask).where(DownloadTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    if task.status not in ("downloading", "pending"):
        raise HTTPException(status_code=400, detail=f"任务状态为 {task.status}，无法暂停")
    
    # 设置暂停信号
    await asyncio.to_thread(redis_client.pause_task, task_id)
    
    return MessageResponse(success=True, message="暂停信号已发送")


@router.post("/{task_id}/resume", response_model=MessageResponse)
async def resume_task_api(task_id: int, db: AsyncSession = Depends(get_async_db)):
    """恢复暂停的任务"""
    result = await db.execute(
        select(DownloadTask).where(DownloadTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    if task.status != "paused":
        raise HTTPException(status_code=400, detail=f"任务状态为 {task.status}，无法恢复")
    
    # 更新状态
    task.status = "pending"
    await db.commit()
    
    # 触发恢复任务
    await asyncio.to_thread(resume_task.delay, task_id)
    
    return MessageResponse(success=True, message="任务已恢复")


@router.post("/{task_id}/cancel", response_model=MessageResponse)
async def cancel_task(task_id: int, db: AsyncSession = Depends(get_async_db)):
    """取消下载任务"""
    result = await db.execute(
        select(DownloadTask).where(DownloadTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    if task.status == "completed":
        raise HTTPException(status_code=400, detail="任务已完成，无法取消")
    
    # 设置暂停信号（让正在下载的任务停止）
    await asyncio.to_thread(redis_client.pause_task, task_id)
    
    # 更新状态为取消
    task.status = "cancelled"
    await db.commit()
    
    # 清理 Redis 进度
    await asyncio.to_thread(redis_client.delete_progress, task_id)
    
    return MessageResponse(success=True, message="任务已取消")


@router.post("/{task_id}/retry", response_model=MessageResponse)
async def retry_task(task_id: int, db: AsyncSession = Depends(get_async_db)):
    """重试失败的任务"""
    result = await db.execute(
        select(DownloadTask).where(DownloadTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    if task.status not in ("failed", "cancelled"):
        raise HTTPException(status_code=400, detail=f"任务状态为 {task.status}，无需重试")
    
    # 重置状态
    task.status = "pending"
    task.error_message = None
    await db.commit()
    
    # 触发下载任务
    await asyncio.to_thread(download_single_file.delay, task_id)
    
    return MessageResponse(success=True, message="任务已重新提交")


@router.post("/{task_id}/force-retry", response_model=MessageResponse)
async def force_retry_task(task_id: int, db: AsyncSession = Depends(get_async_db)):
    """强制重试任务（用于卡住的下载任务）"""
    result = await db.execute(
        select(DownloadTask).where(DownloadTask.id == task_id)
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status == "completed":
        raise HTTPException(status_code=400, detail="任务已完成，无法重试")

    # 清除暂停信号
    await asyncio.to_thread(
        _update_task_runtime, [task_id], resume=True, clear_progress=True
    )

    # 重置状态和进度
    task.status = "pending"
    task.error_message = None
    task.downloaded_bytes = 0
    task.download_speed = 0
    await db.commit()

    # 触发新的下载任务
    await asyncio.to_thread(download_single_file.delay, task_id)

    return MessageResponse(success=True, message="任务已强制重新提交")


@router.delete("/{task_id}", response_model=MessageResponse)
async def delete_task(task_id: int, db: AsyncSession = Depends(get_async_db)):
    """删除下载任务"""
    result = await db.execute(
        select(DownloadTask).where(DownloadTask.id == task_id)
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 如果任务正在下载，先暂停
    if task.status == "downloading":
        await asyncio.to_thread(
            _update_task_runtime, [task_id], pause=True, clear_progress=True
        )
    else:
        await asyncio.to_thread(redis_client.delete_progress, task_id)

    # 删除任务记录
    await db.delete(task)
    await db.commit()

    return MessageResponse(success=True, message="任务已删除")


@router.post("/force-retry-all-downloading", response_model=MessageResponse)
async def force_retry_all_downloading(db: AsyncSession = Depends(get_async_db)):
    """批量强制重试所有卡住的下载中任务"""
    result = await db.execute(
        select(DownloadTask).where(DownloadTask.status == "downloading")
    )
    stuck_tasks = result.scalars().all()

    if not stuck_tasks:
        return MessageResponse(success=True, message="没有下载中的任务", data={"count": 0})

    count = 0
    task_ids = [task.id for task in stuck_tasks]
    await asyncio.to_thread(
        _update_task_runtime, task_ids, resume=True, clear_progress=True
    )
    for task in stuck_tasks:
        task.status = "pending"
        task.error_message = None
        task.downloaded_bytes = 0
        task.download_speed = 0
        count += 1

    await db.commit()

    await asyncio.to_thread(_dispatch_download_tasks, task_ids)

    return MessageResponse(
        success=True,
        message=f"已强制重新提交 {count} 个下载中任务",
        data={"count": count}
    )


@router.post("/retry-all-failed", response_model=MessageResponse)
async def retry_all_failed_tasks(db: AsyncSession = Depends(get_async_db)):
    """一键重试所有失败的任务"""
    state = await asyncio.to_thread(redis_client.get_douyin_risk_state)
    if state.get("active"):
        return MessageResponse(
            success=True,
            message=f"抖音接口正在冷却，已跳过失败任务；约 {state.get('retry_after', 0)} 秒后再试",
            data={"count": 0, "skipped": "risk_cooldown", "retry_after": state.get("retry_after", 0)},
        )
    # 查询所有失败的任务
    result = await db.execute(
        select(DownloadTask).where(DownloadTask.status.in_(["failed", "cancelled"]))
    )
    failed_tasks = result.scalars().all()

    if not failed_tasks:
        return MessageResponse(success=True, message="没有失败的任务需要重试", data={"count": 0})

    # 重置所有失败任务的状态
    count = 0
    for task in failed_tasks:
        task.status = "pending"
        task.error_message = None
        count += 1

    await db.commit()

    # 触发所有任务的下载
    await asyncio.to_thread(_dispatch_download_tasks, [task.id for task in failed_tasks])

    return MessageResponse(
        success=True,
        message=f"已重新提交 {count} 个失败任务",
        data={"count": count}
    )


@router.get("/all-failed-errors", response_model=MessageResponse)
async def get_all_failed_errors(db: AsyncSession = Depends(get_async_db)):
    """获取所有失败任务的错误信息"""
    result = await db.execute(
        select(DownloadTask.id, DownloadTask.file_name, DownloadTask.error_message)
        .where(DownloadTask.status.in_(["failed", "cancelled"]))
        .where(DownloadTask.error_message.isnot(None))
        .order_by(DownloadTask.id.desc())
    )
    failed_tasks = result.all()

    errors = [
        f"[任务{t.id}] {t.file_name or '未知'}: {t.error_message}"
        for t in failed_tasks
    ]

    return MessageResponse(
        success=True,
        message=f"共 {len(errors)} 个失败任务",
        data={"count": len(errors), "errors": errors}
    )


@router.post("/batch-delete", response_model=MessageResponse)
async def batch_delete_tasks(
    status: str = Query(..., description="要删除的任务状态"),
    db: AsyncSession = Depends(get_async_db)
):
    """批量删除指定状态的所有任务"""
    # 查询待删除的任务
    result = await db.execute(
        select(DownloadTask).where(DownloadTask.status == status)
    )
    tasks = result.scalars().all()

    if not tasks:
        return MessageResponse(success=True, message="没有可删除的任务", data={"count": 0})

    count = len(tasks)
    await asyncio.to_thread(
        _update_task_runtime, [task.id for task in tasks], clear_progress=True
    )
    for task in tasks:
        await db.delete(task)

    await db.commit()

    await asyncio.to_thread(
        redis_client.append_activity_log,
        "info", "api", f"🗑️ 批量删除 {count} 个 {status} 状态的任务", "",
    )

    return MessageResponse(
        success=True,
        message=f"已删除 {count} 个任务",
        data={"count": count}
    )


@router.post("/pause-all", response_model=MessageResponse)
async def pause_all_tasks(db: AsyncSession = Depends(get_async_db)):
    """暂停所有正在下载和等待中的任务"""
    result = await db.execute(
        select(DownloadTask).where(DownloadTask.status.in_(["downloading", "pending"]))
    )
    active_tasks = result.scalars().all()

    if not active_tasks:
        return MessageResponse(success=True, message="没有需要暂停的任务", data={"count": 0})

    count = 0
    await asyncio.to_thread(
        _update_task_runtime, [task.id for task in active_tasks], pause=True
    )
    for task in active_tasks:
        if task.status == "pending":
            task.status = "paused"
        count += 1

    await db.commit()

    return MessageResponse(
        success=True,
        message=f"已暂停 {count} 个任务",
        data={"count": count}
    )


@router.post("/refresh-retry/{task_id}", response_model=MessageResponse)
async def refresh_retry_task(task_id: int, db: AsyncSession = Depends(get_async_db)):
    """重新获取下载链接后重试失败的任务"""
    await _raise_if_douyin_cooling()
    result = await db.execute(
        select(DownloadTask).where(DownloadTask.id == task_id)
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status not in ("failed", "cancelled"):
        raise HTTPException(status_code=400, detail=f"任务状态为 {task.status}，无需重试")

    # 获取关联的 Work 和 Author
    work_result = await db.execute(
        select(Work).where(Work.id == task.work_id)
    )
    work = work_result.scalar_one_or_none()
    if not work:
        raise HTTPException(status_code=404, detail="关联的作品不存在")

    author_result = await db.execute(
        select(Author).where(Author.id == work.author_id)
    )
    author = author_result.scalar_one_or_none()
    if not author:
        raise HTTPException(status_code=404, detail="关联的作者不存在")

    # 刷新下载链接
    try:
        current_settings = await asyncio.to_thread(settings.snapshot)
        cookie = await asyncio.to_thread(redis_client.get_cookie) or current_settings.DOUYIN_COOKIE
        if not cookie:
            raise HTTPException(status_code=400, detail="请先配置抖音 Cookie")

        runtime_config = await get_runtime_config(db)

        def _refresh():
            source = build_douyin_source(
                cookie, current_settings.DOWNLOAD_DIR, runtime_config=runtime_config
            )
            return source.refresh_assets(work.aweme_id)

        fresh = await asyncio.to_thread(_refresh)

        if work.work_type == "video":
            refreshed_video_url = latest_video_url(fresh)
            if refreshed_video_url:
                work.video_url = refreshed_video_url
        else:
            image_urls = payload_image_urls(fresh)
            live_photo_urls = payload_live_photo_urls(fresh)
            if image_urls:
                work.image_urls = image_urls
                work.image_count = len(image_urls)
                work.live_photo_urls = live_photo_urls

    except DouyinRequestError as e:
        raise HTTPException(status_code=http_status_for_douyin_error(e), detail=e.as_dict())
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"刷新下载链接失败: {str(e)}")

    # 重置任务状态
    task.status = "pending"
    task.error_message = None
    await asyncio.to_thread(redis_client.delete_progress, task_id)

    await db.commit()

    await asyncio.to_thread(download_single_file.delay, task_id)

    return MessageResponse(success=True, message=f"已刷新下载链接并重新提交任务（作品: {work.aweme_id}）")


@router.post("/refresh-retry-all-failed", response_model=MessageResponse)
async def refresh_retry_all_failed(db: AsyncSession = Depends(get_async_db)):
    """重新获取所有失败任务的下载链接后重试"""
    await _raise_if_douyin_cooling()
    result = await db.execute(
        select(DownloadTask).where(DownloadTask.status.in_(["failed", "cancelled"]))
    )
    failed_tasks = result.scalars().all()

    if not failed_tasks:
        return MessageResponse(success=True, message="没有失败的任务需要重试", data={"count": 0})

    current_settings = await asyncio.to_thread(settings.snapshot)
    cookie = await asyncio.to_thread(redis_client.get_cookie) or current_settings.DOUYIN_COOKIE
    if not cookie:
        raise HTTPException(status_code=400, detail="请先配置抖音 Cookie")

    runtime_config = await get_runtime_config(db)

    def _create_source():
        return build_douyin_source(
            cookie, current_settings.DOWNLOAD_DIR, runtime_config=runtime_config
        )
    source = await asyncio.to_thread(_create_source)

    # 按 work_id 分组，避免同一个作品重复刷新
    work_ids = set(t.work_id for t in failed_tasks)
    refreshed_works = {}

    for wid in work_ids:
        work_result = await db.execute(select(Work).where(Work.id == wid))
        work = work_result.scalar_one_or_none()
        if not work:
            continue
        try:
            def _refresh(aweme_id=work.aweme_id):
                return source.refresh_assets(aweme_id)
            fresh = await asyncio.to_thread(_refresh)

            if work.work_type == "video":
                refreshed_video_url = latest_video_url(fresh)
                if refreshed_video_url:
                    work.video_url = refreshed_video_url
            else:
                image_urls = payload_image_urls(fresh)
                live_photo_urls = payload_live_photo_urls(fresh)
                if image_urls:
                    work.image_urls = image_urls
                    work.image_count = len(image_urls)
                    work.live_photo_urls = live_photo_urls
            refreshed_works[wid] = True
        except DouyinRequestError as exc:
            if exc.code in {"argus_blocked", "rate_limited"}:
                raise HTTPException(status_code=http_status_for_douyin_error(exc), detail=exc.as_dict())
            refreshed_works[wid] = False
        except Exception:
            refreshed_works[wid] = False

    count = 0
    failed_task_ids = [task.id for task in failed_tasks]
    await asyncio.to_thread(
        _update_task_runtime, failed_task_ids, clear_progress=True
    )
    for task in failed_tasks:
        task.status = "pending"
        task.error_message = None
        count += 1

    await db.commit()

    await asyncio.to_thread(_dispatch_download_tasks, failed_task_ids)

    refreshed_count = sum(1 for v in refreshed_works.values() if v)
    return MessageResponse(
        success=True,
        message=f"已刷新 {refreshed_count}/{len(work_ids)} 个作品的链接，重新提交 {count} 个任务",
        data={"count": count, "refreshed": refreshed_count}
    )


@router.post("/redispatch-pending", response_model=MessageResponse)
async def redispatch_pending_tasks(db: AsyncSession = Depends(get_async_db)):
    """
    重新分发所有 pending 状态的任务。
    
    用于 Worker 崩溃后恢复：
    download_author_works 创建了 pending 任务并调用了 .delay()，
    但 Worker 崩溃导致队列中的消息丢失或未消费。
    此端点把所有 pending 任务重新投递到 Celery 队列。
    """
    result = await db.execute(
        select(DownloadTask).where(DownloadTask.status == "pending")
    )
    pending_tasks = result.scalars().all()

    if not pending_tasks:
        return MessageResponse(success=True, message="没有待处理任务", data={"count": 0})

    count = len(pending_tasks)
    await asyncio.to_thread(_dispatch_download_tasks, [task.id for task in pending_tasks])

    await asyncio.to_thread(
        redis_client.append_activity_log,
        "info", "api",
        f"🔄 重新分发 {count} 个 pending 任务到队列",
        f"task_ids={[t.id for t in pending_tasks[:20]]}{'...' if count > 20 else ''}",
    )

    return MessageResponse(
        success=True,
        message=f"已重新分发 {count} 个待处理任务",
        data={"count": count}
    )
