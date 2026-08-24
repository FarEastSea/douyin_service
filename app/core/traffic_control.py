"""跨 Worker 的下载并发与抖音业务请求节流。"""

from __future__ import annotations

from contextlib import contextmanager
import logging
import threading
import time
from uuid import uuid4

from app.core import redis_client
from app.core.config import settings


logger = logging.getLogger(__name__)
DOWNLOAD_SLOTS_KEY = "douyin:download:active_slots"
DOUYIN_REQUEST_PACE_KEY = "douyin:api:request_pace"
DOWNLOAD_SLOT_LEASE_SECONDS = 300


_ACQUIRE_SLOT_SCRIPT = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local token = ARGV[2]
local lease_ms = tonumber(ARGV[3])
local now_parts = redis.call('TIME')
local now_ms = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', key, '-inf', now_ms)
if redis.call('ZCARD', key) < limit then
  redis.call('ZADD', key, now_ms + lease_ms, token)
  redis.call('PEXPIRE', key, lease_ms * 2)
  return 1
end
return 0
"""

_RENEW_SLOT_SCRIPT = """
local key = KEYS[1]
local token = ARGV[1]
local lease_ms = tonumber(ARGV[2])
if redis.call('ZSCORE', key, token) then
  local now_parts = redis.call('TIME')
  local now_ms = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
  redis.call('ZADD', key, now_ms + lease_ms, token)
  redis.call('PEXPIRE', key, lease_ms * 2)
  return 1
end
return 0
"""

_PACE_REQUEST_SCRIPT = """
local key = KEYS[1]
local interval_ms = tonumber(ARGV[1])
local now_parts = redis.call('TIME')
local now_ms = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
local last_ms = tonumber(redis.call('GET', key) or '0')
local wait_ms = last_ms + interval_ms - now_ms
if wait_ms > 0 then
  return wait_ms
end
redis.call('SET', key, now_ms, 'PX', math.max(interval_ms * 4, 60000))
return 0
"""


class _DownloadSlot:
    def __init__(self, token: str, lease_seconds: int) -> None:
        self.token = token
        self.lease_ms = lease_seconds * 1000
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._keep_alive, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _keep_alive(self) -> None:
        while not self._stop.wait(max(10.0, self.lease_ms / 3000)):
            try:
                redis_client.redis_client.eval(
                    _RENEW_SLOT_SCRIPT, 1, DOWNLOAD_SLOTS_KEY, self.token, self.lease_ms
                )
            except Exception as exc:
                logger.warning("续租全局下载配额失败: %s", exc)

    def release(self) -> None:
        self._stop.set()
        try:
            redis_client.redis_client.zrem(DOWNLOAD_SLOTS_KEY, self.token)
        except Exception as exc:
            logger.warning("释放全局下载配额失败: %s", exc)


@contextmanager
def global_download_slot(task_id: int | str):
    """按网页最新值限制所有 Worker 合计的真实下载并发。"""
    token = f"{task_id}:{uuid4().hex}"
    slot = _DownloadSlot(token, DOWNLOAD_SLOT_LEASE_SECONDS)
    acquired = False
    redis_available = True
    while not acquired:
        limit = max(1, min(int(settings.snapshot().MAX_CONCURRENT_DOWNLOADS), 20))
        try:
            acquired = bool(redis_client.redis_client.eval(
                _ACQUIRE_SLOT_SCRIPT,
                1,
                DOWNLOAD_SLOTS_KEY,
                limit,
                token,
                DOWNLOAD_SLOT_LEASE_SECONDS * 1000,
            ))
        except Exception as exc:
            # Redis 故障时仍保留 Celery 本地并发上限，避免整个下载链路停摆。
            logger.warning("全局下载配额不可用，退回 Worker 本地并发限制: %s", exc)
            redis_available = False
            acquired = True
        if not acquired:
            time.sleep(0.25)

    if redis_available:
        slot.start()
    try:
        yield
    finally:
        if redis_available:
            slot.release()


def wait_for_douyin_request_slot(min_interval_seconds: float) -> None:
    """让所有进程的抖音业务 API 请求保持统一最小间隔。"""
    interval_ms = max(0, int(float(min_interval_seconds) * 1000))
    if interval_ms <= 0:
        return
    while True:
        try:
            wait_ms = int(redis_client.redis_client.eval(
                _PACE_REQUEST_SCRIPT, 1, DOUYIN_REQUEST_PACE_KEY, interval_ms
            ))
        except Exception as exc:
            logger.warning("抖音 API 全局节流不可用: %s", exc)
            return
        if wait_ms <= 0:
            return
        time.sleep(min(wait_ms / 1000, 5.0))
