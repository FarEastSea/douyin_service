"""
Redis 客户端模块

为什么这样设计：
1. 进度信息存储在 Redis 中，避免频繁写数据库
2. 使用 Redis 的 HASH 结构存储任务进度，便于原子更新
3. 提供暂停信号机制，通过 Redis SET 存储暂停的任务ID
"""

import redis
import time as _time
from datetime import datetime
from app.core.config import settings
from typing import Optional, Dict, Any, Iterable
import json

# 创建 Redis 连接池
redis_pool = redis.ConnectionPool.from_url(
    settings.redis_url_with_auth,
    decode_responses=True
)

# Redis 客户端
redis_client = redis.Redis(connection_pool=redis_pool)


# ============ 键名常量 ============

PROGRESS_KEY_PREFIX = "douyin:progress:"  # 任务进度
PAUSE_SET_KEY = "douyin:paused_tasks"  # 暂停的任务集合
AUTHOR_DELETING_SET_KEY = "douyin:deleting_authors"  # 正在删除的作者集合
COOKIE_KEY = "douyin:cookie"  # Cookie 存储
STATS_KEY = "douyin:stats"  # 统计数据缓存
STATS_TTL = 60  # 统计缓存60秒
RUNTIME_CONFIG_KEY = "douyin:runtime_config"  # 运行期配置缓存

# X/Twitter 相关键名
X_COOKIE_KEY = "x:cookie_file"
X_TASK_LOG_PREFIX = "x:task:log:"
X_TASK_PID_PREFIX = "x:task:pid:"
X_TASK_STATE_PREFIX = "x:task:state:"
X_TASK_LOG_MAX = settings.X_TASK_LOG_MAX_LINES
X_TASK_LOG_TTL = settings.X_TASK_LOG_TTL_SECONDS
X_TASK_STATE_TTL = settings.X_TASK_STATE_TTL_SECONDS


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


# ============ Cookie 管理 ============

def set_cookie(cookie: str) -> None:
    """存储 Cookie"""
    redis_client.set(COOKIE_KEY, cookie)


def get_cookie() -> Optional[str]:
    """获取 Cookie"""
    return redis_client.get(COOKIE_KEY)


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
    except redis.ConnectionError:
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
    pipe.ltrim(key, -X_TASK_LOG_MAX, -1)
    pipe.expire(key, X_TASK_LOG_TTL)
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
    redis_client.expire(key, X_TASK_STATE_TTL)


def get_x_task_state(task_id: int) -> Optional[Dict[str, Any]]:
    """读取 X 任务的实时状态缓存。"""
    key = f"{X_TASK_STATE_PREFIX}{task_id}"
    raw_state = redis_client.hgetall(key)
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

