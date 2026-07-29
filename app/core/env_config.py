import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel


def _find_project_root() -> Path:
    """以 main.py 所在目录为项目根目录，不依赖启动工作目录或部署绝对路径。"""
    current_file = Path(__file__).resolve()
    for parent in current_file.parents:
        if (parent / "main.py").is_file():
            return parent
    raise RuntimeError(f"无法从 {current_file} 定位包含 main.py 的项目根目录")


# 项目整体迁移时该路径会随 main.py 自动变化。
ENV_PATH = _find_project_root() / ".env"


class EnvField(BaseModel):
    key: str
    label: str
    group: str
    default: str = ""
    required: bool = False
    secret: bool = False
    help: str = ""


ENV_FIELDS: List[EnvField] = [
    EnvField(key="APP_NAME", label="应用名称", group="应用", default="媒体下载管理系统", help="显示与识别当前服务的名称"),
    EnvField(key="DEBUG", label="调试模式", group="应用", default="false", help="生产环境建议关闭"),
    EnvField(key="DOWNLOAD_ROOT", label="下载根目录", group="下载目录", default="/downloads", required=True, help="两个平台下载文件的共同根目录"),
    EnvField(key="DOUYIN_DOWNLOAD_SUBDIR", label="抖音子目录", group="下载目录", default="douyin", required=True, help="根目录下的相对子目录"),
    EnvField(key="X_DOWNLOAD_SUBDIR", label="X 子目录", group="下载目录", default="X", required=True, help="根目录下的相对子目录"),
    EnvField(key="DB_TYPE", label="数据库类型", group="数据库", default="postgresql", required=True),
    EnvField(key="DB_HOST", label="数据库主机", group="数据库", default="localhost", required=True),
    EnvField(key="DB_PORT", label="数据库端口", group="数据库", default="5432", required=True),
    EnvField(key="DB_USER", label="数据库用户", group="数据库", default="postgres", required=True),
    EnvField(key="DB_PASSWORD", label="数据库密码", group="数据库", default="", required=True, secret=True),
    EnvField(key="DB_NAME", label="数据库名称", group="数据库", default="douyin_service", required=True),
    EnvField(key="REDIS_URL", label="Redis 连接地址", group="Redis", default="redis://localhost:6379/0", required=True),
    EnvField(key="REDIS_PASSWORD", label="Redis 密码", group="Redis", default="", secret=True),
    EnvField(key="CELERY_BROKER_URL", label="Celery 消息队列地址", group="后台任务", default="redis://localhost:6379/0"),
    EnvField(key="CELERY_RESULT_BACKEND", label="Celery 结果存储地址", group="后台任务", default="redis://localhost:6379/0"),
    EnvField(key="MAX_CONCURRENT_DOWNLOADS", label="最大同时下载数", group="下载", default="3"),
    EnvField(key="DOWNLOAD_CHUNK_SIZE", label="下载分块大小（字节）", group="下载", default="1048576"),
    EnvField(key="DOWNLOAD_TIMEOUT", label="下载超时（秒）", group="下载", default="30"),
    EnvField(key="DOWNLOAD_RETRY_COUNT", label="下载重试次数", group="下载", default="3"),
    EnvField(key="DOWNLOAD_RETRY_DELAY", label="下载重试间隔（秒）", group="下载", default="5"),
    EnvField(key="DEFAULT_CHECK_INTERVAL", label="默认订阅检查间隔（秒）", group="订阅", default="21600"),
    EnvField(key="MIN_CHECK_INTERVAL", label="最小订阅检查间隔（秒）", group="订阅", default="3600"),
    EnvField(key="AUTO_CHECK_ENABLED", label="启用自动订阅检查", group="订阅", default="true"),
    EnvField(key="REQUEST_DELAY", label="抖音分页请求间隔（秒）", group="抖音", default="3.0"),
    EnvField(key="AUTHOR_CHECK_DELAY", label="作者检查间隔（秒）", group="抖音", default="30.0"),
    EnvField(key="STUCK_TASK_TIMEOUT", label="任务卡住判定时间（秒）", group="任务", default="600"),
    EnvField(key="DOUYIN_COOKIE", label="抖音 Cookie", group="账号", default="", secret=True),
    EnvField(key="X_DOWNLOAD_ENGINE", label="X 下载引擎", group="X", default="gallery-dl"),
    EnvField(key="X_COOKIE", label="X Cookie", group="X", default="", secret=True),
    EnvField(key="X_COOKIE_FILE", label="X Cookie 文件", group="X", default=""),
    EnvField(key="X_TASK_LOG_MAX_LINES", label="X 任务日志最大行数", group="X", default="400"),
    EnvField(key="X_TASK_LOG_TTL_SECONDS", label="X 任务日志保留时间（秒）", group="X", default="604800"),
    EnvField(key="X_TASK_STATE_TTL_SECONDS", label="X 任务状态保留时间（秒）", group="X", default="86400"),
]

FIELD_MAP = {field.key: field for field in ENV_FIELDS}

