"""
Redis 客户端模块

为什么这样设计：
1. 进度信息存储在 Redis 中，避免频繁写数据库
2. 使用 Redis 的 HASH 结构存储任务进度，便于原子更新
3. 提供暂停信号机制，通过 Redis SET 存储暂停的任务ID
"""

import json
from threading import RLock
import time as _time
from datetime import datetime
from typing import Optional, Dict, Any, Iterable
from urllib.parse import quote

import redis

from app.core import env_config
from app.core.diagnostics import clear_runtime_error, report_runtime_error


CONFIG_VERSION_KEY = "douyin:config:version"
_CONFIG_VERSION_POLL_SECONDS = 0.5
_observed_config_version = 0
_version_cached = 0
_version_last_checked = 0.0
_version_source_key: Optional[tuple] = None
_version_lock = RLock()
_source_cached_signature: Optional[tuple] = None
_source_cached_key: Optional[tuple] = None
_source_lock = RLock()


def _redis_url_from_values(values: Dict[str, str]) -> str:
    redis_url = str(values.get("REDIS_URL") or "redis://localhost:6379/0").strip()
    password = str(values.get("REDIS_PASSWORD") or "").strip()
    if password and redis_url.startswith(("redis://", "rediss://")):
        scheme, remainder = redis_url.split("://", 1)
        authority = remainder.split("/", 1)[0]
        if "@" not in authority:
            redis_url = f"{scheme}://:{quote(password, safe='')}@{remainder}"
    return redis_url


def _redis_source_key(source_signature: Optional[tuple] = None) -> tuple:
    global _source_cached_signature, _source_cached_key
    signature = source_signature or (
        *env_config.get_env_file_signature(),
        env_config.get_local_config_generation(),
    )
    with _source_lock:
        if _source_cached_signature == signature and _source_cached_key is not None:
            return _source_cached_key
        values = env_config.read_env_file()
        _source_cached_signature = signature
        _source_cached_key = (*signature, _redis_url_from_values(values))
        return _source_cached_key


def _redis_connection_key() -> tuple:
    return (*_redis_source_key(), _observed_config_version)


