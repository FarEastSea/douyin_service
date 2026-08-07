"""带版本记录的启动迁移执行器。

迁移由 Web 与 Celery 共用；PostgreSQL 使用会话级 advisory lock 串行化。
普通 DDL/回填在事务中执行，CREATE INDEX CONCURRENTLY 使用独立的
AUTOCOMMIT 连接，避免落入事务块。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
import json
import logging
from threading import RLock
from typing import Any, Callable, Iterator, Optional

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.engine import Connection, Engine

from app.core.diagnostics import clear_runtime_error, report_runtime_error


logger = logging.getLogger(__name__)
MIGRATION_LOCK_KEY = 741_852_963
BACKFILL_BATCH_SIZE = 1000
_local_migration_lock = RLock()


TransactionalHandler = Callable[[Connection], Optional[dict[str, Any]]]
BatchedHandler = Callable[[Engine], Optional[dict[str, Any]]]


@dataclass(frozen=True)
class Migration:
    migration_id: str
    migration_type: str
    description: str
    table_name: Optional[str] = None
    column_name: Optional[str] = None
    sql: Optional[str] = None
    generic_sql: Optional[str] = None
    index_name: Optional[str] = None
    constraint_name: Optional[str] = None
    handler: Optional[TransactionalHandler] = None
    batched_handler: Optional[BatchedHandler] = None


def _add_column(
    migration_id: str,
    table_name: str,
    column_name: str,
    sql: str,
) -> Migration:
    return Migration(
        migration_id=migration_id,
        migration_type="add_column",
        description=f"为 {table_name} 增加 {column_name} 列",
        table_name=table_name,
        column_name=column_name,
        sql=sql,
    )


def _create_index(
    migration_id: str,
    description: str,
    index_name: str,
    postgres_sql: str,
    generic_sql: str,
    *,
    constraint_name: Optional[str] = None,
) -> Migration:
    return Migration(
        migration_id=migration_id,
        migration_type="create_index",
        description=description,
        sql=postgres_sql,
        generic_sql=generic_sql,
        index_name=index_name,
        constraint_name=constraint_name,
    )


ADD_COLUMN_MIGRATIONS = [
    _add_column("0001_x_tasks_author", "x_download_tasks", "x_author_id", "ALTER TABLE x_download_tasks ADD COLUMN x_author_id INTEGER REFERENCES x_authors(id)"),
    _add_column("0002_x_authors_display_name", "x_authors", "display_name", "ALTER TABLE x_authors ADD COLUMN display_name VARCHAR(255)"),
    _add_column("0003_x_authors_avatar", "x_authors", "avatar_url", "ALTER TABLE x_authors ADD COLUMN avatar_url TEXT"),
    _add_column("0004_x_authors_account_status", "x_authors", "account_status", "ALTER TABLE x_authors ADD COLUMN account_status VARCHAR(32)"),
    _add_column("0005_x_authors_status_label", "x_authors", "account_status_label", "ALTER TABLE x_authors ADD COLUMN account_status_label VARCHAR(64)"),
    _add_column("0006_x_authors_last_error", "x_authors", "last_error", "ALTER TABLE x_authors ADD COLUMN last_error TEXT"),
    _add_column("0007_x_authors_last_synced", "x_authors", "last_synced_at", "ALTER TABLE x_authors ADD COLUMN last_synced_at TIMESTAMP NULL"),
    _add_column("0008_x_tasks_phase", "x_download_tasks", "phase", "ALTER TABLE x_download_tasks ADD COLUMN phase VARCHAR(32)"),
    _add_column("0009_x_tasks_engine", "x_download_tasks", "engine_name", "ALTER TABLE x_download_tasks ADD COLUMN engine_name VARCHAR(32)"),
    _add_column("0010_x_tasks_total_media", "x_download_tasks", "total_media_count", "ALTER TABLE x_download_tasks ADD COLUMN total_media_count INTEGER"),
    _add_column("0011_x_tasks_downloaded_media", "x_download_tasks", "downloaded_media_count", "ALTER TABLE x_download_tasks ADD COLUMN downloaded_media_count INTEGER"),
    _add_column("0012_x_tasks_progress", "x_download_tasks", "progress_percent", "ALTER TABLE x_download_tasks ADD COLUMN progress_percent FLOAT"),
    _add_column("0013_x_tasks_last_log", "x_download_tasks", "last_log_line", "ALTER TABLE x_download_tasks ADD COLUMN last_log_line TEXT"),
    _add_column("0014_x_tasks_error_code", "x_download_tasks", "error_code", "ALTER TABLE x_download_tasks ADD COLUMN error_code VARCHAR(64)"),
    _add_column("0015_x_tasks_retry_count", "x_download_tasks", "retry_count", "ALTER TABLE x_download_tasks ADD COLUMN retry_count INTEGER"),
    _add_column("0016_x_tasks_heartbeat", "x_download_tasks", "last_heartbeat_at", "ALTER TABLE x_download_tasks ADD COLUMN last_heartbeat_at TIMESTAMP NULL"),
    _add_column("0017_works_excluded", "works", "is_excluded", "ALTER TABLE works ADD COLUMN is_excluded BOOLEAN DEFAULT FALSE NOT NULL"),
    _add_column("0018_works_excluded_at", "works", "excluded_at", "ALTER TABLE works ADD COLUMN excluded_at TIMESTAMP NULL"),
    _add_column("0019_works_excluded_indices", "works", "excluded_file_indices", "ALTER TABLE works ADD COLUMN excluded_file_indices TEXT"),
    _add_column("0020_works_live_photos", "works", "live_photo_urls", "ALTER TABLE works ADD COLUMN live_photo_urls TEXT"),
    _add_column("0021_works_published_at", "works", "published_at", "ALTER TABLE works ADD COLUMN published_at TIMESTAMP NULL"),
    _add_column("0022_authors_auto_update", "authors", "last_auto_update_at", "ALTER TABLE authors ADD COLUMN last_auto_update_at TIMESTAMP NULL"),
]


INDEX_MIGRATIONS = [
    _create_index("0101_idx_status_created", "下载任务状态与创建时间索引", "idx_status_created", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_status_created ON download_tasks (status, created_at)", "CREATE INDEX idx_status_created ON download_tasks (status, created_at)"),
    _create_index("0102_idx_work_status", "下载任务作品与状态索引", "idx_work_status", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_work_status ON download_tasks (work_id, status)", "CREATE INDEX idx_work_status ON download_tasks (work_id, status)"),
    _create_index("0103_idx_x_status_created", "X 任务状态与创建时间索引", "idx_x_status_created", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_x_status_created ON x_download_tasks (status, created_at)", "CREATE INDEX idx_x_status_created ON x_download_tasks (status, created_at)"),
    _create_index("0104_idx_x_media_task", "X 媒体任务索引", "idx_x_media_task", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_x_media_task ON x_media_assets (task_id, created_at)", "CREATE INDEX idx_x_media_task ON x_media_assets (task_id, created_at)"),
    _create_index("0105_idx_x_media_author", "X 媒体作者索引", "idx_x_media_author", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_x_media_author ON x_media_assets (x_author_id, created_at)", "CREATE INDEX idx_x_media_author ON x_media_assets (x_author_id, created_at)"),
    _create_index("0106_idx_author_profile_history", "作者资料历史索引", "idx_author_profile_history", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_author_profile_history ON author_profile_history (author_id, observed_at)", "CREATE INDEX idx_author_profile_history ON author_profile_history (author_id, observed_at)"),
    _create_index("0107_idx_subscription_report_started", "订阅报告时间索引", "idx_subscription_report_started", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_subscription_report_started ON subscription_check_reports (started_at)", "CREATE INDEX idx_subscription_report_started ON subscription_check_reports (started_at)"),
    _create_index("0110_idx_works_author", "作品作者索引", "idx_works_author_id", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_works_author_id ON works (author_id)", "CREATE INDEX idx_works_author_id ON works (author_id)"),
    _create_index("0111_idx_history_completed", "下载历史完成时间索引", "idx_download_history_completed_at", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_download_history_completed_at ON download_history (completed_at)", "CREATE INDEX idx_download_history_completed_at ON download_history (completed_at)"),
    _create_index("0112_idx_history_task", "下载历史任务索引", "idx_download_history_task_id", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_download_history_task_id ON download_history (task_id)", "CREATE INDEX idx_download_history_task_id ON download_history (task_id)"),
    _create_index("0113_idx_history_work", "下载历史作品索引", "idx_download_history_work_id", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_download_history_work_id ON download_history (work_id)", "CREATE INDEX idx_download_history_work_id ON download_history (work_id)"),
    _create_index("0114_idx_x_author_status", "X 作者任务状态索引", "idx_x_download_tasks_author_status", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_x_download_tasks_author_status ON x_download_tasks (x_author_id, status)", "CREATE INDEX idx_x_download_tasks_author_status ON x_download_tasks (x_author_id, status)"),
    _create_index("0115_idx_subscribed_authors", "已订阅作者部分索引", "idx_authors_is_subscribed_true", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_authors_is_subscribed_true ON authors (is_subscribed) WHERE is_subscribed IS TRUE", "CREATE INDEX idx_authors_is_subscribed_true ON authors (is_subscribed)"),
]


_RANKED_TASKS_SQL = """
    SELECT dt.id,
           row_number() OVER (
               PARTITION BY dt.work_id, dt.file_index
               ORDER BY CASE
                   WHEN dt.status = 'completed' THEN 3
                   WHEN dt.status IN ('downloading', 'paused') THEN 2
                   ELSE 1
               END DESC, dt.id DESC
           ) AS keep_rank,
           first_value(dt.id) OVER (
               PARTITION BY dt.work_id, dt.file_index
               ORDER BY CASE
                   WHEN dt.status = 'completed' THEN 3
                   WHEN dt.status IN ('downloading', 'paused') THEN 2
                   ELSE 1
               END DESC, dt.id DESC
           ) AS keeper_id
    FROM download_tasks dt
