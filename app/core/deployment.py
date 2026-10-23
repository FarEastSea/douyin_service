"""发布阶段使用的排空、PostgreSQL 备份与恢复入口。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time


def queue_is_empty() -> bool:
    from app.core.config import settings
    from app.tasks.celery_app import celery_app
    import redis
    with redis.Redis.from_url(settings.effective_celery_broker, socket_connect_timeout=3, socket_timeout=3) as client:
        queued = sum(int(client.llen(key)) for key in client.scan_iter(match="celery*", count=100)
                     if client.type(key) in {b"list", "list"})
        unacked = int(client.hlen("unacked"))
    from app.core.process_manager import process_manager
    from app.core.queue_configuration import inspect_inflight
    worker = process_manager.get_status().get("worker", {})
    active, reserved, scheduled = inspect_inflight(celery_app, settings.effective_celery_broker,
                                                  worker.get("pid") if worker.get("running") else None)
    return not (queued or unacked or any(items for group in (active, reserved, scheduled) for items in (group or {}).values()))


def drain(timeout: float) -> None:
    from app.core.process_manager import process_manager
    from app.core.config_transaction import configuration_lock, journal_path
    from app.core.queue_configuration import pending_path, restart_path
    with configuration_lock():
        if pending_path().exists() or restart_path().exists():
            raise RuntimeError("存在待生效的队列变更，请等待切换完成后发布")
        if journal_path().exists():
            raise RuntimeError("存在未完成的配置事务，恢复完成后才能发布")
        result = process_manager.stop_beat(preserve_desired=True)
    if not result.get("success"):
        raise RuntimeError("调度进程尚未停止，拒绝开始迁移")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if queue_is_empty():
                return
        except Exception:
            # 故障不能降级为排空；保留旧进程并等待协调恢复。
            pass
        time.sleep(2)
    raise RuntimeError("在途任务未排空，发布已中止；旧服务和数据保持可恢复")


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
    parser.add_argument("--timeout", type=float, default=300)
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
