"""队列切换只在旧队列排空后进行；切换期间禁止新的生产者投递。"""
from contextlib import contextmanager
import json
import asyncio
import logging
import socket
from contextvars import ContextVar
from pathlib import Path

from celery import Task
from kombu import Connection, Producer
import redis

from app.core import env_config
from app.core.config_transaction import atomic_json, configuration_lock, journal_path, recover_config_change

resuming_change = ContextVar("resuming_queue_change", default=False)
QUEUE_KEYS = {"REDIS_URL", "REDIS_PASSWORD", "CELERY_CONNECTION_MODE", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND", "MAX_CONCURRENT_DOWNLOADS"}


class QueueDrainRequired(ValueError):
    pass


def inspect_inflight(celery_app, broker: str, worker_pid: int | None = None):
    """兼容旧节点名，但必须把控制回应绑定到已核验的本机进程。"""
    with celery_app.connection_for_read(url=broker) as connection:
        inspector = celery_app.control.inspect(timeout=3, connection=connection)
        destination = None
        if worker_pid:
            stats = inspector.stats() or {}
            candidates = [name for name, values in stats.items()
                          if values.get("pid") == worker_pid and name.endswith("@" + socket.gethostname())]
            if len(candidates) != 1:
                raise QueueDrainRequired("本项目 Worker 尚未确认在途任务，等待恢复后排空")
            destination = candidates[0]
            inspector = celery_app.control.inspect(destination=[destination], timeout=3, connection=connection)
        groups = (inspector.active(), inspector.reserved(), inspector.scheduled())
        if destination and any(destination not in (group or {}) for group in groups):
            raise QueueDrainRequired("本项目 Worker 未完整确认在途任务，等待恢复后排空")
        return groups


def pending_path() -> Path:
    return env_config.ENV_PATH.parent / ".runtime" / "queue-pending.json"


def restart_path() -> Path:
    return env_config.ENV_PATH.parent / ".runtime" / "queue-restart.json"


def resume_process_change() -> None:
    from app.core.config import settings
    from app.core.process_manager import process_manager
    with configuration_lock():
        if journal_path().exists() or not restart_path().exists() or pending_path().exists():
            return
        intent = json.loads(restart_path().read_text(encoding="utf-8"))
        _complete_process_change(intent, settings.snapshot())


def _complete_process_change(intent: dict, current) -> None:
    from app.core.process_manager import configuration_signature, process_manager
    from app.tasks.celery_app import celery_app
    signature = configuration_signature(current)
    # 只有事务提交或补偿已确定后才能调用。回滚时以已恢复的持久配置重定目标。
    intent = {**intent, "target_signature": signature, "concurrency": int(current.MAX_CONCURRENT_DOWNLOADS)}
    atomic_json(restart_path(), intent)
    celery_app.conf.broker_url = current.effective_celery_broker
    celery_app.conf.result_backend = current.effective_celery_backend
    celery_app._backend = celery_app._get_backend()
    for name in ("worker", "beat"):
        if intent.get(name):
            result = process_manager.ensure_configuration(name, signature)
            if not result.get("success"):
                raise RuntimeError(f"后台 {name} 尚未按目标配置恢复")
    status = process_manager.get_status()
    for name in ("worker", "beat"):
        if intent.get(name) and (not status.get(name, {}).get("ready")
                                or status.get(name, {}).get("configuration_signature") != signature):
            raise RuntimeError(f"后台 {name} 的目标配置未确认就绪")
    atomic_json(state_path(), {"state": "applied", "message": "后台连接和进程已恢复"})
    restart_path().unlink()


def stage_queue_change(updates: dict) -> None:
    from app.core.process_manager import process_manager
    if not set(updates) <= QUEUE_KEYS:
        raise ValueError("连接切换需要等待旧任务结束，请单独保存队列或 Redis 配置")
    with configuration_lock():
        if pending_path().exists():
            raise ValueError("已有后台连接变更等待生效，请等待完成")
        status = process_manager.get_status()
        atomic_json(pending_path(), {"updates": updates, "resume": {
            name: bool(status.get(name, {}).get("desired", True)) for name in ("worker", "beat")}})
        atomic_json(state_path(), {"state": "draining", "message": "已保存待生效：停止新投递，旧队列排空后自动切换"})
        process_manager.stop_beat(preserve_desired=True)


def _record_recovery_failure(message: str) -> None:
    try:
        atomic_json(state_path(), {"state": "failed", "message": message})
    except OSError as exc:
        # 文件系统暂不可写时也必须保留恢复循环，原持久意图不会被删除。
        logging.getLogger(__name__).warning("恢复状态暂不能写入（%s）", type(exc).__name__)


async def resume_pending_changes() -> None:
    """由 Web 生命周期管理；持久化意图使进程重启后继续排空。"""
    from app.models.database import get_async_db
    from app.api.system import CompleteConfigUpdate, save_complete_settings
    while True:
        try:
            # 已提交但发布失败的配置也在当前进程中恢复，无需手工重启。
            await asyncio.to_thread(recover_config_change)
            await asyncio.to_thread(resume_process_change)
        except Exception as exc:
            _record_recovery_failure(f"后台进程恢复失败（{type(exc).__name__}），将继续重试")
        if pending_path().exists():
            token = resuming_change.set(True)
            try:
                from app.core.config_transaction import configuration_session
                async with configuration_session():
                    # 与用户撤回共用同一锁，不能在撤回后提交之前读出的旧意图。
                    if pending_path().exists():
                        updates = json.loads(pending_path().read_text(encoding="utf-8"))["updates"]
                        async for db in get_async_db():
                            await save_complete_settings(CompleteConfigUpdate(values=updates), db)
                        pending_path().unlink(missing_ok=True)
            except QueueDrainRequired:
                pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _record_recovery_failure(f"后台配置切换失败（{type(exc).__name__}），已保留变更并自动重试")
                logging.getLogger(__name__).warning("后台配置切换暂未完成: %s", type(exc).__name__)
            finally:
                resuming_change.reset(token)
        await asyncio.sleep(10)


def state_path() -> Path:
    return env_config.ENV_PATH.parent / ".runtime" / "queue-change.json"


def change_status() -> dict:
    path = state_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"state": "applied"}