"""


def _deduplicate_download_tasks(connection: Connection) -> dict[str, Any]:
    """规范 file_index、保留重复任务备份、转移历史后删除重复行。"""
    null_file_ids = [
        int(value)
        for value in connection.execute(
            text("SELECT id FROM download_tasks WHERE file_index IS NULL ORDER BY id")
        ).scalars()
    ]
    if null_file_ids:
        connection.execute(text("UPDATE download_tasks SET file_index = 0 WHERE file_index IS NULL"))

    victim_rows = connection.execute(text(f"""
        WITH ranked AS ({_RANKED_TASKS_SQL})
        SELECT dt.*, ranked.keeper_id
        FROM ranked
        JOIN download_tasks dt ON dt.id = ranked.id
        WHERE ranked.keep_rank > 1
        ORDER BY dt.id
    """)).mappings().all()
    victims = [dict(row) for row in victim_rows]

    history_links = connection.execute(text(f"""
        WITH ranked AS ({_RANKED_TASKS_SQL})
        SELECT dh.id AS history_id, dh.task_id AS original_task_id,
               ranked.keeper_id
        FROM ranked
        JOIN download_history dh ON dh.task_id = ranked.id
        WHERE ranked.keep_rank > 1
        ORDER BY dh.id
    """)).mappings().all()

    dialect = connection.dialect.name
    if victims:
        if dialect == "postgresql":
            connection.execute(text(f"""
                WITH ranked AS ({_RANKED_TASKS_SQL})
                UPDATE download_history AS dh
                SET task_id = ranked.keeper_id
                FROM ranked
                WHERE ranked.keep_rank > 1 AND dh.task_id = ranked.id
            """))
            deleted_ids = connection.execute(text(f"""
                WITH ranked AS ({_RANKED_TASKS_SQL})
                DELETE FROM download_tasks AS dt
                USING ranked
                WHERE ranked.keep_rank > 1 AND dt.id = ranked.id
                RETURNING dt.id
            """)).scalars().all()
        else:
            connection.execute(
                text("""
                    UPDATE download_history
                    SET task_id = :keeper_id
                    WHERE task_id = :original_task_id
                """),
                [dict(row) for row in history_links],
            )
            victim_ids = [int(row["id"]) for row in victims]
            delete_statement = text(
                "DELETE FROM download_tasks WHERE id IN :victim_ids"
            ).bindparams(bindparam("victim_ids", expanding=True))
            result = connection.execute(delete_statement, {"victim_ids": victim_ids})
            deleted_ids = victim_ids if result.rowcount == len(victim_ids) else []
        if len(deleted_ids) != len(victims):
            raise RuntimeError("重复下载任务删除数量与预期不一致")

    remaining = connection.execute(text("""
        SELECT count(*)
        FROM (
            SELECT work_id, file_index
            FROM download_tasks
            GROUP BY work_id, file_index
            HAVING count(*) > 1
        ) AS duplicate_groups
    """)).scalar_one()
    if int(remaining or 0) != 0:
        raise RuntimeError("重复下载任务清理后仍存在重复键")

    if dialect == "postgresql":
        connection.execute(text("ALTER TABLE download_tasks ALTER COLUMN file_index SET NOT NULL"))
    elif dialect in {"mysql", "mariadb"}:
        connection.execute(text("ALTER TABLE download_tasks MODIFY file_index INTEGER NOT NULL DEFAULT 0"))

    return {
        "normalized_null_file_index_ids": null_file_ids,
        "victim_tasks": victims,
        "history_task_links": [dict(row) for row in history_links],
    }


def _replace_history_foreign_keys(connection: Connection) -> dict[str, Any]:
    orphan_count = int(connection.execute(text("""
        SELECT count(*)
        FROM download_history dh
        LEFT JOIN download_tasks dt ON dt.id = dh.task_id
        LEFT JOIN works w ON w.id = dh.work_id
        WHERE dt.id IS NULL OR w.id IS NULL
    """)).scalar_one() or 0)
    if orphan_count:
        raise RuntimeError(f"发现 {orphan_count} 条下载历史孤儿行，需人工确认后再迁移")

    dialect = connection.dialect.name
    replaced: list[str] = []
    if dialect == "postgresql":
        constraints = connection.execute(text("""
            SELECT tc.constraint_name, kcu.column_name, rc.delete_rule
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.constraint_schema = kcu.constraint_schema
            JOIN information_schema.referential_constraints rc
              ON tc.constraint_name = rc.constraint_name
             AND tc.constraint_schema = rc.constraint_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = current_schema()
              AND tc.table_name = 'download_history'
              AND kcu.column_name IN ('task_id', 'work_id')
        """)).mappings().all()
        quote = connection.dialect.identifier_preparer.quote
        for item in constraints:
            if item["delete_rule"] == "CASCADE":
                continue
            connection.execute(text(
                f"ALTER TABLE download_history DROP CONSTRAINT {quote(item['constraint_name'])}"
            ))
            replaced.append(str(item["column_name"]))
        existing_columns = {str(item["column_name"]) for item in constraints if item["delete_rule"] == "CASCADE"}
        if "task_id" not in existing_columns:
            connection.execute(text("ALTER TABLE download_history ADD CONSTRAINT download_history_task_id_fkey FOREIGN KEY (task_id) REFERENCES download_tasks(id) ON DELETE CASCADE"))
        if "work_id" not in existing_columns:
            connection.execute(text("ALTER TABLE download_history ADD CONSTRAINT download_history_work_id_fkey FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE"))
    else:
        # 当前生产目标是 PostgreSQL；其他数据库保留已有外键，避免用
        # PostgreSQL 语法破坏兼容部署。
        logger.warning("数据库 %s 暂不自动替换 download_history 外键", dialect)
    return {"orphan_rows": orphan_count, "replaced_columns": replaced}


def _backfill_counters(engine: Engine) -> dict[str, Any]:
    processed = {"works": 0, "authors": 0, "x_authors": 0}

    last_id = 0
    while True:
        with engine.begin() as connection:
            ids = connection.execute(text("""
                SELECT id FROM works WHERE id > :last_id ORDER BY id LIMIT :batch_size
            """), {"last_id": last_id, "batch_size": BACKFILL_BATCH_SIZE}).scalars().all()
            if not ids:
                break
            upper_id = int(ids[-1])
            connection.execute(text("""
                UPDATE works AS w
                SET is_downloaded = CASE
                    WHEN EXISTS (SELECT 1 FROM download_tasks dt WHERE dt.work_id = w.id)
                     AND NOT EXISTS (
                         SELECT 1 FROM download_tasks dt
                         WHERE dt.work_id = w.id AND coalesce(dt.status, '') <> 'completed'
                     )
                    THEN TRUE ELSE FALSE END
                WHERE w.id > :last_id AND w.id <= :upper_id
            """), {"last_id": last_id, "upper_id": upper_id})
        processed["works"] += len(ids)
        last_id = upper_id

    last_id = 0
    while True:
        with engine.begin() as connection:
            ids = connection.execute(text("""
                SELECT id FROM authors WHERE id > :last_id ORDER BY id LIMIT :batch_size
            """), {"last_id": last_id, "batch_size": BACKFILL_BATCH_SIZE}).scalars().all()
            if not ids:
                break
            upper_id = int(ids[-1])
            connection.execute(text("""
                UPDATE authors AS a
                SET total_works = (
                        SELECT count(*) FROM works w
                        WHERE w.author_id = a.id AND w.is_excluded IS NOT TRUE
                    ),
                    downloaded_works = (
                        SELECT count(*) FROM works w
                        WHERE w.author_id = a.id
                          AND w.is_excluded IS NOT TRUE
                          AND EXISTS (SELECT 1 FROM download_tasks dt WHERE dt.work_id = w.id)
                          AND NOT EXISTS (
                              SELECT 1 FROM download_tasks dt
                              WHERE dt.work_id = w.id AND coalesce(dt.status, '') <> 'completed'
                          )
                    )
                WHERE a.id > :last_id AND a.id <= :upper_id
            """), {"last_id": last_id, "upper_id": upper_id})
        processed["authors"] += len(ids)
        last_id = upper_id

    last_id = 0
    while True:
        with engine.begin() as connection:
            ids = connection.execute(text("""
                SELECT id FROM x_authors WHERE id > :last_id ORDER BY id LIMIT :batch_size
            """), {"last_id": last_id, "batch_size": BACKFILL_BATCH_SIZE}).scalars().all()
            if not ids:
                break
            upper_id = int(ids[-1])
            connection.execute(text("""
                UPDATE x_authors AS xa
                SET total_downloads = (
                    SELECT count(*) FROM x_media_assets xm WHERE xm.x_author_id = xa.id
                )
                WHERE xa.id > :last_id AND xa.id <= :upper_id
            """), {"last_id": last_id, "upper_id": upper_id})
        processed["x_authors"] += len(ids)
        last_id = upper_id

    return processed


DATA_MIGRATIONS = [
    Migration(
        migration_id="0200_deduplicate_download_tasks",
        migration_type="backfill",
        description="规范文件索引、归并重复下载任务并保留回滚信息",
        handler=_deduplicate_download_tasks,
    ),
    _create_index(
        "0201_uq_download_task_work_file",
        "下载任务作品与文件索引唯一约束",
        "uq_download_tasks_work_file_index_idx",
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_download_tasks_work_file_index_idx ON download_tasks (work_id, file_index)",
        "CREATE UNIQUE INDEX uq_download_tasks_work_file_index_idx ON download_tasks (work_id, file_index)",
        constraint_name="uq_download_tasks_work_file_index",
    ),
    Migration(
        migration_id="0202_history_cascade_foreign_keys",
        migration_type="backfill",
        description="将下载历史外键升级为 ON DELETE CASCADE",
        handler=_replace_history_foreign_keys,
    ),
    Migration(
        migration_id="0300_reconcile_media_counters",
        migration_type="backfill",
        description="分批回填作品、作者与 X 作者统计状态",
        batched_handler=_backfill_counters,
    ),
]


MIGRATIONS = [*ADD_COLUMN_MIGRATIONS, *INDEX_MIGRATIONS, *DATA_MIGRATIONS]


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _ensure_migration_table(connection: Connection) -> None:
    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id VARCHAR(128) PRIMARY KEY,
            migration_type VARCHAR(32) NOT NULL,
            description TEXT NOT NULL,
            details TEXT,
            applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """))


