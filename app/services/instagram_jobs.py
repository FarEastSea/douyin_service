"""Instagram 的有界投递、进程监督和分流调度。"""

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.core.config import settings
from app.models.models import (
    InstagramAccountState,
    PlatformAuthor,
    PlatformDownloadTask,
    PlatformSubscriptionRun,
    PlatformSubscriptionState,
)
from app.services.instagram_runtime import budget_until, utcnow
from app.services.platform_task_service import create_platform_task


def options_for(task):
    return json.loads(task.instagram_options or "{}")


def due_job(task, now=None):
    now = now or utcnow()
    until = options_for(task).get("not_before")
    return not until or datetime.fromisoformat(until) <= now


def job_priority(task):
    options = options_for(task)
    kind = options.get("kind")
    priority = (
        0
        if kind == "stories"
        or task.source_type == "stories"
        or task.source_key.startswith("story-")
        else 1
        if not options.get("state_id")
        else 3
        if options.get("scan_mode") == "history"
        else 2
    )
    return (
        priority,
        datetime.fromisoformat(options["last_scheduled_at"])
        if options.get("last_scheduled_at")
        else datetime.min,
        task.id,
    )


def stream_priority(state):
    return (
        0
        if state.content_kind == "stories"
        else 3
        if state.scan_mode == "history"
        else 2,
        state.next_check_at,
        state.id,
    )


def dispatch_due(db):
    """单个分发锁、每分钟最多投递一项，不创建数百条排队消息。"""
    from app.core import redis_client
    from app.tasks.platform_download_tasks import download_platform_profile

    lock = redis_client.redis_client.lock(
        "instagram:dispatch", timeout=30, blocking_timeout=0
    )
    if not lock.acquire(blocking=False):
        return {"dispatched": 0}
    try:
        from app.services.subscription_records import cleanup_records

        cleanup_records(db)
        now, current = utcnow(), settings.snapshot()
        # 已登记但未开始的消息同样占用通道；故障回收只针对过期本项目任务。
        inflight = list(
            db.scalars(
                select(PlatformDownloadTask).where(
                    PlatformDownloadTask.platform == "instagram",
                    PlatformDownloadTask.status.in_(("pending", "downloading")),
                    PlatformDownloadTask.celery_task_id.is_not(None),
                )
            )
        )
        for task in inflight:
            heartbeat = task.last_heartbeat_at or task.started_at or task.created_at
            # 数据库旧时间采用本项目既有本地时间字段。
            if heartbeat and heartbeat < datetime.now() - timedelta(minutes=25):
                # 不凭心跳杀进程或替换仍持有 Redis 所有权的执行。
                if not redis_client.redis_client.exists("instagram:execution"):
                    task.status, task.celery_task_id, task.phase = (
                        "pending",
                        None,
                        "recovered",
                    )
                    for run in db.scalars(
                        select(PlatformSubscriptionRun).where(
                            PlatformSubscriptionRun.task_id == task.id,
                            PlatformSubscriptionRun.status == "running",
                        )
                    ):
                        run.status, run.message, run.finished_at = (
                            "interrupted",
                            "旧执行中断，将从持久断点恢复",
                            now,
                        )
                else:
                    return {"dispatched": 0}
            else:
                return {"dispatched": 0}
        db.commit()
        if until := budget_until(db, current, now):
            for state in db.scalars(
                select(PlatformSubscriptionState)
                .join(PlatformAuthor)
                .where(
                    PlatformAuthor.platform == "instagram",
                    PlatformSubscriptionState.enabled.is_(True),
                    PlatformSubscriptionState.next_check_at <= now,
                )
            ):
                state.deferred_reason = "接口预算耗尽，等待额度恢复"
            db.commit()
            return {
                "dispatched": 0,
                "reason": "waiting_budget",
                "until": until.isoformat(),
            }
        pending = [
            task
            for task in db.scalars(
                select(PlatformDownloadTask)
                .where(
                    PlatformDownloadTask.platform == "instagram",
                    PlatformDownloadTask.status == "pending",
                    PlatformDownloadTask.celery_task_id.is_(None),
                )
                .order_by(PlatformDownloadTask.created_at, PlatformDownloadTask.id)
            )
            if due_job(task, now)
        ]
        eligible = []
        for task in pending:
            state_id = options_for(task).get("state_id")
            state = db.get(PlatformSubscriptionState, state_id) if state_id else None
            author = db.get(PlatformAuthor, state.author_id) if state else None
            if (
                not state_id
                or state
                and state.enabled
                and (
                    state.manual
                    or author
                    and author.is_subscribed
                    and current.AUTO_CHECK_ENABLED
                )
            ):
                eligible.append(task)
        pending = eligible
        tests = [task for task in pending if task.source_type == "session"]
        account = db.get(InstagramAccountState, 1)
        usable = bool(
            current.INSTAGRAM_ENABLED
            and account
            and account.status == "ready"
            and (not account.cooldown_until or account.cooldown_until <= now)
        )
        if not usable and not tests:
            return {"dispatched": 0, "reason": "disabled_or_session_blocked"}
        states = []
        if usable:
            from app.services.instagram_store import interval_for

            for stream in db.scalars(
                select(PlatformSubscriptionState)
                .join(PlatformAuthor)
                .where(
                    PlatformAuthor.platform == "instagram",
                    PlatformSubscriptionState.scan_mode == "recent",
                    PlatformSubscriptionState.enabled.is_(True),
                    PlatformSubscriptionState.manual.is_(False),
                    PlatformSubscriptionState.last_success_at.is_not(None),
                    PlatformSubscriptionState.deferred_reason.is_(None),
                )
            ):
                stream.next_check_at = stream.last_success_at + timedelta(
                    seconds=interval_for(stream.content_kind)
                )
            db.commit()
            states = list(
                db.scalars(
                    select(PlatformSubscriptionState)
                    .join(PlatformAuthor)
                    .where(
                        PlatformAuthor.platform == "instagram",
                        PlatformSubscriptionState.enabled.is_(True),
                        (
                            PlatformAuthor.is_subscribed.is_(True)
                            & bool(current.AUTO_CHECK_ENABLED)
                        )
                        | PlatformSubscriptionState.manual.is_(True),
                        PlatformSubscriptionState.next_check_at <= now,
                        (PlatformSubscriptionState.scan_mode != "history")
                        | PlatformSubscriptionState.history_complete.is_(False),
                    )
                )
            )
            states.sort(key=stream_priority)
        candidates = tests or sorted(pending, key=job_priority)
        first = candidates[0] if candidates else None
        state = states[0] if states else None
        if (
            not tests
            and state
            and (first is None or stream_priority(state)[:2] < job_priority(first)[:2])
        ):
            existing = (
                db.get(PlatformDownloadTask, state.task_id) if state.task_id else None
            )
            if existing and existing.status in {"pending", "downloading"}:
                first = existing if due_job(existing, now) else first
            else:
                author = db.get(PlatformAuthor, state.author_id)
                first = create_platform_task(
                    "instagram",
                    f"scan-{author.external_user_id}-{state.id}",
                    author.profile_url,
                    "profile",
                )
                first.instagram_options = json.dumps(
                    {
                        "state_id": state.id,
                        "kind": state.content_kind,
                        "scan_mode": state.scan_mode,
                    }
                )
                db.add(first)
                db.flush()
                state.task_id = first.id
                db.commit()
        if first is None:
            return {"dispatched": 0}
        options = options_for(first)
        options["last_scheduled_at"] = now.isoformat()
        first.instagram_options = json.dumps(options)
        db.commit()
        download_platform_profile.delay(first.id)
        return {"dispatched": 1, "task_id": first.id}
    finally:
        lock.release()


