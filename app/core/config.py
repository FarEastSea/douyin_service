"""
配置模块

为什么这样设计：
1. 使用 pydantic-settings 管理配置，支持环境变量覆盖
2. 集中管理所有配置项，便于维护
3. 支持 .env 文件，方便本地开发和生产环境切换
"""

from pydantic_settings import BaseSettings
from pathlib import Path
from typing import Optional

from app.core.env_config import read_env_file


class Settings(BaseSettings):
    # 应用配置
    APP_NAME: str = "媒体下载管理系统"
    DEBUG: bool = False

    # 统一下载目录
    DOWNLOAD_ROOT: str = "/downloads"
    DOUYIN_DOWNLOAD_SUBDIR: str = "douyin"
    X_DOWNLOAD_SUBDIR: str = "X"

    @property
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
            # redis://:password@host:port/db
            return self.REDIS_URL.replace("redis://", f"redis://:{password}@")
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
    
    # 卡住任务检测
    STUCK_TASK_TIMEOUT: int = 600  # 下载任务无进度变化超过此时间(秒)视为卡住，默认10分钟
    
    # Celery Worker 并发数（同时下载任务数）
    MAX_CONCURRENT_DOWNLOADS: int = 3
    
    # 抖音请求配置
    REQUEST_DELAY: float = 3.0  # 分页请求间隔，避免频繁请求
    AUTHOR_CHECK_DELAY: float = 30.0  # 自动检查不同作者之间的间隔
    
    # Cookie 配置（可通过环境变量或API设置）
    DOUYIN_COOKIE: Optional[str] = None
    
    # X/Twitter 下载配置
    @property
    def X_DOWNLOAD_DIR(self) -> str:
        return str(Path(self.DOWNLOAD_ROOT).expanduser() / self.X_DOWNLOAD_SUBDIR)
    X_DOWNLOAD_ENGINE: str = "gallery-dl"
    X_COOKIE: Optional[str] = None
    X_COOKIE_FILE: Optional[str] = None
    X_TASK_LOG_MAX_LINES: int = 400
    X_TASK_LOG_TTL_SECONDS: int = 7 * 24 * 3600
    X_TASK_STATE_TTL_SECONDS: int = 24 * 3600
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"  # 忽略 .env 中多余的变量（如旧版 DATABASE_URL）


class WebSettings:
    """动态读取由网页设置中心维护的配置。"""

    @staticmethod
    def snapshot() -> Settings:
        # 显式传入网页配置，使其优先于进程环境变量，且不缓存旧值。
        values = read_env_file()
        # 首次升级兼容：将旧抖音完整目录拆成统一根目录和子目录。
        if "DOWNLOAD_ROOT" not in values and values.get("DOWNLOAD_DIR"):
            legacy = Path(values["DOWNLOAD_DIR"]).expanduser()
            values["DOWNLOAD_ROOT"] = str(legacy.parent)
            values["DOUYIN_DOWNLOAD_SUBDIR"] = legacy.name
        return Settings.model_validate(values)

    def __getattr__(self, name: str):
        return getattr(self.snapshot(), name)


settings = WebSettings()

# 确保下载目录存在
def ensure_download_dir():
    """确保下载目录存在"""
    download_path = Path(settings.DOWNLOAD_DIR)
    if not download_path.exists():
        download_path.mkdir(parents=True, exist_ok=True)
    
    x_download_path = Path(settings.X_DOWNLOAD_DIR)
    if not x_download_path.exists():
        x_download_path.mkdir(parents=True, exist_ok=True)
    
    return Path(settings.DOWNLOAD_ROOT)

