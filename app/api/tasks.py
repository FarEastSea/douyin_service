"""
下载任务 API 路由

为什么这样设计：
1. RESTful 风格 API 设计
2. 支持任务创建、查询、暂停、恢复、取消
3. 使用依赖注入获取数据库会话
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from typing import List, Optional
from datetime import datetime
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
    DouyinDownloader,
    author_profile_has_identity,
    is_video_work_payload,
    latest_video_url,
    payload_image_urls,
    payload_live_photo_urls,
)
from app.core.config import settings
from app.core.env_config import read_env_file
from app.core.runtime_config import get_runtime_config

router = APIRouter(prefix="/tasks", tags=["下载任务"])

VIDEO_PREVIEW_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
IMAGE_PREVIEW_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
PREVIEWABLE_EXTENSIONS = VIDEO_PREVIEW_EXTENSIONS | IMAGE_PREVIEW_EXTENSIONS


def _apply_work_media_payload(work: Work, work_payload: dict, preserve_existing: bool = False) -> None:
    work_type = "video" if is_video_work_payload(work_payload) else "images"
    image_urls = payload_image_urls(work_payload)
    live_photo_urls = payload_live_photo_urls(work_payload)
    video_url = latest_video_url(work_payload)

    work.work_type = work_type

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


def _build_task_preview_data(task: DownloadTask, work: Optional[Work]) -> dict:
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

    image_urls = work.image_urls or []
    live_photo_urls = work.live_photo_urls or []
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


def _serialize_download_task(task: DownloadTask) -> DownloadTaskResponse:
    """将任务 ORM 对象转换为包含预览字段的响应对象。"""
    work = task.work
    author = work.author if work else None
    preview_data = _build_task_preview_data(task, work)

    item = DownloadTaskResponse.model_validate(task)
    item.author_id = author.id if author else None
    item.author_nickname = author.nickname if author else None
    item.aweme_id = work.aweme_id if work else None
    item.work_title = work.title if work else None
    item.work_type = work.work_type if work else None
    item.image_count = work.image_count if work else 0
    item.preview_media_type = preview_data["preview_media_type"]
    item.preview_url = preview_data["preview_url"]
    item.local_preview_available = preview_data["local_preview_available"]
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
        cookie = redis_client.get_cookie() or settings.DOUYIN_COOKIE
        if not cookie:
            raise HTTPException(status_code=400, detail="请先配置抖音 Cookie")
        
        # 检查 Celery Worker 是否在线
        import asyncio
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
        downloader = DouyinDownloader(cookie, settings.DOWNLOAD_DIR, runtime_config=runtime_config)

        url_info = downloader.detect_url_type(request.share_url)

        if url_info["type"] == "work":
            return await _handle_single_work(downloader, url_info["redirect_url"], db)
        else:
            return await _handle_author_download(downloader, request.share_url, db)
    
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建任务失败: {str(e)}")


async def _handle_author_download(
    downloader: DouyinDownloader, share_url: str, db: AsyncSession
) -> BatchDownloadResponse:
    """处理作者主页链接 - 原有逻辑"""
    sec_uid = downloader.get_sec_uid(share_url)
    result = await db.execute(select(Author).where(Author.sec_uid == sec_uid))
    author = result.scalar_one_or_none()
    author_exists = author is not None
    author_info = downloader.get_author_info(sec_uid)

    if not author and not author_profile_has_identity(author_info):
        detail = author_info.get("account_status_detail") or author_info.get("account_status_label") or "抖音未返回可用的作者资料"
        raise HTTPException(status_code=502, detail=f"无法获取作者资料，未创建作者记录：{detail}")

    if not author:
        author = Author(
            sec_uid=sec_uid,
            nickname=author_info.get("nickname"),
            share_url=author_info.get("profile_url") or DouyinDownloader.build_author_profile_url(sec_uid) or share_url,
            avatar_url=author_info.get("avatar_url")
        )
        db.add(author)
        await db.flush()
    else:
        author.nickname = author_info.get("nickname") or author.nickname
        author.avatar_url = author_info.get("avatar_url") or author.avatar_url
        author.share_url = author_info.get("profile_url") or DouyinDownloader.build_author_profile_url(sec_uid) or author.share_url

    author_created_at = author.created_at
    await db.commit()

    author_position = None
    if author_exists and author_created_at:
        pos_result = await db.execute(
            select(func.count(Author.id)).where(Author.created_at > author_created_at)
        )
        author_position = pos_result.scalar() or 0

    download_author_works.delay(author.id, start_index=1)

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
    downloader: DouyinDownloader, redirect_url: str, db: AsyncSession
) -> BatchDownloadResponse:
    """处理单个作品链接"""
    work_data = downloader.get_single_work(redirect_url)
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
            share_url=author_info.get("profile_url") or DouyinDownloader.build_author_profile_url(sec_uid),
            avatar_url=author_info.get("avatar_url")
        )
        db.add(author)
        await db.flush()
    else:
        author.share_url = author_info.get("profile_url") or DouyinDownloader.build_author_profile_url(sec_uid) or author.share_url

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
        existing = await db.execute(
            select(DownloadTask).where(
                DownloadTask.work_id == work.id, DownloadTask.file_index == 0
            )
        )
        existing_task = existing.scalar_one_or_none()
        if existing_task:
            if existing_task.status in ("failed", "cancelled"):
                existing_task.status = "pending"
                existing_task.error_message = None
                reused_task_ids.append(existing_task.id)
            # completed/downloading/pending/paused 跳过
        else:
            task = DownloadTask(work_id=work.id, file_index=0, status="pending")
            db.add(task)
            await db.flush()
            created_task_ids.append(task.id)
    else:
        for img_idx in range(work.image_count):
            existing = await db.execute(
                select(DownloadTask).where(
                    DownloadTask.work_id == work.id,
                    DownloadTask.file_index == img_idx
                )
            )
            existing_task = existing.scalar_one_or_none()
            if existing_task:
                if existing_task.status in ("failed", "cancelled"):
                    existing_task.status = "pending"
                    existing_task.error_message = None
                    reused_task_ids.append(existing_task.id)
            else:
                task = DownloadTask(work_id=work.id, file_index=img_idx, status="pending")
                db.add(task)
                await db.flush()
                created_task_ids.append(task.id)

    await db.commit()

    all_task_ids = created_task_ids + reused_task_ids
    for tid in all_task_ids:
        download_single_file.delay(tid)

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
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_async_db)
):
    """获取下载任务列表（带分页）"""
    # 构建基础查询
    base_query = select(DownloadTask).options(
        selectinload(DownloadTask.work).selectinload(Work.author)
    )
    count_query = select(func.count(DownloadTask.id))
    
    if status:
        base_query = base_query.where(DownloadTask.status == status)
        count_query = count_query.where(DownloadTask.status == status)
    
    if author_id:
        base_query = base_query.join(Work).where(Work.author_id == author_id)
        count_query = count_query.join(Work).where(Work.author_id == author_id)
    
    # 获取总数
    total_result = await db.execute(count_query)
    total = total_result.scalar()
    
    # 获取分页数据
    query = base_query.order_by(DownloadTask.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    
    result = await db.execute(query)
    tasks = result.scalars().all()
    
    # 计算总页数
    pages = (total + page_size - 1) // page_size if total > 0 else 1
    
    items = [_serialize_download_task(task) for task in tasks]
    
    return PaginatedTasksResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        pages=pages
    )


@router.get("/{task_id}/preview")
async def preview_task_video(task_id: int, db: AsyncSession = Depends(get_async_db)):
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

    file_path = Path(task.file_path).expanduser().resolve()
    configured_download_dir = read_env_file().get("DOWNLOAD_DIR") or settings.DOWNLOAD_DIR
    download_root = Path(configured_download_dir).expanduser().resolve()

    try:
        file_path.relative_to(download_root)
    except ValueError:
        raise HTTPException(status_code=403, detail="文件不在下载目录内")

    if file_path.suffix.lower() not in PREVIEWABLE_EXTENSIONS:
        raise HTTPException(status_code=400, detail="仅支持预览图片或视频文件")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="预览文件不存在")

    media_type = mimetypes.guess_type(str(file_path))[0] or "video/mp4"
    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=file_path.name,
        headers={"Cache-Control": "private, max-age=60"},
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
    progress = redis_client.get_progress(task_id)
    
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
    redis_client.pause_task(task_id)
    
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
    resume_task.delay(task_id)
    
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
    redis_client.pause_task(task_id)
    
    # 更新状态为取消
    task.status = "cancelled"
    await db.commit()
    
    # 清理 Redis 进度
    redis_client.delete_progress(task_id)
    
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
    download_single_file.delay(task_id)
    
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
    redis_client.resume_task(task_id)

    # 清除 Redis 进度
    redis_client.delete_progress(task_id)

    # 重置状态和进度
    task.status = "pending"
    task.error_message = None
    task.downloaded_bytes = 0
    task.download_speed = 0
    await db.commit()

    # 触发新的下载任务
    download_single_file.delay(task_id)

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
        redis_client.pause_task(task_id)

    # 清理 Redis 进度
    redis_client.delete_progress(task_id)

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
    for task in stuck_tasks:
        redis_client.resume_task(task.id)
        redis_client.delete_progress(task.id)
        task.status = "pending"
        task.error_message = None
        task.downloaded_bytes = 0
        task.download_speed = 0
        count += 1

    await db.commit()

    for task in stuck_tasks:
        download_single_file.delay(task.id)

    return MessageResponse(
        success=True,
        message=f"已强制重新提交 {count} 个下载中任务",
        data={"count": count}
    )


@router.post("/retry-all-failed", response_model=MessageResponse)
async def retry_all_failed_tasks(db: AsyncSession = Depends(get_async_db)):
    """一键重试所有失败的任务"""
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
    for task in failed_tasks:
        download_single_file.delay(task.id)

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
    for task in tasks:
        # 清理 Redis 进度
        redis_client.delete_progress(task.id)
        await db.delete(task)

    await db.commit()

    redis_client.append_activity_log("info", "api",
        f"🗑️ 批量删除 {count} 个 {status} 状态的任务", "")

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
    for task in active_tasks:
        redis_client.pause_task(task.id)
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
        cookie = redis_client.get_cookie() or settings.DOUYIN_COOKIE
        if not cookie:
            raise HTTPException(status_code=400, detail="请先配置抖音 Cookie")

        import asyncio
        runtime_config = await get_runtime_config(db)

        def _refresh():
            downloader = DouyinDownloader(cookie, settings.DOWNLOAD_DIR, runtime_config=runtime_config)
            return downloader.refresh_work_urls(work.aweme_id)

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

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"刷新下载链接失败: {str(e)}")

    # 重置任务状态
    task.status = "pending"
    task.error_message = None
    redis_client.delete_progress(task_id)

    await db.commit()

    download_single_file.delay(task_id)

    return MessageResponse(success=True, message=f"已刷新下载链接并重新提交任务（作品: {work.aweme_id}）")


@router.post("/refresh-retry-all-failed", response_model=MessageResponse)
async def refresh_retry_all_failed(db: AsyncSession = Depends(get_async_db)):
    """重新获取所有失败任务的下载链接后重试"""
    result = await db.execute(
        select(DownloadTask).where(DownloadTask.status.in_(["failed", "cancelled"]))
    )
    failed_tasks = result.scalars().all()

    if not failed_tasks:
        return MessageResponse(success=True, message="没有失败的任务需要重试", data={"count": 0})

    cookie = redis_client.get_cookie() or settings.DOUYIN_COOKIE
    if not cookie:
        raise HTTPException(status_code=400, detail="请先配置抖音 Cookie")

    import asyncio
    runtime_config = await get_runtime_config(db)

    def _create_downloader():
        return DouyinDownloader(cookie, settings.DOWNLOAD_DIR, runtime_config=runtime_config)
    downloader = await asyncio.to_thread(_create_downloader)

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
                return downloader.refresh_work_urls(aweme_id)
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
        except Exception:
            refreshed_works[wid] = False

    count = 0
    for task in failed_tasks:
        task.status = "pending"
        task.error_message = None
        redis_client.delete_progress(task.id)
        count += 1

    await db.commit()

    for task in failed_tasks:
        download_single_file.delay(task.id)

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
    for task in pending_tasks:
        download_single_file.delay(task.id)

    redis_client.append_activity_log("info", "api",
        f"🔄 重新分发 {count} 个 pending 任务到队列",
        f"task_ids={[t.id for t in pending_tasks[:20]]}{'...' if count > 20 else ''}")

    return MessageResponse(
        success=True,
        message=f"已重新分发 {count} 个待处理任务",
        data={"count": count}
    )
