"""Long-running operations tasks that must survive HTTP gateway timeouts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
import traceback

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import redis_client
from app.core.config import settings
from app.models.database import create_isolated_async_engine, get_sync_db
from app.services.storage_audit import run_storage_audit, storage_repair_targets
from app.services.storage_maintenance import apply_storage_repair_plan
from app.tasks.celery_app import celery_app


logger = logging.getLogger(__name__)


async def _apply_all_storage_targets(
    root: Path,
    targets: list[dict],
    state: dict,
) -> dict:
    """Apply a large audited set in independently committed, revalidated batches."""
    engine = create_isolated_async_engine()
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    totals = {
        "planned": len(targets),
        "applied": 0,
        "relinked": 0,
        "moved": 0,
        "marked_tasks": 0,
        "skipped": 0,
        "errors": 0,
        "batches": 0,
    }
    try:
        for offset in range(0, len(targets), 200):
            batch = targets[offset:offset + 200]
            async with session_factory() as db:
                result = await apply_storage_repair_plan(db, root, batch)
            totals["applied"] += int(result["applied"])
            totals["relinked"] += len(result["relinked"])
            totals["moved"] += len(result["moved"])
            totals["marked_tasks"] += len(result["marked_tasks"])
            totals["skipped"] += len(result["skipped"])
            totals["errors"] += len(result["apply_errors"])
            totals["batches"] += 1
            state.update({
                "phase": "applying",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "progress": {**totals},
            })
            redis_client.set_storage_repair_all_state(state)
        return totals
    finally:
        await engine.dispose()


@celery_app.task(bind=True, name="app.tasks.operations_tasks.run_storage_audit")
def run_storage_audit_task(
    self,
    job_id: str,
    max_records: int = 200_000,
    max_files: int = 50_000,
):
    """Persist progress and the final report so the browser can reconnect safely."""
    started_at = datetime.now(timezone.utc).isoformat()
    state = {
        "job_id": job_id,
        "celery_task_id": getattr(getattr(self, "request", None), "id", None),
        "status": "running",
        "phase": "records",
        "started_at": started_at,
        "updated_at": started_at,
        "progress": {"scanned_records": 0, "scanned_files": 0},
        "result": None,
        "error": None,
    }
    redis_client.set_storage_audit_state(state)
    db = get_sync_db()

    def publish_progress(values):
        state["phase"] = values.get("phase", state["phase"])
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        state["progress"].update(values)
        redis_client.set_storage_audit_state(state)

    try:
        current = settings.snapshot()
        report = run_storage_audit(
            db,
            current.DOWNLOAD_ROOT,
            max_records=max_records,
            max_files=max_files,
            progress=publish_progress,
        )
        finished_at = datetime.now(timezone.utc).isoformat()
        state.update({
            "status": "completed",
            "phase": "completed",
            "updated_at": finished_at,
            "finished_at": finished_at,
            "progress": {
                "scanned_records": report["scanned_records"],
                "total_records": report["total_records"],
                "scanned_files": report["scanned_files"],
            },
            "result": report,
        })
        redis_client.set_storage_audit_state(state)
        redis_client.append_activity_log(
            "info",
            "storage-audit",
            "存储巡检完成",
            f"记录={report['scanned_records']}/{report['total_records']}，文件={report['scanned_files']}，"
            f"旧路径={report['issue_counts']['relinkable_records']}，"
            f"缺失={report['issue_counts']['missing_records']}，"
            f"临时文件={report['issue_counts']['partial_files']}，"
            f"孤立媒体={report['issue_counts']['orphan_files']}，"
            f"样本上限={report['sample_limit']}",
            event_code="storage_audit_completed",
            correlation_id=job_id,
        )
        return report
    except Exception as exc:
        db.rollback()
        finished_at = datetime.now(timezone.utc).isoformat()
        error = f"{type(exc).__name__}: {str(exc)[:1000]}"
        state.update({
            "status": "failed",
            "phase": "failed",
            "updated_at": finished_at,
            "finished_at": finished_at,
            "error": error,
        })
        redis_client.set_storage_audit_state(state)
        redis_client.append_activity_log(
            "error",
            "storage-audit",
            "存储巡检失败",
            error,
            event_code="storage_audit_failed",
            correlation_id=job_id,
        )
        logger.error("存储巡检失败 job_id=%s: %s\n%s", job_id, error, traceback.format_exc())
        raise
    finally:
        db.close()
        try:
            redis_client.release_storage_audit_lock(job_id)
        except Exception:
            logger.warning("释放存储巡检锁失败 job_id=%s", job_id)


@celery_app.task(
    bind=True,
    name="app.tasks.operations_tasks.run_storage_repair_all",
    soft_time_limit=6900,
    time_limit=7200,
)
def run_storage_repair_all_task(
    self,
    job_id: str,
    max_records: int = 200_000,
    max_files: int = 50_000,
):
    """Scan once, then process every discovered issue in safe 200-item batches."""
    started_at = datetime.now(timezone.utc).isoformat()
    state = {
        "job_id": job_id,
        "celery_task_id": getattr(getattr(self, "request", None), "id", None),
        "status": "running",
        "phase": "scanning",
        "started_at": started_at,
        "updated_at": started_at,
        "progress": {"scanned_records": 0, "scanned_files": 0},
        "result": None,
        "error": None,
    }
    redis_client.set_storage_repair_all_state(state)
    db = get_sync_db()

    def publish_progress(values):
        state["phase"] = "scanning"
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        state["progress"].update(values)
        redis_client.set_storage_repair_all_state(state)

    try:
        current = settings.snapshot()
        root = Path(current.DOWNLOAD_ROOT).expanduser().resolve(strict=False)
        report = run_storage_audit(
            db,
            str(root),
            max_records=max_records,
            max_files=max_files,
            sample_limit=max_records + max_files,
            progress=publish_progress,
        )
        targets = storage_repair_targets(report)
        db.close()
        db = None
        state.update({
            "phase": "applying",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "progress": {"planned": len(targets), "applied": 0, "batches": 0},
        })
        redis_client.set_storage_repair_all_state(state)
        totals = asyncio.run(_apply_all_storage_targets(root, targets, state))
        verification_error = None
        remaining_counts = None
        verification_db = None
        try:
            state.update({
                "phase": "verifying",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "progress": {**totals, "scanned_records": 0, "scanned_files": 0},
            })
            redis_client.set_storage_repair_all_state(state)
            verification_db = get_sync_db()

            def publish_verification(values):
                state["phase"] = "verifying"
                state["updated_at"] = datetime.now(timezone.utc).isoformat()
                state["progress"].update(values)
                redis_client.set_storage_repair_all_state(state)

            verification_report = run_storage_audit(
                verification_db,
                str(root),
                max_records=max_records,
                max_files=max_files,
                sample_limit=200,
                progress=publish_verification,
            )
            remaining_counts = verification_report["issue_counts"]
            verified_at = datetime.now(timezone.utc).isoformat()
            redis_client.set_storage_audit_state({
                "job_id": f"{job_id}-verification",
                "status": "completed",
                "phase": "completed",
                "started_at": state["started_at"],
                "updated_at": verified_at,
                "finished_at": verified_at,
                "progress": {
                    "scanned_records": verification_report["scanned_records"],
                    "total_records": verification_report["total_records"],
                    "scanned_files": verification_report["scanned_files"],
                },
                "result": verification_report,
                "error": None,
            })
        except Exception as exc:
            verification_error = f"{type(exc).__name__}: {str(exc)[:500]}"
            logger.warning("存储全部维护已完成，但复检失败 job_id=%s: %s", job_id, verification_error)
        finally:
            if verification_db is not None:
                verification_db.close()
        finished_at = datetime.now(timezone.utc).isoformat()
        repair_state = {
            "mode": "all",
            "applied_at": finished_at,
            **totals,
            "records_truncated": report["records_truncated"],
            "files_truncated": report["files_truncated"],
            "remaining_counts": remaining_counts,
            "verification_error": verification_error,
        }
        redis_client.set_storage_repair_state(repair_state)
        state.update({
            "status": "completed",
            "phase": "completed",
            "updated_at": finished_at,
            "finished_at": finished_at,
            "progress": totals,
            "result": repair_state,
        })
        redis_client.set_storage_repair_all_state(state)
        try:
            redis_client.append_activity_log(
                "warning",
                "storage-maintenance",
                f"存储全部维护已处理 {totals['applied']}/{totals['planned']} 项",
                f"批次={totals['batches']}，回填={totals['relinked']}，隔离={totals['moved']}，"
                f"标记任务={totals['marked_tasks']}，跳过={totals['skipped']}，错误={totals['errors']}",
                event_code="storage_repair_all_completed",
                correlation_id=job_id,
            )
        except Exception as exc:
            logger.warning("存储全部维护已完成，但活动日志写入失败 job_id=%s: %s", job_id, exc)
        return repair_state
    except Exception as exc:
        if db is not None:
            db.rollback()
        finished_at = datetime.now(timezone.utc).isoformat()
        error = f"{type(exc).__name__}: {str(exc)[:1000]}"
        state.update({
            "status": "failed",
            "phase": "failed",
            "updated_at": finished_at,
            "finished_at": finished_at,
            "error": error,
        })
        redis_client.set_storage_repair_all_state(state)
        try:
            redis_client.append_activity_log(
                "error",
                "storage-maintenance",
                "存储全部维护失败",
                error,
                event_code="storage_repair_all_failed",
                correlation_id=job_id,
            )
        except Exception:
            pass
        logger.error("存储全部维护失败 job_id=%s: %s\n%s", job_id, error, traceback.format_exc())
        raise
    finally:
        if db is not None:
            db.close()
        try:
            redis_client.release_storage_repair_all_lock(job_id)
        except Exception:
            logger.warning("释放存储全部维护锁失败 job_id=%s", job_id)
