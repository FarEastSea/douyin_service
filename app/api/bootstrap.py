from fastapi import APIRouter, Body, HTTPException

from app.core.env_config import validate_env, write_env_updates


router = APIRouter(tags=["初始化配置"])


@router.get("/bootstrap/status")
async def bootstrap_status():
    return validate_env()


@router.post("/bootstrap/config")
async def save_bootstrap_config(payload: dict = Body(...)):
    try:
        write_env_updates(payload.get("values", payload))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存配置失败: {str(e)[:300]}")
    status = validate_env()
    return {
        "success": True,
        "message": "配置已保存，请重启 Web 服务后生效。",
        **status,
    }
