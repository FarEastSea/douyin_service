"""所有网页配置写入的统一业务服务。"""
from pathlib import Path
import asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import ValidationError
from app.models.models import SystemConfig
from app.models.schemas import MessageResponse
from app.core import redis_client
from app.core.config import settings
from app.core.env_config import FIELD_MAP, read_env_file, check_download_directory
from app.core.runtime_config import RUNTIME_CONFIG_ENV_KEYS, save_runtime_config as persist_runtime_config
from app.core.queue_configuration import QueueDrainRequired, resuming_change, stage_queue_change
from app.core.config_transaction import configuration_session
from app.services.douyin_cookie import require_douyin_uifid
from app.services.douyin_account import save_account_profile
from app.services.platform_credentials import save_platform_cookie
from app.services.platform_profile_download import PROFILE_PLATFORM_SPECS
from app.services.media_paths import migrate_download_paths

GENERIC_PLATFORM_CONFIG = {
    platform: (spec.download_subdir_env_key, spec.default_download_subdir, spec.cookie_env_key)
    for platform, spec in PROFILE_PLATFORM_SPECS.items()
}
GENERIC_PLATFORM_COOKIE_KEYS = {item[2] for item in GENERIC_PLATFORM_CONFIG.values()}


def _require_writable_configuration():
    from app.core.env_config import ENV_PATH
    if (ENV_PATH.parent / ".runtime" / "deployment-maintenance").exists():
        raise HTTPException(status_code=503, detail="项目正在发布，配置暂未保存；请稍后重试")


def field_error(key: str, message: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"message": message, "field_errors": {key: message}})


async def record_configuration_activity(message: str, detail: str) -> None:
    try:
        await asyncio.to_thread(redis_client.append_activity_log, "info", "system", message, detail)
    except Exception:
        # 数据库和配置文件已提交，日志服务故障不能让调用方重复保存。
        pass


async def save_platform_credential(db: AsyncSession, platform: str, cookie: str):
    async with configuration_session():
        _require_writable_configuration()
        return await save_platform_cookie(db, platform, cookie)


async def save_douyin_account(db: AsyncSession, **values):
    async with configuration_session():
        _require_writable_configuration()
        return await save_account_profile(db, **values)


async def save_archive_configuration(db: AsyncSession, values: dict):
    from app.services.archive_rules import save_archive_rules
    async with configuration_session():
        _require_writable_configuration()
        return await save_archive_rules(db, values)


async def save_configuration(db: AsyncSession, values: dict, *, preview: bool = False):
    async with configuration_session():
        _require_writable_configuration()
        return await _save_configuration(db, values, preview=preview)


