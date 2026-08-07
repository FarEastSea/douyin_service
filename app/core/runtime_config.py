"""
运行期配置管理。

这些配置保存在 system_config 表中，避免覆盖部署时影响 .env 或已有数据。
"""

from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.env_config import write_env_updates
from app.models.models import SystemConfig


CONFIG_PREFIX = "runtime:"

RUNTIME_CONFIG_ENV_KEYS = {
    "auto_check_enabled": "AUTO_CHECK_ENABLED",
    "subscription_check_interval": "DEFAULT_CHECK_INTERVAL",
    "douyin_request_delay": "REQUEST_DELAY",
    "douyin_risk_cooldown_seconds": "DOUYIN_RISK_COOLDOWN_SECONDS",
    "douyin_risk_auto_retry": "DOUYIN_RISK_AUTO_RETRY",
    "author_check_delay": "AUTHOR_CHECK_DELAY",
    "download_timeout": "DOWNLOAD_TIMEOUT",
    "download_retry_count": "DOWNLOAD_RETRY_COUNT",
    "download_retry_delay": "DOWNLOAD_RETRY_DELAY",
    "stuck_task_timeout": "STUCK_TASK_TIMEOUT",
}


RUNTIME_CONFIG_SCHEMA: Dict[str, Dict[str, Any]] = {
    "auto_check_enabled": {
        "type": "bool",
        "default": settings.AUTO_CHECK_ENABLED,
        "label": "自动检查订阅",
    },
    "subscription_check_interval": {
        "type": "int",
        "default": settings.DEFAULT_CHECK_INTERVAL,
        "min": settings.MIN_CHECK_INTERVAL,
        "max": 7 * 24 * 3600,
        "label": "订阅检查间隔",
        "unit": "秒",
    },
    "douyin_request_delay": {
        "type": "float",
        "default": settings.REQUEST_DELAY,
        "min": 1.0,
        "max": 120.0,
        "label": "抖音分页请求间隔",
        "unit": "秒",
    },
    "douyin_risk_cooldown_seconds": {
        "type": "int",
        "default": settings.DOUYIN_RISK_COOLDOWN_SECONDS,
        "min": 60,
        "max": 3600,
        "label": "抖音风控冷却时长",
        "unit": "秒",
    },
    "douyin_risk_auto_retry": {
        "type": "bool",
        "default": settings.DOUYIN_RISK_AUTO_RETRY,
        "label": "风控冷却后自动恢复一次",
    },
    "author_check_delay": {
        "type": "float",
        "default": settings.AUTHOR_CHECK_DELAY,
        "min": 5.0,
        "max": 600.0,
        "label": "作者之间检查间隔",
        "unit": "秒",
    },
    "download_timeout": {
        "type": "int",
        "default": settings.DOWNLOAD_TIMEOUT,
        "min": 5,
        "max": 300,
        "label": "下载请求超时",
        "unit": "秒",
    },
    "download_retry_count": {
        "type": "int",
        "default": settings.DOWNLOAD_RETRY_COUNT,
        "min": 0,
        "max": 10,
        "label": "下载重试次数",
    },
    "download_retry_delay": {
        "type": "int",
        "default": settings.DOWNLOAD_RETRY_DELAY,
        "min": 0,
        "max": 300,
        "label": "下载重试延迟",
        "unit": "秒",
    },
    "stuck_task_timeout": {
        "type": "int",
        "default": settings.STUCK_TASK_TIMEOUT,
        "min": 300,
        "max": 24 * 3600,
        "label": "卡住任务超时",
        "unit": "秒",
    },
}


def get_runtime_defaults() -> Dict[str, Any]:
    return {key: spec["default"] for key, spec in RUNTIME_CONFIG_SCHEMA.items()}


def _serialize(value: Any, value_type: str) -> str:
    if value_type == "bool":
        return "true" if bool(value) else "false"
    return str(value)


