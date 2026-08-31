"""
配置模块

为什么这样设计：
1. 网页持久化的 .env 是配置权威来源
2. Pydantic 负责逐项校验，损坏字段可降级到代码默认值
3. 进程内缓存由文件签名和 Redis 配置版本共同失效
"""

from pathlib import Path
from functools import cached_property
from threading import RLock
from typing import Optional
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, ValidationError

from app.core import env_config
from app.core.diagnostics import clear_runtime_errors, report_runtime_error


class Settings(BaseModel):
    # 只校验 WebSettings 显式传入的网页持久化值。若继续继承
    # BaseSettings，删除空字段后进程环境变量会悄悄补位，从而覆盖
    # 网页“清空后回到代码默认值”的语义。
    model_config = ConfigDict(extra="ignore")

    # 应用配置
    APP_NAME: str = "媒体下载管理系统"
    DEBUG: bool = False

    # 管理端安全配置
    ADMIN_TOKEN: str = ""
    CORS_ALLOWED_ORIGINS: str = ""

    # 统一下载目录
    DOWNLOAD_ROOT: str = "/downloads"
    DOUYIN_DOWNLOAD_SUBDIR: str = "douyin"
    X_DOWNLOAD_SUBDIR: str = "X"
    TIKTOK_DOWNLOAD_SUBDIR: str = "TikTok"

    @cached_property
    def DOWNLOAD_DIR(self) -> str:
        return str(Path(self.DOWNLOAD_ROOT).expanduser() / self.DOUYIN_DOWNLOAD_SUBDIR)

    # 数据库配置
    DB_TYPE: str = "postgresql"
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_USER: str = ""
    DB_PASSWORD: str = ""
    DB_NAME: str = "douyin_service"

    @property
    def effective_database_url(self) -> str:
        """根据 DB_TYPE 构建实际的数据库 URL"""
        if self.DB_TYPE == "mysql":
            user_part = self.DB_USER
            if self.DB_PASSWORD:
                user_part = f"{self.DB_USER}:{self.DB_PASSWORD}"
            return f"mysql+pymysql://{user_part}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"
        # 默认 PostgreSQL
        user_part = self.DB_USER
        if self.DB_PASSWORD:
            user_part = f"{self.DB_USER}:{self.DB_PASSWORD}"
        return f"postgresql://{user_part}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    # Redis 配置 - Celery 消息队列和进度缓存
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_PASSWORD: Optional[str] = None
    redis_password: Optional[str] = None  # 兼容小写的环境变量名

    @property
    def redis_url_with_auth(self) -> str:
        """返回带认证的 Redis URL"""
        # 优先使用大写的 REDIS_PASSWORD
        password = self.REDIS_PASSWORD or self.redis_password
        if password:
            for scheme in ("redis://", "rediss://"):
                if self.REDIS_URL.startswith(scheme):
                    remainder = self.REDIS_URL[len(scheme):]
                    authority = remainder.split("/", 1)[0]
                    if "@" not in authority:
                        return f"{scheme}:{quote(password, safe='')}@{remainder}"
        return self.REDIS_URL
    
    # Celery 配置
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/0"
    
    # 下载配置
    DOWNLOAD_CHUNK_SIZE: int = 1024 * 1024  # 1MB 分块下载
    DOWNLOAD_TIMEOUT: int = 30  # 请求超时时间
    DOWNLOAD_RETRY_COUNT: int = 3  # 重试次数
    DOWNLOAD_RETRY_DELAY: int = 5  # 重试延迟(秒)
    
    # 订阅检查配置
    DEFAULT_CHECK_INTERVAL: int = 21600  # 默认检查间隔6小时
    MIN_CHECK_INTERVAL: int = 3600  # 最小检查间隔1小时
    AUTO_CHECK_ENABLED: bool = True
    SUBSCRIPTION_KNOWN_STREAK: int = 12  # 连续命中多少个已知作品后停止增量扫描
    SUBSCRIPTION_MAX_PAGES: int = 50  # 增量扫描安全上限，命中时失败关闭
    SUBSCRIPTION_SAFE_LOOKBACK_PAGES: int = 2  # 即使命中停止边界也至少回看这些页
    SUBSCRIPTION_FULL_RECONCILE_INTERVAL: int = 604800  # 每位作者全量对账间隔，默认7天
    
    # 卡住任务检测
    STUCK_TASK_TIMEOUT: int = 600  # 下载任务无进度变化超过此时间(秒)视为卡住，默认10分钟
    
    # 下载全局并发上限；Celery 本地进程数只是第一层保护。
    MAX_CONCURRENT_DOWNLOADS: int = 3
    
    # 抖音请求配置
    REQUEST_DELAY: float = 3.0  # 所有 Worker 共享的抖音业务请求最小间隔
    AUTHOR_CHECK_DELAY: float = 30.0  # 自动检查不同作者之间的间隔
    DOUYIN_RISK_COOLDOWN_SECONDS: int = 300  # 命中抖音风控后的全局冷却
    DOUYIN_RISK_AUTO_RETRY: bool = True  # 冷却结束后自动恢复一次

    # 通知中心。所有值由网页设置中心持久化，并在发送时动态读取。
    NOTIFY_ENABLED: bool = False
    NOTIFY_ON_NEW_WORKS: bool = True
    NOTIFY_ON_DOWNLOAD_FAILURE: bool = True
    NOTIFY_ON_RISK: bool = True
    NOTIFY_ON_SUBSCRIPTION_FAILURE: bool = True
    NOTIFY_DEDUPE_SECONDS: int = 300
    WEBHOOK_ENABLED: bool = False
    WEBHOOK_URL: str = ""
    WEBHOOK_SECRET: str = ""
    BARK_ENABLED: bool = False
    BARK_SERVER_URL: str = "https://api.day.app"
    BARK_DEVICE_KEY: str = ""
    EMAIL_ENABLED: bool = False
    SMTP_HOST: str = ""
    SMTP_PORT: int = 465
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    SMTP_TO: str = ""
    SMTP_SECURITY: str = "ssl"
    GOTIFY_ENABLED: bool = False
    GOTIFY_SERVER_URL: str = ""
    GOTIFY_TOKEN: str = ""
    
    # Cookie 配置（可通过环境变量或API设置）
    DOUYIN_COOKIE: Optional[str] = None
    
    # X/Twitter 下载配置
    @cached_property
    def X_DOWNLOAD_DIR(self) -> str:
        return str(Path(self.DOWNLOAD_ROOT).expanduser() / self.X_DOWNLOAD_SUBDIR)
    X_DOWNLOAD_ENGINE: str = "gallery-dl"
    X_COOKIE: Optional[str] = None
    X_COOKIE_FILE: Optional[str] = None
    X_TASK_LOG_MAX_LINES: int = 400
    X_TASK_LOG_TTL_SECONDS: int = 7 * 24 * 3600
    X_TASK_STATE_TTL_SECONDS: int = 24 * 3600

    # TikTok 首批复用 gallery-dl；账号配置仍由网页设置中心维护。
    @cached_property
    def TIKTOK_DOWNLOAD_DIR(self) -> str:
        return str(Path(self.DOWNLOAD_ROOT).expanduser() / self.TIKTOK_DOWNLOAD_SUBDIR)
    TIKTOK_DOWNLOAD_ENGINE: str = "gallery-dl"
    TIKTOK_COOKIE: Optional[str] = None
    TIKTOK_COOKIE_FILE: Optional[str] = None
    
