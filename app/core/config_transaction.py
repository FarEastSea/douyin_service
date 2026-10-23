"""数据库/.env 变更协调。持久化日志仅放在权限受控的本地运行目录。"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager, asynccontextmanager
from contextvars import ContextVar
import json
import logging
import os
from pathlib import Path
import time
from uuid import uuid4

from sqlalchemy import select, create_engine, text

from app.core import env_config

_configuration_lease = ContextVar("configuration_lease", default=False)
logger = logging.getLogger(__name__)


def journal_path() -> Path:
    return env_config.ENV_PATH.parent / ".runtime" / "config-change.json"


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = path.with_name(path.name + ".tmp")
    with open(candidate, "w", encoding="utf-8") as handle:
        os.chmod(candidate, 0o600)
        json.dump(value, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(candidate, path)


@contextmanager
def configuration_lock():
    if _configuration_lease.get():
        yield
        return
    path = journal_path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0"); handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@asynccontextmanager
async def configuration_session():
    """配置入口先取文件锁，再锁数据库记录，所有写入统一锁顺序。"""
    lock = configuration_lock()
    acquisition = asyncio.create_task(asyncio.to_thread(lock.__enter__))
    try:
        await asyncio.shield(acquisition)
    except asyncio.CancelledError:
        await acquisition
        await asyncio.to_thread(lock.__exit__, None, None, None)
        raise
    token = _configuration_lease.set(True)
    try:
        yield
    finally:
        _configuration_lease.reset(token)
        await asyncio.to_thread(lock.__exit__, None, None, None)


def pending_values() -> dict | None:
    path = journal_path()
    if path.exists():
        entry = json.loads(path.read_text(encoding="utf-8"))
        if entry["state"] == "prepared":
            return entry["old_values"]
    return None


def _restore(entry: dict) -> None:
    if entry["old_exists"]:
        candidate = env_config.ENV_PATH.with_name(".env.restore.tmp")
        with open(candidate, "w", encoding="utf-8", newline="") as handle:
            os.chmod(candidate, entry["old_mode"])
            handle.write(entry["old_content"])
            handle.flush(); os.fsync(handle.fileno())
        os.replace(candidate, env_config.ENV_PATH)
    elif env_config.ENV_PATH.exists():
        env_config.ENV_PATH.unlink()


def recover_config_change() -> None:
    with configuration_lock():
        path = journal_path()
        if not path.exists():
            return
        entry = json.loads(path.read_text(encoding="utf-8"))
        args = {"connect_timeout": 5} if entry["database_url"].startswith(("postgresql", "mysql")) else {}
        engine = create_engine(entry["database_url"], connect_args=args)
        try:
            committed = entry.get("state") == "committed"
            if not committed:
                with engine.connect() as connection:
                    committed = connection.execute(text("SELECT value FROM system_config WHERE key=:key"),
                                                   {"key": "config-change:" + entry["id"]}).scalar()
            if not committed:
                _restore(entry)
            else:
                entry["state"] = "committed"
                atomic_json(path, entry)
            env_config.notify_config_changed()
            path.unlink()
        finally:
            engine.dispose()


async def commit_configuration(db, updates: dict) -> dict:
    """持久化结果和后续发布结果分开；已提交的配置不会再次报保存失败。"""
    async with configuration_session():
        return await _commit_configuration(db, updates)


async def _commit_configuration(db, updates: dict) -> dict:
    from app.core.config import settings
    from app.models.models import SystemConfig
    path = journal_path()
    committed = False
    entry = None
    transition = None
    transition_phase = None
    transition_entered = False
    try:
        if path.exists():
            raise RuntimeError("存在未恢复的配置变更，请先恢复后再保存")
        for key, value in updates.items():
            if key in env_config.FIELD_MAP:
                env_config._validate_env_value(key, "" if value is None else str(value))
        from app.core.config import Settings
        from app.core.queue_configuration import queue_transition
        candidate = Settings.model_validate({**settings.snapshot().model_dump(), **updates})
        transition = queue_transition(candidate)
        transition_phase = await asyncio.to_thread(transition.__enter__)
        transition_entered = True
        entry = {
            "id": uuid4().hex, "state": "prepared", "created_at": time.time(),
            "database_url": settings.snapshot().effective_database_url,
            "old_values": env_config.read_env_file(raw=True),
            "old_exists": env_config.ENV_PATH.exists(),
            "old_mode": (env_config.ENV_PATH.stat().st_mode & 0o777) if env_config.ENV_PATH.exists() else 0o600,
            "old_content": env_config.ENV_PATH.read_text(encoding="utf-8") if env_config.ENV_PATH.exists() else "",
        }
        await db.flush()
        await asyncio.to_thread(atomic_json, path, entry)
        db.add(SystemConfig(key="config-change:" + entry["id"], value="committed"))
        await asyncio.to_thread(env_config.write_env_updates, updates, coordinated=True)
        await db.commit()
        committed = True
    except BaseException:
        await db.rollback()
        if entry:
            # commit 连接中断的结果可能不确定；重新读取持久化标记后才能补偿。
            try:
                marker = await db.scalar(select(SystemConfig.value).where(
                    SystemConfig.key == "config-change:" + entry["id"]))
                if marker == "committed":
                    committed = True
                else:
                    await asyncio.to_thread(_restore, entry)
                    await asyncio.to_thread(env_config.notify_config_changed)
                    if path.exists():
                        path.unlink()
            except Exception:
                # 保留日志，启动恢复会依据持久化标记决定提交或补偿；不能猜测提交结果。
                pass
        if not committed:
            if transition_entered:
                try:
                    await asyncio.to_thread(transition.__exit__, RuntimeError, RuntimeError("配置未提交"), None)
                except Exception:
                    logger.warning("未提交配置的后台进程恢复待重试", exc_info=False)
            raise
    # 提交证明独立传给进程协调器；发布清单失败也不能走未提交的旧配置恢复。
    transition_phase["committed"] = True
    outcome = {"configuration_state": "applied"}
    try:
        # committed 是持久化的提交证明；清单仍保留到发布和协调全部完成。
        entry["state"] = "committed"
        await asyncio.to_thread(atomic_json, path, entry)
        await asyncio.to_thread(env_config.notify_config_changed)
        active_transition, transition = transition, None
        await asyncio.to_thread(active_transition.__exit__, None, None, None)
        path.unlink()
    except Exception as exc:
        outcome["configuration_state"] = "recovering"
        logger.warning("配置已提交，发布协调待自动恢复（%s）", type(exc).__name__)
    finally:
        try:
            if transition_entered and transition is not None:
                await asyncio.to_thread(transition.__exit__, RuntimeError, RuntimeError("配置已提交，发布待恢复"), None)
        except Exception:
            outcome["configuration_state"] = "recovering"
            logger.warning("已提交配置的后台进程恢复待重试", exc_info=False)
    return outcome
