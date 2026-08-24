"""
系统配置和历史记录 API

为什么这样设计：
1. 系统配置 API：管理 Cookie 等全局配置
2. 历史记录 API：查看已完成的下载
3. 状态检查 API：检查服务健康状态
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Body
from sqlalchemy import select, func, text, create_engine
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any, Dict, List, Optional
from pydantic import BaseModel
from pathlib import Path
import asyncio

from app.models.database import get_async_db
from app.models.models import Author, Work, DownloadTask, DownloadHistory, SystemConfig
from app.models.schemas import (
    DownloadHistoryResponse, CookieUpdate, SystemStatus, MessageResponse
)
from app.core import redis_client
from app.core.config import settings
from app.core import updater
from app.core.env_config import (
    ENV_FIELDS,
    FIELD_MAP,
    check_download_directory,
    get_env_values,
    read_env_file,
    validate_env,
    write_env_updates,
)
from app.core.network_security import validate_database_test_target
from app.core.runtime_config import (
    RUNTIME_CONFIG_ENV_KEYS,
    RUNTIME_CONFIG_SCHEMA,
    get_runtime_config,
    save_runtime_config as persist_runtime_config,
)

from app.services.media_paths import migrate_download_paths

router = APIRouter(tags=["系统管理"])


class RuntimeConfigUpdate(BaseModel):
    auto_check_enabled: Optional[bool] = None
    subscription_check_interval: Optional[int] = None
    douyin_request_delay: Optional[float] = None
    douyin_risk_cooldown_seconds: Optional[int] = None
    douyin_risk_auto_retry: Optional[bool] = None
    author_check_delay: Optional[float] = None
    download_timeout: Optional[int] = None
    download_retry_count: Optional[int] = None
    download_retry_delay: Optional[int] = None
    stuck_task_timeout: Optional[int] = None


class CompleteConfigUpdate(BaseModel):
    values: Dict[str, Any]


# ============ Cookie 配置 ============

@router.post("/config/cookie", response_model=MessageResponse)
async def update_cookie(
    request: CookieUpdate,
    db: AsyncSession = Depends(get_async_db)
):
    """更新抖音 Cookie"""
    # 存储到数据库（持久化）
    result = await db.execute(
        select(SystemConfig).where(SystemConfig.key == "douyin_cookie")
    )
    config = result.scalar_one_or_none()
    
    if config:
        config.value = request.cookie
    else:
        config = SystemConfig(key="douyin_cookie", value=request.cookie)
        db.add(config)

    try:
        await db.flush()
        await asyncio.to_thread(write_env_updates, {"DOUYIN_COOKIE": request.cookie})
        await db.commit()
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        await db.rollback()
        raise

    # 网页保存值优先，并同步到快速缓存。
    await asyncio.to_thread(redis_client.set_cookie, request.cookie)

    return MessageResponse(success=True, message="Cookie 已更新并同步到 .env")


@router.get("/config/cookie", response_model=MessageResponse)
async def get_cookie_status(db: AsyncSession = Depends(get_async_db)):
    """检查 Cookie 是否已配置"""
    cookie = await asyncio.to_thread(redis_client.get_cookie)
    
    if not cookie:
        result = await db.execute(
            select(SystemConfig).where(SystemConfig.key == "douyin_cookie")
        )
        config = result.scalar_one_or_none()
        cookie = config.value if config else None
    if not cookie:
        cookie = (await asyncio.to_thread(settings.snapshot)).DOUYIN_COOKIE
    
    return MessageResponse(
        success=bool(cookie),
        message="Cookie 已配置" if cookie else "Cookie 未配置",
        data={"configured": bool(cookie)}
    )


# ============ 运行期配置 ============

@router.get("/config/runtime")
async def get_runtime_settings(db: AsyncSession = Depends(get_async_db)):
    """获取可在设置页调整的运行期配置"""
    config = await get_runtime_config(db)
    limits = {
        key: {
            "label": spec.get("label", key),
            "type": spec.get("type"),
            "min": spec.get("min"),
            "max": spec.get("max"),
            "unit": spec.get("unit"),
            "default": spec.get("default"),
        }
        for key, spec in RUNTIME_CONFIG_SCHEMA.items()
    }
    return {
        "success": True,
        "config": config,
        "limits": limits,
        "service": await asyncio.to_thread(process_manager.get_status),
    }


@router.get("/system/douyin-risk-state")
async def get_douyin_risk_state():
    """供前端展示全局抖音风控冷却倒计时。"""
    state = await asyncio.to_thread(redis_client.get_douyin_risk_state)
    return {"success": True, **state}


@router.post("/config/runtime", response_model=MessageResponse)
async def update_runtime_settings(
    request: RuntimeConfigUpdate,
    db: AsyncSession = Depends(get_async_db),
):
    """更新运行期配置，保存到 system_config、.env 并同步缓存"""
    try:
        config = await persist_runtime_config(db, request.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await asyncio.to_thread(
        redis_client.append_activity_log,
        "info",
        "system",
        "运行配置已更新",
        ", ".join(sorted(request.model_dump(exclude_unset=True).keys())) or "无变化",
    )
    return MessageResponse(success=True, message="运行配置已保存并同步到 .env", data={"config": config})


@router.get("/config/all")
async def get_complete_settings(db: AsyncSession = Depends(get_async_db)):
    """返回设置中心使用的完整配置清单，敏感值始终脱敏。"""
    values = await asyncio.to_thread(get_env_values, mask_secret=True)
    runtime = await get_runtime_config(db)
    for runtime_key, env_key in RUNTIME_CONFIG_ENV_KEYS.items():
        if runtime_key in runtime and env_key in values:
            value = runtime[runtime_key]
            values[env_key]["value"] = "true" if value is True else "false" if value is False else str(value)

    return {
        "success": True,
        "fields": [field.model_dump() for field in ENV_FIELDS],
        "values": values,
        "runtime_keys": list(RUNTIME_CONFIG_ENV_KEYS.values()),
    }


@router.post("/config/all", response_model=MessageResponse)
async def save_complete_settings(
    request: CompleteConfigUpdate,
    db: AsyncSession = Depends(get_async_db),
):
    """统一保存全部网页配置，并同步运行时配置与平台 Cookie。"""
    updates = {key: value for key, value in request.values.items() if key in FIELD_MAP}
    if not updates:
        raise HTTPException(status_code=400, detail="没有可保存的配置")

    current = await asyncio.to_thread(read_env_file)
    for key in ("DOUYIN_DOWNLOAD_SUBDIR", "X_DOWNLOAD_SUBDIR"):
        if key not in updates:
            continue
        subdir = Path(str(updates[key]).strip())
        if subdir.is_absolute() or ".." in subdir.parts or not str(subdir).strip("./\\"):
            raise HTTPException(status_code=400, detail=f"{FIELD_MAP[key].label}必须是根目录下的相对子目录")
    for key, value in updates.items():
        field = FIELD_MAP[key]
        if field.required and not str(value if value is not None else "").strip():
            raise HTTPException(status_code=400, detail=f"{field.label}不能为空")

    download_error = await asyncio.to_thread(
        check_download_directory,
        {**current, **updates},
    )
    if download_error:
        raise HTTPException(status_code=400, detail=download_error["message"])

    runtime_by_env = {env_key: runtime_key for runtime_key, env_key in RUNTIME_CONFIG_ENV_KEYS.items()}
    runtime_updates = {
        runtime_by_env[key]: value
        for key, value in updates.items()
        if key in runtime_by_env
    }

    requested_admin_token = updates.get("ADMIN_TOKEN")
    requested_admin_token_text = str(requested_admin_token).strip() if requested_admin_token is not None else ""
    new_admin_token = (
        requested_admin_token_text
        if requested_admin_token_text not in {"", "********"}
        else None
    )

    try:
        if runtime_updates:
            await persist_runtime_config(db, runtime_updates)

        environment_updates = {key: value for key, value in updates.items() if key not in runtime_by_env}
        if environment_updates:
            await asyncio.to_thread(write_env_updates, environment_updates)
        legacy_douyin = current.get("DOWNLOAD_DIR")
        old_root = current.get("DOWNLOAD_ROOT") or (str(Path(legacy_douyin).parent) if legacy_douyin else "/downloads")
        old_douyin = legacy_douyin or str(Path(old_root) / current.get("DOUYIN_DOWNLOAD_SUBDIR", "douyin"))
        old_x = current.get("X_DOWNLOAD_DIR") or str(Path(old_root) / current.get("X_DOWNLOAD_SUBDIR", "X"))
        new_root = str(updates.get("DOWNLOAD_ROOT", old_root))
        new_douyin = str(Path(new_root) / str(updates.get("DOUYIN_DOWNLOAD_SUBDIR", current.get("DOUYIN_DOWNLOAD_SUBDIR", "douyin"))))
        new_x = str(Path(new_root) / str(updates.get("X_DOWNLOAD_SUBDIR", current.get("X_DOWNLOAD_SUBDIR", "X"))))
        path_changes = {
            "tasks": 0,
            "history": 0,
            "x_tasks": 0,
            "x_media": 0,
            "unresolved": {"tasks": 0, "history": 0, "x_tasks": 0, "x_media": 0},
            "unresolved_total": 0,
        }
        if old_douyin != new_douyin or old_x != new_x:
            path_changes = await migrate_download_paths(
                db,
                old_download_dir=old_douyin,
                new_download_dir=new_douyin,
                old_x_download_dir=old_x,
                new_x_download_dir=new_x,
            )

        cookie_keys = {"DOUYIN_COOKIE": "douyin_cookie", "X_COOKIE": "x_cookie"}
        for env_key, config_key in cookie_keys.items():
            value = updates.get(env_key)
            if value == "********":
                value = current.get(env_key, "")
            if value is None or not str(value).strip():
                continue
            result = await db.execute(select(SystemConfig).where(SystemConfig.key == config_key))
            config_row = result.scalar_one_or_none()
            if config_row:
                config_row.value = str(value).strip()
            else:
                db.add(SystemConfig(key=config_key, value=str(value).strip()))

        await db.commit()
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        await db.rollback()
        raise

    douyin_cookie = updates.get("DOUYIN_COOKIE")
    if douyin_cookie and douyin_cookie != "********":
        await asyncio.to_thread(redis_client.set_cookie, str(douyin_cookie).strip())
    x_cookie = updates.get("X_COOKIE")
    if x_cookie and x_cookie != "********":
        await asyncio.to_thread(redis_client.set_x_cookie, str(x_cookie).strip())

    await asyncio.to_thread(
        redis_client.append_activity_log,
        "info",
        "system",
        "设置中心配置已更新",
        ", ".join(sorted(updates)),
    )
    hot_reload_keys = {
        "DEBUG",
        "DOWNLOAD_ROOT",
        "DOUYIN_DOWNLOAD_SUBDIR",
        "X_DOWNLOAD_SUBDIR",
        "DB_TYPE",
        "DB_HOST",
        "DB_PORT",
        "DB_USER",
        "DB_PASSWORD",
        "DB_NAME",
        "REDIS_URL",
        "REDIS_PASSWORD",
        "DOWNLOAD_CHUNK_SIZE",
        "MIN_CHECK_INTERVAL",
        "X_DOWNLOAD_ENGINE",
        "X_COOKIE_FILE",
        "X_TASK_LOG_MAX_LINES",
        "X_TASK_LOG_TTL_SECONDS",
        "X_TASK_STATE_TTL_SECONDS",
    }
    restart_keys = sorted(
        key for key in updates
        if key not in RUNTIME_CONFIG_ENV_KEYS
        and key not in {
            "DOUYIN_COOKIE",
            "X_COOKIE",
            "ADMIN_TOKEN",
            "CORS_ALLOWED_ORIGINS",
            *hot_reload_keys,
        }
    )
    message = "配置已保存"
    unresolved_paths = int(path_changes.get("unresolved_total", 0))
    if unresolved_paths:
        message += f"；有 {unresolved_paths} 行媒体路径未能重定位，请检查目录内容"
    if restart_keys:
        message += "；部分基础配置需重启 Web、Worker 和 Beat 后生效"
    return MessageResponse(
        success=True,
        message=message,
        data={
            "restart_required": bool(restart_keys),
            "restart_keys": restart_keys,
            "migrated_paths": path_changes,
            "admin_token": new_admin_token,
        },
    )

# ============ 下载历史 ============

@router.get("/history", response_model=List[DownloadHistoryResponse])
async def list_download_history(
    author_id: Optional[int] = Query(None, description="按作者筛选"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_async_db)
):
    """获取下载历史列表"""
    query = select(DownloadHistory)
    
    if author_id:
        query = query.join(Work).where(Work.author_id == author_id)
    
    query = query.order_by(DownloadHistory.completed_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    
    result = await db.execute(query)
    history = result.scalars().all()
    
    return history


@router.get("/history/stats")
async def get_download_stats(db: AsyncSession = Depends(get_async_db)):
    """获取下载统计信息"""
    # 总下载数
    total_result = await db.execute(
        select(func.count(DownloadHistory.id))
    )
    total_downloads = total_result.scalar() or 0
    
    # 总文件大小
    size_result = await db.execute(
        select(func.sum(DownloadHistory.file_size))
    )
    total_size = size_result.scalar() or 0
    
    # 今日下载数
    from datetime import datetime, timedelta
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_result = await db.execute(
        select(func.count(DownloadHistory.id)).where(
            DownloadHistory.completed_at >= today
        )
    )
    today_downloads = today_result.scalar() or 0
    
    return {
        "total_downloads": total_downloads,
        "total_size_bytes": total_size,
        "total_size_human": format_size(total_size),
        "today_downloads": today_downloads
    }


# ============ 系统状态 ============

@router.get("/status", response_model=SystemStatus)
async def get_system_status(db: AsyncSession = Depends(get_async_db)):
    """获取统计、Redis、Celery 与本地进程的合并状态。"""
    try:
        redis_connected = await asyncio.to_thread(redis_client.check_connection)
    except Exception:
        redis_connected = False
    stat_keys = {
        "total_authors",
        "subscribed_authors",
        "pending_tasks",
        "downloading_tasks",
        "total_downloads",
    }
    # 这五个计数变化频繁，60 秒 Redis 缓存会让页面长期显示旧值。
    # 单条 SQL 直接读取当前数据库快照，前端轮询即可及时反映任务变化。
    stats_row = (await db.execute(select(
        select(func.count(Author.id)).scalar_subquery().label("total_authors"),
        select(func.count(Author.id)).where(Author.is_subscribed.is_(True)).scalar_subquery().label("subscribed_authors"),
        select(func.count(DownloadTask.id)).where(DownloadTask.status == "pending").scalar_subquery().label("pending_tasks"),
        select(func.count(DownloadTask.id)).where(DownloadTask.status == "downloading").scalar_subquery().label("downloading_tasks"),
        select(func.count(DownloadHistory.id)).scalar_subquery().label("total_downloads"),
    ))).mappings().one()
    stats = {key: int(stats_row[key] or 0) for key in stat_keys}

    # Celery workers (通过 broker ping，带超时)
    celery_workers = 0
    try:
        def _inspect():
            from app.tasks.celery_app import celery_app
            insp = celery_app.control.inspect(timeout=2.0)
            return insp.ping()
        ping_result = await asyncio.to_thread(_inspect)
        if ping_result:
            celery_workers = len(ping_result)
    except Exception:
        pass

    process_status = await asyncio.to_thread(process_manager.get_status)

    status = SystemStatus(
        redis_connected=redis_connected,
        celery_workers=celery_workers,
        **stats,
        worker_process_running=bool(process_status.get("worker", {}).get("running")),
        beat_process_running=bool(process_status.get("beat", {}).get("running")),
    )
    return status


@router.get("/health")
async def health_check():
    """健康检查接口"""
    return {"status": "healthy"}


@router.get("/celery-status")
async def get_celery_status():
    """
    专用 Celery Worker 状态检查
    
    通过 broker (Redis) 发送 ping，检测 Worker 是否在线。
    timeout=2s - Worker 在线时通常 <100ms 响应，2s 足以判定离线。
    """
    import asyncio

    def _ping():
        from app.tasks.celery_app import celery_app
        inspector = celery_app.control.inspect(timeout=2.0)
        return inspector.ping()

    try:
        ping = await asyncio.to_thread(_ping)
        if ping:
            workers = list(ping.keys())
            return {"online": True, "workers": workers, "count": len(workers)}
        return {"online": False, "workers": [], "count": 0}
    except Exception as e:
        return {"online": False, "workers": [], "count": 0, "error": str(e)[:200]}


@router.get("/celery-debug")
async def celery_debug():
    """
    Celery 深度诊断 — 检查队列、Worker 状态、注册任务、已有队列内容
    用于排查 "任务已提交但没有执行" 的问题
    """
    import asyncio

    def _diagnose():
        import redis as redis_lib
        from app.tasks.celery_app import celery_app
        diag = {}

        # 1. Broker URL（脱敏）
        broker = str(celery_app.conf.broker_url or "")
        if '@' in broker:
            at_idx = broker.index('@')
            colon_idx = broker.rfind(':', 0, at_idx)
            diag["broker_url"] = broker[:colon_idx+1] + '***' + broker[at_idx:]
        else:
            diag["broker_url"] = broker

        # 2. Redis 队列长度 — 关键！看任务是否积压在某个队列
        r = redis_lib.Redis.from_url(settings.redis_url_with_auth, decode_responses=True)
        diag["queue_lengths"] = {}
        for q in ["celery", "download", "scheduler", "default"]:
            try:
                qlen = r.llen(q)
                diag["queue_lengths"][q] = qlen
            except Exception:
                diag["queue_lengths"][q] = -1

        # 2b. 如果 celery 队列有任务，peek 第一条看格式
        try:
            first_msg = r.lindex("celery", 0)
            if first_msg:
                import json
                msg = json.loads(first_msg)
                diag["celery_queue_peek"] = {
                    "headers_task": msg.get("headers", {}).get("task", "?"),
                    "headers_id": msg.get("headers", {}).get("id", "?"),
                }
            else:
                diag["celery_queue_peek"] = None
        except Exception as e:
            diag["celery_queue_peek"] = f"error: {e}"

        # 3. Worker inspect（带超时）
        insp = celery_app.control.inspect(timeout=3.0)
        diag["ping"] = insp.ping() or {}

        reg = insp.registered() or {}
        # 只返回每个 Worker 的任务名列表
        diag["registered"] = {w: [t for t in tasks if not t.startswith("celery.")]
                              for w, tasks in reg.items()}

        aq = insp.active_queues() or {}
        diag["active_queues"] = {w: [q["name"] for q in queues] for w, queues in aq.items()}

        diag["active_tasks"] = insp.active() or {}
        diag["reserved_tasks"] = insp.reserved() or {}

        # 4. 配置信息
        diag["config"] = {
            "task_routes": dict(celery_app.conf.task_routes or {}),
            "task_default_queue": celery_app.conf.task_default_queue,
            "task_default_exchange": str(celery_app.conf.task_default_exchange),
            "worker_concurrency": celery_app.conf.worker_concurrency,
            "worker_prefetch_multiplier": celery_app.conf.worker_prefetch_multiplier,
            "task_serializer": celery_app.conf.task_serializer,
        }

        return diag

    try:
        result = await asyncio.to_thread(_diagnose)
        return result
    except Exception as e:
        import traceback as tb
        return {"error": str(e), "traceback": tb.format_exc()[:1000]}


@router.post("/celery-test")
async def run_celery_test():
    """
    提交一个极简测试任务到 Celery 队列，并等待最多 15 秒看是否执行。
    如果成功，说明 Worker 能正常消费队列；如果超时，说明任务链断裂。
    """
    import asyncio

    try:
        from app.tasks.celery_app import echo_test

        async_result = await asyncio.to_thread(echo_test.delay)
        task_id = async_result.id

        def _wait():
            try:
                return async_result.get(timeout=15)
            except Exception as e:
                return {"error": f"{type(e).__name__}: {str(e)[:200]}"}

        task_result = await asyncio.to_thread(_wait)
        ok = isinstance(task_result, dict) and task_result.get("ok") is True

        return {
            "success": ok,
            "task_id": task_id,
            "result": task_result,
            "message": "✅ Worker 正常工作！任务已成功执行。" if ok
                       else "❌ Worker 未在 15 秒内执行任务。请检查 Worker 进程是否正常。"
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"提交测试任务失败: {str(e)[:200]}"
        }


@router.post("/celery-purge-old")
async def purge_old_queues():
    """清除旧的 download/scheduler 队列中的积压任务"""
    import asyncio
    import redis as redis_lib

    def _purge():
        r = redis_lib.Redis.from_url(settings.redis_url_with_auth, decode_responses=True)
        purged = {}
        for q in ["download", "scheduler"]:
            length = r.llen(q)
            if length > 0:
                r.delete(q)
                purged[q] = length
            else:
                purged[q] = 0
        return purged

    try:
        result = await asyncio.to_thread(_purge)
        await asyncio.to_thread(
            redis_client.append_activity_log,
            "info",
            "system",
            "🗑️ 已清除旧队列",
            f"download={result.get('download', 0)}, scheduler={result.get('scheduler', 0)}",
        )
        return {"success": True, "purged": result}
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}


@router.get("/worker-log")
async def get_worker_log(lines: int = Query(100, ge=1, le=1000)):
    """读取 Worker 日志文件的最后 N 行"""
    log_path = "logs/download_tasks.log"
    if not Path(log_path).exists():
        return {"lines": [], "message": "日志文件不存在"}

    try:
        return await asyncio.to_thread(_tail_text_file, Path(log_path), lines)
    except Exception as e:
        return {"lines": [], "error": str(e)[:200]}


def _tail_text_file(path: Path, line_count: int, max_bytes: int = 512 * 1024) -> dict:
    """只读日志末尾固定字节，避免大日志整体进内存。"""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        start = max(0, size - max_bytes)
        handle.seek(start)
        data = handle.read(max_bytes)
    decoded = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and decoded:
        decoded = decoded[1:]
    tail = decoded[-line_count:]
    return {
        "lines": tail,
        "total_lines": len(decoded) if start == 0 else None,
        "truncated": start > 0,
        "sampled_bytes": len(data),
    }


# ============ 进度查询 ============

@router.get("/progress/active")
async def get_active_progress():
    """获取所有活跃任务的进度"""
    progress = await asyncio.to_thread(redis_client.get_all_progress)
    return {"tasks": progress}


# ============ 数据库配置 ============

class DatabaseConfig(BaseModel):
    db_type: str = "postgresql"
    db_host: str = ""
    db_port: int = 0
    db_user: str = ""
    db_password: str = ""
    db_name: str = ""


@router.get("/config/database")
async def get_database_config():
    """获取当前数据库配置（密码脱敏）"""
    current = await asyncio.to_thread(settings.snapshot)
    env_status = await asyncio.to_thread(validate_env)
    return {
        "db_type": current.DB_TYPE,
        "db_host": current.DB_HOST,
        "db_port": current.DB_PORT,
        "db_user": current.DB_USER,
        "db_name": current.DB_NAME,
        "db_password_set": bool(current.DB_PASSWORD),
        "env": env_status,
    }

@router.post("/config/database/test")
async def test_database_connection(cfg: DatabaseConfig):
    """测试数据库连接"""
    try:
        effective_port = cfg.db_port or (3306 if cfg.db_type == "mysql" else 5432)
        await asyncio.to_thread(validate_database_test_target, cfg.db_host, effective_port)
        current = await asyncio.to_thread(read_env_file)
        db_password = cfg.db_password or current.get("DB_PASSWORD", "")
        if cfg.db_type == "postgresql":
            user_part = cfg.db_user
            if db_password:
                user_part = f"{cfg.db_user}:{db_password}"
            url = f"postgresql://{user_part}@{cfg.db_host}:{cfg.db_port or 5432}/{cfg.db_name}"
        elif cfg.db_type == "mysql":
            user_part = cfg.db_user
            if db_password:
                user_part = f"{cfg.db_user}:{db_password}"
            url = f"mysql+pymysql://{user_part}@{cfg.db_host}:{cfg.db_port or 3306}/{cfg.db_name}?charset=utf8mb4"
        else:
            return {"success": False, "message": f"不支持的数据库类型: {cfg.db_type}"}

        def _test_connection():
            engine = create_engine(
                url,
                pool_pre_ping=True,
                connect_args={"connect_timeout": 3},
            )
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
            finally:
                engine.dispose()

        await asyncio.to_thread(_test_connection)
        return {"success": True, "message": "连接成功"}
    except Exception as e:
        return {"success": False, "message": f"连接失败: {str(e)[:300]}"}


@router.post("/config/database")
async def save_database_config(cfg: DatabaseConfig):
    """保存数据库配置到 .env 文件"""
    try:
        current = await asyncio.to_thread(read_env_file)
        updates = {
            "DB_TYPE": cfg.db_type,
            "DB_HOST": cfg.db_host,
            "DB_PORT": str(cfg.db_port or (5432 if cfg.db_type == "postgresql" else 3306)),
            "DB_USER": cfg.db_user,
            "DB_PASSWORD": cfg.db_password or current.get("DB_PASSWORD", ""),
            "DB_NAME": cfg.db_name,
        }
        db_password = updates["DB_PASSWORD"]
        if cfg.db_type == "postgresql":
            user_part = f"{cfg.db_user}:{db_password}" if db_password else cfg.db_user
            updates["DATABASE_URL"] = f"postgresql://{user_part}@{cfg.db_host}:{cfg.db_port or 5432}/{cfg.db_name}"
        elif cfg.db_type == "mysql":
            user_part = f"{cfg.db_user}:{db_password}" if db_password else cfg.db_user
            updates["DATABASE_URL"] = f"mysql+pymysql://{user_part}@{cfg.db_host}:{cfg.db_port or 3306}/{cfg.db_name}?charset=utf8mb4"
        else:
            return MessageResponse(success=False, message=f"不支持的数据库类型: {cfg.db_type}")
        await asyncio.to_thread(write_env_updates, updates)
        return MessageResponse(success=True, message="数据库配置已保存，下一次请求将自动使用新连接")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存配置失败: {str(e)}")

# ============ 工具函数 ============

def format_size(size_bytes: int) -> str:
    """格式化文件大小"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.2f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.2f} MB"
    else:
        return f"{size_bytes / 1024 / 1024 / 1024:.2f} GB"