async def _save_configuration(db: AsyncSession, values: dict, *, preview: bool = False):
    """统一保存全部网页配置，并同步运行时配置与平台 Cookie。"""
    if any(key not in FIELD_MAP for key in values):
        raise HTTPException(status_code=400, detail="包含未知配置字段")
    updates = {key: value for key, value in values.items() if key in FIELD_MAP
               and not (FIELD_MAP[key].secret and (value is None or str(value).strip() in {"", "********"}))}
    if not updates:
        raise HTTPException(status_code=400, detail="没有可保存的配置")
    if preview and any(key != "DOWNLOAD_ROOT" and not key.endswith("_DOWNLOAD_SUBDIR") for key in updates):
        raise HTTPException(status_code=400, detail="预演仅支持下载目录配置")
    douyin_cookie_update = updates.get("DOUYIN_COOKIE")
    if douyin_cookie_update not in {None, "", "********"}:
        try:
            require_douyin_uifid(str(douyin_cookie_update).strip())
        except ValueError as error:
            raise field_error("DOUYIN_COOKIE", str(error)) from error

    current = await asyncio.to_thread(read_env_file)
    database_keys = {"DB_TYPE", "DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"}
    path_keys = {"DOWNLOAD_ROOT", "DOUYIN_DOWNLOAD_SUBDIR", "X_DOWNLOAD_SUBDIR",
                 *(item[0] for item in GENERIC_PLATFORM_CONFIG.values())}
    if set(updates) & database_keys and set(updates) & path_keys:
        raise HTTPException(status_code=400, detail="数据库连接与媒体目录请分开保存；先确认目标数据库，再迁移其文件关联")
    credential_keys = {"DOUYIN_COOKIE", "X_COOKIE", *GENERIC_PLATFORM_COOKIE_KEYS}
    supplied_credentials = set(updates) & credential_keys
    if supplied_credentials:
        if len(updates) != 1:
            raise HTTPException(status_code=400, detail="平台凭据请在对应账号页面单独保存，不能与其他设置混合提交")
        key = next(iter(supplied_credentials))
        if key == "DOUYIN_COOKIE":
            await save_account_profile(db, cookie=str(updates[key]).strip(), user_agent=None,
                                       proxy_enabled=None, proxy_url=None)
        elif key == "X_COOKIE":
            cookie = str(updates[key]).strip()
            from app.core.env_config import _validate_env_value
            try:
                _validate_env_value(key, cookie)
            except ValueError as exc:
                raise field_error(key, str(exc)) from exc
            row = await db.scalar(select(SystemConfig).where(SystemConfig.key == "x_cookie"))
            if row:
                row.value = cookie
            else:
                db.add(SystemConfig(key="x_cookie", value=cookie))
            from app.core.config_transaction import commit_configuration
            activation = await commit_configuration(db, {"X_COOKIE": cookie})
            # 缓存故障不能把已提交的设置伪装成保存失败。
            try:
                await asyncio.to_thread(redis_client.set_x_cookie, cookie)
            except Exception:
                pass
        else:
            platform = next(p for p, (_, _, cookie_key) in GENERIC_PLATFORM_CONFIG.items() if cookie_key == key)
            await save_platform_cookie(db, platform, str(updates[key]).strip())
        return MessageResponse(success=True, message=("平台凭据已保存，发布待自动恢复"
            if key == "X_COOKIE" and activation["configuration_state"] == "recovering" else "平台凭据已更新"),
            data=activation if key == "X_COOKIE" else None)
    for key, value in updates.items():
        from app.core.env_config import _validate_env_value
        try:
            _validate_env_value(key, "" if value is None else str(value))
        except ValueError as exc:
            raise field_error(key, str(exc)) from exc
    from app.core.env_config import _check_database, _check_redis
    connection_values = {**(await asyncio.to_thread(settings.snapshot)).model_dump(), **updates}
    from app.core.config import Settings
    try:
        Settings.model_validate(connection_values)
    except ValidationError as exc:
        errors = {str(item["loc"][0]): "配置格式不正确，请检查输入值" for item in exc.errors()}
        raise HTTPException(status_code=400, detail={"message": "部分配置格式不正确", "field_errors": errors}) from exc
    for key, minimum, maximum in (("MAX_CONCURRENT_DOWNLOADS", 1, 20), ("DB_PORT", 1, 65535),
                                  ("DOWNLOAD_CHUNK_SIZE", 1, 64 * 1024 * 1024)):
        if key in updates and not minimum <= int(updates[key]) <= maximum:
            raise field_error(key, f"{FIELD_MAP[key].label}必须介于 {minimum} 和 {maximum} 之间")
    from app.core.runtime_config import _coerce_value
    runtime_by_env = {env_key: runtime_key for runtime_key, env_key in RUNTIME_CONFIG_ENV_KEYS.items()}
    for key, value in updates.items():
        if key in runtime_by_env:
            try:
                _coerce_value(runtime_by_env[key], value, strict=True)
            except ValueError as exc:
                raise field_error(key, str(exc)) from exc
    if set(updates) & {"DB_TYPE", "DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"}:
        error = await asyncio.to_thread(_check_database, connection_values, require_schema=True)
        if error:
            raise field_error("DB_HOST", "目标数据库连接或结构验证失败，配置未保存；目标必须具有当前应用表结构，本操作不会搬迁数据")
    if set(updates) & {"REDIS_URL", "REDIS_PASSWORD"}:
        error = await asyncio.to_thread(_check_redis, connection_values)
        if error:
            raise field_error("REDIS_URL", "目标 Redis 连接验证失败，配置未保存")
    platform_subdir_keys = tuple(
        item[0] for item in GENERIC_PLATFORM_CONFIG.values()
    )
    for key in ("DOUYIN_DOWNLOAD_SUBDIR", "X_DOWNLOAD_SUBDIR", *platform_subdir_keys):
        if key not in updates:
            continue
        subdir = Path(str(updates[key]).strip())
        if subdir.is_absolute() or ".." in subdir.parts or not str(subdir).strip("./\\"):
            raise field_error(key, f"{FIELD_MAP[key].label}必须是根目录下的相对子目录")
    for key, value in updates.items():
        field = FIELD_MAP[key]
        if field.required and not str(value if value is not None else "").strip():
            raise field_error(key, f"{field.label}不能为空")

    download_error = await asyncio.to_thread(
        check_download_directory,
        {**current, **updates},
    )
    if download_error:
        raise field_error(download_error["key"] if download_error["key"] in FIELD_MAP else "DOWNLOAD_ROOT", download_error["message"])

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
        runtime_environment = {}
        if runtime_updates:
            runtime_environment = await persist_runtime_config(db, runtime_updates, commit=False)

        environment_updates = {
            key: value for key, value in updates.items()
            if key not in runtime_by_env
            and key not in {"DOUYIN_COOKIE", *GENERIC_PLATFORM_COOKIE_KEYS}
        }
        legacy_douyin = current.get("DOWNLOAD_DIR")
        old_root = current.get("DOWNLOAD_ROOT") or (str(Path(legacy_douyin).parent) if legacy_douyin else "/downloads")
        old_douyin = legacy_douyin or str(Path(old_root) / current.get("DOUYIN_DOWNLOAD_SUBDIR", "douyin"))
        old_x = current.get("X_DOWNLOAD_DIR") or str(Path(old_root) / current.get("X_DOWNLOAD_SUBDIR", "X"))
        new_root = str(updates.get("DOWNLOAD_ROOT", old_root))
        new_douyin = str(Path(new_root) / str(updates.get("DOUYIN_DOWNLOAD_SUBDIR", current.get("DOUYIN_DOWNLOAD_SUBDIR", "douyin"))))
        new_x = str(Path(new_root) / str(updates.get("X_DOWNLOAD_SUBDIR", current.get("X_DOWNLOAD_SUBDIR", "X"))))
        platform_download_dirs = {
            platform: (
                str(Path(old_root) / current.get(subdir_key, default_subdir)),
                str(Path(new_root) / str(updates.get(
                    subdir_key, current.get(subdir_key, default_subdir)
                ))),
            )
            for platform, (subdir_key, default_subdir, _) in GENERIC_PLATFORM_CONFIG.items()
        }
        path_changes = {
            "tasks": 0,
            "history": 0,
            "x_tasks": 0,
            "x_media": 0,
            "platform_tasks": 0,
            "platform_media": 0,
            "unresolved": {"tasks": 0, "history": 0, "x_tasks": 0, "x_media": 0, "platform_tasks": 0, "platform_media": 0},
            "unresolved_total": 0,
        }
        if (
            old_douyin != new_douyin
            or old_x != new_x
            or any(old != new for old, new in platform_download_dirs.values())
        ):
            path_changes = await migrate_download_paths(
                db,
                old_download_dir=old_douyin,
                new_download_dir=new_douyin,
                old_x_download_dir=old_x,
                new_x_download_dir=new_x,
                platform_download_dirs=platform_download_dirs,
            )
            if path_changes.get("unresolved_total"):
                raise ValueError("目标目录尚未包含全部已有文件，目录变更未保存；不会搬移文件或盲目改写记录")

        if preview:
            await db.rollback()
            return MessageResponse(success=True, message="目录预演通过；只更新已确认存在的文件关联，不搬移文件", data={"migrated_paths": path_changes})

        from app.core.config_transaction import commit_configuration
        activation = await commit_configuration(db, {**environment_updates, **runtime_environment})
    except QueueDrainRequired:
        await db.rollback()
        if resuming_change.get():
            raise
        try:
            await asyncio.to_thread(stage_queue_change, updates)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return MessageResponse(success=True, message="连接变更已保存待生效：旧队列排空后将自动切换并重启后台服务", data={"configuration_state": "draining"})
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        await db.rollback()
        raise

    await record_configuration_activity("设置中心配置已更新", ", ".join(sorted(updates)))
    restart_keys = []
    message = ("配置已保存，发布或后台协调尚未完成，系统将自动恢复，无需重复保存"
               if activation["configuration_state"] == "recovering" else "配置已保存，系统将自动协调服务状态")
    return MessageResponse(
        success=True,
        message=message,
        data={
            **activation,
            "restart_required": bool(restart_keys),
            "restart_keys": restart_keys,
            "migrated_paths": path_changes,
            "admin_token": new_admin_token,
            "values": {key: value for key, value in {**environment_updates, **runtime_environment}.items()
                       if key in updates and key in FIELD_MAP and not FIELD_MAP[key].secret},
        },
    )
