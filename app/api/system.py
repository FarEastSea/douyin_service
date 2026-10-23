"""
系统配置和历史记录 API

为什么这样设计：
1. 系统配置 API：管理 Cookie 等全局配置
2. 历史记录 API：查看已完成的下载
3. 状态检查 API：检查服务健康状态
"""

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy import select, func, text, create_engine
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field
from pathlib import Path
import asyncio
import json

from app.models.database import get_async_db
from app.models.models import Author, Work, DownloadTask, DownloadHistory, SystemConfig
from app.models.schemas import (
    DownloadHistoryResponse, CookieUpdate, SystemStatus, MessageResponse
)
from app.core import redis_client
from app.core.config import settings
from app.core import updater
from app.services.douyin_cookie import require_douyin_uifid
from app.services.platform_credentials import save_platform_cookie
from app.services.platform_profile_download import PROFILE_PLATFORM_SPECS
from app.services.douyin_account import get_account_status, save_account_profile
from app.services.douyin_errors import douyin_error_type_label, localize_douyin_reason
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
from app.core.health import build_readiness
from app.core.runtime_config import (
    RUNTIME_CONFIG_ENV_KEYS,
    RUNTIME_CONFIG_SCHEMA,
    get_runtime_schema,
    get_runtime_config,
    save_runtime_config as persist_runtime_config,
)

from app.services.media_paths import migrate_download_paths
from app.services.notifications import send_notification
from app.services.archive_rules import get_archive_rules, save_archive_rules
from app.core.queue_configuration import QueueDrainRequired, resuming_change, stage_queue_change
from app.core.configuration_service import save_archive_configuration, save_douyin_account, record_configuration_activity

router = APIRouter(tags=["系统管理"])

GENERIC_PLATFORM_CONFIG = {
    platform: (
        spec.download_subdir_env_key,
        spec.default_download_subdir,
        spec.cookie_env_key,
    )
    for platform, spec in PROFILE_PLATFORM_SPECS.items()
}
GENERIC_PLATFORM_COOKIE_KEYS = {
    cookie_key for _, _, cookie_key in GENERIC_PLATFORM_CONFIG.values()
}


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
    subscription_known_streak: Optional[int] = None
    subscription_max_pages: Optional[int] = None
    subscription_safe_lookback_pages: Optional[int] = None
    subscription_full_reconcile_interval: Optional[int] = None


class CompleteConfigUpdate(BaseModel):
    values: Dict[str, Any]


class NotificationTestRequest(BaseModel):
    channel: Literal["all", "webhook", "bark", "email", "gotify"] = "all"


class DouyinAccountUpdate(BaseModel):
    cookie: Optional[str] = None
    user_agent: Optional[str] = None
    proxy_enabled: Optional[bool] = None
    proxy_url: Optional[str] = None


class ArchiveRulesUpdate(BaseModel):
    directory_template: str
    filename_template: str
    work_types: List[Literal["video", "images"]]
    published_from: Optional[str] = None
    published_to: Optional[str] = None
    min_file_size_mb: float = 0
    max_file_size_mb: float = 0
    metadata_formats: List[Literal["json", "csv"]] = Field(default_factory=list)


@router.get("/config/archive-rules")
async def read_archive_rules(db: AsyncSession = Depends(get_async_db)):
    try:
        return {"success": True, "rules": await get_archive_rules(db)}
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/config/archive-rules")
async def update_archive_rules(
    request: ArchiveRulesUpdate,
    db: AsyncSession = Depends(get_async_db),
):
    try:
        rules = await save_archive_configuration(db, request.model_dump())
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_configuration_activity(
        "归档规则已更新",
        f"directory={rules['directory_template']}, metadata={','.join(rules['metadata_formats']) or 'none'}",
    )
    return {"success": True, "message": "归档规则已保存；仅影响新建任务", "rules": rules}


# ============ Cookie 配置 ============