class _RedisManager:
    def __init__(self) -> None:
        self._lock = RLock()
        self._client: Optional[redis.Redis] = None
        self._pool: Optional[redis.ConnectionPool] = None
        self._key: Optional[tuple] = None
        self._failed_key: Optional[tuple] = None
        self._failed_client: Optional[redis.Redis] = None
        self._failed_pool: Optional[redis.ConnectionPool] = None
        self._failed_error: Optional[Exception] = None
        self._retry_after = 0.0

    def get_client(self) -> redis.Redis:
        key = _redis_connection_key()
        with self._lock:
            if self._client is not None and self._key == key:
                return self._client
            if self._failed_key == key and _time.monotonic() < self._retry_after:
                if self._client is not None:
                    return self._client
                if self._failed_client is not None:
                    return self._failed_client
                raise redis.ConnectionError(str(self._failed_error or "Redis 连接不可用"))

            pool: Optional[redis.ConnectionPool] = None
            candidate: Optional[redis.Redis] = None
            try:
                pool = redis.ConnectionPool.from_url(
                    key[-2],
                    decode_responses=True,
                    socket_connect_timeout=1,
                    socket_timeout=2,
                    health_check_interval=30,
                )
                candidate = redis.Redis(connection_pool=pool)
                candidate.ping()
            except Exception as exc:
                report_runtime_error("REDIS_CONNECTION", "Redis 连接", "Redis", exc)
                previous_failed_pool = self._failed_pool
                self._failed_key = key
                self._failed_client = candidate
                self._failed_pool = pool
                self._failed_error = exc
                self._retry_after = _time.monotonic() + 1.0
                if previous_failed_pool is not None and previous_failed_pool is not pool:
                    previous_failed_pool.disconnect()
                if self._client is not None:
                    if pool is not None:
                        pool.disconnect()
                    self._failed_client = None
                    self._failed_pool = None
                    return self._client
                if candidate is not None:
                    return candidate
                raise

            old_pool = self._pool
            failed_pool = self._failed_pool
            self._client = candidate
            self._pool = pool
            self._key = key
            self._failed_key = None
            self._failed_client = None
            self._failed_pool = None
            self._failed_error = None
            self._retry_after = 0.0
            clear_runtime_error("REDIS_CONNECTION")
            if old_pool is not None and old_pool is not pool:
                old_pool.disconnect()
            if failed_pool is not None and failed_pool is not pool and failed_pool is not old_pool:
                failed_pool.disconnect()
            return candidate

    def reset(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.disconnect()
            if self._failed_pool is not None and self._failed_pool is not self._pool:
                self._failed_pool.disconnect()
            self._client = None
            self._pool = None
            self._key = None
            self._failed_key = None
            self._failed_client = None
            self._failed_pool = None
            self._failed_error = None
            self._retry_after = 0.0


_redis_manager = _RedisManager()


class _RedisClientProxy:
    def __getattr__(self, name: str):
        return getattr(_redis_manager.get_client(), name)


redis_client = _RedisClientProxy()


def capture_config_version_client() -> Optional[redis.Redis]:
    try:
        return _redis_manager.get_client()
    except Exception:
        return None


def get_config_version_cached(source_signature: Optional[tuple] = None) -> int:
    global _observed_config_version, _version_cached, _version_last_checked, _version_source_key
    now = _time.monotonic()
    source_key = _redis_source_key(source_signature)
    with _version_lock:
        if source_key == _version_source_key and now - _version_last_checked < _CONFIG_VERSION_POLL_SECONDS:
            return _version_cached

    try:
        raw = _redis_manager.get_client().get(CONFIG_VERSION_KEY)
        version = int(raw or 0)
        clear_runtime_error("REDIS_CONFIG_VERSION")
    except Exception as exc:
        report_runtime_error("REDIS_CONFIG_VERSION", "配置版本同步", "Redis", exc)
        version = _version_cached

    with _version_lock:
        _observed_config_version = version
        _version_cached = version
        _version_last_checked = now
        _version_source_key = source_key
        return version


def bump_config_version(client: Optional[redis.Redis] = None) -> Optional[int]:
    global _observed_config_version, _version_cached, _version_last_checked, _version_source_key
    try:
        target = client or _redis_manager.get_client()
        version = int(target.incr(CONFIG_VERSION_KEY))
        clear_runtime_error("REDIS_CONFIG_VERSION")
    except Exception as exc:
        report_runtime_error("REDIS_CONFIG_VERSION", "配置版本同步", "Redis", exc)
        return None

    with _version_lock:
        _observed_config_version = version
        _version_cached = version
        _version_last_checked = _time.monotonic()
        _version_source_key = _redis_source_key()
    return version


def reset_redis_client() -> None:
    """测试与故障恢复使用：丢弃当前连接，下次调用自动重建。"""
    _redis_manager.reset()


# ============ 键名常量 ============

PROGRESS_KEY_PREFIX = "douyin:progress:"  # 任务进度
PAUSE_SET_KEY = "douyin:paused_tasks"  # 暂停的任务集合
AUTHOR_DELETING_SET_KEY = "douyin:deleting_authors"  # 正在删除的作者集合
COOKIE_KEY = "douyin:cookie"  # Cookie 存储
STATS_KEY = "douyin:stats"  # 统计数据缓存
STATS_TTL = 60  # 统计缓存60秒
RUNTIME_CONFIG_KEY = "douyin:runtime_config"  # 运行期配置缓存
DOUYIN_RISK_STATE_KEY = "douyin:risk:cooldown"  # 抖音接口全局风控冷却

# X/Twitter 相关键名
X_COOKIE_KEY = "x:cookie_file"
X_TASK_LOG_PREFIX = "x:task:log:"
X_TASK_PID_PREFIX = "x:task:pid:"
X_TASK_STATE_PREFIX = "x:task:state:"
PLATFORM_TASK_LOG_PREFIX = "platform:task:log:"
PLATFORM_TASK_PID_PREFIX = "platform:task:pid:"
PLATFORM_TASK_STATE_PREFIX = "platform:task:state:"
def _x_task_log_max() -> int:
    from app.core.config import settings

    return settings.X_TASK_LOG_MAX_LINES


def _x_task_log_ttl() -> int:
    from app.core.config import settings

    return settings.X_TASK_LOG_TTL_SECONDS


def _x_task_state_ttl() -> int:
    from app.core.config import settings

    return settings.X_TASK_STATE_TTL_SECONDS


# ============ 进度管理 ============

def update_progress(task_id: int, data: Dict[str, Any]) -> None:
    """
    更新任务进度
    
    Args:
        task_id: 数据库任务ID
        data: 进度数据，包含 downloaded_bytes, total_bytes, speed 等
    """
    key = f"{PROGRESS_KEY_PREFIX}{task_id}"
    data["last_updated"] = str(_time.time())
    redis_client.hset(key, mapping={
        k: str(v) if not isinstance(v, str) else v 
        for k, v in data.items()
    })
    redis_client.expire(key, 7 * 24 * 3600)


def get_progress(task_id: int) -> Optional[Dict[str, Any]]:
    """
    获取任务进度
    
    Args:
        task_id: 数据库任务ID
        
    Returns:
        进度数据字典，或 None
    """
    key = f"{PROGRESS_KEY_PREFIX}{task_id}"
    data = redis_client.hgetall(key)
    if not data:
        return None
    
    result = {}
    for k, v in data.items():
        if k in ('downloaded_bytes', 'total_bytes', 'task_id', 'work_id'):
            result[k] = int(v)
        elif k in ('speed', 'progress_percent', 'last_updated'):
            result[k] = float(v)
        else:
            result[k] = v
    return result


def delete_progress(task_id: int) -> None:
    """删除任务进度"""
    key = f"{PROGRESS_KEY_PREFIX}{task_id}"
    redis_client.delete(key)


def get_all_progress() -> Dict[int, Dict[str, Any]]:
    """获取所有活跃任务的进度"""
    result = {}
    pattern = f"{PROGRESS_KEY_PREFIX}*"
    for key in redis_client.scan_iter(pattern):
        task_id = int(key.split(":")[-1])
        progress = get_progress(task_id)
        if progress:
            result[task_id] = progress
    return result


# ============ 暂停/恢复机制 ============

def pause_task(task_id: int) -> bool:
    """
    暂停任务
    
    通过将任务ID加入暂停集合，下载循环会检测到并停止
    """
    return redis_client.sadd(PAUSE_SET_KEY, str(task_id)) > 0


def resume_task(task_id: int) -> bool:
    """
    恢复任务
    
    从暂停集合中移除任务ID
    """
    return redis_client.srem(PAUSE_SET_KEY, str(task_id)) > 0


def is_task_paused(task_id: int) -> bool:
    """检查任务是否被暂停"""
    return redis_client.sismember(PAUSE_SET_KEY, str(task_id))


def get_paused_tasks() -> set:
    """获取所有暂停的任务ID"""
    return {int(tid) for tid in redis_client.smembers(PAUSE_SET_KEY)}


def clear_task_pause_states(task_ids: Iterable[int]) -> int:
    """批量清理任务暂停标记，避免删除后残留脏状态。"""
    normalized_ids = [str(task_id) for task_id in task_ids if task_id is not None]
    if not normalized_ids:
        return 0
    return redis_client.srem(PAUSE_SET_KEY, *normalized_ids)


def mark_author_deleting(author_id: int) -> bool:
    """标记作者正在删除，供下载任务快速停止。"""
    return redis_client.sadd(AUTHOR_DELETING_SET_KEY, str(author_id)) > 0


def clear_author_deleting(author_id: int) -> bool:
    """清理作者删除标记。"""
    return redis_client.srem(AUTHOR_DELETING_SET_KEY, str(author_id)) > 0


def is_author_deleting(author_id: int) -> bool:
    """检查作者是否处于删除流程中。"""
    return redis_client.sismember(AUTHOR_DELETING_SET_KEY, str(author_id))


# ============ 运行期配置缓存 ============

def set_runtime_config(config: Dict[str, Any]) -> None:
    """缓存运行期配置"""
    redis_client.hset(RUNTIME_CONFIG_KEY, mapping={
        key: json.dumps(value, ensure_ascii=False)
        for key, value in config.items()
    })
    redis_client.expire(RUNTIME_CONFIG_KEY, 7 * 24 * 3600)


def get_runtime_config() -> Dict[str, Any]:
    """获取运行期配置缓存"""
    raw = redis_client.hgetall(RUNTIME_CONFIG_KEY)
    result: Dict[str, Any] = {}
    for key, value in raw.items():
        try:
            result[key] = json.loads(value)
        except Exception:
            result[key] = value
    return result


# ============ 连接检查 ============

def check_connection() -> bool:
    """检查 Redis 连接"""
    try:
        redis_client.ping()
        return True
    except Exception:
        return False


# ============ 统计数据缓存 ============

def get_stats_cached() -> Optional[dict]:
    """获取缓存的统计数据"""
    cached = redis_client.get(STATS_KEY)
    if cached:
        return json.loads(cached)
    return None


# ============ 活动日志系统 ============

ACTIVITY_LOG_KEY = "douyin:activity_log"
ACTIVITY_LOG_MAX = 500
ACTIVITY_LOG_TTL = 7 * 24 * 3600


def append_activity_log(level: str, source: str, message: str, detail: str = "") -> None:
    """追加一条活动日志到 Redis（原子管道操作）"""
    import time as _time
    entry = json.dumps({
        "ts": _time.time(),
        "level": level,
        "source": source,
        "msg": message,
        "detail": detail[:500] if detail else ""
    }, ensure_ascii=False)
    pipe = redis_client.pipeline()
    pipe.lpush(ACTIVITY_LOG_KEY, entry)
    pipe.ltrim(ACTIVITY_LOG_KEY, 0, ACTIVITY_LOG_MAX - 1)
    pipe.expire(ACTIVITY_LOG_KEY, ACTIVITY_LOG_TTL)
    pipe.execute()


def get_activity_logs(start: int = 0, count: int = 100) -> list:
    """获取活动日志列表（最新在前）"""
    raw = redis_client.lrange(ACTIVITY_LOG_KEY, start, start + count - 1)
    logs = []
    for item in raw:
        try:
            logs.append(json.loads(item))
        except Exception:
            pass
    return logs


def clear_activity_logs() -> None:
    """清空活动日志"""
    redis_client.delete(ACTIVITY_LOG_KEY)


def get_activity_log_size() -> int:
    """获取活动日志占用的内存字节数（近似）"""
    try:
        length = redis_client.llen(ACTIVITY_LOG_KEY)
        if length == 0:
            return 0
        debug = redis_client.debug_object(ACTIVITY_LOG_KEY)
        return debug.get("serializedlength", length * 200)
    except Exception:
        return redis_client.llen(ACTIVITY_LOG_KEY) * 200


def trim_activity_logs(keep: int = 200) -> int:
    """裁剪活动日志，只保留最新的 keep 条，返回裁剪前的总数"""
    total = redis_client.llen(ACTIVITY_LOG_KEY)
    if total > keep:
        redis_client.ltrim(ACTIVITY_LOG_KEY, 0, keep - 1)
    return total


def set_stats_cached(data: dict) -> None:
    """缓存统计数据"""
    redis_client.set(STATS_KEY, json.dumps(data), ex=STATS_TTL)


def invalidate_stats_cache() -> None:
    """使统计数据缓存失效"""
    redis_client.delete(STATS_KEY)


# ============ X/Twitter Cookie 管理 ============

def set_x_cookie(cookie: str) -> None:
    """存储 X Cookie 内容"""
    redis_client.set(X_COOKIE_KEY, cookie)


def get_x_cookie() -> Optional[str]:
    """获取 X Cookie 内容"""
    return redis_client.get(X_COOKIE_KEY)


# ============ X/Twitter 任务日志 ============

def append_x_task_log(task_id: int, line: str) -> None:
    """追加一行日志到 X 任务日志列表"""
    key = f"{X_TASK_LOG_PREFIX}{task_id}"
    pipe = redis_client.pipeline()
    pipe.rpush(key, line)
    pipe.ltrim(key, -_x_task_log_max(), -1)
    pipe.expire(key, _x_task_log_ttl())
    pipe.execute()


def get_x_task_log(task_id: int, start: int = 0) -> list:
    """获取 X 任务日志（从 start 行开始）"""
    key = f"{X_TASK_LOG_PREFIX}{task_id}"
    return redis_client.lrange(key, start, -1)


def get_x_task_log_size(task_id: int) -> int:
    """获取 X 任务日志总行数。"""
    return redis_client.llen(f"{X_TASK_LOG_PREFIX}{task_id}")


def delete_x_task_log(task_id: int) -> None:
    """删除 X 任务日志"""
    redis_client.delete(f"{X_TASK_LOG_PREFIX}{task_id}")


def update_x_task_state(task_id: int, data: Dict[str, Any]) -> None:
    """更新 X 任务的实时状态缓存。"""
    key = f"{X_TASK_STATE_PREFIX}{task_id}"
    serialized: Dict[str, str] = {}
    for field_name, value in data.items():
        if value is None:
            continue
        if isinstance(value, datetime):
            serialized[field_name] = value.isoformat()
        elif isinstance(value, bool):
            serialized[field_name] = "1" if value else "0"
        else:
            serialized[field_name] = str(value)

    if not serialized:
        return

    redis_client.hset(key, mapping=serialized)
    redis_client.expire(key, _x_task_state_ttl())


def _deserialize_x_task_state(raw_state: Dict[str, str]) -> Optional[Dict[str, Any]]:
    if not raw_state:
        return None

    result: Dict[str, Any] = {}
    int_fields = {"file_count", "total_media_count", "downloaded_media_count", "retry_count"}
    float_fields = {"progress_percent"}
    datetime_fields = {"last_heartbeat_at", "started_at", "completed_at"}

    for field_name, value in raw_state.items():
        if field_name in int_fields:
            try:
                result[field_name] = int(value)
            except (TypeError, ValueError):
                continue
        elif field_name in float_fields:
            try:
                result[field_name] = float(value)
            except (TypeError, ValueError):
                continue
        elif field_name in datetime_fields:
            try:
                result[field_name] = datetime.fromisoformat(value)
            except (TypeError, ValueError):
                continue
        else:
            result[field_name] = value

    return result


def set_douyin_risk_state(error_type: str, reason: str, cooldown_seconds: int) -> Dict[str, Any]:
    """记录跨 Web/Worker 进程共享的抖音接口冷却状态。"""
    ttl = max(0, int(cooldown_seconds))
    payload = {
        "active": True,
        "error_type": str(error_type or "argus_blocked"),
        "reason": str(reason or "")[:500],
        "last_seen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    if ttl > 0:
        redis_client.setex(DOUYIN_RISK_STATE_KEY, ttl, serialized)
    else:
        # Cookie 身份信息缺失不会随时间自行恢复，保持拦截直到用户更新 Cookie。
        redis_client.set(DOUYIN_RISK_STATE_KEY, serialized)
    return {**payload, "retry_after": ttl}


def get_douyin_risk_state() -> Dict[str, Any]:
    """返回当前风控状态及 Redis 剩余 TTL。"""
    raw = redis_client.get(DOUYIN_RISK_STATE_KEY)
    ttl = int(redis_client.ttl(DOUYIN_RISK_STATE_KEY)) if raw else -2
    if not raw or ttl == -2 or ttl == 0:
        return {
            "active": False, "error_type": None, "reason": None,
            "last_seen_at": None, "retry_after": 0,
        }
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        payload = {}
    return {
        "active": True,
        "error_type": payload.get("error_type") or "argus_blocked",
        "reason": payload.get("reason"),
        "last_seen_at": payload.get("last_seen_at"),
        "retry_after": max(0, ttl),
    }


def clear_douyin_risk_state() -> bool:
    return bool(redis_client.delete(DOUYIN_RISK_STATE_KEY))


def get_x_task_state(task_id: int) -> Optional[Dict[str, Any]]:
    """读取一个 X 任务的实时状态缓存。"""
    raw_state = redis_client.hgetall(f"{X_TASK_STATE_PREFIX}{task_id}")
    return _deserialize_x_task_state(raw_state)


def get_x_task_states(task_ids: Iterable[int]) -> Dict[int, Optional[Dict[str, Any]]]:
    """通过 Redis pipeline 一次读取多个 X 任务状态。"""
    normalized_ids = [int(task_id) for task_id in task_ids]
    if not normalized_ids:
        return {}
    pipeline = redis_client.pipeline(transaction=False)
    for task_id in normalized_ids:
        pipeline.hgetall(f"{X_TASK_STATE_PREFIX}{task_id}")
    raw_states = pipeline.execute()
    return {
        task_id: _deserialize_x_task_state(raw_state)
        for task_id, raw_state in zip(normalized_ids, raw_states)
    }


def delete_x_task_state(task_id: int) -> None:
    """删除 X 任务的实时状态缓存。"""
    redis_client.delete(f"{X_TASK_STATE_PREFIX}{task_id}")


# ============ X/Twitter 任务 PID（用于取消） ============

def set_x_task_pid(task_id: int, pid: int) -> None:
    """存储 X 下载任务的子进程 PID"""
    key = f"{X_TASK_PID_PREFIX}{task_id}"
    redis_client.set(key, str(pid))
    redis_client.expire(key, 24 * 3600)


def get_x_task_pid(task_id: int) -> Optional[int]:
    """获取 X 下载任务的子进程 PID"""
    val = redis_client.get(f"{X_TASK_PID_PREFIX}{task_id}")
    return int(val) if val else None


def delete_x_task_pid(task_id: int) -> None:
    """删除 X 任务 PID"""
    redis_client.delete(f"{X_TASK_PID_PREFIX}{task_id}")


def _platform_task_key(prefix: str, platform: str, task_id: int) -> str:
    normalized = "".join(char for char in str(platform).lower() if char.isalnum() or char in "_-")
    if not normalized:
        raise ValueError("平台 ID 不能为空")
    return f"{prefix}{normalized}:{int(task_id)}"


def append_platform_task_log(platform: str, task_id: int, line: str) -> None:
    key = _platform_task_key(PLATFORM_TASK_LOG_PREFIX, platform, task_id)
    pipe = redis_client.pipeline()
    pipe.rpush(key, line)
    pipe.ltrim(key, -_x_task_log_max(), -1)
    pipe.expire(key, _x_task_log_ttl())
    pipe.execute()


def get_platform_task_log(platform: str, task_id: int, start: int = 0) -> list:
    return redis_client.lrange(
        _platform_task_key(PLATFORM_TASK_LOG_PREFIX, platform, task_id), start, -1
    )


def get_platform_task_log_size(platform: str, task_id: int) -> int:
    return redis_client.llen(_platform_task_key(PLATFORM_TASK_LOG_PREFIX, platform, task_id))


def delete_platform_task_log(platform: str, task_id: int) -> None:
    redis_client.delete(_platform_task_key(PLATFORM_TASK_LOG_PREFIX, platform, task_id))


def update_platform_task_state(platform: str, task_id: int, data: Dict[str, Any]) -> None:
    key = _platform_task_key(PLATFORM_TASK_STATE_PREFIX, platform, task_id)
    serialized: Dict[str, str] = {}
    for field_name, value in data.items():
        if value is None:
            continue
        serialized[field_name] = value.isoformat() if isinstance(value, datetime) else str(value)
    if serialized:
        redis_client.hset(key, mapping=serialized)
        redis_client.expire(key, _x_task_state_ttl())


def get_platform_task_state(platform: str, task_id: int) -> Optional[Dict[str, Any]]:
    raw = redis_client.hgetall(_platform_task_key(PLATFORM_TASK_STATE_PREFIX, platform, task_id))
    return _deserialize_x_task_state(raw)


def get_platform_task_states(platform: str, task_ids: Iterable[int]) -> Dict[int, Optional[Dict[str, Any]]]:
    normalized_ids = [int(task_id) for task_id in task_ids]
    if not normalized_ids:
        return {}
    pipe = redis_client.pipeline(transaction=False)
    for task_id in normalized_ids:
        pipe.hgetall(_platform_task_key(PLATFORM_TASK_STATE_PREFIX, platform, task_id))
    return {
        task_id: _deserialize_x_task_state(raw)
        for task_id, raw in zip(normalized_ids, pipe.execute())
    }


def delete_platform_task_state(platform: str, task_id: int) -> None:
    redis_client.delete(_platform_task_key(PLATFORM_TASK_STATE_PREFIX, platform, task_id))


def set_platform_task_pid(platform: str, task_id: int, pid: int) -> None:
    key = _platform_task_key(PLATFORM_TASK_PID_PREFIX, platform, task_id)
    redis_client.set(key, str(pid))
    redis_client.expire(key, 24 * 3600)


def get_platform_task_pid(platform: str, task_id: int) -> Optional[int]:
    value = redis_client.get(_platform_task_key(PLATFORM_TASK_PID_PREFIX, platform, task_id))
    return int(value) if value else None


def delete_platform_task_pid(platform: str, task_id: int) -> None:
    redis_client.delete(_platform_task_key(PLATFORM_TASK_PID_PREFIX, platform, task_id))