# ============ 活动日志 ============

@router.get("/logs")
async def get_logs(
    start: int = Query(0, ge=0),
    count: int = Query(100, ge=1, le=500),
):
    """获取活动日志"""
    logs = await asyncio.to_thread(redis_client.get_activity_logs, start, count)
    return {"logs": logs, "start": start, "count": len(logs)}


@router.delete("/logs")
async def clear_logs():
    """清空活动日志"""
    await asyncio.to_thread(redis_client.clear_activity_logs)
    return MessageResponse(success=True, message="日志已清空")


# ============ Celery 进程管理 ============

from app.core.process_manager import process_manager


@router.get("/process/status")
async def get_process_status():
    """获取 Worker 和 Beat 进程状态"""
    return await asyncio.to_thread(process_manager.get_status)


@router.post("/process/worker/start")
async def start_worker(concurrency: Optional[int] = Body(None, embed=True)):
    """启动 Celery Worker"""
    result = await asyncio.to_thread(process_manager.start_worker, concurrency)
    if result.get("success"):
        await asyncio.to_thread(
            redis_client.append_activity_log,
            "info", "system", "▶️ Worker 已启动",
            f"PID={result.get('pid')}, concurrency={result.get('concurrency')}",
        )
    return result


@router.post("/process/worker/stop")
async def stop_worker():
    """停止 Celery Worker"""
    result = await asyncio.to_thread(process_manager.stop_worker)
    if result.get("success"):
        await asyncio.to_thread(
            redis_client.append_activity_log, "info", "system", "⏹️ Worker 已停止", result["message"]
        )
    return result