def _record_migration(
    connection: Connection,
    migration: Migration,
    details: Optional[dict[str, Any]] = None,
) -> None:
    connection.execute(text("""
        INSERT INTO schema_migrations (id, migration_type, description, details)
        VALUES (:id, :migration_type, :description, :details)
    """), {
        "id": migration.migration_id,
        "migration_type": migration.migration_type,
        "description": migration.description,
        "details": json.dumps(details, ensure_ascii=True, default=_json_default) if details else None,
    })


@contextmanager
def _migration_lock(engine: Engine) -> Iterator[None]:
    dialect = engine.dialect.name
    if dialect == "postgresql":
        connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            connection.execute(text("SELECT pg_advisory_lock(:lock_key)"), {"lock_key": MIGRATION_LOCK_KEY})
            yield
        finally:
            try:
                connection.execute(text("SELECT pg_advisory_unlock(:lock_key)"), {"lock_key": MIGRATION_LOCK_KEY})
            finally:
                connection.close()
        return

    if dialect in {"mysql", "mariadb"}:
        connection = engine.connect()
        try:
            acquired = connection.execute(text("SELECT GET_LOCK('douyin_schema_migrations', 120)" )).scalar_one()
            if int(acquired or 0) != 1:
                raise RuntimeError("等待数据库迁移锁超时")
            yield
        finally:
            try:
                connection.execute(text("SELECT RELEASE_LOCK('douyin_schema_migrations')"))
            finally:
                connection.close()
        return

    with _local_migration_lock:
        yield


