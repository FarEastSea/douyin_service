"""服务存活与就绪检查。

存活只证明 Web 进程能响应；就绪检查覆盖数据库、Redis、
Celery Worker 和 Beat，供发布验收与管理页共用。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable


async def _check(
    action: Callable[[], Awaitable[Any]],
    *,
    success_message: str,
    failure_message: str,
    timeout: float,
) -> dict[str, Any]:
    try:
        value = await asyncio.wait_for(action(), timeout=timeout)
        result = {"ok": True, "message": success_message}
        if value is not None:
            result["value"] = value
        return result
    except Exception:
        return {"ok": False, "message": failure_message}


async def _check_database() -> None:
    from sqlalchemy import text

    from app.models.database import get_async_engine

    engine = await get_async_engine()
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def _check_redis() -> None:
    from app.core import redis_client

    connected = await asyncio.to_thread(redis_client.check_connection)
    if not connected:
        raise RuntimeError("Redis ping failed")


async def _read_process_status() -> dict[str, Any]:
    from app.core.process_manager import process_manager

    return await asyncio.to_thread(process_manager.get_status)


async def _ping_workers() -> int:
    def ping() -> int:
        from app.tasks.celery_app import celery_app

        result = celery_app.control.inspect(timeout=2.0).ping()
        return len(result or {})

    count = await asyncio.to_thread(ping)
    if count < 1:
        raise RuntimeError("No Celery worker replied")
    return count


async def build_readiness(*, degraded_mode: bool) -> dict[str, Any]:
    """生成不包含连接地址或异常原文的就绪状态。"""
    components: dict[str, dict[str, Any]] = {
        "configuration": {
            "ok": not degraded_mode,
            "message": "配置已就绪" if not degraded_mode else "配置尚未完成",
        }
    }

    if degraded_mode:
        for name, message in {
            "database": "等待配置完成",
            "redis": "等待配置完成",
            "worker": "等待配置完成",
            "beat": "等待配置完成",
        }.items():
            components[name] = {"ok": False, "message": message}
    else:
        database, redis, process = await asyncio.gather(
            _check(
                _check_database,
                success_message="数据库可用",
                failure_message="数据库不可用",
                timeout=3.0,
            ),
            _check(
                _check_redis,
                success_message="Redis 可用",
                failure_message="Redis 不可用",
                timeout=3.0,
            ),
            _check(
                _read_process_status,
                success_message="进程状态已读取",
                failure_message="进程状态不可用",
                timeout=2.0,
            ),
        )
        components["database"] = database
        components["redis"] = redis

        process_value = process.get("value") if process.get("ok") else {}
        worker_running = bool((process_value or {}).get("worker", {}).get("running"))
        beat_running = bool((process_value or {}).get("beat", {}).get("running"))

        if worker_running and redis.get("ok"):
            worker = await _check(
                _ping_workers,
                success_message="Celery Worker 可用",
                failure_message="Celery Worker 未响应",
                timeout=3.0,
            )
            worker_count = worker.pop("value", None)
            if worker_count is not None:
                worker["workers"] = int(worker_count)
            components["worker"] = worker
        else:
            components["worker"] = {
                "ok": False,
                "message": "Celery Worker 未运行" if not worker_running else "Redis 不可用",
            }

        components["beat"] = {
            "ok": beat_running,
            "message": "Celery Beat 可用" if beat_running else "Celery Beat 未运行",
        }

    ready = all(component.get("ok") is True for component in components.values())
    return {
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "components": components,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
