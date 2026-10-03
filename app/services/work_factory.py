"""按全局作品 ID 原子创建或复用作品，不改变已有归属与排除状态。"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.models import Work


def _identity(aweme_id: str, author_id: int) -> dict:
    value = str(aweme_id or "").strip()
    if not value or len(value) > 64:
        raise ValueError("作品 aweme_id 缺失或长度不合法")
    return {"aweme_id": value, "author_id": author_id, "work_type": "video"}


def _existing(aweme_id: str):
    # 更新元数据前锁定同一行，避免并发请求覆盖统计或重复追加快照。
    return select(Work).where(Work.aweme_id == aweme_id).with_for_update().execution_options(
        populate_existing=True,
    )


def ensure_work_sync(db: Session, aweme_id: str, author_id: int) -> tuple[Work, bool]:
    values = _identity(aweme_id, author_id)
    # 部分业务会话关闭了 autoflush；先保留上一条作品的元数据与快照。
    db.flush()
    existing = db.scalar(_existing(values["aweme_id"]))
    if existing is not None:
        return existing, False
    if db.get_bind().dialect.name == "postgresql":
        inserted_id = db.scalar(postgresql_insert(Work).values(**values).on_conflict_do_nothing(
            index_elements=["aweme_id"],
        ).returning(Work.id))
        work = db.get(Work, inserted_id) if inserted_id is not None else db.scalar(_existing(values["aweme_id"]))
        if work is None:
            raise RuntimeError("作品原子创建后无法读取")
        return work, inserted_id is not None
    try:
        with db.begin_nested():
            work = Work(**values)
            db.add(work)
            db.flush()
        return work, True
    except IntegrityError:
        # 仅恢复已存在的同 ID 作品；外键、非空等其他错误继续上抛。
        existing = db.scalar(_existing(values["aweme_id"]))
        if existing is None:
            raise
        return existing, False


async def ensure_work_async(db: AsyncSession, aweme_id: str, author_id: int) -> tuple[Work, bool]:
    values = _identity(aweme_id, author_id)
    await db.flush()
    existing = (await db.execute(_existing(values["aweme_id"]))).scalar_one_or_none()
    if existing is not None:
        return existing, False
    if db.get_bind().dialect.name == "postgresql":
        inserted_id = (await db.execute(postgresql_insert(Work).values(**values).on_conflict_do_nothing(
            index_elements=["aweme_id"],
        ).returning(Work.id))).scalar_one_or_none()
        work = await db.get(Work, inserted_id) if inserted_id is not None else (
            await db.execute(_existing(values["aweme_id"]))
        ).scalar_one_or_none()
        if work is None:
            raise RuntimeError("作品原子创建后无法读取")
        return work, inserted_id is not None
    try:
        async with db.begin_nested():
            work = Work(**values)
            db.add(work)
            await db.flush()
        return work, True
    except IntegrityError:
        existing = (await db.execute(_existing(values["aweme_id"]))).scalar_one_or_none()
        if existing is None:
            raise
        return existing, False