def _build_database_url(values: Dict[str, str]) -> Tuple[str, Dict[str, int]]:
    db_type = (values.get("DB_TYPE") or "postgresql").strip().lower()
    db_host = (values.get("DB_HOST") or "").strip()
    db_port = values.get("DB_PORT") or ("3306" if db_type == "mysql" else "5432")
    db_user = (values.get("DB_USER") or "").strip()
    db_password = values.get("DB_PASSWORD") or ""
    db_name = (values.get("DB_NAME") or "").strip()
    user_part = f"{db_user}:{db_password}" if db_password else db_user

    if db_type == "mysql":
        return (
            f"mysql+pymysql://{user_part}@{db_host}:{int(db_port)}/{db_name}?charset=utf8mb4",
            {"connect_timeout": 3},
        )
    if db_type == "postgresql":
        return (
            f"postgresql://{user_part}@{db_host}:{int(db_port)}/{db_name}",
            {"connect_timeout": 3},
        )
    raise ValueError(f"不支持的数据库类型：{db_type}")


def _check_database(values: Dict[str, str]) -> Optional[Dict[str, str]]:
    try:
        from sqlalchemy import create_engine, text

        url, connect_args = _build_database_url(values)
        engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        finally:
            engine.dispose()
        return None
    except Exception as e:
        return {
            "key": "DATABASE_CONNECTION",
            "label": "数据库连接",
            "group": "数据库",
            "message": f"{type(e).__name__}: {str(e)[:300]}",
        }


def _check_redis(values: Dict[str, str]) -> Optional[Dict[str, str]]:
    redis_url = (values.get("REDIS_URL") or "").strip()
    if not redis_url:
        return None
    redis_password = (values.get("REDIS_PASSWORD") or "").strip()
    if redis_password and redis_url.startswith("redis://") and "@" not in redis_url.split("redis://", 1)[1].split("/", 1)[0]:
        redis_url = redis_url.replace("redis://", f"redis://:{redis_password}@", 1)
    try:
        import redis as redis_lib

        client = redis_lib.Redis.from_url(redis_url, socket_connect_timeout=3, socket_timeout=3)
        client.ping()
        return None
    except Exception as e:
        return {
            "key": "REDIS_CONNECTION",
            "label": "Redis 连接",
            "group": "Redis",
            "message": f"{type(e).__name__}: {str(e)[:300]}",
        }


def check_download_directory(values: Dict[str, str]) -> Optional[Dict[str, str]]:
    root_value = str(values.get("DOWNLOAD_ROOT") or "").strip()
    if not root_value:
        return None

    root = Path(root_value).expanduser()
    if not root.exists():
        message = "下载根目录不存在，请先在服务器上创建该目录"
    elif not root.is_dir():
        message = "下载根目录不是目录"
    elif not os.access(root, os.R_OK | os.W_OK | os.X_OK):
        message = "下载根目录不可访问或不可写，请检查目录权限"
    else:
        for key in ("DOUYIN_DOWNLOAD_SUBDIR", "X_DOWNLOAD_SUBDIR"):
            value = str(values.get(key) or "").strip()
            if not value:
                continue
            subdir = Path(value)
            if subdir.is_absolute() or ".." in subdir.parts or not str(subdir).strip("./\\"):
                return {
                    "key": key,
                    "label": FIELD_MAP[key].label,
                    "group": FIELD_MAP[key].group,
                    "message": "下载子目录必须是根目录下的相对路径",
                }
        return None

    return {
        "key": "DOWNLOAD_ROOT_ACCESS",
        "label": FIELD_MAP["DOWNLOAD_ROOT"].label,
        "group": FIELD_MAP["DOWNLOAD_ROOT"].group,
        "message": f"{message}: {root}",
    }


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

    errors = []
    if not missing:
        download_error = check_download_directory(values)
        if download_error:
            errors.append(download_error)
        db_error = _check_database(values)
        if db_error:
            errors.append(db_error)
        redis_error = _check_redis(values)
        if redis_error:
            errors.append(redis_error)

    return {
        "ready": not missing and not errors,
        "env_exists": ENV_PATH.exists(),
        "missing": missing,
        "errors": errors,
        "fields": [field.model_dump() for field in ENV_FIELDS],
        "values": get_env_values(mask_secret=True),
    }


def write_env_updates(updates: Dict[str, Any]) -> Dict[str, str]:
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
        new_lines.append("# 媒体下载管理系统环境配置")
    for field in ENV_FIELDS:
        if field.key in clean_updates and field.key not in seen:
            new_lines.append(f"{field.key}={clean_updates[field.key]}")

    content = "\n".join(new_lines).rstrip() + "\n"
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = ENV_PATH.stat().st_mode if ENV_PATH.exists() else None
    temp_path = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=ENV_PATH.parent,
            prefix=".env.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = Path(temp_file.name)
        if existing_mode is not None:
            os.chmod(temp_path, existing_mode)
        os.replace(temp_path, ENV_PATH)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()

    persisted = read_env_file()
    mismatched = {
        key: value
        for key, value in clean_updates.items()
        if persisted.get(key) != value
    }
    if mismatched:
        raise OSError(f".env 写入校验失败: {', '.join(sorted(mismatched))}")
    return {key: persisted[key] for key in clean_updates}
