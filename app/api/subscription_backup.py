"""订阅清单备份：导出全部平台的作者清单，导入时补建抖音与 X 作者。"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis_client
from app.core.config import settings
from app.models.database import get_async_db
from app.models.schemas import MessageResponse
from app.services.subscription_backup import (
    BackupFormatError,
    export_subscriptions,
    import_subscriptions,
)

router = APIRouter(prefix="/subscriptions", tags=["订阅备份"])


class SubscriptionImportRequest(BaseModel):
    backup: dict[str, Any]
    restore_subscriptions: bool = True
    dry_run: bool = True


@router.get("/export")
async def export_subscription_backup(db: AsyncSession = Depends(get_async_db)):
    """导出作者清单；不包含 Cookie、代理或其他凭据。"""
    return await export_subscriptions(db)


@router.post("/import", response_model=MessageResponse)
async def import_subscription_backup(
    request: SubscriptionImportRequest, db: AsyncSession = Depends(get_async_db),
):
    """导入备份。默认只预演；确认后以 dry_run=false 再次提交才会写入。"""
    config = await asyncio.to_thread(settings.snapshot)
    try:
        result = await import_subscriptions(
            db, request.backup,
            restore_subscriptions=request.restore_subscriptions, dry_run=request.dry_run,
            douyin_min_interval=int(config.MIN_CHECK_INTERVAL),
            douyin_default_interval=int(config.DEFAULT_CHECK_INTERVAL),
        )
        if not request.dry_run:
            await db.commit()
    except BackupFormatError as exc:
        await db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except Exception:
        await db.rollback()
        raise
    created = result["douyin"]["created"] + result["x"]["created"]
    if request.dry_run:
        message = f"预演完成：将新增 {created} 位作者"
    else:
        message = f"已导入 {created} 位作者"
        await asyncio.to_thread(
            redis_client.append_activity_log, "info", "system", "订阅备份已导入",
            f"抖音 {result['douyin']['created']} 位，X {result['x']['created']} 位",
        )
    return MessageResponse(success=True, message=message, data=result)
