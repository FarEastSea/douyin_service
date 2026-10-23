"""下载状态事务的锁顺序：作品身份锁、Author、Work、DownloadTask。

身份锁只用于建作品，必须在任何父行锁之前批量申请。网络传输不持有这些锁；
仅写进度的短事务可以只锁任务，但不能在同一事务中再进入状态变更。
"""
from __future__ import annotations

import hashlib

from sqlalchemy import event, select, text

from app.models.models import Author, DownloadTask, Work


class DownloadLockOrderError(RuntimeError):
    pass


def _state(db):
    state = db.info.get("download_state_locks")
    if state is None:
        state = {"authors": set(), "works": set(), "identities": set()}
        db.info["download_state_locks"] = state
        if not db.info.get("download_state_lock_listener"):
            def clear(session, transaction):
                if transaction.parent is None:
                    session.info.pop("download_state_locks", None)
            event.listen(db, "after_transaction_end", clear)
            db.info["download_state_lock_listener"] = True
    return state


def lock_authors_sync(db, author_ids):
    state = _state(db)
    requested = {int(value) for value in author_ids if value is not None}
    missing = requested - state["authors"]
    if not missing:
        return
    if state["works"] or (state["authors"] and min(missing) < max(state["authors"])):
        raise DownloadLockOrderError("必须在锁作品前按作者 ID 顺序预锁完整作者集合")
    with db.no_autoflush:
        found = set(db.scalars(select(Author.id).where(Author.id.in_(sorted(missing)))
                               .order_by(Author.id).with_for_update()).all())
    if found != missing:
        raise LookupError("作者已被删除")
    state["authors"].update(found)


def lock_works_sync(db, work_ids, *, refresh=True):
    ids = sorted({int(value) for value in work_ids if value is not None})
    if not ids:
        return
    state = _state(db)
    missing = set(ids) - state["works"]
    if not missing:
        return
    with db.no_autoflush:
        rows = db.execute(select(Work.id, Work.author_id).where(Work.id.in_(sorted(missing)))).all()
    lock_authors_sync(db, [row.author_id for row in rows])
    with db.no_autoflush:
        # 业务入口可能在等待父锁前已经读取 Work。锁定后读取当前版本，避免
        # ORM 将旧快照上的 False→False 当作无变化，漏写并发完成后的状态。
        locked = {work.id for work in db.scalars(select(Work).where(Work.id.in_(sorted(missing)))
            .order_by(Work.id).with_for_update().execution_options(populate_existing=refresh)).all()}
    if locked != missing:
        raise LookupError("作品已被删除")
    state["works"].update(locked)


def lock_task_graph_sync(db, task_id, *, refresh=True):
    with db.no_autoflush:
        work_id = db.scalar(select(DownloadTask.work_id).where(DownloadTask.id == task_id))
    if work_id is None:
        return None
    lock_works_sync(db, [work_id], refresh=refresh)
    with db.no_autoflush:
        return db.scalar(select(DownloadTask).where(DownloadTask.id == task_id)
                         .with_for_update().execution_options(populate_existing=refresh))


def prepare_work_batch_sync(db, aweme_ids, author_ids):
    values = sorted({str(value).strip() for value in aweme_ids})
    state = _state(db)
    new_values = set(values) - state["identities"]
    if new_values and (state["authors"] or state["works"]):
        raise DownloadLockOrderError("必须在锁作者前预锁本批全部作品身份")
    if db.get_bind().dialect.name == "postgresql":
        for value in sorted(new_values):
            key = int.from_bytes(hashlib.sha256(("douyin-work:" + value).encode()).digest()[:8], "big", signed=True)
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    state["identities"].update(new_values)
    with db.no_autoflush:
        rows = db.execute(select(Work.id, Work.author_id).where(Work.aweme_id.in_(values))).all()
    lock_authors_sync(db, [*author_ids, *(row.author_id for row in rows)])
    lock_works_sync(db, [row.id for row in rows])


async def lock_authors(db, author_ids):
    await db.run_sync(lambda session: lock_authors_sync(session, author_ids))


async def lock_works(db, work_ids, *, refresh=True):
    await db.run_sync(lambda session: lock_works_sync(session, work_ids, refresh=refresh))


async def lock_task_graph(db, task_id):
    return await db.run_sync(lambda session: lock_task_graph_sync(session, task_id))


async def prepare_work_batch(db, aweme_ids, author_ids):
    await db.run_sync(lambda session: prepare_work_batch_sync(session, aweme_ids, author_ids))
