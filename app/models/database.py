"""
数据库连接和会话管理

为什么这样设计：
1. 使用 SQLAlchemy 2.0 异步引擎，配合 FastAPI 异步特性
2. 提供同步引擎给 Celery 任务使用（Celery 任务是同步的）
3. 使用依赖注入模式管理数据库会话
4. 连接池配置优化并发性能
"""

import asyncio
from threading import RLock
import time
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import sessionmaker, declarative_base

from app.core.config import settings
from app.core.diagnostics import clear_runtime_error, report_runtime_error

Base = declarative_base()

def _build_async_url(sync_url: str) -> str:
    if sync_url.startswith("postgresql://"):
        return sync_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif "pymysql" in sync_url:
        return sync_url.replace("mysql+pymysql://", "mysql+aiomysql://", 1)
    return sync_url

_engine_lock = RLock()
_async_engine: Optional[AsyncEngine] = None
_async_engine_key: Optional[tuple] = None
_async_failed_key: Optional[tuple] = None
_async_retry_after = 0.0
_sync_engine: Optional[Engine] = None
_sync_engine_key: Optional[tuple] = None
_sync_failed_key: Optional[tuple] = None
_sync_retry_after = 0.0


def _current_engine_config():
    current, config_key = settings.snapshot_with_key()
    return current, config_key


async def get_async_engine() -> AsyncEngine:
    global _async_engine, _async_engine_key, _async_failed_key, _async_retry_after
    current, config_key = _current_engine_config()
    with _engine_lock:
        if _async_engine is not None and _async_engine_key == config_key:
            return _async_engine
        if _async_engine is not None and _async_failed_key == config_key and time.monotonic() < _async_retry_after:
            return _async_engine

    candidate: Optional[AsyncEngine] = None
    try:
        candidate = create_async_engine(
            _build_async_url(current.effective_database_url),
            echo=current.DEBUG,
            future=True,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
        )
        async with candidate.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        if candidate is not None:
            await candidate.dispose()
        report_runtime_error("DATABASE_ASYNC_CONNECTION", "异步数据库连接", "数据库", exc)
        with _engine_lock:
            _async_failed_key = config_key
            _async_retry_after = time.monotonic() + 1.0
            if _async_engine is not None:
                return _async_engine
        raise RuntimeError("新的异步数据库配置不可用") from exc

    duplicate_engine: Optional[AsyncEngine] = None
    with _engine_lock:
        # 候选连接验证期间，另一个请求可能已完成同一版本的切换。
        # 此时保留已发布的引擎并释放本次重复创建的候选，避免把刚
        # 返回给并发请求的引擎立即 dispose。
        if _async_engine is not None and _async_engine_key == config_key:
            duplicate_engine = candidate
            selected_engine = _async_engine
            old_engine = None
        else:
            old_engine = _async_engine
            _async_engine = candidate
            _async_engine_key = config_key
            _async_failed_key = None
            _async_retry_after = 0.0
            selected_engine = candidate
    clear_runtime_error("DATABASE_ASYNC_CONNECTION")
    if duplicate_engine is not None:
        await duplicate_engine.dispose()
    if old_engine is not None and old_engine is not candidate:
        await old_engine.dispose()
    return selected_engine


def get_sync_engine() -> Engine:
    global _sync_engine, _sync_engine_key, _sync_failed_key, _sync_retry_after
    current, config_key = _current_engine_config()
    with _engine_lock:
        if _sync_engine is not None and _sync_engine_key == config_key:
            return _sync_engine
        if _sync_engine is not None and _sync_failed_key == config_key and time.monotonic() < _sync_retry_after:
            return _sync_engine

    candidate: Optional[Engine] = None
    try:
        candidate = create_engine(
            current.effective_database_url,
            echo=current.DEBUG,
            future=True,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
        )
        with candidate.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        if candidate is not None:
            candidate.dispose()
        report_runtime_error("DATABASE_SYNC_CONNECTION", "同步数据库连接", "数据库", exc)
        with _engine_lock:
            _sync_failed_key = config_key
            _sync_retry_after = time.monotonic() + 1.0
            if _sync_engine is not None:
                return _sync_engine
        raise RuntimeError("新的同步数据库配置不可用") from exc

    duplicate_engine: Optional[Engine] = None
    with _engine_lock:
        if _sync_engine is not None and _sync_engine_key == config_key:
            duplicate_engine = candidate
            selected_engine = _sync_engine
            old_engine = None
        else:
            old_engine = _sync_engine
            _sync_engine = candidate
            _sync_engine_key = config_key
            _sync_failed_key = None
            _sync_retry_after = 0.0
            selected_engine = candidate
    clear_runtime_error("DATABASE_SYNC_CONNECTION")
    if duplicate_engine is not None:
        duplicate_engine.dispose()
    if old_engine is not None and old_engine is not candidate:
        old_engine.dispose()
    return selected_engine


async def get_async_db():
    """FastAPI 依赖注入：获取异步数据库会话"""
    engine = await get_async_engine()
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    async with session_factory() as session:
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
    session_factory = sessionmaker(
        bind=get_sync_engine(),
        autocommit=False,
        autoflush=False,
    )
    db = session_factory()
    try:
        return db
    except Exception:
        db.rollback()
        raise


async def init_db():
    """在线程中执行同步迁移，避免阻塞 FastAPI 事件循环。"""
    await asyncio.to_thread(init_db_sync)


def init_db_sync():
    """在全局数据库锁下执行一次带版本记录的启动迁移。"""
    # 确保所有 ORM 表已注册到 Base.metadata。
    from app.models import models as _models  # noqa: F401
    from app.models.migrations import run_schema_migrations

    engine = get_sync_engine()
    run_schema_migrations(engine, Base.metadata)

