"""Snapshot-based failed-task operations; never iterate a changing failed page."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from uuid import uuid4

from billiard.exceptions import SoftTimeLimitExceeded

from sqlalchemy import literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import redis_client
from app.core.error_handling import sanitize_text
from app.models.database import create_isolated_async_engine
from app.models.models import DownloadTask, PlatformDownloadTask, XDownloadTask
from app.services.unified_task_operations import TaskOperationError, operate_task, parse_task_key


PREFIX = "operations:failed-retry:"
TTL = 7 * 24 * 3600
LOCK_TTL = 3600
LEASE_TTL = 120


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def failed_task_keys(db: AsyncSession, platform: str = "") -> list[str]:
    statements = []
    if not platform or platform == "douyin":
        statements.append(select(literal("douyin").label("platform"), DownloadTask.id.label("id"))
                          .where(DownloadTask.status == "failed"))
    if not platform or platform == "x":
        statements.append(select(literal("x").label("platform"), XDownloadTask.id.label("id"))
                          .where(XDownloadTask.status == "failed"))
    if platform not in {"douyin", "x"}:
        statement = select(PlatformDownloadTask.platform.label("platform"), PlatformDownloadTask.id.label("id"))
        statement = statement.where(PlatformDownloadTask.status == "failed")
        if platform:
            statement = statement.where(PlatformDownloadTask.platform == platform)
        statements.append(statement)
    combined = statements[0] if len(statements) == 1 else union_all(*statements)
    return [f"{row.platform}:{row.id}" for row in (await db.execute(combined)).all()]


def get_job(job_id: str | None = None, *, action: str = "retry") -> dict:
    client = redis_client.redis_client
    job_id = job_id or client.get(PREFIX + ("latest" if action == "retry" else "latest:delete"))
    raw = client.get(PREFIX + str(job_id)) if job_id else None
    if not raw:
        return {"status": "idle"}
    state = json.loads(raw)
    if state["status"] in {"queued", "running"} and client.get(PREFIX + "lock") != state["job_id"]:
        state.update(status="interrupted", error="后台作业已中断或等待超时；已完成操作仍有效，请核对剩余失败任务。")
    return state


def save_job(state: dict) -> None:
    redis_client.redis_client.set(PREFIX + state["job_id"], json.dumps(state, ensure_ascii=False), ex=TTL)


def release_lock(job_id: str) -> None:
    redis_client.redis_client.eval(
        "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end",
        1, PREFIX + "lock", job_id,
    )


def create_job(keys: list[str], platform: str, *, action: str = "retry") -> tuple[dict, bool]:
    if action not in {"retry", "delete"}:
        raise ValueError("不支持的全部失败任务操作")
    client = redis_client.redis_client
    job_id = uuid4().hex
    if not client.set(PREFIX + "lock", job_id, nx=True, ex=LOCK_TTL):
        active_id = client.get(PREFIX + "lock")
        state = get_job(active_id) if active_id else {"status": "idle"}
        if state["status"] == "idle" or state.get("action", "retry") != action or state.get("platform") != platform:
            raise TaskOperationError("另一个失败任务批量作业正在执行，请稍后刷新", 409)
        return state, False
    state = {"job_id": job_id, "platform": platform, "action": action, "status": "queued", "created_at": now(),
             "updated_at": now(), "total": len(keys), "processed": 0, "succeeded": 0,
             "skipped": 0, "failed": 0, "failures": [], "error": None}
    try:
        with client.pipeline(transaction=True) as pipe:
            pipe.set(PREFIX + job_id + ":keys", json.dumps(keys), ex=TTL)
            pipe.set(PREFIX + job_id, json.dumps(state, ensure_ascii=False), ex=TTL)
            pipe.set(PREFIX + ("latest" if action == "retry" else "latest:delete"), job_id, ex=TTL)
            pipe.execute()
    except Exception:
        release_lock(job_id)
        raise
    return state, True


def claim_job(job_id: str, token: str) -> bool:
    return bool(redis_client.redis_client.eval(
        "if redis.call('get',KEYS[1]) ~= ARGV[1] then return 0 end "
        "return redis.call('set',KEYS[2],ARGV[2],'NX','EX',ARGV[3]) and 1 or 0",
        2, PREFIX + "lock", PREFIX + job_id + ":lease", job_id, token, LEASE_TTL,
    ))


def renew_job(job_id: str, token: str) -> bool:
    return bool(redis_client.redis_client.eval(
        "if redis.call('get',KEYS[1]) ~= ARGV[1] or redis.call('get',KEYS[2]) ~= ARGV[2] then return 0 end "
        "redis.call('expire',KEYS[1],ARGV[3]); redis.call('expire',KEYS[2],ARGV[4]); return 1",
        2, PREFIX + "lock", PREFIX + job_id + ":lease", job_id, token, LOCK_TTL, LEASE_TTL,
    ))


def release_claim(job_id: str, token: str) -> None:
    redis_client.redis_client.eval(
        "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end",
        1, PREFIX + job_id + ":lease", token,
    )


async def run_job(job_id: str, token: str) -> dict:
    state = get_job(job_id)
    action = state.get("action", "retry")
    if action not in {"retry", "delete"}:
        raise ValueError("后台作业操作无效")
    keys = json.loads(redis_client.redis_client.get(PREFIX + job_id + ":keys") or "[]")
    engine = create_isolated_async_engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    state.update(status="running", updated_at=now())
    save_job(state)
    try:
        if state.get("in_flight"):
            # A lost worker may have dispatched before persisting its result.
            state["skipped"] += 1
            state["processed"] += 1
            if len(state["failures"]) < 200:
                state["failures"].append({"task_key": state["in_flight"],
                    "message": "上次执行中断，投递结果不确定；本轮未再次操作，请核对任务状态。"})
            state.pop("in_flight")
            save_job(state)
        for key in keys[state["processed"]:]:
            if not await asyncio.to_thread(renew_job, job_id, token):
                raise RuntimeError("后台作业执行权已失效，停止以避免重复操作")
            try:
                async with sessions() as db:
                    platform, task_id = parse_task_key(key)
                    model = DownloadTask if platform == "douyin" else XDownloadTask if platform == "x" else PlatformDownloadTask
                    conditions = [model.id == task_id]
                    if model == PlatformDownloadTask:
                        conditions.append(model.platform == platform)
                    task_status = await db.scalar(select(model.status).where(*conditions).with_for_update())
                    if task_status != "failed":
                        state["skipped"] += 1
                        await db.rollback()
                    else:
                        state["in_flight"] = key
                        await asyncio.to_thread(save_job, state)
                        await asyncio.wait_for(operate_task(db, key, action), timeout=60)
                        state["succeeded"] += 1
            except (SoftTimeLimitExceeded, TimeoutError):
                raise
            except Exception as exc:
                state["failed"] += 1
                if len(state["failures"]) < 200:
                    state["failures"].append({"task_key": key, "message": sanitize_text(str(exc), limit=500)})
                # A queue outage must not reset thousands of tasks to pending without delivery.
                if isinstance(exc, TaskOperationError) and exc.status_code == 503:
                    state["processed"] += 1
                    state.pop("in_flight", None)
                    raise
            state["processed"] += 1
            state.pop("in_flight", None)
            state["updated_at"] = now()
            await asyncio.to_thread(save_job, state)
        state.update(status="partial" if state["failed"] or state["failures"] else "completed", updated_at=now())
    except BaseException as exc:
        state.update(status="interrupted", updated_at=now(), error=sanitize_text(f"{type(exc).__name__}: {exc}", limit=500))
        raise
    finally:
        try:
            save_job(state)
            release_lock(job_id)
            release_claim(job_id, token)
        finally:
            await engine.dispose()
    return state
