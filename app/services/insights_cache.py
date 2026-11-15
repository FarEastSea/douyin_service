"""洞察统计的 Redis 缓存。

统计需要扫描大量媒体记录，不应每次打开页面都重算：
- 结果在新鲜期内直接复用；
- 过期后只允许一个请求重算，其余请求继续拿上一份结果；
- 重算失败或 Redis 不可用时退回上一份结果或直接计算，页面不因缓存故障不可用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable
from uuid import uuid4

from app.core import redis_client

logger = logging.getLogger(__name__)

KEY_PREFIX = "insights:v1:"
LOCK_PREFIX = "insights:lock:"
# 过期结果保留一天，供重算期间和重算失败时回退。
RETAIN_SECONDS = 24 * 3600
# 重算锁的最长持有时间；进程在重算中途退出时锁自动释放。
LOCK_SECONDS = 120
# 手动刷新的最小间隔，避免连续点击反复触发全量统计。
MIN_FORCE_INTERVAL = 60


def _read(name: str) -> dict[str, Any] | None:
    try:
        raw = redis_client.redis_client.get(KEY_PREFIX + name)
        entry = json.loads(raw) if raw else None
    except Exception as exc:
        logger.warning("洞察缓存读取失败，改为直接计算: %s", type(exc).__name__)
        return None
    if isinstance(entry, dict) and isinstance(entry.get("data"), dict) and isinstance(entry.get("at"), (int, float)):
        return entry
    return None


def _write(name: str, data: dict[str, Any], token: str) -> float:
    at = time.time()
    try:
        redis_client.redis_client.eval(
            "if redis.call('get',KEYS[1]) == ARGV[1] then "
            "return redis.call('set',KEYS[2],ARGV[2],'EX',ARGV[3]) else return 0 end",
            2, LOCK_PREFIX + name, KEY_PREFIX + name, token,
            json.dumps({"at": at, "data": data}, ensure_ascii=False), RETAIN_SECONDS,
        )
    except Exception as exc:
        logger.warning("洞察缓存写入失败，下次打开会重新计算: %s", type(exc).__name__)
    return at


class InsightsBusy(RuntimeError):
    pass


def _acquire(name: str, token: str) -> bool:
    try:
        return bool(redis_client.redis_client.set(LOCK_PREFIX + name, token, nx=True, ex=LOCK_SECONDS))
    except Exception as exc:
        # 无法协调时允许本次请求自行计算。
        logger.warning("洞察重算锁不可用: %s", type(exc).__name__)
        return True


def _release(name: str, token: str) -> None:
    try:
        redis_client.redis_client.eval(
            "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end",
            1, LOCK_PREFIX + name, token,
        )
    except Exception as exc:
        logger.warning("洞察重算锁释放失败，将在 %s 秒后自动过期: %s", LOCK_SECONDS, type(exc).__name__)


async def cached(
    name: str, fresh_seconds: int, compute: Callable[[], Awaitable[dict[str, Any]]], *, force: bool = False,
) -> tuple[dict[str, Any], float, bool]:
    """返回 (数据, 生成时间戳, 是否为过期的回退结果)。"""
    entry = await asyncio.to_thread(_read, name)
    if entry is not None:
        age = time.time() - entry["at"]
        if age < (MIN_FORCE_INTERVAL if force else fresh_seconds):
            return entry["data"], entry["at"], False
    token = uuid4().hex
    owner = await asyncio.to_thread(_acquire, name, token)
    if not owner and entry is not None:
        # 另一个请求正在重算，先返回上一份结果。
        return entry["data"], entry["at"], True
    if not owner:
        # 冷缓存同样只允许一个聚合者；不能让同时打开页面的每个请求各扫全表。
        for _ in range(100):
            await asyncio.sleep(0.1)
            entry = await asyncio.to_thread(_read, name)
            if entry is not None:
                return entry["data"], entry["at"], False
            owner = await asyncio.to_thread(_acquire, name, token)
            if owner:
                break
        if not owner:
            raise InsightsBusy("洞察统计正在生成，请稍后重试")
    try:
        data = await compute()
    except Exception:
        if entry is not None:
            logger.exception("洞察统计重算失败，返回上一份结果: %s", name)
            return entry["data"], entry["at"], True
        raise
    else:
        at = await asyncio.to_thread(_write, name, data, token)
        return data, at, False
    finally:
        if owner:
            await asyncio.to_thread(_release, name, token)
