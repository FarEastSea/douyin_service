"""媒体平台注册表 API。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.platform_registry import platform_registry


router = APIRouter(prefix="/platforms", tags=["媒体平台"])


class PlatformDetectRequest(BaseModel):
    value: str = Field(..., min_length=1, max_length=4096)


@router.get("")
async def list_platforms():
    """返回所有已启用平台及其真实能力。"""
    return {"items": [item.to_dict() for item in platform_registry.list()]}


@router.get("/{platform_id}")
async def get_platform(platform_id: str):
    try:
        definition = platform_registry.get(platform_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return definition.to_dict()


@router.post("/detect")
async def detect_platform(request: PlatformDetectRequest):
    """仅根据域名识别平台，不发起任何外部请求。"""
    detected = platform_registry.detect(request.value)
    if not detected:
        raise HTTPException(status_code=422, detail="无法识别或暂不支持该媒体平台")
    return detected.to_dict()