@router.post("/config/cookie", response_model=MessageResponse)
async def update_cookie(
    request: CookieUpdate,
    db: AsyncSession = Depends(get_async_db)
):
    """更新抖音 Cookie"""
    try:
        status = await save_douyin_account(
            db, cookie=request.cookie, user_agent=None,
            proxy_enabled=None, proxy_url=None,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return MessageResponse(
        success=True,
        message="Cookie 已加密保存并绑定到默认请求上下文",
        data=status,
    )


@router.get("/config/cookie", response_model=MessageResponse)
async def get_cookie_status(db: AsyncSession = Depends(get_async_db)):
    """检查 Cookie 是否已配置"""
    status = await get_account_status(db)
    return MessageResponse(
        success=bool(status.get("configured")) and bool(status.get("has_uifid")),
        message=status.get("status_label") or "未配置",
        data=status,
    )


@router.get("/config/douyin-account")
async def get_douyin_account(db: AsyncSession = Depends(get_async_db)):
    return {"success": True, "account": await get_account_status(db)}


@router.post("/config/douyin-account", response_model=MessageResponse)
async def update_douyin_account(
    request: DouyinAccountUpdate,
    db: AsyncSession = Depends(get_async_db),
):
    try:
        status = await save_douyin_account(
            db,
            cookie=request.cookie,
            user_agent=request.user_agent,
            proxy_enabled=request.proxy_enabled,
            proxy_url=request.proxy_url,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    await record_configuration_activity(
        "抖音账号请求上下文已更新",
        f"proxy_enabled={status.get('proxy_enabled')}, status={status.get('status')}",
    )
    return MessageResponse(
        success=True,
        message="账号请求上下文已加密保存，下次请求动态生效",
        data=status,
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
        for key, spec in get_runtime_schema().items()
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
    normalized_error_type = state.get("error_type")
    if "uifid not found" in str(state.get("reason") or "").lower():
        normalized_error_type = "browser_identity_missing"
    return {
        "success": True,
        **state,
        "error_type_label": douyin_error_type_label(normalized_error_type),
        "reason_label": localize_douyin_reason(
            normalized_error_type, state.get("reason")
        ),
        "requires_cookie_update": normalized_error_type == "browser_identity_missing",
        "requires_account_update": normalized_error_type in {
            "account_isolated", "browser_identity_missing",
        },
    }


@router.post("/config/runtime", response_model=MessageResponse)
async def update_runtime_settings(
    request: RuntimeConfigUpdate,
    db: AsyncSession = Depends(get_async_db),
):
    """更新运行期配置，保存到 system_config、.env 并同步缓存"""
    try:
        from app.core.configuration_service import save_configuration
        updates = request.model_dump(exclude_unset=True)
        config = await get_runtime_config(db)
        saved = await save_configuration(db, {RUNTIME_CONFIG_ENV_KEYS[key]: value for key, value in updates.items()
                                      if key in RUNTIME_CONFIG_ENV_KEYS and value is not None})
        config.update({key: value for key, value in updates.items() if value is not None})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await record_configuration_activity(
        "运行配置已更新",
        ", ".join(sorted(request.model_dump(exclude_unset=True).keys())) or "无变化",
    )
    return MessageResponse(success=True, message=saved.message,
                           data={"config": config, "configuration_state": (saved.data or {}).get("configuration_state", "applied")})


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
    preview: bool = Query(False),
):
    from app.core.configuration_service import save_configuration
    return await save_configuration(db, request.values, preview=preview is True)



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

async def _read_live_stats(db: AsyncSession) -> dict[str, int]:
    """直接读取会频繁变化的首页统计，不掺杂外部服务健康检查。"""
    stat_keys = {
        "total_authors",
        "subscribed_authors",
        "pending_tasks",
        "downloading_tasks",
        "total_downloads",
    }
    stats_row = (await db.execute(select(
        select(func.count(Author.id)).scalar_subquery().label("total_authors"),
        select(func.count(Author.id)).where(Author.is_subscribed.is_(True)).scalar_subquery().label("subscribed_authors"),
        select(func.count(DownloadTask.id)).where(DownloadTask.status == "pending").scalar_subquery().label("pending_tasks"),
        select(func.count(DownloadTask.id)).where(DownloadTask.status == "downloading").scalar_subquery().label("downloading_tasks"),
        select(func.count(DownloadHistory.id)).scalar_subquery().label("total_downloads"),
    ))).mappings().one()
    return {key: int(stats_row[key] or 0) for key in stat_keys}


@router.get("/status/stats")
async def get_live_stats(db: AsyncSession = Depends(get_async_db)):
    """供页面轮询的轻量实时统计接口。"""
    return await _read_live_stats(db)


@router.get("/status", response_model=SystemStatus)
async def get_system_status(db: AsyncSession = Depends(get_async_db)):
    """获取统计、Redis、Celery 与本地进程的合并状态。"""
    try:
        redis_connected = await asyncio.to_thread(redis_client.check_connection)
    except Exception:
        redis_connected = False
    stats = await _read_live_stats(db)

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


@router.post("/notifications/test", response_model=MessageResponse)
async def test_notification(request: NotificationTestRequest):
    """使用已保存配置测试通知；敏感值和带 Token 的请求地址不回显。"""
    channels = None if request.channel == "all" else [request.channel]
    result = await asyncio.to_thread(
        send_notification,
        "test",
        "媒体下载管理系统测试通知",
        "如果你收到这条消息，说明当前通知渠道配置可用。",
        level="info",
        channels=channels,
        force=True,
    )
    success = result["sent"] > 0
    await asyncio.to_thread(
        redis_client.append_activity_log,
        "info" if success else "warning",
        "system",
        "通知测试完成" if success else "通知测试未发送",
        json.dumps(result.get("channels", {}), ensure_ascii=False),
    )
    return MessageResponse(
        success=success,
        message=f"已成功发送 {result['sent']} 个通知渠道" if success else "没有渠道发送成功，请检查已保存的通知配置",
        data=result,
    )
@router.get("/status/readiness")
async def get_readiness(request: Request):
    """返回与发布验收一致的核心依赖状态。"""
    return await build_readiness(degraded_mode=bool(request.app.state.degraded_mode))


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
    result = await asyncio.to_thread(process_manager.get_status)
    from app.core.queue_configuration import change_status
    result["configuration"] = await asyncio.to_thread(change_status)
    return result


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


@router.get("/config/queue-change")
async def get_queue_change():
    from app.core.queue_configuration import change_status, pending_path
    status = await asyncio.to_thread(change_status)
    return {**status, "pending": pending_path().exists()}


@router.post("/config/queue-change/cancel")
async def cancel_queue_change():
    from app.core.config_transaction import configuration_session, atomic_json
    from app.core.queue_configuration import pending_path, state_path
    async with configuration_session():
        from app.core.configuration_service import _require_writable_configuration
        _require_writable_configuration()
        pending_path().unlink(missing_ok=True)
        status = await asyncio.to_thread(process_manager.get_status)
        result = (await asyncio.to_thread(process_manager.start_beat)
                  if status.get("beat", {}).get("desired", True) else {"success": True})
        if not result.get("success"):
            await asyncio.to_thread(atomic_json, state_path(), {"state": "failed", "message": "变更已撤回，但调度未恢复，请在运行维护查看进程状态"})
            raise HTTPException(status_code=503, detail="变更已撤回，但调度启动失败")
        await asyncio.to_thread(atomic_json, state_path(), {"state": "applied", "message": "待生效变更已撤回，继续使用当前配置"})
    return MessageResponse(success=True, message="待生效变更已撤回")


@router.post("/process/concurrency")
async def set_concurrency(body: ConcurrencyUpdate, db: AsyncSession = Depends(get_async_db)):
    if not 1 <= body.concurrency <= 20:
        raise HTTPException(status_code=400, detail="并发数必须为 1 到 20")
    result = await save_complete_settings(CompleteConfigUpdate(values={"MAX_CONCURRENT_DOWNLOADS": body.concurrency}), db)
    return {"success": result.success, "concurrency": body.concurrency, "message": result.message}


def _update_env_key(key: str, value: str):
    """通过统一配置写入器更新 .env，确保带空格值可被 Bash source。"""
    write_env_updates({key: value})


# ============ 版本更新（Git） ============

@router.get("/update/info")
async def get_update_info():
    """读取当前版本信息（不联网，仅基于本地仓库状态）"""
    import asyncio
    try:
        result = await asyncio.to_thread(updater.check_update, False)
        return {**result, "deployment_mode": "guarded", "can_apply": False}
    except updater.GitUpdateError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取版本信息出错：{type(e).__name__}: {str(e)[:300]}")


@router.get("/update/check")
async def check_update():
    """检查远程仓库是否有可用更新（会执行 git fetch 联网比较）"""
    import asyncio
    try:
        result = await asyncio.to_thread(updater.check_update, True)
        return {**result, "deployment_mode": "guarded", "can_apply": False}
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
    """版本检查保留；发布必须经过排空、备份和相同依赖制品验证。"""
    raise HTTPException(status_code=409, detail={
        "code": "GUARDED_DEPLOYMENT_REQUIRED",
        "message": "请通过现有 Jenkins 发布流程更新；直接在线拉取代码已停用",
        "action": "发布流程会先完成只读预检，再排空任务、备份数据库、切换代码并验证，失败时恢复备份。",
    })
