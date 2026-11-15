"""数据洞察：媒体库规模、入库趋势、任务结果与磁盘空间的只读汇总。"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.database import get_async_db
from app.services.disk_space import disk_usage
from app.services.insights import ALLOWED_WINDOWS, build_overall, build_window
from app.services.insights_cache import InsightsBusy, cached

router = APIRouter(prefix="/insights", tags=["数据洞察"])

# 全时段统计要扫描全部媒体记录，复用时间更长；窗口统计只读最近两个周期。
OVERALL_FRESH_SECONDS = 30 * 60
WINDOW_FRESH_SECONDS = 15 * 60


async def _read_disk() -> dict:
    # 每次请求读取最新网页配置中的下载根目录和预警阈值。
    config = await asyncio.to_thread(settings.snapshot)
    return await asyncio.to_thread(disk_usage, config)


@router.get("")
async def get_insights(
    days: int = Query(30, description="统计窗口天数：7、30、90、180 或 365"),
    refresh: bool = Query(False, description="忽略缓存立即重算；一分钟内的重复刷新仍返回上一份结果"),
    db: AsyncSession = Depends(get_async_db),
):
    if days not in ALLOWED_WINDOWS:
        raise HTTPException(400, "统计窗口仅支持 7、30、90、180 或 365 天")
    try:
        return await _build_response(days, refresh, db)
    except InsightsBusy as exc:
        raise HTTPException(503, str(exc), headers={"Retry-After": "10"}) from exc


async def _build_response(days: int, refresh: bool, db: AsyncSession):
    import hashlib
    # 数据库切换不能复用另一数据库的统计。只保存不可逆摘要，不暴露连接信息。
    scope = hashlib.sha256(str(db.get_bind().url).encode()).hexdigest()[:20]
    overall, overall_at, overall_stale = await cached(
        f"{scope}:overall", OVERALL_FRESH_SECONDS, lambda: build_overall(db), force=refresh,
    )
    # 键里带上日期：跨过零点后窗口随之前移，不沿用前一天的结果。
    window, window_at, window_stale = await cached(
        f"{scope}:window:{days}:{date.today().isoformat()}", WINDOW_FRESH_SECONDS,
        lambda: build_window(db, days), force=refresh,
    )
    generated_at = min(overall_at, window_at)
    return {
        **window,
        **overall,
        "generated_at": datetime.fromtimestamp(generated_at, timezone.utc).isoformat(),
        "stale": overall_stale or window_stale,
        "refresh_seconds": {"overall": OVERALL_FRESH_SECONDS, "window": WINDOW_FRESH_SECONDS},
        # 磁盘空间读取很轻，始终取实时值。
        "disk": await _read_disk(),
    }


@router.get("/disk")
async def get_disk_space():
    """总览页轮询使用的轻量磁盘空间读取，不访问数据库。"""
    return await _read_disk()