@router.post("/process/beat/start")
async def start_beat():
    """启动 Celery Beat 定时调度器"""
    result = await asyncio.to_thread(process_manager.start_beat)
    if result.get("success"):
        await asyncio.to_thread(
            redis_client.append_activity_log, "info", "system", "▶️ Beat 已启动", result["message"]
        )
    return result


@router.post("/process/beat/stop")
async def stop_beat():
    """停止 Celery Beat"""
    result = await asyncio.to_thread(process_manager.stop_beat)
    if result.get("success"):
        await asyncio.to_thread(
            redis_client.append_activity_log, "info", "system", "⏹️ Beat 已停止", result["message"]
        )
    return result


class ConcurrencyUpdate(BaseModel):
    concurrency: int


@router.get("/process/concurrency")
async def get_concurrency():
    """获取当前最大并发下载数"""
    return {"concurrency": await asyncio.to_thread(lambda: process_manager.worker_concurrency)}


@router.post("/process/concurrency")
async def set_concurrency(body: ConcurrencyUpdate):
    """修改最大并发下载数（会重启 Worker 生效）"""
    new_val = max(1, min(body.concurrency, 20))

    # 更新 .env 持久化
    await asyncio.to_thread(_update_env_key, "MAX_CONCURRENT_DOWNLOADS", str(new_val))

    # 重启 Worker 使新并发数生效
    result = await asyncio.to_thread(process_manager.restart_worker, new_val)
    await asyncio.to_thread(
        redis_client.append_activity_log,
        "info", "system", f"🔄 并发数已调整为 {new_val}", result["message"],
    )
    return {"success": True, "concurrency": new_val, "message": result["message"]}


