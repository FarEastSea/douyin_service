"""排除项目维护接口：查看删除后不再下载的项目，按需恢复下载。"""

from __future__ import annotations

from typing import Annotated, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_async_db
from app.models.schemas import MessageResponse
from app.services.exclusions import list_exclusions, restore_exclusions
from app.services.media_library import PLATFORMS


router = APIRouter(prefix="/exclusions", tags=["排除项目"])


class RestoreRequest(BaseModel):
    keys: List[Annotated[str, Field(min_length=3, max_length=80)]] = Field(..., min_length=1, max_length=200)


@router.get("")
async def exclusion_list(
    platform: str = Query("", max_length=32),
    q: str = Query("", max_length=255),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_async_db),
):
    if platform and platform not in PLATFORMS:
        raise HTTPException(404, "平台不存在")
    items = await list_exclusions(db, platform=platform, q=q.strip())
    total = len(items)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    start = (page - 1) * page_size
    return {"items": items[start:start + page_size], "total": total, "page": page, "pages": pages}


@router.post("/restore", response_model=MessageResponse)
async def exclusion_restore(request: RestoreRequest, db: AsyncSession = Depends(get_async_db)):
    result = await restore_exclusions(db, request.keys)
    succeeded, failed = result["succeeded"], result["failed"]
    message = f"已恢复 {len(succeeded)} 项"
    if failed:
        message += f"，{len(failed)} 项未处理"
    return MessageResponse(success=bool(succeeded), message=message, data=result)
