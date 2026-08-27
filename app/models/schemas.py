"""
Pydantic 数据模型 - 用于 API 请求/响应验证

为什么这样设计：
1. 分离请求模型(Create/Update)和响应模型(Response)
2. 使用 Optional 处理可选字段
3. 提供完整的类型提示，自动生成 API 文档
"""

from pydantic import BaseModel, Field
from typing import Dict, Optional, List
from datetime import datetime
from enum import Enum


# ============ 枚举类型 ============

class TaskStatus(str, Enum):
    """任务状态枚举"""
    PENDING = "pending"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkType(str, Enum):
    """作品类型枚举"""
    VIDEO = "video"
    IMAGES = "images"


# ============ 作者相关 ============

class AuthorCreate(BaseModel):
    """创建作者请求"""
    share_url: str = Field(..., description="抖音分享链接")
    is_subscribed: bool = Field(False, description="是否订阅")
    check_interval: int = Field(21600, description="检查间隔(秒)", ge=3600)


class AuthorUpdate(BaseModel):
    """更新作者请求"""
    is_subscribed: Optional[bool] = None
    check_interval: Optional[int] = Field(None, ge=3600)


class AuthorResponse(BaseModel):
    """作者响应"""
    id: int
    sec_uid: str
    nickname: Optional[str]
    share_url: Optional[str]
    avatar_url: Optional[str]
    is_subscribed: bool
    check_interval: int
    last_check_time: Optional[datetime]
    total_works: int
    downloaded_works: int
    last_error: Optional[str] = None
    created_at: datetime
    already_exists: bool = False
    position: Optional[int] = None
    auto_update_status: Optional[str] = None
    auto_update_message: Optional[str] = None
    last_auto_update_at: Optional[datetime] = None
    expected_next_auto_update_at: Optional[datetime] = None
    is_last_breakpoint: bool = False
    profile_history_count: int = 0
    
    class Config:
        from_attributes = True


# ============ 作品相关 ============

class WorkFileItem(BaseModel):
    """作品内单个文件（任务）信息，用于图集单文件管理"""
    task_id: int
    file_index: int
    status: str
    file_name: Optional[str] = None
    preview_url: Optional[str] = None
    media_type: str = "image"
    local_available: bool = False


class WorkResponse(BaseModel):
    """作品响应"""
    id: int
    aweme_id: str
    author_id: int
    title: Optional[str]
    work_type: str
    image_count: int
    is_downloaded: bool
    discovered_at: datetime
    published_at: Optional[datetime] = None
    video_url: Optional[str] = None
    cover_url: Optional[str] = None
    duration_ms: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    music_title: Optional[str] = None
    music_author: Optional[str] = None
    music_url: Optional[str] = None
    hashtags: List[str] = Field(default_factory=list)
    metadata_schema_version: int = 1
    raw_data_version: int = 1
    metadata_refreshed_at: Optional[datetime] = None
    digg_count: Optional[int] = None
    comment_count: Optional[int] = None
    collect_count: Optional[int] = None
    share_count: Optional[int] = None
    play_count: Optional[int] = None
    image_urls: List[str] = Field(default_factory=list)
    primary_preview_url: Optional[str] = None
    download_status: str = "not_started"
    completed_task_count: int = 0
    total_task_count: int = 0
    is_excluded: bool = False
    excluded_file_indices: List[int] = Field(default_factory=list)
    files: List[WorkFileItem] = Field(default_factory=list)
    
    class Config:
        from_attributes = True


# ============ 下载任务相关 ============

class DownloadTaskCreate(BaseModel):
    """创建下载任务请求"""
    share_url: str = Field(..., description="抖音分享链接（用户主页或单个作品）")
    start_index: int = Field(1, description="起始作品序号", ge=1)
    wait_time: float = Field(1.0, description="下载间隔(秒)", ge=0)


class DownloadTaskResponse(BaseModel):
    """下载任务响应"""
    id: int
    work_id: int
    celery_task_id: Optional[str]
    file_index: int
    file_name: Optional[str]
    status: str
    total_bytes: int
    downloaded_bytes: int
    download_speed: float
    progress_percent: float
    file_path: Optional[str]
    error_message: Optional[str]
    error_code: Optional[str] = None
    error_category: Optional[str] = None
    error_action: Optional[str] = None
    retry_after: int = 0
    retry_count: int
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    # 关联信息
    author_id: Optional[int] = None
    author_nickname: Optional[str] = None
    aweme_id: Optional[str] = None
    work_title: Optional[str] = None
    work_type: Optional[str] = None
    published_at: Optional[datetime] = None
    image_count: int = 0
    preview_media_type: Optional[str] = None
    preview_url: Optional[str] = None
    local_preview_available: bool = False
    
    class Config:
        from_attributes = True


class TaskProgressResponse(BaseModel):
    """任务进度响应"""
    task_id: int
    celery_task_id: Optional[str]
    status: str
    file_name: Optional[str]
    total_bytes: int
    downloaded_bytes: int
    progress_percent: float
    download_speed: float  # 字节/秒
    eta_seconds: Optional[int]  # 预计剩余时间


class BatchDownloadRequest(BaseModel):
    """批量下载请求"""
    share_url: str = Field(..., description="作者分享链接")
    download_all: bool = Field(True, description="是否下载全部作品")
    work_ids: Optional[List[str]] = Field(None, description="指定作品ID列表")


