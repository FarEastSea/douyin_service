"""跨 Web 生命周期的 Celery 托管：持久化意图、身份接管和就绪确认。"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from hashlib import sha256
from uuid import uuid4
from typing import Optional

import psutil

from app.core.config_transaction import atomic_json


def configuration_signature(current=None) -> str:
    """绑定真实进程的完整队列配置，不在运行清单暴露连接凭据。"""
    if current is None:
        from app.core.config import settings
        current = settings.snapshot()
    payload = {"broker": current.effective_celery_broker,
               "backend": current.effective_celery_backend,
               "concurrency": int(current.MAX_CONCURRENT_DOWNLOADS)}
    return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class ProcessManager:
    def __init__(self):
        self._project_dir = str(Path(__file__).resolve().parent.parent.parent)
        self._python = sys.executable
        self._lock = threading.RLock()
        self._children: dict[str, subprocess.Popen] = {}

    @property
    def _state_path(self) -> Path:
        return Path(self._project_dir) / ".runtime" / "processes.json"

    @contextmanager
    def _guard(self):
        with self._lock:
            path = self._state_path.with_suffix(".lock")
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a+b") as handle:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    if not handle.read(1):
                        handle.write(b"0"); handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict:
        if self._state_path.exists():
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        return {name: {"desired": True} for name in ("worker", "beat")}

    def _save(self, state: dict) -> None:
        atomic_json(self._state_path, state)

    def _pidfile(self, name: str) -> Path:
        return Path(self._project_dir) / "logs" / f"celery_{name}.pid"

    def _process(self, name: str, entry: dict) -> Optional[psutil.Process]:
        """身份包含创建时间，不能仅凭可能复用的 PID 杀进程。"""
        child = self._children.get(name)
        if child is not None:
            child.poll()
        pid = entry.get("pid")
        recorded = bool(pid and entry.get("created_at"))
        try:
            file_pid = int(self._pidfile(name).read_text().strip())
        except (OSError, ValueError):
            file_pid = None
        if pid and not psutil.pid_exists(int(pid)) and file_pid and file_pid != pid:
            # 上一代 Web 可在 spawn 后、写状态前退出；Celery 的独占 PID 文件
            # 是待核验候选，仍须匹配工作目录和命令身份。
            pid, recorded = file_pid, False
        if not pid:
            pid = file_pid
        if not pid:
            return None
        try:
            process = psutil.Process(int(pid))
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return None
            if Path(process.cwd()).resolve() != Path(self._project_dir).resolve():
                return None
            if recorded:
                if abs(process.create_time() - float(entry["created_at"])) > 0.01:
                    return None
                if Path(process.exe()).resolve() != Path(entry["executable"]).resolve():
                    return None
            else:
                command = " ".join(process.cmdline()).lower()
                marker = "celeryd" if name == "worker" else "celery beat"
                if not (("celery" in command and name in command) or marker in command):
                    return None
                # spawn 后 Web 退出而状态尚未落盘时，只接管该进程自身携带的代际。
                try:
                    proof = json.loads((self._state_path.parent / f"{name}-ready.json").read_text(encoding="utf-8"))
                    if proof.get("pid") == process.pid and proof.get("checked_at", 0) >= process.create_time():
                        entry["token"] = proof.get("token")
                except (OSError, ValueError, TypeError):
                    pass
            # 清单只记录意图；代际证明必须来自已核验 PID 的真实启动环境。
            entry["configuration_signature"] = process.environ().get("MEDIA_PROCESS_CONFIGURATION")
            entry.update(pid=process.pid, created_at=process.create_time(), executable=process.exe())
            return process
        except (psutil.Error, OSError, ValueError, KeyError):
            return None

    def _worker_name(self) -> str:
        identity = sha256(str(Path(self._project_dir).resolve()).encode()).hexdigest()[:12]
        return f"douyin-{identity}@{socket.gethostname()}"

    def _ready(self, name: str, process: psutil.Process, entry: dict) -> bool:
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return False
        from app.core.config import settings
        if entry.get("configuration_signature") != configuration_signature(settings.snapshot()):
            return False
        if name == "beat":
            try:
                ready = json.loads((self._state_path.parent / "beat-ready.json").read_text(encoding="utf-8"))
                return (ready.get("pid") == process.pid and ready.get("token") == entry.get("token")
                        and ready.get("broker") == sha256(settings.effective_celery_broker.encode()).hexdigest()
                        and 0 <= time.time() - ready.get("checked_at", 0) <= 15)
            except (OSError, ValueError, TypeError):
                return False
        from app.tasks.celery_app import celery_app
        destination = self._worker_name()
        try:
            proof = json.loads((self._state_path.parent / "worker-ready.json").read_text(encoding="utf-8"))
            if (proof.get("pid") != process.pid or proof.get("token") != entry.get("token")
                    or proof.get("broker") != sha256(settings.effective_celery_broker.encode()).hexdigest()
                    or proof.get("checked_at", 0) < process.create_time()):
                return False
            with celery_app.connection_for_read(url=settings.effective_celery_broker) as connection:
                replies = celery_app.control.inspect(destination=[destination], timeout=1, connection=connection).ping()
            return bool(replies and destination in replies)
        except Exception:
            return False

    def _start_locked(self, name: str, state: dict, *, desired: bool = True) -> dict:
        from app.core.config import settings
        runtime = self._state_path.parent
        if (runtime / "deployment-maintenance").exists() and not (runtime / "deployment-verifying").exists():
            return {"success": False, "message": "项目正在发布，后台进程启动已延迟"}
        entry = state.setdefault(name, {})
        entry["desired"] = desired
        process = self._process(name, entry)
        target_signature = configuration_signature(settings.snapshot())
        if process is not None and entry.get("configuration_signature") != target_signature:
            stopped = self._stop_locked(name, state, preserve_desired=True)
            if not stopped.get("success"):
                return stopped
            process = None
        if process is None:
            pidfile = self._pidfile(name)
            if pidfile.exists():
                try:
                    stale_pid = int(pidfile.read_text().strip())
                    if psutil.pid_exists(stale_pid):
                        return {"success": False, "message": f"{name} PID 文件指向未确认身份的进程"}
                except (OSError, ValueError):
                    pass
                pidfile.unlink(missing_ok=True)
            concurrency = max(1, min(int(settings.MAX_CONCURRENT_DOWNLOADS), 20))
            command = [self._python, "-m", "celery", "-A", "app.tasks.celery_app", name,
                       f"--pidfile={pidfile}", "--loglevel=info"]
            if name == "worker":
                command += [f"--hostname={self._worker_name()}", "--pool=prefork" if os.name != "nt" else "--pool=solo",
                            f"--concurrency={concurrency}", "-Q", "celery", "--max-tasks-per-child=50"]
                entry["concurrency"] = concurrency
            else:
                command += [f"--schedule={pidfile.parent / 'celerybeat-schedule'}",
                            "--scheduler=app.core.managed_scheduler:ManagedScheduler"]
            pidfile.parent.mkdir(parents=True, exist_ok=True)
            entry["token"] = uuid4().hex
            entry["configuration_signature"] = target_signature
            with open(pidfile.with_suffix(".log"), "a", encoding="utf-8") as log:
                child = subprocess.Popen(command, cwd=self._project_dir, stdout=log,
                                         stderr=subprocess.STDOUT, start_new_session=True,
                                         env={**os.environ, "MEDIA_PROCESS_TOKEN": entry["token"],
                                              "MEDIA_PROCESS_CONFIGURATION": target_signature},
                                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self._children[name] = child
            process = psutil.Process(child.pid)
            entry.update(pid=child.pid, created_at=process.create_time(), executable=process.exe(), ready=False)
            self._save(state)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if self._ready(name, process, entry):
                    entry.update(ready=True, failures=0, retry_after=0, unready_since=None)
                    self._save(state)
                    return {"success": True, "message": f"{name} 已就绪", "pid": process.pid,
                            "concurrency": entry.get("concurrency")}
                if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                    break
            except psutil.Error:
                break
            time.sleep(0.25)
        failures = int(entry.get("failures", 0)) + 1
        entry.update(ready=False, failures=failures, retry_after=time.time() + min(60, 2 ** min(failures, 6)))
        entry["unready_since"] = entry.get("unready_since") or time.time()
        self._save(state)
        return {"success": False, "message": f"{name} 尚未就绪，系统将自动重试", "pid": process.pid}

    def start_worker(self, concurrency: Optional[int] = None) -> dict:
        from app.core.config import settings
        if concurrency is not None and int(concurrency) != int(settings.MAX_CONCURRENT_DOWNLOADS):
            return {"success": False, "message": "请先在网页设置保存并发数，再启动 Worker"}
        with self._guard():
            state = self._read()
            return self._start_locked("worker", state)

    def start_beat(self) -> dict:
        with self._guard():
            return self._start_locked("beat", self._read())

    def ensure_configuration(self, name: str, signature: str) -> dict:
        """恢复只接受当前持久配置；旧代际立即替换，同代际未就绪最多等待 90 秒。"""
        if name not in {"worker", "beat"}:
            raise ValueError("未知托管进程")
        with self._guard():
            if signature != configuration_signature():
                return {"success": False, "message": "恢复目标与当前持久配置不一致"}
            state = self._read()
            entry = state.setdefault(name, {})
            process = self._process(name, entry)
            if (process is not None and entry.get("configuration_signature") == signature
                    and entry.get("unready_since")
                    and time.time() - entry["unready_since"] >= 90
                    and not self._ready(name, process, entry)):
                stopped = self._stop_locked(name, state, preserve_desired=True)
                if not stopped.get("success"):
                    return stopped
                entry["unready_since"] = None
            return self._start_locked(name, state)

    def _stop_locked(self, name: str, state: dict, *, preserve_desired: bool) -> dict:
        entry = state.setdefault(name, {})
        if not preserve_desired:
            entry["desired"] = False
        process = self._process(name, entry)
        self._save(state)
        if process is None and self._pidfile(name).exists():
            try:
                if psutil.pid_exists(int(self._pidfile(name).read_text().strip())):
                    return {"success": False, "message": f"{name} PID 身份无法确认，未停止该进程"}
            except (OSError, ValueError):
                pass
        if process is not None:
            try:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=30)
            except psutil.TimeoutExpired:
                return {"success": False, "message": f"{name} 正等待在途任务结束"}
            except psutil.NoSuchProcess:
                pass
            except psutil.Error:
                return {"success": False, "message": f"{name} 进程无法停止"}
        entry.update(pid=None, ready=False)
        self._save(state)
        return {"success": True, "message": f"{name} 已停止"}

    def stop_worker(self, *, preserve_desired: bool = False) -> dict:
        with self._guard():
            return self._stop_locked("worker", self._read(), preserve_desired=preserve_desired)

    def stop_beat(self, *, preserve_desired: bool = False) -> dict:
        with self._guard():
            return self._stop_locked("beat", self._read(), preserve_desired=preserve_desired)

    def restart_worker(self, concurrency: Optional[int] = None) -> dict:
        from app.core.config import settings
        if concurrency is not None and int(concurrency) != int(settings.MAX_CONCURRENT_DOWNLOADS):
            return {"success": False, "message": "请先在网页设置保存并发数，再重启 Worker"}
        result = self.stop_worker(preserve_desired=True)
        return self.start_worker(concurrency) if result.get("success") else result

    def get_status(self) -> dict:
        with self._guard():
            state = self._read()
            result = {}
            for name in ("worker", "beat"):
                entry = state.setdefault(name, {"desired": True})
                process = self._process(name, entry)
                ready = False
                if process is not None:
                    try:
                        ready = self._ready(name, process, entry)
                    except psutil.Error:
                        pass
                entry["ready"] = ready
                result[name] = {"running": process is not None, "pid": process.pid if process else None,
                                "desired": bool(entry.get("desired", True)), "ready": ready,
                                "configuration_signature": entry.get("configuration_signature"),
                                "concurrency": entry.get("concurrency", 3)}
            self._save(state)
            return result

    def reconcile(self) -> None:
        from app.core.queue_configuration import pending_path, restart_path, change_status
        if pending_path().exists() or restart_path().exists() or change_status().get("state") == "switching":
            return
        runtime = Path(self._project_dir) / ".runtime"
        if (runtime / "deployment-maintenance").exists() and not (runtime / "deployment-verifying").exists():
            return
        with self._guard():
            # 等待进程锁期间可能已经开始配置切换，必须在持锁后再次核验。
            if (pending_path().exists() or restart_path().exists()
                    or change_status().get("state") == "switching"
                    or ((runtime / "deployment-maintenance").exists()
                        and not (runtime / "deployment-verifying").exists())):
                return
            state = self._read()
            for name in ("worker", "beat"):
                entry = state.setdefault(name, {"desired": True})
                process = self._process(name, entry)
                if process is not None:
                    try:
                        entry["ready"] = self._ready(name, process, entry)
                    except psutil.Error:
                        entry["ready"] = False
                    if entry["ready"]:
                        entry["unready_since"] = None
                    else:
                        entry["unready_since"] = entry.get("unready_since") or time.time()
                        if time.time() - entry["unready_since"] >= 90 and entry.get("desired", True):
                            stopped = self._stop_locked(name, state, preserve_desired=True)
                            if not stopped.get("success"):
                                entry["retry_after"] = time.time() + 60
                                continue
                            process = None
                            entry["unready_since"] = None
                if entry.get("desired", True) and (process is None or not entry.get("ready")) and time.time() >= entry.get("retry_after", 0):
                    self._start_locked(name, state)
            self._save(state)

    @property
    def worker_concurrency(self) -> int:
        return int(self.get_status()["worker"]["concurrency"])

    def shutdown_all(self):
        beat = self.stop_beat(preserve_desired=True)
        if not beat.get("success"):
            return beat
        return self.stop_worker(preserve_desired=True)


process_manager = ProcessManager()


if __name__ == "__main__":
    if "--shutdown" in sys.argv:
        outcome = process_manager.shutdown_all()
        raise SystemExit(0 if outcome.get("success") else 1)