def recover_queue_change() -> None:
    if pending_path().exists():
        atomic_json(state_path(), {"state": "draining", "message": "正在恢复待生效的后台连接变更"})
    elif restart_path().exists():
        atomic_json(state_path(), {'state': 'switching', 'message': '按持久化配置恢复后台进程'})
    elif change_status().get('state') == 'switching':
        atomic_json(state_path(), {'state': 'applied', 'message': '已按持久化配置恢复后台连接；未完成配置由变更日志先行补偿'})


class DynamicQueueTask(Task):
    abstract = True

    def apply_async(self, args=None, kwargs=None, **options):
        with configuration_lock():
            return self._publish(args, kwargs, options)

    def _publish(self, args, kwargs, options):
        from app.core.config import settings
        from celery import current_task
        executing = bool(getattr(getattr(current_task, "request", None), "id", None))
        if journal_path().exists():
            raise RuntimeError("配置发布尚未完成，任务暂未投递；系统正在自动恢复")
        if (env_config.ENV_PATH.parent / ".runtime" / "deployment-maintenance").exists() and not executing:
            raise RuntimeError("项目正在发布，暂停新任务投递；请稍后重试")
        if (restart_path().exists() or change_status().get("state") == "switching"
                or (pending_path().exists() and not executing)):
            raise RuntimeError("后台队列正在切换，暂未投递；请稍后重试")
        current = settings.snapshot()
        # 显式创建当前地址的生产者，不能复用模块加载时的旧连接池。
        options.pop("producer", None)
        with Connection(current.effective_celery_broker, connect_timeout=5) as connection:
            with Producer(connection) as producer:
                return super().apply_async(args=args, kwargs=kwargs, producer=producer, **options)