def _update_env_key(key: str, value: str):
    """通过统一配置写入器更新 .env，确保带空格值可被 Bash source。"""
    write_env_updates({key: value})


# ============ 版本更新（Git） ============

@router.get("/update/info")
async def get_update_info():
    """读取当前版本信息（不联网，仅基于本地仓库状态）"""
    import asyncio
    try:
        return await asyncio.to_thread(updater.check_update, False)
    except updater.GitUpdateError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取版本信息出错：{type(e).__name__}: {str(e)[:300]}")


@router.get("/update/check")
async def check_update():
    """检查远程仓库是否有可用更新（会执行 git fetch 联网比较）"""
    import asyncio
    try:
        return await asyncio.to_thread(updater.check_update, True)
    except updater.GitUpdateError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"检查更新出错：{type(e).__name__}: {str(e)[:300]}")


@router.get("/update/diagnose")
async def diagnose_update():
    """返回版本更新的诊断信息（检测到的项目目录、git 环境、运行用户、原始命令输出）"""
    import asyncio
    try:
        return await asyncio.to_thread(updater.diagnose)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"诊断出错：{type(e).__name__}: {str(e)[:300]}")


@router.post("/update/apply")
async def apply_update():
    """拉取远程仓库最新代码（git pull --ff-only），成功后重启 Worker/Beat 以加载新代码"""
    import asyncio
    try:
        result = await asyncio.to_thread(updater.apply_update)
    except updater.GitUpdateError as e:
        await asyncio.to_thread(
            redis_client.append_activity_log, "error", "system", "❌ 版本更新失败", str(e)[:300]
        )
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新出错：{str(e)[:300]}")

    restart_msgs = []
    if result.get("updated"):
        before_short = (result.get("before") or {}).get("short", "")
        after_short = (result.get("after") or {}).get("short", "")
        await asyncio.to_thread(
            redis_client.append_activity_log,
            "info", "system", "✅ 已拉取远程更新", f"{before_short} → {after_short}",
        )
        # 重启后台进程以加载新代码
        try:
            worker_result = await asyncio.to_thread(process_manager.restart_worker)
            restart_msgs.append(worker_result.get("message", ""))
        except Exception as e:
            restart_msgs.append(f"Worker 重启失败: {str(e)[:150]}")
        try:
            await asyncio.to_thread(process_manager.stop_beat)
            beat_result = await asyncio.to_thread(process_manager.start_beat)
            restart_msgs.append(beat_result.get("message", ""))
        except Exception as e:
            restart_msgs.append(f"Beat 重启失败: {str(e)[:150]}")

    result["restart"] = [m for m in restart_msgs if m]
    result["restart_note"] = (
        "后台 Worker/Beat 已重启。前端页面刷新即可生效；若更新涉及 Web/接口代码，"
        "请点击「重启 Web 服务」或在宝塔面板重启本项目以完全生效。"
    )
    return result


