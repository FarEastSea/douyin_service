import os
import re
import shlex
from threading import RLock
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from pydantic import BaseModel

from app.core.diagnostics import get_runtime_errors


def _find_project_root() -> Path:
    """以 main.py 所在目录为项目根目录，不依赖启动工作目录或部署绝对路径。"""
    current_file = Path(__file__).resolve()
    for parent in current_file.parents:
        if (parent / "main.py").is_file():
            return parent
    raise RuntimeError(f"无法从 {current_file} 定位包含 main.py 的项目根目录")


# 项目整体迁移时该路径会随 main.py 自动变化。
ENV_PATH = _find_project_root() / ".env"
_LOCAL_CONFIG_GENERATION = 0
_LOCAL_CONFIG_LOCK = RLock()
_DOWNLOAD_PATH_ENV_KEYS = {
    "DOWNLOAD_ROOT", "DOWNLOAD_DIR", "X_DOWNLOAD_DIR",
    "DOUYIN_DOWNLOAD_SUBDIR", "X_DOWNLOAD_SUBDIR",
}


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
    EnvField(key="ADMIN_TOKEN", label="管理 Token", group="安全", default="", required=True, secret=True, help="所有管理 API 的 Bearer Token；修改后当前浏览器会自动更新登录态"),
    EnvField(key="CORS_ALLOWED_ORIGINS", label="允许的跨域来源", group="安全", default="", help="逗号分隔的完整来源，例如 https://admin.example.com；同源访问无需填写"),
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
    EnvField(key="REQUEST_DELAY", label="抖音接口最小请求间隔（秒）", group="抖音", default="3.0", help="所有 Worker 共享，避免多个任务在同一秒集中请求抖音"),
    EnvField(key="AUTHOR_CHECK_DELAY", label="作者检查间隔（秒）", group="抖音", default="30.0"),
    EnvField(key="DOUYIN_RISK_COOLDOWN_SECONDS", label="抖音风控冷却时长（秒）", group="抖音", default="300"),
    EnvField(key="DOUYIN_RISK_AUTO_RETRY", label="风控冷却后自动恢复一次", group="抖音", default="true"),
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


def get_env_file_signature() -> Tuple[int, int]:
    """返回可快速比较的配置文件签名。"""
    try:
        stat = ENV_PATH.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return 0, 0


def get_local_config_generation() -> int:
    with _LOCAL_CONFIG_LOCK:
        return _LOCAL_CONFIG_GENERATION


def _increment_local_config_generation() -> int:
    global _LOCAL_CONFIG_GENERATION
    with _LOCAL_CONFIG_LOCK:
        _LOCAL_CONFIG_GENERATION += 1
        return _LOCAL_CONFIG_GENERATION


def parse_cors_origins(value: Any) -> List[str]:
    """解析并规范化显式 CORS 来源；空值表示不开放跨域访问。"""
    origins: List[str] = []
    for item in str(value or "").split(","):
        origin = item.strip().rstrip("/")
        if not origin:
            continue
        if origin == "*":
            raise ValueError("允许的跨域来源不能使用通配符 *")
        try:
            parsed = urlsplit(origin)
            port = parsed.port
        except ValueError as exc:
            raise ValueError(f"跨域来源格式不正确：{origin}") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(f"跨域来源必须是完整的 http/https 来源且不能包含路径：{origin}")
        hostname = parsed.hostname.lower()
        host = f"[{hostname}]" if ":" in hostname else hostname
        default_port = 443 if parsed.scheme.lower() == "https" else 80
        if port is not None and port != default_port:
            host = f"{host}:{port}"
        normalized = f"{parsed.scheme.lower()}://{host}"
        if normalized not in origins:
            origins.append(normalized)
    return origins


def _validate_env_value(key: str, value: str) -> None:
    field = FIELD_MAP[key]
    forbidden = []
    if "\r" in value or "\n" in value:
        forbidden.append("换行")
    if "$" in value:
        forbidden.append("$")
    if "`" in value:
        forbidden.append("反引号")
    if "\\" in value:
        forbidden.append("反斜杠")
    if forbidden:
        raise ValueError(f"{field.label}包含不允许的字符：{'、'.join(forbidden)}")
    if key == "ADMIN_TOKEN" and value and not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", value):
        raise ValueError("管理 Token 只能包含英文字母、数字及 . _ ~ + / = -")
    if key == "CORS_ALLOWED_ORIGINS":
        parse_cors_origins(value)

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
        values[key.strip()] = _decode_env_value(value)
    return values


def _decode_env_value(value: str) -> str:
    """读取由 shell 安全引号包裹的值，同时兼容历史未加引号的 .env。"""
    raw = str(value or "").strip()
    if not raw or raw[0] not in {"'", '"'}:
        return raw
    try:
        parts = shlex.split(raw, comments=False, posix=True)
    except ValueError:
        return raw
    return parts[0] if len(parts) == 1 else raw


def serialize_env_value(value: Any) -> str:
    """生成可被 Bash ``source`` 安全读取且可无损还原的单行值。"""
    return shlex.quote("" if value is None else str(value))


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

    existing_error_keys = {item.get("key") for item in errors}
    errors.extend(item for item in get_runtime_errors() if item.get("key") not in existing_error_keys)

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
    version_client = None
    try:
        from app.core import redis_client as redis_runtime

        version_client = redis_runtime.capture_config_version_client()
    except Exception:
        pass
    clean_updates = {}
    for key, value in updates.items():
        if key not in FIELD_MAP:
            continue
        if value == "********" and FIELD_MAP[key].secret and current.get(key):
            continue
        raw_value = "" if value is None else str(value)
        _validate_env_value(key, raw_value)
        clean_value = raw_value.strip()
        clean_updates[key] = clean_value

    lines = ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines() if ENV_PATH.exists() else []
    seen = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in clean_updates:
                new_lines.append(f"{key}={serialize_env_value(clean_updates[key])}")
                seen.add(key)
                continue
            if key in _DOWNLOAD_PATH_ENV_KEYS:
                # 同步修复历史版本遗留的 DOWNLOAD_DIR/X_DOWNLOAD_DIR。虽然新配置
                # 已改用根目录 + 子目录，它们仍会在 ``source .env`` 时被 Bash 解析。
                _, existing_value = stripped.split("=", 1)
                new_lines.append(f"{key}={serialize_env_value(_decode_env_value(existing_value))}")
                continue
        new_lines.append(line)

    if not new_lines:
        new_lines.append("# 媒体下载管理系统环境配置")
    for field in ENV_FIELDS:
        if field.key in clean_updates and field.key not in seen:
            new_lines.append(f"{field.key}={serialize_env_value(clean_updates[field.key])}")

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

    _increment_local_config_generation()
    try:
        from app.core import redis_client as redis_runtime

        redis_runtime.bump_config_version(client=version_client)
    except Exception:
        # .env 是权威来源；Redis 版本只负责跨进程加速失效，失败不能回滚
        # 已经完成且校验通过的持久化写入。
        pass
    try:
        from app.core.config import settings

        settings.invalidate()
        settings.snapshot()
    except Exception:
        # 配置降级错误会由 WebSettings 自身登记；持久化结果仍然有效。
        pass
    return {key: persisted[key] for key in clean_updates}
