"""Long-running operations tasks that must survive HTTP gateway timeouts."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import traceback

from app.core import redis_client
from app.core.config import settings
from app.models.database import get_sync_db
from app.services.storage_audit import run_storage_audit
from app.tasks.celery_app import celery_app


logger = logging.getLogger(__name__)


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
