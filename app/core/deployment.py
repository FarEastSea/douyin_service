"""发布阶段使用的排空、PostgreSQL 备份与恢复入口。"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time


logger = logging.getLogger(__name__)
SUBSCRIPTION_TASK = "app.tasks.download_tasks.check_subscriptions"
# 单次存储修复允许运行两小时；发布不能把正常执行误判为挂死。
DEFAULT_DRAIN_TIMEOUT = 125 * 60


def _broker_counts(client) -> tuple[int, int]:
    queued = sum(int(client.llen(key)) for key in client.scan_iter(match="celery*", count=100)
                 if client.type(key) in {b"list", "list"})
    return queued, int(client.hlen("unacked"))


def _queue_snapshot() -> dict:
    from app.core.config import settings
    from app.tasks.celery_app import celery_app
    import redis
    with redis.Redis.from_url(settings.effective_celery_broker, socket_connect_timeout=3, socket_timeout=3) as client:
        from app.core.process_manager import process_manager
        from app.core.queue_configuration import inspect_inflight
        worker = process_manager.get_status().get("worker", {})
        active, reserved, scheduled = inspect_inflight(celery_app, settings.effective_celery_broker,
                                                      worker.get("pid") if worker.get("running") else None)
        # 先核验执行端再读取 broker，避免父任务刚投递子任务并完成时漏掉新消息。
        queued, unacked = _broker_counts(client)
    return {"queued": queued, "unacked": unacked, "active": active or {},
            "reserved": reserved or {}, "scheduled": scheduled or {}}


def _requests(snapshot: dict, group: str):
    for worker, tasks in snapshot[group].items():
        for task in tasks:
            request = task.get("request", task) if group == "scheduled" else task
            yield worker, request


def _snapshot_summary(snapshot: dict) -> dict:
    summary = {key: snapshot[key] for key in ("queued", "unacked")}
    for group in ("active", "reserved", "scheduled"):
        requests = list(_requests(snapshot, group))
        summary[group] = len(requests)
        # 只记录程序任务名称及计数，不记录作者、任务参数或连接信息。
        summary[group + "_types"] = dict(Counter(
            name if re.fullmatch(r"[a-zA-Z0-9_.]+", name) else "unknown"
            for _, request in requests for name in [str(request.get("name") or "unknown")]))
    return summary


def _snapshot_is_empty(snapshot: dict) -> bool:
    return not (snapshot["queued"] or snapshot["unacked"] or any(
        tasks for group in ("active", "reserved", "scheduled") for tasks in snapshot[group].values()))


def queue_is_empty() -> bool:
    return _snapshot_is_empty(_queue_snapshot())


def _matching_subscription_continuation(request: dict, state: dict) -> bool:
    """旧 Worker 没有维护栅栏；只允许撤销已经有持久断点的自动续检。"""
    if request.get("name") != SUBSCRIPTION_TASK or not request.get("id") or request.get("args") not in (None, [], ()):
        return False
    kwargs = request.get("kwargs")
    if not isinstance(kwargs, dict) or not state.get("active") or not state.get("remaining_author_ids"):
        return False
    allowed = {"force", "author_ids", "cycle_id", "cycle_total", "cycle_checked", "cycle_new_works",
               "full_reconcile", "risk_retry_attempt"}
    if set(kwargs) - allowed or kwargs.get("force") is not False or kwargs.get("full_reconcile", False) is not False:
        return False
    if kwargs.get("risk_retry_attempt", 0) not in (0, 1) or state.get("full_reconcile", False) or state.get("force", False):
        return False
    return bool(state.get("cycle_id")) and all((
        kwargs.get("cycle_id") == state.get("cycle_id"),
        kwargs.get("author_ids") == state.get("remaining_author_ids"),
        kwargs.get("cycle_total") == state.get("total_authors"),
        kwargs.get("cycle_checked") == state.get("checked_authors"),
        kwargs.get("cycle_new_works") == state.get("new_works"),
    ))


def _defer_legacy_subscription_continuations(snapshot: dict, revoked: set[str]) -> None:
    from app.core import env_config
    if not (env_config.ENV_PATH.parent / ".runtime" / "deployment-maintenance").exists():
        return
    # 正在执行的扫描必须自行提交断点并结束，绝不终止活动任务。
    if any(request.get("name") == SUBSCRIPTION_TASK
           for group in ("active", "reserved") for _, request in _requests(snapshot, group)):
        return
    candidates = [(worker, request) for worker, request in _requests(snapshot, "scheduled")
                  if request.get("name") == SUBSCRIPTION_TASK and request.get("id") not in revoked]
    if not candidates:
        return
    from app.models.database import get_sync_db
    from app.tasks.download_tasks import _load_subscription_cycle_state
    from app.tasks.celery_app import celery_app
    from app.core.config import settings
    db = get_sync_db()
    try:
        state = _load_subscription_cycle_state(db)
        with celery_app.connection_for_write(url=settings.effective_celery_broker) as connection:
            for worker, request in candidates:
                if _matching_subscription_continuation(request, state):
                    # revoke 仅标记跳过；仍等 Worker 对该消息正常 ACK，不能提前声称排空。
                    replies = celery_app.control.revoke(request["id"], terminate=False, destination=[worker],
                                                       reply=True, timeout=3, connection=connection)
                    if not any(worker in reply and "ok" in reply[worker] for reply in (replies or [])):
                        raise RuntimeError("订阅续检撤销尚未获得目标 Worker 确认")
                    revoked.add(request["id"])
    finally:
        db.close()


def _record_drain_state(state: dict) -> None:
    from app.core import env_config
    from app.core.config_transaction import atomic_json
    try:
        atomic_json(env_config.ENV_PATH.parent / ".runtime" / "deployment-drain.json", state)
    except OSError as exc:
        logger.warning("发布排空诊断暂不能写入（%s）", type(exc).__name__)


def drain(timeout: float) -> None:
    from app.core.process_manager import process_manager
    from app.core.config_transaction import configuration_lock, journal_path
    from app.core.queue_configuration import pending_path, restart_path
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("发布排空时间必须为有限正数")
    with configuration_lock():
        if pending_path().exists() or restart_path().exists():
            raise RuntimeError("存在待生效的队列变更，请等待切换完成后发布")
        if journal_path().exists():
            raise RuntimeError("存在未完成的配置事务，恢复完成后才能发布")
        result = process_manager.stop_beat(preserve_desired=True)
    if not result.get("success"):
        raise RuntimeError("调度进程尚未停止，拒绝开始迁移")
    started = time.monotonic()
    deadline = started + timeout
    revoked: set[str] = set()
    empty_samples = 0
    last_summary = None
    last_error = None
    coordination_failure_started = None
    last_report = 0.0
    while time.monotonic() < deadline:
        try:
            snapshot = _queue_snapshot()
            _defer_legacy_subscription_continuations(snapshot, revoked)
            summary = _snapshot_summary(snapshot)
            last_error = None
            coordination_failure_started = None
            empty_samples = empty_samples + 1 if _snapshot_is_empty(snapshot) else 0
            state = {"state": "drained" if empty_samples >= 2 else "waiting", "stage": "drain",
                     "elapsed_seconds": round(time.monotonic() - started), "budget_seconds": timeout,
                     "deferred_subscription_messages": len(revoked), **summary}
            now = time.monotonic()
            if summary != last_summary or now - last_report >= 30 or empty_samples >= 2:
                _record_drain_state(state)
                print("Deployment drain: " + json.dumps(state, ensure_ascii=False), flush=True)
                last_summary, last_report = summary, now
            if empty_samples >= 2:
                return
        except Exception as exc:
            # 故障不能降级为排空；保留旧进程并等待协调恢复。
            empty_samples = 0
            last_error = type(exc).__name__
            coordination_failure_started = coordination_failure_started or time.monotonic()
            if time.monotonic() - last_report >= 30 or not last_report:
                state = {"state": "waiting_coordination", "stage": "drain", "error_class": last_error,
                         "elapsed_seconds": round(time.monotonic() - started), "budget_seconds": timeout}
                _record_drain_state(state)
                print("Deployment drain: " + json.dumps(state, ensure_ascii=False), flush=True)
                last_report = time.monotonic()
            if time.monotonic() - coordination_failure_started >= 60:
                break
        time.sleep(2)
    reason = "coordination_unavailable" if last_error else "tasks_still_inflight"
    _record_drain_state({"state": "failed", "stage": "drain", "reason": reason,
                         "error_class": last_error, "elapsed_seconds": round(time.monotonic() - started),
                         "budget_seconds": timeout, **(last_summary or {})})
    raise RuntimeError(f"发布排空超时（{reason}{': ' + last_error if last_error else ''}）；未执行迁移，旧服务和数据保持可恢复")


def database_snapshot(path: Path, *, restore: bool = False) -> None:
    from app.core.config import settings
    current = settings.snapshot()
    if current.DB_TYPE != "postgresql":
        raise RuntimeError("自动发布备份当前仅支持实际生产使用的 PostgreSQL")
    executable = shutil.which("pg_restore" if restore else "pg_dump")
    if not executable:
        raise RuntimeError("发布需要 PostgreSQL 客户端 pg_dump/pg_restore，尚未开始数据库迁移")
    path = path.resolve()
    if restore and (not path.is_file() or not path.stat().st_size):
        raise RuntimeError("数据库恢复备份不存在或为空")
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    common = ["--host", current.DB_HOST, "--port", str(current.DB_PORT), "--username", current.DB_USER,
              "--dbname", current.DB_NAME, "--no-password"]
    environment = {**os.environ, "PGPASSWORD": current.DB_PASSWORD, "PGCONNECT_TIMEOUT": "5"}
    if restore:
        _restore_snapshot(path, current, executable, common, environment)
        return
    command = [executable, *common, "--format=custom", "--no-owner", "--no-privileges", "--file", str(path)]
    completed = subprocess.run(command, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               timeout=900, check=False)
    if completed.returncode:
        raise RuntimeError("数据库恢复失败；保留维护模式和备份等待恢复" if restore else "数据库备份失败，尚未开始迁移")
    if not restore:
        os.chmod(path, 0o600)
        check = shutil.which("pg_restore")
        if not check or subprocess.run([check, "--list", str(path)], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=30).returncode:
            raise RuntimeError("数据库备份完整性检查失败，尚未开始迁移")


def _recovery_schemas(current) -> list[str]:
    """只读验证恢复所需权限，发布停止服务前即可调用。"""
    from sqlalchemy import create_engine, text
    if current.DB_TYPE != "postgresql":
        raise RuntimeError("受保护发布当前仅支持 PostgreSQL")
    engine = create_engine(current.effective_database_url, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            if not connection.execute(text("SELECT has_database_privilege(current_database(), 'CREATE')")).scalar():
                raise RuntimeError("恢复角色缺少数据库 CREATE 权限，发布尚未开始")
            schemas = list(connection.execute(text("""
                SELECT nspname, pg_has_role(nspowner, 'USAGE')
                FROM pg_namespace
                WHERE nspname NOT LIKE 'pg\\_%' ESCAPE '\\' AND nspname <> 'information_schema'
            """)))
            if any(not owned for _, owned in schemas):
                raise RuntimeError("恢复角色不能管理全部应用 schema，发布尚未开始；请使用数据库拥有者角色")
            return [name for name, _ in schemas]
    finally:
        engine.dispose()


def _restore_snapshot(path, current, pg_restore, common, environment) -> None:
    """一次事务清理新迁移对象并恢复；--clean 不能处理备份外的新依赖。"""
    psql = shutil.which("psql")
    if not psql:
        raise RuntimeError("数据库恢复需要 PostgreSQL 客户端 psql；维护模式和备份已保留")
    schemas = _recovery_schemas(current)
    paths = []
    try:
        for _ in range(4):
            descriptor, name = tempfile.mkstemp(prefix=".restore-", suffix=".sql", dir=path.parent)
            os.chmod(name, 0o600)
            os.close(descriptor)
            paths.append(Path(name))
        cleanup_sql, snapshot_sql, archive_toc, restore_toc = paths
        quote = lambda name: '"' + name.replace('"', '""') + '"'
        with archive_toc.open("wb") as output:
            listed = subprocess.run([pg_restore, "--list", str(path)], env=environment, stdout=output,
                                    stderr=subprocess.PIPE, timeout=30, check=False)
        if listed.returncode:
            raise RuntimeError("备份目录验证失败，尚未修改现有数据库；维护模式和备份已保留")
        with archive_toc.open(encoding="utf-8") as source, restore_toc.open("w", encoding="utf-8") as target:
            for line in source:
                # public 的 TOC 项有时仍只导出 initdb 注释。统一由本事务创建，
                # 从恢复列表排除其 CREATE 项，兼容省略和显式创建两种归档。
                if not re.match(r"^\d+;\s+\d+\s+\d+\s+SCHEMA\s+-\s+public\s", line):
                    target.write(line)
        cleanup = [f"DROP SCHEMA IF EXISTS {quote(name)} CASCADE;" for name in schemas]
        cleanup.append('CREATE SCHEMA public;')
        cleanup_sql.write_text("\n".join(cleanup), encoding="utf-8")
        decoded = subprocess.run([pg_restore, "--no-owner", "--no-privileges", "--use-list", str(restore_toc),
                                  "--file", str(snapshot_sql), str(path)],
                                 env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 timeout=900, check=False)
        if decoded.returncode:
            raise RuntimeError("备份解码失败，尚未修改现有数据库；维护模式和备份已保留")
        restored = subprocess.run([psql, *common, "--no-psqlrc", "--single-transaction", "--set=ON_ERROR_STOP=on",
                                   "--file", str(cleanup_sql), "--file", str(snapshot_sql)],
                                  env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                  timeout=900, check=False)
        if restored.returncode:
            raise RuntimeError("数据库恢复事务失败，已回滚到恢复前状态；维护模式和备份已保留")
    finally:
        for temporary in paths:
            temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("drain", "assert-empty", "assert-recovery", "backup", "restore", "shutdown", "resume"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--path")
    parser.add_argument("--timeout", type=float, default=DEFAULT_DRAIN_TIMEOUT)
    options = parser.parse_args()
    root = Path(options.root).resolve(strict=True)
    if not (root / "main.py").is_file() or root == Path(root.anchor):
        raise RuntimeError("发布项目目录无效")
    from app.core import env_config
    env_config.ENV_PATH = root / ".env"
    from app.core.config import settings
    settings.invalidate()
    from app.core.process_manager import process_manager
    process_manager._project_dir = str(root)
    if options.action == "assert-recovery":
        _recovery_schemas(settings.snapshot())
    elif options.action == "drain":
        drain(options.timeout)
    elif options.action == "assert-empty":
        if not queue_is_empty():
            raise RuntimeError("停止旧入口后仍有遗留队列消息，拒绝执行数据库迁移")
    elif options.action == "shutdown":
        if not process_manager.shutdown_all().get("success"):
            raise RuntimeError("后台 Worker 尚未停止，拒绝执行数据库迁移")
    elif options.action == "resume":
        process_manager.reconcile()
    else:
        if not options.path:
            raise RuntimeError("需要提供备份路径")
        database_snapshot(Path(options.path), restore=options.action == "restore")


if __name__ == "__main__":
    main()
