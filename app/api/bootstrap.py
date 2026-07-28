from fastapi import APIRouter, Body, HTTPException, Request

from pydantic import BaseModel
from sqlalchemy import create_engine, text

from app.core.env_config import read_env_file, validate_env, write_env_updates


router = APIRouter(tags=["初始化配置"])

def _effective_bootstrap_status(request: Request):
    status = validate_env()
    if getattr(request.app.state, "degraded_mode", False):
        status = {**status, "ready": False, "errors": list(status.get("errors", []))}
        startup_status = getattr(request.app.state, "bootstrap_status", {}) or {}
        startup_errors = startup_status.get("errors", [])
        if startup_errors:
            existing = {item.get("key") for item in status["errors"]}
            status["errors"].extend(item for item in startup_errors if item.get("key") not in existing)
        elif not status["errors"]:
            status["errors"].append({
                "key": "RESTART_REQUIRED",
                "label": "需要重启服务",
                "group": "应用",
                "message": "配置已保存，请重启 Web 服务后再使用正常功能。",
            })
    return status


@router.get("/bootstrap/status")
async def bootstrap_status(request: Request):
    return _effective_bootstrap_status(request)


@router.post("/bootstrap/config")
async def save_bootstrap_config(request: Request, payload: dict = Body(...)):
    try:
        write_env_updates(payload.get("values", payload))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存配置失败: {str(e)[:300]}")
    status = _effective_bootstrap_status(request)
    return {
        "success": True,
        "message": "配置已保存，请重启 Web 服务使配置生效。",
        **status,
    }


class DatabaseConfig(BaseModel):
    db_type: str = "postgresql"
    db_host: str = ""
    db_port: int = 0
    db_user: str = ""
    db_password: str = ""
    db_name: str = ""


def _database_url_from_config(cfg: DatabaseConfig):
    db_type = (cfg.db_type or "postgresql").lower()
    db_password = cfg.db_password or read_env_file().get("DB_PASSWORD", "")
    user_part = f"{cfg.db_user}:{db_password}" if db_password else cfg.db_user
    if db_type == "postgresql":
        return f"postgresql://{user_part}@{cfg.db_host}:{cfg.db_port or 5432}/{cfg.db_name}", {"connect_timeout": 3}
    if db_type == "mysql":
        return f"mysql+pymysql://{user_part}@{cfg.db_host}:{cfg.db_port or 3306}/{cfg.db_name}?charset=utf8mb4", {"connect_timeout": 3}
    raise ValueError(f"不支持的数据库类型：{cfg.db_type}")


@router.get("/config/database")
async def get_database_config():
    values = read_env_file()
    return {
        "db_type": values.get("DB_TYPE", "postgresql"),
        "db_host": values.get("DB_HOST", ""),
        "db_port": int(values.get("DB_PORT") or 5432) if str(values.get("DB_PORT") or "5432").isdigit() else 5432,
        "db_user": values.get("DB_USER", ""),
        "db_name": values.get("DB_NAME", ""),
        "db_password_set": bool(values.get("DB_PASSWORD")),
        "env": validate_env(),
    }


@router.post("/config/database/test")
async def test_database_connection(cfg: DatabaseConfig):
    try:
        url, connect_args = _database_url_from_config(cfg)
        engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        finally:
            engine.dispose()
        return {"success": True, "message": "数据库连接成功"}
    except Exception as e:
        return {"success": False, "message": f"数据库连接失败: {type(e).__name__}: {str(e)[:300]}"}


@router.post("/config/database")
async def save_database_config(cfg: DatabaseConfig):
    if cfg.db_type not in {"postgresql", "mysql"}:
        return {"success": False, "message": f"不支持的数据库类型: {cfg.db_type}"}
    try:
        updates = {
            "DB_TYPE": cfg.db_type,
            "DB_HOST": cfg.db_host,
            "DB_PORT": str(cfg.db_port or (5432 if cfg.db_type == "postgresql" else 3306)),
            "DB_USER": cfg.db_user,
            "DB_PASSWORD": cfg.db_password or read_env_file().get("DB_PASSWORD", ""),
            "DB_NAME": cfg.db_name,
        }
        write_env_updates(updates)
        return {"success": True, "message": "数据库配置已保存，请重启 Web 服务后生效"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存数据库配置失败: {str(e)[:300]}")


@router.post("/service/restart")
async def restart_web_service():
    import os
    import signal

    def read_proc_cmdline(pid: int) -> str:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except Exception:
            return ""

    ppid = os.getppid()
    cmdline = read_proc_cmdline(ppid)
    if "gunicorn" not in cmdline.lower():
        return {
            "success": False,
            "reload_supported": False,
            "message": "配置已保存，请重启 Web 服务后再使用正常功能。",
        }
    try:
        os.kill(ppid, signal.SIGHUP)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重启 Web 服务失败: {str(e)[:200]}")
    return {"success": True, "reload_supported": True, "message": "已触发 Web 服务热重载"}