def _coerce_value(key: str, value: Any, *, strict: bool) -> Any:
    spec = RUNTIME_CONFIG_SCHEMA[key]
    value_type = spec["type"]

    try:
        if value_type == "bool":
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in {"1", "true", "yes", "on"}:
                    coerced = True
                elif normalized in {"0", "false", "no", "off"}:
                    coerced = False
                else:
                    raise ValueError
            else:
                coerced = bool(value)
        elif value_type == "int":
            coerced = int(value)
        elif value_type == "float":
            coerced = float(value)
        else:
            coerced = value
    except (TypeError, ValueError):
        if strict:
            raise ValueError(f"{spec['label']}格式不正确")
        return spec["default"]

    min_value = spec.get("min")
    max_value = spec.get("max")
    if min_value is not None and coerced < min_value:
        if strict:
            raise ValueError(f"{spec['label']}不能小于 {min_value}{spec.get('unit', '')}")
        return spec["default"]
    if max_value is not None and coerced > max_value:
        if strict:
            raise ValueError(f"{spec['label']}不能大于 {max_value}{spec.get('unit', '')}")
        return spec["default"]

    return coerced


def normalize_runtime_config(values: Optional[Dict[str, Any]] = None, *, strict: bool = False) -> Dict[str, Any]:
    values = values or {}
    normalized = get_runtime_defaults()
    for key, value in values.items():
        if key not in RUNTIME_CONFIG_SCHEMA or value is None:
            continue
        normalized[key] = _coerce_value(key, value, strict=strict)
    return normalized


def get_cached_runtime_config() -> Dict[str, Any]:
    try:
        from app.core import redis_client

        cached = redis_client.get_runtime_config()
        if cached:
            return normalize_runtime_config(cached)
    except Exception:
        pass
    return get_runtime_defaults()


async def get_runtime_config(db: AsyncSession) -> Dict[str, Any]:
    result = await db.execute(
        select(SystemConfig).where(SystemConfig.key.in_([CONFIG_PREFIX + key for key in RUNTIME_CONFIG_SCHEMA]))
    )
    values = {row.key.removeprefix(CONFIG_PREFIX): row.value for row in result.scalars().all()}
    config = normalize_runtime_config(values)
    try:
        from app.core import redis_client

        redis_client.set_runtime_config(config)
    except Exception:
        pass
    return config


def get_runtime_config_sync(db: Optional[Session] = None) -> Dict[str, Any]:
    if db is None:
        return get_cached_runtime_config()

    try:
        rows = db.execute(
            select(SystemConfig).where(SystemConfig.key.in_([CONFIG_PREFIX + key for key in RUNTIME_CONFIG_SCHEMA]))
        ).scalars().all()
        values = {row.key.removeprefix(CONFIG_PREFIX): row.value for row in rows}
        config = normalize_runtime_config(values)
        try:
            from app.core import redis_client

            redis_client.set_runtime_config(config)
        except Exception:
            pass
        return config
    except Exception:
        return get_cached_runtime_config()


async def save_runtime_config(db: AsyncSession, updates: Dict[str, Any]) -> Dict[str, Any]:
    allowed_updates = {
        key: _coerce_value(key, value, strict=True)
        for key, value in updates.items()
        if key in RUNTIME_CONFIG_SCHEMA and value is not None
    }

    if not allowed_updates:
        current = await get_runtime_config(db)
        return current

    result = await db.execute(
        select(SystemConfig).where(SystemConfig.key.in_([CONFIG_PREFIX + key for key in allowed_updates]))
    )
    existing = {row.key.removeprefix(CONFIG_PREFIX): row for row in result.scalars().all()}

    for key, value in allowed_updates.items():
        stored_value = _serialize(value, RUNTIME_CONFIG_SCHEMA[key]["type"])
        if key in existing:
            existing[key].value = stored_value
        else:
            db.add(SystemConfig(key=CONFIG_PREFIX + key, value=stored_value))

    # 网页设置是运行配置的权威来源：数据库供当前进程立即读取，
    # .env 供 Web/Celery 重启后继续使用同一组值。
    try:
        await db.flush()
        write_env_updates({
            RUNTIME_CONFIG_ENV_KEYS[key]: _serialize(value, RUNTIME_CONFIG_SCHEMA[key]["type"])
            for key, value in allowed_updates.items()
        })
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    config = await get_runtime_config(db)
    return config
