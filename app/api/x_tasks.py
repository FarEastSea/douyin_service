"""
X/Twitter 下载任务 API 路由
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from typing import Optional
import os
import signal

from app.models.database import get_async_db
from app.models.models import XDownloadTask, XAuthor, SystemConfig
from app.models.schemas import (
    XDownloadRequest, XDownloadTaskResponse, PaginatedXTasksResponse,
    XCookieUpdate, MessageResponse,
    XAuthorCreate, XAuthorResponse, PaginatedXAuthorsResponse,
)
from app.tasks.x_download_tasks import download_x_profile, check_x_subscriptions
from app.services.x_cookie_manager import X_COOKIE_CONFIG_KEY
from app.services.x_profile import parse_x_username, normalize_x_profile_url
from app.services.x_task_service import (
    ACTIVE_X_TASK_STATUSES,
    cancel_x_task as cancel_x_task_record,
    create_x_author,
    create_x_download_task,
    prepare_x_task_for_retry,
    serialize_x_author,
    serialize_x_task,
    sync_x_author,
)
from app.core import redis_client

router = APIRouter(prefix="/x", tags=["X/Twitter 下载"])


@router.post("/download", response_model=XDownloadTaskResponse)
async def create_x_download(
    request: XDownloadRequest,
    db: AsyncSession = Depends(get_async_db),
):
    """创建 X/Twitter 下载任务，自动添加用户到用户管理"""
    try:
        username = parse_x_username(request.profile_url)
        profile_url = normalize_x_profile_url(request.profile_url, username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = await db.execute(select(XAuthor).where(XAuthor.username == username))
    author = result.scalar_one_or_none()
    if not author:
        author = create_x_author(username, profile_url)
        db.add(author)
        await db.flush()
    else:
        sync_x_author(author, profile_url=profile_url)

    active_task_result = await db.execute(
        select(XDownloadTask)
        .options(selectinload(XDownloadTask.x_author))
        .where(
            XDownloadTask.x_author_id == author.id,
            XDownloadTask.status.in_(ACTIVE_X_TASK_STATUSES),
        )
        .order_by(XDownloadTask.created_at.desc())
    )
    active_task = active_task_result.scalars().first()
    if active_task:
        return serialize_x_task(active_task, redis_client.get_x_task_state(active_task.id))

    task = create_x_download_task(author)
    db.add(task)
    await db.commit()
    await db.refresh(task)

    download_x_profile.delay(task.id)

    return serialize_x_task(task)


@router.get("/tasks", response_model=PaginatedXTasksResponse)
async def list_x_tasks(
    status: Optional[str] = Query(None, description="按状态筛选"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_async_db),
):
    """获取 X 下载任务列表"""
    base_query = select(XDownloadTask).options(selectinload(XDownloadTask.x_author))
    count_query = select(func.count(XDownloadTask.id))

    if status:
        base_query = base_query.where(XDownloadTask.status == status)
        count_query = count_query.where(XDownloadTask.status == status)

    total_result = await db.execute(count_query)
    total = total_result.scalar()

    query = base_query.order_by(XDownloadTask.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    tasks = result.scalars().all()

    pages = (total + page_size - 1) // page_size if total > 0 else 1

    return PaginatedXTasksResponse(
        items=[serialize_x_task(task, redis_client.get_x_task_state(task.id)) for task in tasks],
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


@router.get("/tasks/{task_id}", response_model=XDownloadTaskResponse)
async def get_x_task(task_id: int, db: AsyncSession = Depends(get_async_db)):
    """获取单个 X 任务详情"""
    result = await db.execute(
        select(XDownloadTask)
        .options(selectinload(XDownloadTask.x_author))
        .where(XDownloadTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return serialize_x_task(task, redis_client.get_x_task_state(task.id))


@router.get("/tasks/{task_id}/log")
async def get_x_task_log(
    task_id: int,
    start: int = Query(0, ge=0, description="从第几行开始"),
):
    """获取 X 任务实时日志（从 Redis）"""
    lines = redis_client.get_x_task_log(task_id, start)
    return {
        "task_id": task_id,
        "start": start,
        "lines": lines,
        "total": redis_client.get_x_task_log_size(task_id),
    }


@router.delete("/tasks/{task_id}", response_model=MessageResponse)
async def delete_x_task(task_id: int, db: AsyncSession = Depends(get_async_db)):
    """删除 X 下载任务"""
    result = await db.execute(
        select(XDownloadTask).where(XDownloadTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status == "downloading":
        _kill_x_task(task_id)

    redis_client.delete_x_task_log(task_id)
    redis_client.delete_x_task_pid(task_id)
    redis_client.delete_x_task_state(task_id)

    await db.delete(task)
    await db.commit()
    return MessageResponse(success=True, message="任务已删除")


@router.post("/tasks/{task_id}/cancel", response_model=MessageResponse)
async def cancel_x_task(task_id: int, db: AsyncSession = Depends(get_async_db)):
    """取消 X 下载任务"""
    result = await db.execute(
        select(XDownloadTask).where(XDownloadTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status not in ("downloading", "pending"):
        raise HTTPException(status_code=400, detail=f"任务状态为 {task.status}，无法取消")

    _kill_x_task(task_id)
    cancel_x_task_record(task)
    await db.commit()
    redis_client.delete_x_task_state(task_id)
    return MessageResponse(success=True, message="任务已取消")


@router.post("/tasks/{task_id}/retry", response_model=MessageResponse)
async def retry_x_task(task_id: int, db: AsyncSession = Depends(get_async_db)):
    """重试失败的 X 下载任务"""
    result = await db.execute(
        select(XDownloadTask).where(XDownloadTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status not in ("failed", "cancelled"):
        raise HTTPException(status_code=400, detail=f"任务状态为 {task.status}，无需重试")

    prepare_x_task_for_retry(task)
    await db.commit()

    redis_client.delete_x_task_log(task_id)
    redis_client.delete_x_task_state(task_id)
    download_x_profile.delay(task_id)

    return MessageResponse(success=True, message="任务已重新提交")


# ============ X 作者管理 ============

@router.post("/authors/", response_model=XAuthorResponse)
async def add_x_author(
    request: XAuthorCreate,
    db: AsyncSession = Depends(get_async_db),
):
    """添加 X 用户"""
    try:
        username = parse_x_username(request.profile_url)
        profile_url = normalize_x_profile_url(request.profile_url, username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = await db.execute(
        select(XAuthor).where(XAuthor.username == username)
    )
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail=f"用户 @{username} 已存在")

    author = create_x_author(
        username,
        profile_url,
        is_subscribed=request.is_subscribed,
        check_interval=request.check_interval,
    )
    db.add(author)
    await db.commit()
    await db.refresh(author)
    return serialize_x_author(author)


@router.get("/authors/", response_model=PaginatedXAuthorsResponse)
async def list_x_authors(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_async_db),
):
    """获取 X 用户列表"""
    count_result = await db.execute(select(func.count(XAuthor.id)))
    total = count_result.scalar()

    query = select(XAuthor).order_by(XAuthor.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    authors = result.scalars().all()

    pages = (total + page_size - 1) // page_size if total > 0 else 1
    return PaginatedXAuthorsResponse(
        items=[serialize_x_author(author) for author in authors],
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


@router.delete("/authors/{author_id}", response_model=MessageResponse)
async def delete_x_author(author_id: int, db: AsyncSession = Depends(get_async_db)):
    """删除 X 用户"""
    result = await db.execute(select(XAuthor).where(XAuthor.id == author_id))
    author = result.scalar_one_or_none()
    if not author:
        raise HTTPException(status_code=404, detail="用户不存在")
    await db.delete(author)
    await db.commit()
    return MessageResponse(success=True, message=f"已删除 @{author.username}")


@router.post("/authors/{author_id}/subscribe", response_model=MessageResponse)
async def subscribe_x_author(author_id: int, db: AsyncSession = Depends(get_async_db)):
    """订阅 X 用户"""
    result = await db.execute(select(XAuthor).where(XAuthor.id == author_id))
    author = result.scalar_one_or_none()
    if not author:
        raise HTTPException(status_code=404, detail="用户不存在")
    author.is_subscribed = True
    await db.commit()
    return MessageResponse(success=True, message=f"已订阅 @{author.username}")


@router.post("/authors/{author_id}/unsubscribe", response_model=MessageResponse)
async def unsubscribe_x_author(author_id: int, db: AsyncSession = Depends(get_async_db)):
    """取消订阅 X 用户"""
    result = await db.execute(select(XAuthor).where(XAuthor.id == author_id))
    author = result.scalar_one_or_none()
    if not author:
        raise HTTPException(status_code=404, detail="用户不存在")
    author.is_subscribed = False
    await db.commit()
    return MessageResponse(success=True, message=f"已取消订阅 @{author.username}")


@router.post("/authors/{author_id}/download", response_model=MessageResponse)
async def download_x_author(
    author_id: int,
    db: AsyncSession = Depends(get_async_db),
):
    """手动触发下载 X 用户媒体"""
    result = await db.execute(select(XAuthor).where(XAuthor.id == author_id))
    author = result.scalar_one_or_none()
    if not author:
        raise HTTPException(status_code=404, detail="用户不存在")

    active_task_result = await db.execute(
        select(XDownloadTask).where(
            XDownloadTask.x_author_id == author.id,
            XDownloadTask.status.in_(ACTIVE_X_TASK_STATUSES),
        )
    )
    active_task = active_task_result.scalars().first()
    if active_task:
        return MessageResponse(success=True, message=f"@{author.username} 已有进行中的下载任务")

    task = create_x_download_task(author)
    db.add(task)
    await db.commit()
    await db.refresh(task)

    download_x_profile.delay(task.id)
    return MessageResponse(success=True, message=f"已创建 @{author.username} 下载任务")


@router.post("/authors/check-all", response_model=MessageResponse)
async def check_all_x_subscriptions():
    """检查所有 X 订阅用户更新"""
    check_x_subscriptions.delay()
    return MessageResponse(success=True, message="正在检查 X 订阅更新...")


# ============ X Cookie 管理 ============

@router.post("/config/cookie", response_model=MessageResponse)
async def save_x_cookie(
    request: XCookieUpdate,
    db: AsyncSession = Depends(get_async_db),
):
    """保存 X Cookie 内容"""
    cookie = request.cookie.strip()
    if not cookie:
        raise HTTPException(status_code=400, detail="Cookie 内容不能为空")

    redis_client.set_x_cookie(cookie)

    result = await db.execute(
        select(SystemConfig).where(SystemConfig.key == X_COOKIE_CONFIG_KEY)
    )
    config = result.scalar_one_or_none()
    if config:
        config.value = cookie
    else:
        db.add(SystemConfig(key=X_COOKIE_CONFIG_KEY, value=cookie))
    await db.commit()

    return MessageResponse(success=True, message="X Cookie 已保存")


@router.get("/config/cookie")
async def check_x_cookie(db: AsyncSession = Depends(get_async_db)):
    """检查 X Cookie 配置状态"""
    cookie = redis_client.get_x_cookie()
    if not cookie:
        result = await db.execute(
            select(SystemConfig).where(SystemConfig.key == X_COOKIE_CONFIG_KEY)
        )
        config = result.scalar_one_or_none()
        cookie = config.value if config else None

    configured = bool(cookie and len(cookie.strip()) > 0)
    return {"configured": configured}


def _kill_x_task(task_id: int):
    """尝试终止 X 下载子进程"""
    pid = redis_client.get_x_task_pid(task_id)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        redis_client.delete_x_task_pid(task_id)
