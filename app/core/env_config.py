from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


ENV_PATH = Path(".env")


class EnvField(BaseModel):
    key: str
    label: str
    group: str
    default: str = ""
    required: bool = False
    secret: bool = False
    help: str = ""


ENV_FIELDS: List[EnvField] = [
    EnvField(key="DEBUG", label="Debug", group="Application", default="false"),
    EnvField(key="DOWNLOAD_DIR", label="Download directory", group="Application", default="/downloads", required=True),
    EnvField(key="X_DOWNLOAD_DIR", label="X download directory", group="Application", default="/downloads/X"),
    EnvField(key="DB_TYPE", label="Database type", group="Database", default="postgresql", required=True),
    EnvField(key="DB_HOST", label="Database host", group="Database", default="localhost", required=True),
    EnvField(key="DB_PORT", label="Database port", group="Database", default="5432", required=True),
    EnvField(key="DB_USER", label="Database user", group="Database", default="postgres", required=True),
    EnvField(key="DB_PASSWORD", label="Database password", group="Database", default="", required=True, secret=True),
    EnvField(key="DB_NAME", label="Database name", group="Database", default="douyin_service", required=True),
    EnvField(key="REDIS_URL", label="Redis URL", group="Redis", default="redis://localhost:6379/0", required=True),
    EnvField(key="REDIS_PASSWORD", label="Redis password", group="Redis", default="", secret=True),
    EnvField(key="CELERY_BROKER_URL", label="Celery broker URL", group="Celery", default="redis://localhost:6379/0"),
    EnvField(key="CELERY_RESULT_BACKEND", label="Celery result backend", group="Celery", default="redis://localhost:6379/0"),
    EnvField(key="MAX_CONCURRENT_DOWNLOADS", label="Max concurrent downloads", group="Download", default="3"),
    EnvField(key="DOWNLOAD_CHUNK_SIZE", label="Download chunk size", group="Download", default="1048576"),
    EnvField(key="DOWNLOAD_TIMEOUT", label="Download timeout seconds", group="Download", default="30"),
    EnvField(key="DOWNLOAD_RETRY_COUNT", label="Download retry count", group="Download", default="3"),
    EnvField(key="DOWNLOAD_RETRY_DELAY", label="Download retry delay", group="Download", default="5"),
    EnvField(key="DEFAULT_CHECK_INTERVAL", label="Default check interval", group="Subscription", default="21600"),
    EnvField(key="MIN_CHECK_INTERVAL", label="Minimum check interval", group="Subscription", default="3600"),
    EnvField(key="AUTO_CHECK_ENABLED", label="Auto check enabled", group="Subscription", default="true"),
    EnvField(key="REQUEST_DELAY", label="Douyin request delay", group="Douyin", default="3.0"),
    EnvField(key="AUTHOR_CHECK_DELAY", label="Author check delay", group="Douyin", default="30.0"),
    EnvField(key="STUCK_TASK_TIMEOUT", label="Stuck task timeout", group="Task", default="600"),
    EnvField(key="DOUYIN_COOKIE", label="Douyin Cookie", group="Account", default="", secret=True),
    EnvField(key="X_DOWNLOAD_ENGINE", label="X download engine", group="X", default="gallery-dl"),
    EnvField(key="X_COOKIE_FILE", label="X Cookie file", group="X", default=""),
    EnvField(key="X_TASK_LOG_MAX_LINES", label="X task log max lines", group="X", default="400"),
    EnvField(key="X_TASK_LOG_TTL_SECONDS", label="X task log TTL seconds", group="X", default="604800"),
    EnvField(key="X_TASK_STATE_TTL_SECONDS", label="X task state TTL seconds", group="X", default="86400"),
]

FIELD_MAP = {field.key: field for field in ENV_FIELDS}


def read_env_file() -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for line in ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def get_env_values(mask_secret: bool = True) -> Dict[str, Any]:
    raw = read_env_file()
    values: Dict[str, Any] = {}
    for field in ENV_FIELDS:
        value = raw.get(field.key, field.default)
        values[field.key] = {
            "value": "********" if mask_secret and field.secret and value else value,
            "configured": bool(str(value).strip()),
            "secret": field.secret,
        }
    return values


def validate_env(values: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    values = values if values is not None else read_env_file()
    missing = []
    for field in ENV_FIELDS:
        if field.required and not str(values.get(field.key, "")).strip():
            missing.append({"key": field.key, "label": field.label, "group": field.group})
    return {
        "ready": not missing,
        "env_exists": ENV_PATH.exists(),
        "missing": missing,
        "fields": [field.model_dump() for field in ENV_FIELDS],
        "values": get_env_values(mask_secret=True),
    }


def write_env_updates(updates: Dict[str, Any]) -> None:
    current = read_env_file()
    clean_updates = {}
    for key, value in updates.items():
        if key not in FIELD_MAP:
            continue
        if value == "********" and FIELD_MAP[key].secret and current.get(key):
            continue
        clean_updates[key] = "" if value is None else str(value).strip()

    lines = ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines() if ENV_PATH.exists() else []
    seen = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in clean_updates:
                new_lines.append(f"{key}={clean_updates[key]}")
                seen.add(key)
                continue
        new_lines.append(line)

    if not new_lines:
        new_lines.append("# douyin_service environment")
    for field in ENV_FIELDS:
        if field.key in clean_updates and field.key not in seen:
            new_lines.append(f"{field.key}={clean_updates[field.key]}")

    ENV_PATH.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")