class BatchDownloadResponse(BaseModel):
    """批量下载响应"""
    url_type: str = Field("author", description="链接类型: author / work")
    author_id: Optional[int] = None
    author_nickname: Optional[str] = None
    total_works: int = 0
    created_tasks: int = 0
    task_ids: List[int] = []
    author_already_exists: bool = Field(False, description="作者是否已经存在于数据库中")
    author_position: Optional[int] = Field(None, description="作者在列表中的位置(0-based)，仅当作者已存在时返回")


# ============ 下载历史相关 ============

class DownloadHistoryResponse(BaseModel):
    """下载历史响应"""
    id: int
    task_id: int
    work_id: int
    author_nickname: Optional[str]
    work_title: Optional[str]
    file_path: Optional[str]
    file_size: Optional[int]
    download_duration: Optional[int]
    completed_at: datetime
    
    class Config:
        from_attributes = True


# ============ 系统配置相关 ============

class CookieUpdate(BaseModel):
    """更新 Cookie 请求"""
    cookie: str = Field(..., description="抖音 Cookie")


class SystemStatus(BaseModel):
    """系统状态响应"""
    redis_connected: bool
    celery_workers: int
    pending_tasks: int
    downloading_tasks: int
    total_authors: int
    subscribed_authors: int
    total_downloads: int
    worker_process_running: bool = False
    beat_process_running: bool = False


# ============ 通用响应 ============

class MessageResponse(BaseModel):
    """通用消息响应"""
    success: bool
    message: str
    data: Optional[dict] = None


class PaginatedResponse(BaseModel):
    """分页响应基类"""
    total: int
    page: int
    page_size: int
    pages: int


class PaginatedAuthorsResponse(BaseModel):
    """作者分页响应"""
    items: List[AuthorResponse]
    total: int
    page: int
    page_size: int
    pages: int


class PaginatedWorksResponse(BaseModel):
    """作品分页响应"""
    items: List[WorkResponse]
    total: int
    page: int
    page_size: int
    pages: int


class PaginatedTasksResponse(BaseModel):
    """任务分页响应"""
    items: List[DownloadTaskResponse]
    total: int
    page: int
    page_size: int
    pages: int
    status_counts: Dict[str, int] = Field(default_factory=dict)


# ============ X/Twitter 下载相关 ============

class XDownloadRequest(BaseModel):
    """X/Twitter 下载请求"""
    profile_url: str = Field(..., description="X/Twitter 个人主页 URL 或用户名")


class XDownloadTaskResponse(BaseModel):
    """X/Twitter 下载任务响应"""
    id: int
    username: str
    profile_url: str
    x_author_id: Optional[int] = None
    status: str
    phase: Optional[str] = None
    engine_name: Optional[str] = None
    celery_task_id: Optional[str]
    download_dir: Optional[str]
    file_count: int
    total_media_count: int = 0
    downloaded_media_count: int = 0
    progress_percent: float = 0
    output_log: Optional[str]
    last_log_line: Optional[str] = None
    error_message: Optional[str]
    error_code: Optional[str] = None
    retry_count: int = 0
    last_heartbeat_at: Optional[datetime] = None
    author_display_name: Optional[str] = None
    author_account_status: Optional[str] = None
    has_live_state: bool = False
    preview_count: int = 0
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]

    class Config:
        from_attributes = True


class WorkStatsSnapshotResponse(BaseModel):
    """作品互动统计历史。"""
    id: int
    work_id: int
    digg_count: Optional[int] = None
    comment_count: Optional[int] = None
    collect_count: Optional[int] = None
    share_count: Optional[int] = None
    play_count: Optional[int] = None
    observed_at: datetime
    source: str

    class Config:
        from_attributes = True


class XMediaAssetResponse(BaseModel):
    id: int
    task_id: int
    media_type: str
    filename: str
    size_bytes: int = 0
    mime_type: Optional[str] = None
    preview_url: str
    download_url: str
    created_at: datetime


class PaginatedXTasksResponse(BaseModel):
    """X/Twitter 任务分页响应"""
    items: List[XDownloadTaskResponse]
    total: int
    page: int
    page_size: int
    pages: int


class XCookieUpdate(BaseModel):
    """更新 X/Twitter Cookie"""
    cookie: str = Field(..., description="X/Twitter Cookie 内容")


# ============ X/Twitter 作者管理 ============

class XAuthorCreate(BaseModel):
    """创建 X 用户请求"""
    profile_url: str = Field(..., description="X/Twitter 个人主页 URL 或用户名")
    is_subscribed: bool = Field(False, description="是否订阅")
    check_interval: int = Field(3600, description="检查间隔(秒)", ge=300)


class XAuthorResponse(BaseModel):
    """X 用户响应"""
    id: int
    username: str
    profile_url: str
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    account_status: Optional[str] = None
    account_status_label: Optional[str] = None
    last_error: Optional[str] = None
    last_synced_at: Optional[datetime] = None
    is_subscribed: bool
    check_interval: int
    last_check_time: Optional[datetime]
    total_downloads: int
    created_at: datetime

    class Config:
        from_attributes = True


class PaginatedXAuthorsResponse(BaseModel):
    """X 用户分页响应"""
    items: List[XAuthorResponse]
    total: int
    page: int
    page_size: int
    pages: int