def _read_proc_cmdline(pid: int) -> str:
    """读取 /proc/<pid>/cmdline（仅 Linux），失败返回空串。"""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except Exception:
        return ""


@router.post("/service/restart")
async def restart_web_service():
    """
    热重启 Web 服务（gunicorn）。

    通过向 gunicorn 主进程（当前进程的父进程）发送 SIGHUP 触发优雅重载：
    gunicorn 会以最新代码启动新 worker 并平滑替换旧 worker。
    仅在确认父进程确为 gunicorn 时才发送信号，避免误伤其它托管方式。
    """
    import os
    import signal

    ppid = os.getppid()
    cmdline = _read_proc_cmdline(ppid)

    if "gunicorn" not in cmdline.lower():
        return {
            "success": False,
            "reload_supported": False,
            "message": (
                "未检测到 gunicorn 主进程，无法自动热重载。"
                f"（父进程: {cmdline[:120] or ppid}）请在宝塔面板手动重启本项目。"
            ),
        }

    try:
        os.kill(ppid, signal.SIGHUP)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"发送热重载信号失败：{str(e)[:200]}")

    await asyncio.to_thread(
        redis_client.append_activity_log,
        "info", "system", "🔄 已向 gunicorn 发送热重载信号 (SIGHUP)", f"master_pid={ppid}",
    )
    return {
        "success": True,
        "reload_supported": True,
        "master_pid": ppid,
        "message": (
            "已向 gunicorn 主进程发送热重载 (SIGHUP)，数秒内将以最新代码重启工作进程。"
            "若配置了 --preload 则热重载不生效，需在宝塔面板重启本项目。"
        ),
    }