def _applied_migration_ids(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return set(connection.execute(text("SELECT id FROM schema_migrations")).scalars().all())


def _apply_add_column(engine: Engine, migration: Migration) -> None:
    with engine.begin() as connection:
        inspector = inspect(connection)
        if migration.table_name not in inspector.get_table_names():
            raise RuntimeError(f"迁移目标表不存在：{migration.table_name}")
        columns = {item["name"] for item in inspector.get_columns(migration.table_name)}
        if migration.column_name not in columns:
            connection.execute(text(migration.sql))
        _record_migration(connection, migration)


def _postgres_index_is_valid(connection: Connection, index_name: str) -> Optional[bool]:
    row = connection.execute(text("""
        SELECT idx.indisvalid
        FROM pg_class index_relation
        JOIN pg_index idx ON idx.indexrelid = index_relation.oid
        JOIN pg_namespace ns ON ns.oid = index_relation.relnamespace
        WHERE ns.nspname = current_schema() AND index_relation.relname = :index_name
    """), {"index_name": index_name}).one_or_none()
    return bool(row[0]) if row is not None else None


def _attach_unique_constraint(connection: Connection, migration: Migration) -> None:
    if not migration.constraint_name or connection.dialect.name != "postgresql":
        return
    exists = connection.execute(text("""
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'download_tasks'::regclass AND conname = :constraint_name
    """), {"constraint_name": migration.constraint_name}).scalar_one_or_none()
    if exists:
        return
    quote = connection.dialect.identifier_preparer.quote
    connection.execute(text(
        f"ALTER TABLE download_tasks ADD CONSTRAINT {quote(migration.constraint_name)} "
        f"UNIQUE USING INDEX {quote(migration.index_name)}"
    ))


def _constraint_exists(engine: Engine, constraint_name: Optional[str]) -> bool:
    if not constraint_name or engine.dialect.name != "postgresql":
        return False
    with engine.connect() as connection:
        return bool(connection.execute(text("""
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'download_tasks'::regclass AND conname = :constraint_name
        """), {"constraint_name": constraint_name}).scalar_one_or_none())


def _apply_create_index(engine: Engine, migration: Migration) -> None:
    dialect = engine.dialect.name
    # 全新数据库由 metadata.create_all 直接建立唯一约束，无需再创建
    # 一条仅供挂载约束使用的临时唯一索引。
    if _constraint_exists(engine, migration.constraint_name):
        with engine.begin() as connection:
            _record_migration(connection, migration)
        return
    if dialect == "postgresql":
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            validity = _postgres_index_is_valid(connection, migration.index_name)
            if validity is False:
                quote = connection.dialect.identifier_preparer.quote
                connection.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {quote(migration.index_name)}"))
            connection.execute(text(migration.sql))
    else:
        with engine.begin() as connection:
            existing = {
                item["name"]
                for table_name in inspect(connection).get_table_names()
                for item in inspect(connection).get_indexes(table_name)
            }
            if migration.index_name not in existing:
                connection.execute(text(migration.generic_sql))

    with engine.begin() as connection:
        _attach_unique_constraint(connection, migration)
        _record_migration(connection, migration)


def _apply_transactional_handler(engine: Engine, migration: Migration) -> None:
    with engine.begin() as connection:
        details = migration.handler(connection)
        _record_migration(connection, migration, details)


def _apply_batched_handler(engine: Engine, migration: Migration) -> None:
    details = migration.batched_handler(engine)
    with engine.begin() as connection:
        _record_migration(connection, migration, details)


def run_schema_migrations(engine: Engine, metadata) -> None:
    """创建新表并按版本顺序执行所有尚未应用的迁移。"""
    current_migration: Optional[Migration] = None
    try:
        with _migration_lock(engine):
            with engine.begin() as connection:
                metadata.create_all(bind=connection)
                _ensure_migration_table(connection)

            applied = _applied_migration_ids(engine)
            for current_migration in MIGRATIONS:
                if current_migration.migration_id in applied:
                    continue
                if current_migration.migration_type == "add_column":
                    _apply_add_column(engine, current_migration)
                elif current_migration.migration_type == "create_index":
                    _apply_create_index(engine, current_migration)
                elif current_migration.migration_type == "backfill" and current_migration.handler:
                    _apply_transactional_handler(engine, current_migration)
                elif current_migration.migration_type == "backfill" and current_migration.batched_handler:
                    _apply_batched_handler(engine, current_migration)
                else:
                    raise RuntimeError(f"不支持的迁移定义：{current_migration.migration_id}")
                applied.add(current_migration.migration_id)
        clear_runtime_error("DATABASE_MIGRATION")
    except Exception as exc:
        migration_id = current_migration.migration_id if current_migration else "bootstrap"
        report_runtime_error(
            "DATABASE_MIGRATION",
            "数据库结构迁移",
            "数据库",
            f"迁移 {migration_id} 失败：{type(exc).__name__}: {exc}",
        )
        logger.exception("数据库迁移失败: %s", migration_id)
        raise RuntimeError(f"数据库迁移失败：{migration_id}") from exc
