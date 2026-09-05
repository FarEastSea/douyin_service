"""
数据库 ORM 模型定义

为什么这样设计：
1. Author 表：存储作者信息和订阅状态，last_aweme_id 用于增量检查新作品
2. Work 表：存储发现的作品，image_urls 用 JSON 存储图集URL列表
3. DownloadTask 表：每个文件一个任务，支持断点续传（temp_file_path）
4. DownloadHistory 表：归档已完成的下载，便于查询统计
"""

from sqlalchemy import (
    Boolean,
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text as sql_text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.models.database import Base
import json
from datetime import datetime

from app.models.work_media import (
    normalize_image_urls,
    normalize_optional_urls,
    prepare_image_urls_for_storage,
    prepare_optional_urls_for_storage,
)


class Author(Base):
    """作者/订阅表"""
    __tablename__ = "authors"
    __table_args__ = (
        Index(
            "idx_authors_is_subscribed_true",
            "is_subscribed",
            postgresql_where=sql_text("is_subscribed IS TRUE"),
        ),
    )
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    sec_uid = Column(String(255), unique=True, nullable=False, index=True)
    nickname = Column(String(255))
    share_url = Column(Text)
    avatar_url = Column(Text)  # 头像URL
    
    # 订阅相关
    is_subscribed = Column(Boolean, default=False)
    check_interval = Column(Integer, default=21600)  # 检查间隔(秒)
    last_check_time = Column(DateTime)
    last_auto_update_at = Column(DateTime)
    last_full_reconcile_at = Column(DateTime, server_default=func.now())
    last_aweme_id = Column(String(64))  # 上次最新作品ID
    
    # 统计
    total_works = Column(Integer, default=0)
    downloaded_works = Column(Integer, default=0)
    
    # 错误信息（后台任务失败时记录）
    last_error = Column(Text)
    
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    
    # 关系
    works = relationship("Work", back_populates="author", cascade="all, delete-orphan")
    profile_history = relationship(
        "AuthorProfileHistory", back_populates="author",
        cascade="all, delete-orphan", passive_deletes=True,
    )


class AuthorProfileHistory(Base):
    """作者昵称或头像变更历史。"""
    __tablename__ = "author_profile_history"
    __table_args__ = (Index("idx_author_profile_history", "author_id", "observed_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    author_id = Column(Integer, ForeignKey("authors.id", ondelete="CASCADE"), nullable=False)
    field_name = Column(String(16), nullable=False)
    old_value = Column(Text)
    new_value = Column(Text)
    observed_at = Column(DateTime, default=datetime.now, nullable=False)
    source = Column(String(32), default="profile_sync")

    author = relationship("Author", back_populates="profile_history")


class Work(Base):
    """作品表"""
    __tablename__ = "works"
    __table_args__ = (
        Index("idx_works_author_id", "author_id"),
        Index("idx_works_author_published", "author_id", "published_at"),
    )
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    aweme_id = Column(String(64), unique=True, nullable=False, index=True)
    author_id = Column(Integer, ForeignKey("authors.id"), nullable=False)
    
    title = Column(Text)  # 作品描述
    work_type = Column(String(16), nullable=False)  # 'video' 或 'images'
    
    # 视频相关
    video_url = Column(Text)
    cover_url = Column(Text)
    duration_ms = Column(Integer)
    width = Column(Integer)
    height = Column(Integer)

    # 音乐、话题与采集元数据
    music_title = Column(Text)
    music_author = Column(Text)
    music_url = Column(Text)
    _hashtags = Column("hashtags", Text)
    metadata_schema_version = Column(Integer, default=1)
    raw_data_version = Column(Integer, default=1)
    metadata_refreshed_at = Column(DateTime)

    # 最新统计仅用于列表快速读取；每次变化都先写入 WorkStatsSnapshot，
    # 因此刷新不会覆盖历史趋势。
    digg_count = Column(BigInteger)
    comment_count = Column(BigInteger)
    collect_count = Column(BigInteger)
    share_count = Column(BigInteger)
    play_count = Column(BigInteger)
    
    # 图集相关
    image_count = Column(Integer, default=0)
    _image_urls = Column("image_urls", Text)  # JSON 存储
    _live_photo_urls = Column("live_photo_urls", Text)  # 与图片索引对齐的实况 MP4 URL
    
    # 状态
    is_downloaded = Column(Boolean, default=False)
    
    # 作品管理：软排除（删除后保留记录但不再下载，防止订阅检查重新拉取）
    is_excluded = Column(Boolean, default=False, nullable=False)
    excluded_at = Column(DateTime, nullable=True)
    # 图集中被单独删除的文件索引（JSON 数组），用于重新下载时跳过
    _excluded_file_indices = Column("excluded_file_indices", Text)
    
    discovered_at = Column(DateTime, server_default=func.now())
    # Actual platform publication time; legacy rows remain nullable until refreshed.
    published_at = Column(DateTime, nullable=True)
    
    # 关系
    author = relationship("Author", back_populates="works")
    download_tasks = relationship("DownloadTask", back_populates="work", cascade="all, delete-orphan")
    stats_snapshots = relationship(
        "WorkStatsSnapshot", back_populates="work",
        cascade="all, delete-orphan", passive_deletes=True,
    )
    
    @property
    def image_urls(self):
        """获取图片URL列表"""
        if self._image_urls:
            return normalize_image_urls(self._image_urls)
        return []
    
    @image_urls.setter
    def image_urls(self, value):
        """设置图片URL列表"""
        normalized_value = prepare_image_urls_for_storage(value)
        if normalized_value:
            self._image_urls = json.dumps(normalized_value)
        else:
            self._image_urls = None

    @property
    def live_photo_urls(self):
        """获取与图片索引对齐的实况视频 URL 列表。"""
        if self._live_photo_urls:
            return normalize_optional_urls(self._live_photo_urls)
        return []

    @live_photo_urls.setter
    def live_photo_urls(self, value):
        normalized_value = prepare_optional_urls_for_storage(value)
        if normalized_value:
            self._live_photo_urls = json.dumps(normalized_value)
        else:
            self._live_photo_urls = None

    @property
    def hashtags(self):
        if not self._hashtags:
            return []
        try:
            values = json.loads(self._hashtags)
        except (TypeError, ValueError):
            return []
        if not isinstance(values, list):
            return []
        return [str(value).strip() for value in values if str(value).strip()]

    @hashtags.setter
    def hashtags(self, value):
        normalized = list(dict.fromkeys(
            str(item).strip().lstrip("#") for item in (value or [])
            if str(item).strip().lstrip("#")
        ))
        self._hashtags = json.dumps(normalized, ensure_ascii=False) if normalized else None
    
    @property
    def excluded_file_indices(self):
        """获取被单独删除的图集文件索引列表"""
        if not self._excluded_file_indices:
            return []
        try:
            data = json.loads(self._excluded_file_indices)
            if isinstance(data, list):
                return sorted({int(i) for i in data})
        except (ValueError, TypeError):
            pass
        return []
    
    @excluded_file_indices.setter
    def excluded_file_indices(self, value):
        """设置被单独删除的图集文件索引列表"""
        if value:
            normalized = sorted({int(i) for i in value})
            self._excluded_file_indices = json.dumps(normalized)
        else:
            self._excluded_file_indices = None


class WorkStatsSnapshot(Base):
    """作品互动统计快照；只追加变化，不覆盖历史。"""
    __tablename__ = "work_stats_snapshots"
    __table_args__ = (
        Index("idx_work_stats_snapshot_observed", "work_id", "observed_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    work_id = Column(Integer, ForeignKey("works.id", ondelete="CASCADE"), nullable=False)
    digg_count = Column(BigInteger)
    comment_count = Column(BigInteger)
    collect_count = Column(BigInteger)
    share_count = Column(BigInteger)
    play_count = Column(BigInteger)
    observed_at = Column(DateTime, default=datetime.now, nullable=False)
    source = Column(String(32), default="douyin_web", nullable=False)

    work = relationship("Work", back_populates="stats_snapshots")


class DownloadTask(Base):
    """下载任务表"""
    __tablename__ = "download_tasks"
    __table_args__ = (
        # 状态+创建时间复合索引（用于分页查询）
        Index('idx_status_created', 'status', 'created_at'),
        # work_id+状态复合索引（用于按作品筛选）
        Index('idx_work_status', 'work_id', 'status'),
        UniqueConstraint(
            'work_id', 'file_index',
            name='uq_download_tasks_work_file_index',
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    work_id = Column(Integer, ForeignKey("works.id"), nullable=False)
    celery_task_id = Column(String(64), index=True)  # Celery 任务ID
    
    file_index = Column(Integer, default=0, nullable=False)  # 文件索引（图集中的第几张，从0开始）
    file_name = Column(String(255))  # 文件名
    
    # 状态: pending, downloading, paused, completed, skipped, failed, cancelled
    status = Column(String(16), default="pending", index=True)

    # 创建任务时固化归档规则，避免网页配置变化影响已排队任务。
    archive_rule_snapshot = Column(Text)
    
    # 进度追踪
    total_bytes = Column(Integer, default=0)
    downloaded_bytes = Column(Integer, default=0)
    download_speed = Column(Float, default=0)  # 字节/秒
    
    # 文件路径
    file_path = Column(Text)  # 最终保存路径
    temp_file_path = Column(Text)  # 临时文件路径（用于断点续传）
    
    # 错误处理
    error_message = Column(Text)
    retry_count = Column(Integer, default=0)
    
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    started_at = Column(DateTime)  # 开始下载时间
    completed_at = Column(DateTime)  # 完成时间
    
    # 关系
    work = relationship("Work", back_populates="download_tasks")
    
    @property
    def progress_percent(self):
        """计算下载进度百分比"""
        if self.total_bytes and self.total_bytes > 0:
            return round(self.downloaded_bytes / self.total_bytes * 100, 2)
        return 0


class DownloadHistory(Base):
    """下载历史表"""
    __tablename__ = "download_history"
    __table_args__ = (
        Index("idx_download_history_completed_at", "completed_at"),
        Index("idx_download_history_task_id", "task_id"),
        Index("idx_download_history_work_id", "work_id"),
    )
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("download_tasks.id", ondelete="CASCADE"), nullable=False)
    work_id = Column(Integer, ForeignKey("works.id", ondelete="CASCADE"), nullable=False)
    
    author_nickname = Column(String(255))
    work_title = Column(Text)
    file_path = Column(Text)
    file_size = Column(Integer)
    download_duration = Column(Integer)  # 下载耗时(秒)
    
    completed_at = Column(DateTime, server_default=func.now())


class XAuthor(Base):
    """X/Twitter 用户管理表"""
    __tablename__ = "x_authors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(255), unique=True, nullable=False, index=True)
    profile_url = Column(Text, nullable=False)
    display_name = Column(String(255))
    avatar_url = Column(Text)
    account_status = Column(String(32), default="active", index=True)
    account_status_label = Column(String(64), default="正常")
    last_error = Column(Text)
    last_synced_at = Column(DateTime)

    is_subscribed = Column(Boolean, default=False)
    check_interval = Column(Integer, default=3600)
    last_check_time = Column(DateTime)
    total_downloads = Column(Integer, default=0)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    download_tasks = relationship("XDownloadTask", back_populates="x_author", cascade="all, delete-orphan")


class XDownloadTask(Base):
    """X/Twitter 下载任务表 - gallery-dl 以整个用户为单位下载"""
    __tablename__ = "x_download_tasks"
    __table_args__ = (
        Index('idx_x_status_created', 'status', 'created_at'),
        Index('idx_x_download_tasks_author_status', 'x_author_id', 'status'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(255), nullable=False, index=True)
    profile_url = Column(Text, nullable=False)
    x_author_id = Column(Integer, ForeignKey("x_authors.id"), nullable=True)
    
    # pending / downloading / completed / failed / cancelled
    status = Column(String(16), default="pending", index=True)
    phase = Column(String(32), default="queued")
    engine_name = Column(String(32), default="gallery-dl")
    celery_task_id = Column(String(64), index=True)
    
    download_dir = Column(Text)
    file_count = Column(Integer, default=0)
    total_media_count = Column(Integer, default=0)
    downloaded_media_count = Column(Integer, default=0)
    progress_percent = Column(Float, default=0)
    output_log = Column(Text, default="")
    last_log_line = Column(Text)
    error_message = Column(Text)
    error_code = Column(String(64))
    retry_count = Column(Integer, default=0)
    
    created_at = Column(DateTime, server_default=func.now())
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    last_heartbeat_at = Column(DateTime)

    x_author = relationship("XAuthor", back_populates="download_tasks")
    media_assets = relationship("XMediaAsset", back_populates="task", cascade="all, delete-orphan")


class XMediaAsset(Base):
    """X 下载得到的单个本地媒体资源。"""
    __tablename__ = "x_media_assets"
    __table_args__ = (
        Index("idx_x_media_task", "task_id", "created_at"),
        Index("idx_x_media_author", "x_author_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("x_download_tasks.id", ondelete="CASCADE"), nullable=False)
    x_author_id = Column(Integer, ForeignKey("x_authors.id", ondelete="CASCADE"), nullable=True)
    media_type = Column(String(16), nullable=False)
    file_path = Column(Text, nullable=False, unique=True)
    filename = Column(Text, nullable=False)
    size_bytes = Column(Integer, default=0)
    mime_type = Column(String(128))
    created_at = Column(DateTime, server_default=func.now())

    task = relationship("XDownloadTask", back_populates="media_assets")


class PlatformDownloadTask(Base):
    """可由平台注册表驱动的主页或单条作品下载任务。"""
    __tablename__ = "platform_download_tasks"
    __table_args__ = (
        Index("idx_platform_task_platform_status", "platform", "status", "created_at"),
        Index("idx_platform_task_source", "platform", "source_key"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(32), nullable=False, index=True)
    source_key = Column(String(255), nullable=False)
    source_url = Column(Text, nullable=False)
    source_type = Column(String(16), nullable=False, default="profile")
    status = Column(String(16), default="pending", index=True)
    phase = Column(String(32), default="queued")
    engine_name = Column(String(32), default="gallery-dl")
    celery_task_id = Column(String(64), index=True)
    download_dir = Column(Text)
    file_count = Column(Integer, default=0)
    downloaded_media_count = Column(Integer, default=0)
    progress_percent = Column(Float, default=0)
    output_log = Column(Text, default="")
    last_log_line = Column(Text)
    error_message = Column(Text)
    error_code = Column(String(64))
    retry_count = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    last_heartbeat_at = Column(DateTime)

    media_assets = relationship(
        "PlatformMediaAsset", back_populates="task", cascade="all, delete-orphan"
    )


class PlatformMediaAsset(Base):
    """通用平台任务下载得到的本地媒体资源。"""
    __tablename__ = "platform_media_assets"
    __table_args__ = (
        Index("idx_platform_media_task", "task_id", "created_at"),
        Index("idx_platform_media_platform", "platform", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(
        Integer,
        ForeignKey("platform_download_tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    platform = Column(String(32), nullable=False, index=True)
    media_type = Column(String(16), nullable=False)
    file_path = Column(Text, nullable=False)
    filename = Column(Text, nullable=False)
    size_bytes = Column(Integer, default=0)
    mime_type = Column(String(128))
    created_at = Column(DateTime, server_default=func.now())

    task = relationship("PlatformDownloadTask", back_populates="media_assets")


class PlatformCredential(Base):
    """非抖音平台的加密登录凭据；接口永不返回密文。"""
    __tablename__ = "platform_credentials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(32), unique=True, nullable=False, index=True)
    encrypted_cookie = Column(Text, nullable=False)
    cookie_fingerprint = Column(String(16), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class SystemConfig(Base):
    """非敏感运行配置与任务状态；平台凭据使用独立密文模型。"""
    __tablename__ = "system_config"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(64), unique=True, nullable=False)
    value = Column(Text)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class DouyinAccountProfile(Base):
    """单一、低并发的抖音请求上下文；敏感值只保存密文。"""
    __tablename__ = "douyin_account_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), default="默认账号", nullable=False)
    encrypted_cookie = Column(Text, nullable=False)
    encrypted_proxy_url = Column(Text)
    cookie_fingerprint = Column(String(16), nullable=False)
    uifid_fingerprint = Column(String(16))
    user_agent = Column(Text, nullable=False)
    proxy_enabled = Column(Boolean, default=False, nullable=False)
    status = Column(String(32), default="unknown", nullable=False, index=True)
    isolation_reason = Column(String(64))
    consecutive_failures = Column(Integer, default=0, nullable=False)
    last_success_at = Column(DateTime)
    last_failure_at = Column(DateTime)
    last_failure_code = Column(String(64))
    last_checked_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class SubscriptionCheckReport(Base):
    """一次订阅自动/手动检查的可审计结果。"""
    __tablename__ = "subscription_check_reports"
    __table_args__ = (Index("idx_subscription_report_started", "started_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    celery_task_id = Column(String(64), index=True)
    trigger_type = Column(String(16), nullable=False, default="auto")
    status = Column(String(32), nullable=False, default="running", index=True)
    total_authors = Column(Integer, default=0)
    due_authors = Column(Integer, default=0)
    checked_authors = Column(Integer, default=0)
    success_authors = Column(Integer, default=0)
    new_works = Column(Integer, default=0)
    warning_authors = Column(Integer, default=0)
    failed_authors = Column(Integer, default=0)
    skipped_authors = Column(Integer, default=0)
    remaining_authors = Column(Integer, default=0)
    summary = Column(Text)
    details_json = Column(Text, default="[]")
    started_at = Column(DateTime, default=datetime.now, nullable=False)
    finished_at = Column(DateTime)

