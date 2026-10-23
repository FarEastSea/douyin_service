"""受管 Beat 的就绪证明：调度循环和当前队列必须同时可用。"""
from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import time

from celery.beat import PersistentScheduler
import redis

from app.core.config import settings
from app.core.config_transaction import atomic_json


class ManagedScheduler(PersistentScheduler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_interval = min(self.max_interval, 5)
        self._last_ready = 0.0

    def tick(self, *args, **kwargs):
        # 发布验收允许验证调度循环，但正式放行前不能触发周期任务。
        maintenance = Path.cwd() / ".runtime" / "deployment-maintenance"
        interval = 1 if maintenance.exists() else super().tick(*args, **kwargs)
        if time.monotonic() - self._last_ready >= 3:
            broker = settings.effective_celery_broker
            # 调度进程存活不足以证明连接可用，探测必须绑定当前配置。
            with redis.Redis.from_url(broker, socket_connect_timeout=1, socket_timeout=1) as client:
                client.ping()
            atomic_json(Path.cwd() / ".runtime" / "beat-ready.json", {
                "pid": os.getpid(), "token": os.environ.get("MEDIA_PROCESS_TOKEN", ""),
                "broker": sha256(broker.encode()).hexdigest(), "checked_at": time.time(),
            })
            self._last_ready = time.monotonic()
        return min(interval, 5)