def execute_instagram_task(celery_task, db, task):
    from app.core import redis_client
    from app.core.traffic_control import global_download_slot
    from app.services.child_process import close_download_process, iter_download_output
    from app.services.download_lifecycle import lock_download_attempt
    from app.services.external_download_coordination import (
        check_external_execution,
        register_external_process,
    )

    token = celery_task.request.id
    process = None
    lines = []

    def check():
        check_external_execution("instagram", task.id, token)

    try:
        with global_download_slot(token):
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "app.services.instagram_runner",
                    str(task.id),
                    token,
                ],
                cwd=str(Path(__file__).resolve().parents[2]),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            register_external_process("instagram", task.id, token, process.pid)
            for line in iter_download_output(process, check):
                try:
                    event = json.loads(line)
                    message = str(event["message"])[:300]
                except (KeyError, TypeError, ValueError):
                    continue
                # 只有自有运行器的白名单消息可进入任务日志。
                if event.get("event") != "heartbeat":
                    lines.append(message)
                    lines = lines[-200:]
                    redis_client.append_platform_task_log("instagram", task.id, message)
                db.expire_all()
                row = db.get(PlatformDownloadTask, task.id)
                if row and row.status == "downloading" and row.celery_task_id == token:
                    lock_download_attempt(db, task.id, token, PlatformDownloadTask)
                    row.last_heartbeat_at, row.last_log_line = datetime.now(), message
                    db.commit()
            return_code = process.wait()
        db.expire_all()
        row = db.get(PlatformDownloadTask, task.id)
        if row.status == "downloading" and row.celery_task_id == token:
            lock_download_attempt(db, row.id, token, PlatformDownloadTask)
            row.status, row.phase, row.completed_at = "failed", "failed", datetime.now()
            row.error_code, row.error_message = (
                "engine_error",
                "Instagram 解析或文件验证未完成，请检查引擎依赖与账号权限",
            )
            row.output_log = "\n".join(lines)
            db.commit()
        # child 已在自己的 fenced 事务中提交终态，不能用旧 Session 快照覆盖。
        return {
            "success": row.status == "completed",
            "status": row.status,
            "return_code": return_code,
        }
    finally:
        close_download_process(process)