@contextmanager
def queue_transition(candidate):
    from app.core.config import settings
    from app.core.process_manager import process_manager
    from app.tasks.celery_app import celery_app
    old = settings.snapshot()
    phase = {"committed": False}
    if pending_path().exists() and not resuming_change.get():
        if any(getattr(candidate, key) != getattr(old, key) for key in QUEUE_KEYS):
            raise ValueError("已有连接变更正在等待生效")
    old_key = (old.effective_celery_broker, old.effective_celery_backend)
    new_key = (candidate.effective_celery_broker, candidate.effective_celery_backend)
    if restart_path().exists() and not resuming_change.get():
        if (old_key != new_key or old.redis_url_with_auth != candidate.redis_url_with_auth
                or old.MAX_CONCURRENT_DOWNLOADS != candidate.MAX_CONCURRENT_DOWNLOADS):
            raise ValueError("后台连接仍在恢复，请等待恢复完成后再修改队列配置")
    if (old_key == new_key and old.redis_url_with_auth == candidate.redis_url_with_auth
            and old.MAX_CONCURRENT_DOWNLOADS == candidate.MAX_CONCURRENT_DOWNLOADS):
        if resuming_change.get():
            if restart_path().exists():
                intent = json.loads(restart_path().read_text(encoding="utf-8"))
            else:
                intent = json.loads(pending_path().read_text(encoding="utf-8")).get("resume", {})
            _complete_process_change(intent, candidate)
        yield phase
        return
    path = state_path()
    # 保存入口持有跨进程配置锁；状态对所有 Worker/Web/Beat 可见。
    atomic_json(path, {"state": "switching", "message": "暂停投递并核验旧队列"})
    status = process_manager.get_status()
    intent = json.loads(pending_path().read_text(encoding="utf-8")).get("resume", {}) if pending_path().exists() else {}
    restart_beat = bool(intent.get("beat", status.get("beat", {}).get("desired", True)))
    restart_worker = bool(intent.get("worker", status.get("worker", {}).get("desired", True)))
    try:
        if restart_beat or status.get("beat", {}).get("running"):
            if not process_manager.stop_beat(preserve_desired=True).get("success"):
                raise RuntimeError("旧调度进程未能停止，连接切换未保存")
        for url in new_key:
            if not url.startswith(("redis://", "rediss://")):
                raise ValueError("独立队列目前仅支持 Redis/rediss 地址")
            client = redis.Redis.from_url(url, socket_connect_timeout=3, socket_timeout=3)
            try:
                client.ping()
            finally:
                client.close()
        client = redis.Redis.from_url(old_key[0], socket_connect_timeout=3, socket_timeout=3)
        try:
            queued = sum(int(client.llen(key)) for key in client.scan_iter(match="celery*", count=100)
                         if client.type(key) == b"list")
            unacked = int(client.hlen("unacked"))
        finally:
            client.close()
        active, reserved, scheduled = inspect_inflight(
            celery_app, old_key[0], status.get("worker", {}).get("pid"))
        if queued or unacked or any(items for group in (active, reserved, scheduled) for items in (group or {}).values()):
            raise QueueDrainRequired("旧队列仍有任务，需要排空后切换")
        if status.get("worker", {}).get("running") and active is None:
            raise ValueError("无法确认旧 Worker 是否排空，切换未保存")
        from app.core.process_manager import configuration_signature
        atomic_json(restart_path(), {"worker": restart_worker, "beat": restart_beat,
                                    "target_signature": configuration_signature(candidate),
                                    "concurrency": int(candidate.MAX_CONCURRENT_DOWNLOADS)})
        if restart_worker or status.get("worker", {}).get("running"):
            if not process_manager.stop_worker(preserve_desired=True).get("success"):
                raise RuntimeError("旧 Worker 未能停止，连接切换未保存")
        yield phase
        _complete_process_change({"worker": restart_worker, "beat": restart_beat}, candidate)
        restart_beat = False
    except QueueDrainRequired:
        atomic_json(path, {"state": "draining", "message": "等待旧队列排空后自动切换"})
        raise
    except Exception as exc:
        atomic_json(path, {"state": "failed", "message": f"后台连接切换未完成（{type(exc).__name__}），以已持久化配置为准；进程恢复将自动重试"})
        raise
    finally:
        # 已提交/提交结果不明时，prepared 清单仍会读旧值。保留停止状态，
        # 由事务恢复确定持久配置后再替换进程，绝不能在这里误启动旧代际。
        if not phase["committed"] and not journal_path().exists():
            if restart_path().exists():
                _complete_process_change({"worker": restart_worker, "beat": restart_beat}, settings.snapshot())
            elif restart_beat and not pending_path().exists():
                process_manager.start_beat()
