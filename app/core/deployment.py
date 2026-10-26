"""发布阶段使用的排空、PostgreSQL 备份与恢复入口。"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
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


_APPLICATION_SCHEMA = "n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\' AND n.nspname <> 'information_schema'"
_SCHEMA_QUERIES = {
    "schemas": f"SELECT jsonb_build_array(n.nspname) AS item FROM pg_catalog.pg_namespace n WHERE {_APPLICATION_SCHEMA}",
    "relations": f"""SELECT jsonb_build_array(n.nspname,c.relname,c.relkind,c.relrowsecurity,c.relforcerowsecurity,
        c.relreplident,pg_catalog.pg_get_expr(c.relpartbound,c.oid)) AS item
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE {_APPLICATION_SCHEMA} AND c.relkind IN ('r','p','v','m','S','f')""",
    "columns": f"""SELECT jsonb_build_array(n.nspname,c.relname,a.attname,
        row_number() OVER (PARTITION BY c.oid ORDER BY a.attnum),
        pg_catalog.format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,a.attgenerated,
        pg_catalog.pg_get_expr(d.adbin,d.adrelid),cn.nspname,coll.collname) AS item
        FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
        LEFT JOIN pg_catalog.pg_collation coll ON coll.oid=a.attcollation
        LEFT JOIN pg_catalog.pg_namespace cn ON cn.oid=coll.collnamespace
        WHERE {_APPLICATION_SCHEMA} AND c.relkind IN ('r','p','v','m','f') AND a.attnum>0 AND NOT a.attisdropped""",
    "constraints": f"""SELECT jsonb_build_array(n.nspname,c.conname,c.contype,
        cls.relname,t.typname,pg_catalog.pg_get_constraintdef(c.oid,true)) AS item
        FROM pg_catalog.pg_constraint c JOIN pg_catalog.pg_namespace n ON n.oid=c.connamespace
        LEFT JOIN pg_catalog.pg_class cls ON cls.oid=c.conrelid
        LEFT JOIN pg_catalog.pg_type t ON t.oid=c.contypid WHERE {_APPLICATION_SCHEMA}""",
    "indexes": f"""SELECT jsonb_build_array(n.nspname,c.relname,pg_catalog.pg_get_indexdef(i.indexrelid),
        i.indisvalid,i.indisready) AS item FROM pg_catalog.pg_index i
        JOIN pg_catalog.pg_class c ON c.oid=i.indexrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE {_APPLICATION_SCHEMA}""",
    "types": f"""SELECT jsonb_build_array(n.nspname,t.typname,t.typtype,t.typnotnull,
        pg_catalog.format_type(t.typbasetype,t.typtypmod),t.typdefault,
        (SELECT jsonb_agg(e.enumlabel ORDER BY e.enumsortorder) FROM pg_catalog.pg_enum e WHERE e.enumtypid=t.oid)) AS item
        FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace
        WHERE {_APPLICATION_SCHEMA} AND t.typtype IN ('c','d','e','r','m')""",
    "views": f"""SELECT jsonb_build_array(n.nspname,c.relname,pg_catalog.pg_get_viewdef(c.oid,true)) AS item
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE {_APPLICATION_SCHEMA} AND c.relkind IN ('v','m')""",
    "functions": f"""SELECT jsonb_build_array(n.nspname,p.proname,pg_catalog.pg_get_functiondef(p.oid)) AS item
        FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        WHERE {_APPLICATION_SCHEMA} AND p.prokind IN ('f','p','w')""",
    "triggers": f"""SELECT jsonb_build_array(n.nspname,c.relname,t.tgname,pg_catalog.pg_get_triggerdef(t.oid,true)) AS item
        FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE {_APPLICATION_SCHEMA} AND NOT t.tgisinternal""",
    "sequences": """SELECT jsonb_build_array(schemaname,sequencename,data_type,start_value,min_value,max_value,
        increment_by,cycle,cache_size) AS item FROM pg_catalog.pg_sequences
        WHERE schemaname NOT LIKE 'pg\\_%' ESCAPE '\\' AND schemaname <> 'information_schema'""",
    "extensions": """SELECT jsonb_build_array(e.extname,e.extversion,n.nspname) AS item
        FROM pg_catalog.pg_extension e JOIN pg_catalog.pg_namespace n ON n.oid=e.extnamespace""",
}


def _fingerprint_sql(query: str) -> str:
    return f"""SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
        COALESCE(jsonb_agg(item ORDER BY item::text COLLATE "C"),'[]'::jsonb)::text,'UTF8')),'hex')
        FROM ({query}) AS fingerprint_rows"""


def _quoted_name(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _schema_digest(schema: dict) -> str:
    return hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _database_baseline(connection) -> dict:
    """只保留结构摘要、迁移台账摘要和行数，不保存业务行内容。"""
    from sqlalchemy import text
    connection.execute(text("SET LOCAL search_path = ''; SET LOCAL DateStyle = 'ISO, YMD'; SET LOCAL TimeZone = 'UTC'"))
    schema = {name: connection.execute(text(_fingerprint_sql(query))).scalar_one()
              for name, query in _SCHEMA_QUERIES.items()}
    relations = list(connection.execute(text(f"""SELECT n.nspname,c.relname,c.relkind
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE {_APPLICATION_SCHEMA} AND c.relkind IN ('r','p','S') ORDER BY n.nspname,c.relname""")))
    rows, sequences, migrations = [], [], []
    for namespace, name, kind in relations:
        target = _quoted_name(namespace) + "." + _quoted_name(name)
        if kind == "S":
            last_value, is_called = connection.execute(text(f"SELECT last_value,is_called FROM {target}")).one()
            sequences.append([namespace, name, last_value, is_called])
        else:
            rows.append([namespace, name, connection.execute(text(f"SELECT count(*) FROM {target}")).scalar_one()])
            if name == "schema_migrations":
                ledger = f"SELECT pg_catalog.to_jsonb(ledger) AS item FROM {target} ledger"
                migrations.append([namespace, name, connection.execute(text(_fingerprint_sql(ledger))).scalar_one()])
    return {"schema": schema, "schema_sha256": _schema_digest(schema), "rows": rows,
            "sequences": sequences, "migrations": migrations}


def _archive_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path(path: Path) -> Path:
    return path.with_name(path.name + ".manifest.json")


def _read_snapshot_manifest(path: Path) -> dict:
    """自动恢复不接纳没有基线证明的历史归档；原文件保留供明确的人工恢复。"""
    try:
        manifest = json.loads(_manifest_path(path).read_text(encoding="utf-8"))
        baseline = manifest["baseline"]
        valid_hash = lambda value: isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
        if (manifest["format"] != 1 or manifest["bytes"] != path.stat().st_size
                or not valid_hash(manifest["sha256"]) or _archive_digest(path) != manifest["sha256"]
                or set(baseline["schema"]) != set(_SCHEMA_QUERIES)
                or any(not valid_hash(value) for value in baseline["schema"].values())
                or baseline["schema_sha256"] != _schema_digest(baseline["schema"])):
            raise ValueError
        for group, size in (("rows", 3), ("sequences", 4), ("migrations", 3)):
            entries = baseline[group]
            if not isinstance(entries, list) or any(not isinstance(item, list) or len(item) != size
                    or not all(isinstance(value, str) and value for value in item[:2]) for item in entries):
                raise ValueError
            if len({tuple(item[:2]) for item in entries}) != len(entries):
                raise ValueError
        if (any(type(count) is not int or count < 0 for _, _, count in baseline["rows"])
                or any(type(value) is not int or type(called) is not bool
                       for _, _, value, called in baseline["sequences"])
                or any(not valid_hash(value) for _, _, value in baseline["migrations"])):
            raise ValueError
        return baseline
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RuntimeError("恢复归档缺少有效基线清单或完整性验证失败，尚未修改数据库；维护模式和备份已保留") from exc


def _snapshot_validation_sql(baseline: dict) -> str:
    """在 psql 恢复事务提交前验证，任何不一致都会回滚整个恢复事务。"""
    checks = []
    for name, query in _SCHEMA_QUERIES.items():
        checks.append(f"IF ({_fingerprint_sql(query)}) IS DISTINCT FROM {_sql_literal(baseline['schema'][name])} "
                      "THEN RAISE EXCEPTION 'Restored schema fingerprint differs from backup'; END IF;")
    for namespace, name, count in baseline["rows"]:
        target = _quoted_name(namespace) + "." + _quoted_name(name)
        checks.append(f"IF (SELECT count(*) FROM {target}) <> {count} THEN "
                      "RAISE EXCEPTION 'Restored row count differs from backup'; END IF;")
    for namespace, name, value, called in baseline["sequences"]:
        target = _quoted_name(namespace) + "." + _quoted_name(name)
        checks.append(f"IF NOT (SELECT last_value = {value} AND is_called = {str(called).upper()} FROM {target}) "
                      "THEN RAISE EXCEPTION 'Restored sequence differs from backup'; END IF;")
    for namespace, name, digest in baseline["migrations"]:
        target = _quoted_name(namespace) + "." + _quoted_name(name)
        query = _fingerprint_sql(f"SELECT pg_catalog.to_jsonb(ledger) AS item FROM {target} ledger")
        checks.append(f"IF ({query}) IS DISTINCT FROM {_sql_literal(digest)} THEN "
                      "RAISE EXCEPTION 'Restored migration ledger differs from backup'; END IF;")
    block = "BEGIN\n" + "\n".join(checks) + "\nEND"
    return ("SET LOCAL search_path = ''; SET LOCAL DateStyle = 'ISO, YMD'; SET LOCAL TimeZone = 'UTC';\n"
            "SET LOCAL standard_conforming_strings = on;\nDO " + _sql_literal(block) + ";\n")


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
        baseline = _read_snapshot_manifest(path)
        _restore_snapshot(path, current, executable, common, environment, baseline)
        return
    if path.exists() or _manifest_path(path).exists():
        raise RuntimeError("数据库备份路径已存在，拒绝覆盖恢复依据")
    from sqlalchemy import create_engine, text
    engine = create_engine(current.effective_database_url, connect_args={"connect_timeout": 5},
                           isolation_level="REPEATABLE READ")
    temporary = []
    try:
        for suffix in (".dump", ".json"):
            descriptor, name = tempfile.mkstemp(prefix=".backup-", suffix=suffix, dir=path.parent)
            os.chmod(name, 0o600)
            os.close(descriptor)
            temporary.append(Path(name))
        archive, manifest_path = temporary
        with engine.connect() as connection, connection.begin():
            connection.execute(text("SET TRANSACTION READ ONLY"))
            snapshot = connection.execute(text("SELECT pg_export_snapshot()")).scalar_one()
            baseline = _database_baseline(connection)
            command = [executable, *common, "--format=custom", "--no-owner", "--no-privileges",
                       "--snapshot", snapshot, "--file", str(archive)]
            completed = subprocess.run(command, env=environment, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.PIPE, timeout=900, check=False)
            if completed.returncode:
                raise RuntimeError("数据库备份失败，尚未开始迁移")
            # 序列状态不受 MVCC 快照保护；备份过程中推进序列意味着排空/独占条件失效。
            if _database_baseline(connection) != baseline:
                raise RuntimeError("备份期间数据库状态发生变化，拒绝迁移；旧服务和数据保持可恢复")
        check = shutil.which("pg_restore")
        if not check or subprocess.run([check, "--list", str(archive)], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=30).returncode:
            raise RuntimeError("数据库备份完整性检查失败，尚未开始迁移")
        with archive.open("r+b") as output:
            os.fsync(output.fileno())
        manifest = {"format": 1, "bytes": archive.stat().st_size,
                    "sha256": _archive_digest(archive), "baseline": baseline}
        with manifest_path.open("w", encoding="utf-8") as output:
            json.dump(manifest, output, sort_keys=True, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(archive, path)
        os.replace(manifest_path, _manifest_path(path))
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        engine.dispose()
        for item in temporary:
            item.unlink(missing_ok=True)


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


def _restore_snapshot(path, current, pg_restore, common, environment, baseline) -> None:
    """一次事务清理新迁移对象并恢复；--clean 不能处理备份外的新依赖。"""
    psql = shutil.which("psql")
    if not psql:
        raise RuntimeError("数据库恢复需要 PostgreSQL 客户端 psql；维护模式和备份已保留")
    schemas = _recovery_schemas(current)
    paths = []
    try:
        for _ in range(5):
            descriptor, name = tempfile.mkstemp(prefix=".restore-", suffix=".sql", dir=path.parent)
            os.chmod(name, 0o600)
            os.close(descriptor)
            paths.append(Path(name))
        cleanup_sql, snapshot_sql, archive_toc, restore_toc, validation_sql = paths
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
        validation_sql.write_text(_snapshot_validation_sql(baseline), encoding="utf-8")
        decoded = subprocess.run([pg_restore, "--no-owner", "--no-privileges", "--use-list", str(restore_toc),
                                  "--file", str(snapshot_sql), str(path)],
                                 env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 timeout=900, check=False)
        if decoded.returncode:
            raise RuntimeError("备份解码失败，尚未修改现有数据库；维护模式和备份已保留")
        restored = subprocess.run([psql, *common, "--no-psqlrc", "--single-transaction", "--set=ON_ERROR_STOP=on",
                                   "--file", str(cleanup_sql), "--file", str(snapshot_sql), "--file", str(validation_sql)],
                                  env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                  timeout=900, check=False)
        if restored.returncode:
            raise RuntimeError("数据库恢复事务或基线验证失败，已回滚到恢复前状态；维护模式和备份已保留")
        from sqlalchemy import create_engine
        engine = create_engine(current.effective_database_url, connect_args={"connect_timeout": 5},
                               isolation_level="REPEATABLE READ")
        try:
            with engine.connect() as connection, connection.begin():
                if _database_baseline(connection) != baseline:
                    raise RuntimeError("数据库恢复后的基线已变化，维护模式和备份已保留；禁止启动业务进程")
        finally:
            engine.dispose()
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
