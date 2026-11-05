"""Unified read API for subscription execution history."""

from fastapi import APIRouter, Depends, HTTPException, Query
from app.models.database import get_sync_db
from app.services.subscription_records import unified_run_detail, unified_runs

router = APIRouter(prefix="/subscription-runs", tags=["订阅运行记录"])


def session():
    with get_sync_db() as db:
        yield db


@router.get("")
def runs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db=Depends(session),
):
    return unified_runs(db, page, page_size)


@router.get("/{run_key}")
def detail(run_key: str, db=Depends(session)):
    try:
        return unified_run_detail(db, run_key)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
