"""
数据库连接和会话管理

为什么这样设计：
1. 使用 SQLAlchemy 2.0 异步引擎，配合 FastAPI 异步特性
2. 提供同步引擎给 Celery 任务使用（Celery 任务是同步的）
3. 使用依赖注入模式管理数据库会话
4. 连接池配置优化并发性能
"""

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import sessionmaker, declarative_base
from app.core.config import settings

Base = declarative_base()

SYNC_DATABASE_URL = settings.effective_database_url

def _build_async_url(sync_url: str) -> str:
    if sync_url.startswith("postgresql://"):
        return sync_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif "pymysql" in sync_url:
        return sync_url.replace("mysql+pymysql://", "mysql+aiomysql://", 1)
    return sync_url

ASYNC_DATABASE_URL = _build_async_url(SYNC_DATABASE_URL)

async_engine = create_async_engine(
    ASYNC_DATABASE_URL,
    echo=settings.DEBUG,
    future=True,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False
)

sync_engine = create_engine(
    SYNC_DATABASE_URL,
    echo=settings.DEBUG,
    future=True,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True
)

# 同步会话工厂
SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    autocommit=False,
    autoflush=False
)


async def get_async_db():
    """FastAPI 依赖注入：获取异步数据库会话"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


def get_sync_db():
    """Celery 任务：获取同步数据库会话"""
    db = SyncSessionLocal()
    try:
        return db
    except Exception:
        db.rollback()
        raise


async def init_db():
    """初始化数据库表，并自动迁移缺失的列"""
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 自动迁移：为已有表添加缺失的列
        await conn.run_sync(_migrate_missing_columns)


def _migrate_missing_columns(connection):
    """检查并添加缺失的列"""
    from sqlalchemy import text, inspect as sa_inspect
    inspector = sa_inspect(connection)

    migrations = [
        ("x_download_tasks", "x_author_id", "ALTER TABLE x_download_tasks ADD COLUMN x_author_id INTEGER REFERENCES x_authors(id)"),
        ("x_authors", "display_name", "ALTER TABLE x_authors ADD COLUMN display_name VARCHAR(255)"),
        ("x_authors", "avatar_url", "ALTER TABLE x_authors ADD COLUMN avatar_url TEXT"),
        ("x_authors", "account_status", "ALTER TABLE x_authors ADD COLUMN account_status VARCHAR(32)"),
        ("x_authors", "account_status_label", "ALTER TABLE x_authors ADD COLUMN account_status_label VARCHAR(64)"),
        ("x_authors", "last_error", "ALTER TABLE x_authors ADD COLUMN last_error TEXT"),
        ("x_authors", "last_synced_at", "ALTER TABLE x_authors ADD COLUMN last_synced_at TIMESTAMP NULL"),
        ("x_download_tasks", "phase", "ALTER TABLE x_download_tasks ADD COLUMN phase VARCHAR(32)"),
        ("x_download_tasks", "engine_name", "ALTER TABLE x_download_tasks ADD COLUMN engine_name VARCHAR(32)"),
        ("x_download_tasks", "total_media_count", "ALTER TABLE x_download_tasks ADD COLUMN total_media_count INTEGER"),
        ("x_download_tasks", "downloaded_media_count", "ALTER TABLE x_download_tasks ADD COLUMN downloaded_media_count INTEGER"),
        ("x_download_tasks", "progress_percent", "ALTER TABLE x_download_tasks ADD COLUMN progress_percent FLOAT"),
        ("x_download_tasks", "last_log_line", "ALTER TABLE x_download_tasks ADD COLUMN last_log_line TEXT"),
        ("x_download_tasks", "error_code", "ALTER TABLE x_download_tasks ADD COLUMN error_code VARCHAR(64)"),
        ("x_download_tasks", "retry_count", "ALTER TABLE x_download_tasks ADD COLUMN retry_count INTEGER"),
        ("x_download_tasks", "last_heartbeat_at", "ALTER TABLE x_download_tasks ADD COLUMN last_heartbeat_at TIMESTAMP NULL"),
        ("works", "is_excluded", "ALTER TABLE works ADD COLUMN is_excluded BOOLEAN DEFAULT FALSE NOT NULL"),
        ("works", "excluded_at", "ALTER TABLE works ADD COLUMN excluded_at TIMESTAMP NULL"),
        ("works", "excluded_file_indices", "ALTER TABLE works ADD COLUMN excluded_file_indices TEXT"),
    ]

    for table_name, column_name, alter_sql in migrations:
        if table_name not in inspector.get_table_names():
            continue
        existing_columns = [col["name"] for col in inspector.get_columns(table_name)]
        if column_name not in existing_columns:
            try:
                connection.execute(text(alter_sql))
                print(f"  Migrate: {table_name} added column {column_name}")
            except Exception as e:
                print(f"  Migrate skip: {table_name}.{column_name} - {e}")


def init_db_sync():
    """同步初始化数据库表（用于 Celery worker 启动时）"""
    Base.metadata.create_all(bind=sync_engine)

