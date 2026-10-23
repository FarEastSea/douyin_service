from fastapi import APIRouter, Body, HTTPException, Request
import asyncio

from pydantic import BaseModel
from sqlalchemy import create_engine, text

from app.core.diagnostics import get_runtime_errors
from app.core.env_config import ENV_FIELDS, FIELD_MAP, ENV_PATH, _build_database_url, _check_database, _check_redis, check_download_directory, read_env_file, validate_env, write_env_updates
from app.core.network_security import validate_database_test_target


router = APIRouter(tags=["初始化配置"])

def _effective_bootstrap_status(request: Request):
    status = validate_env()
    if getattr(request.app.state, "degraded_mode", False):
        status = {**status, "ready": False, "errors": list(status.get("errors", []))}
        startup_status = getattr(request.app.state, "bootstrap_status", {}) or {}
        startup_errors = [
            item
            for item in startup_status.get("errors", [])
            if item.get("key") in {"APP_STARTUP", "DATABASE_INIT", "DOWNLOAD_DIRECTORY"}
        ]
        startup_errors.extend(get_runtime_errors())
        if startup_errors:
            existing = {item.get("key") for item in status["errors"]}
            status["errors"].extend(item for item in startup_errors if item.get("key") not in existing)
        elif not status["errors"] and not status.get("missing"):
            status["errors"].append({
                "key": "RECOVERY_PENDING",
                "label": "正在恢复服务",
                "group": "应用",
                "message": "配置已保存，系统正在验证并自动恢复完整功能。",
            })
    return status


@router.get("/bootstrap/status")
async def bootstrap_status(request: Request):
    return await asyncio.to_thread(_effective_bootstrap_status, request)


@router.post("/bootstrap/config")
async def save_bootstrap_config(request: Request, payload: dict = Body(...)):
    values = payload.get("values", payload)
    if not isinstance(values, dict):
        raise HTTPException(status_code=400, detail="配置内容格式不正确")
    return await _save_configuration_values(request, values)


INITIALIZATION_KEYS = {
    "ADMIN_TOKEN", "CORS_ALLOWED_ORIGINS", "DOWNLOAD_ROOT", "DOUYIN_DOWNLOAD_SUBDIR", "X_DOWNLOAD_SUBDIR",
    "DB_TYPE", "DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME", "REDIS_URL", "REDIS_PASSWORD",
}


async def _save_configuration_values(request: Request, values: dict):
    """已有数据库统一走业务事务；缺库初始化只允许必要字段。"""
    if any(key not in FIELD_MAP for key in values):
        raise HTTPException(status_code=400, detail="包含未知配置字段")
    degraded = bool(getattr(request.app.state, "degraded_mode", True))
    if degraded and not set(values) <= INITIALIZATION_KEYS:
        raise HTTPException(status_code=400, detail="维护模式仅允许修复初始化所需配置")
    from app.core.config_transaction import configuration_session
    from app.models.database import get_async_engine
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy import inspect
    current = await asyncio.to_thread(read_env_file)
    existing = not degraded
    old_unavailable = False
    if degraded:
        try:
            engine = await get_async_engine()
            async with engine.connect() as connection:
                existing = await connection.run_sync(lambda conn: inspect(conn).has_table("download_tasks"))
        except Exception:
            old_unavailable = True
    if existing:
        from app.core.configuration_service import save_configuration
        async with async_sessionmaker(await get_async_engine(), expire_on_commit=False)() as db:
            return await save_configuration(db, values)
    async with configuration_session():
        from app.core.configuration_service import _require_writable_configuration, field_error
        _require_writable_configuration()
        initialized = (ENV_PATH.parent / ".runtime" / "initialized").exists()
        changed = {key: value for key, value in values.items() if str(value) != str(current.get(key, ""))}
        if initialized or (old_unavailable and current.get("DB_NAME") and current.get("ADMIN_TOKEN")):
            path_keys = {key for key in changed if key == "DOWNLOAD_ROOT" or key.endswith("_DOWNLOAD_SUBDIR")}
            if path_keys:
                raise HTTPException(status_code=409, detail="已有数据库暂不可用，请先恢复连接，再变更下载目录")
        candidate = {**current, **values}
        for field in ENV_FIELDS:
            if field.required and not str(candidate.get(field.key, field.default)).strip():
                raise field_error(field.key, f"{field.label}不能为空")
        error = await asyncio.to_thread(check_download_directory, candidate)
        if not error:
            error = await asyncio.to_thread(_check_database, candidate, require_schema=initialized)
        if not error:
            error = await asyncio.to_thread(_check_redis, candidate)
        if error:
            key = {"DATABASE_CONNECTION": "DB_HOST", "REDIS_CONNECTION": "REDIS_URL",
                   "DOWNLOAD_ROOT_ACCESS": "DOWNLOAD_ROOT"}.get(error["key"], error["key"])
            raise field_error(key, error["message"])
        try:
            persisted = await asyncio.to_thread(write_env_updates, values)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "message": "配置已保存，系统将自动验证并恢复完整功能",
            "persisted_keys": sorted(persisted), "admin_token": persisted.get("ADMIN_TOKEN"),
            "values": {key: value for key, value in persisted.items() if not FIELD_MAP[key].secret}}


class DatabaseConfig(BaseModel):
    db_type: str = "postgresql"
    db_host: str = ""
    db_port: int = 0
    db_user: str = ""
    db_password: str = ""
    db_name: str = ""


def _database_url_from_config(cfg: DatabaseConfig):
    return _build_database_url({
        "DB_TYPE": cfg.db_type,
        "DB_HOST": cfg.db_host,
        "DB_PORT": str(cfg.db_port) if cfg.db_port else "",
        "DB_USER": cfg.db_user,
        "DB_PASSWORD": cfg.db_password or read_env_file().get("DB_PASSWORD", ""),
        "DB_NAME": cfg.db_name,
    })


@router.get("/config/database")
async def get_database_config():
    values = await asyncio.to_thread(read_env_file)
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
        effective_port = cfg.db_port or (3306 if cfg.db_type == "mysql" else 5432)
        await asyncio.to_thread(validate_database_test_target, cfg.db_host, effective_port)

        def _test_connection():
            url, connect_args = _database_url_from_config(cfg)
            engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
            finally:
                engine.dispose()

        await asyncio.to_thread(_test_connection)
        return {"success": True, "message": "数据库连接成功"}
    except Exception as e:
        return {"success": False, "message": f"数据库连接失败: {type(e).__name__}: {str(e)[:300]}"}


@router.post("/config/database")
async def save_database_config(request: Request, cfg: DatabaseConfig):
    if cfg.db_type not in {"postgresql", "mysql"}:
        raise HTTPException(status_code=400, detail=f"不支持的数据库类型: {cfg.db_type}")
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
        return await _save_configuration_values(request, updates)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