class WebSettings:
    """动态读取网页配置，并用可跨进程失效的进程内快照加速。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._cached_settings: Optional[Settings] = None
        self._cached_key: Optional[tuple] = None

    @staticmethod
    def _cache_key() -> tuple:
        source_signature = (
            *env_config.get_env_file_signature(),
            env_config.get_local_config_generation(),
        )
        redis_version = 0
        try:
            from app.core.redis_client import get_config_version_cached

            redis_version = get_config_version_cached(source_signature)
        except Exception:
            pass
        return (*source_signature, redis_version)

    @staticmethod
    def _validated_settings(values: dict[str, str]) -> Settings:
        clear_runtime_errors("SETTINGS_VALIDATION_")
        remaining = dict(values)
        while True:
            try:
                return Settings.model_validate(remaining)
            except ValidationError as exc:
                invalid_keys = {
                    str(error.get("loc", ("UNKNOWN",))[0])
                    for error in exc.errors()
                    if error.get("loc")
                }
                removable = invalid_keys.intersection(remaining)
                for error in exc.errors():
                    location = error.get("loc") or ("UNKNOWN",)
                    key = str(location[0])
                    field = env_config.FIELD_MAP.get(key)
                    report_runtime_error(
                        f"SETTINGS_VALIDATION_{key}",
                        field.label if field else key,
                        field.group if field else "配置",
                        f"配置值格式无效，已回退代码默认值：{error.get('msg', '校验失败')}",
                    )
                if not removable:
                    raise
                for key in removable:
                    remaining.pop(key, None)

    def snapshot_with_key(self) -> tuple[Settings, tuple]:
        cache_key = self._cache_key()
        with self._lock:
            if self._cached_settings is not None and self._cached_key == cache_key:
                return self._cached_settings, cache_key

        # 空字符串表示未配置；剔除后由 Settings 的代码默认值承接。
        values = {
            key: value
            for key, value in env_config.read_env_file().items()
            if str(value).strip()
        }
        # 首次升级兼容：将旧抖音完整目录拆成统一根目录和子目录。
        if "DOWNLOAD_ROOT" not in values and values.get("DOWNLOAD_DIR"):
            legacy = Path(values["DOWNLOAD_DIR"]).expanduser()
            values["DOWNLOAD_ROOT"] = str(legacy.parent)
            values["DOUYIN_DOWNLOAD_SUBDIR"] = legacy.name
        current = self._validated_settings(values)

        with self._lock:
            self._cached_settings = current
            self._cached_key = cache_key
        return current, cache_key

    def snapshot(self) -> Settings:
        return self.snapshot_with_key()[0]

    def invalidate(self) -> None:
        with self._lock:
            self._cached_settings = None
            self._cached_key = None

    def __getattr__(self, name: str):
        return getattr(self.snapshot(), name)


settings = WebSettings()

# 确保下载目录存在
def ensure_download_dir():
    """确保下载目录存在"""
    current = settings.snapshot()
    values = {
        "DOWNLOAD_ROOT": current.DOWNLOAD_ROOT,
        "DOUYIN_DOWNLOAD_SUBDIR": current.DOUYIN_DOWNLOAD_SUBDIR,
        "X_DOWNLOAD_SUBDIR": current.X_DOWNLOAD_SUBDIR,
        "TIKTOK_DOWNLOAD_SUBDIR": current.TIKTOK_DOWNLOAD_SUBDIR,
    }
    error = env_config.check_download_directory(values)
    if error:
        raise ValueError(error["message"])

    root = Path(current.DOWNLOAD_ROOT).expanduser()
    # 根目录必须由管理员预先创建；仅平台子目录由应用按配置自动创建。
    (root / current.DOUYIN_DOWNLOAD_SUBDIR).mkdir(parents=True, exist_ok=True)
    (root / current.X_DOWNLOAD_SUBDIR).mkdir(parents=True, exist_ok=True)
    (root / current.TIKTOK_DOWNLOAD_SUBDIR).mkdir(parents=True, exist_ok=True)
    return root

