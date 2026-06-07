"""
Celery 进程管理器

通过 subprocess 管理 Celery Worker 和 Beat 进程，
不再依赖宝塔面板的计划任务或 start.sh。
FastAPI 启动时自动拉起，网页端可控制启停。
"""

import sys
import os
import signal
import subprocess
import threading
import time
from typing import Optional
from pathlib import Path


def _kill_tree(pid: int, sig: int):
    """向进程组发送信号（Linux），或直接 kill（Windows fallback）"""
    try:
        os.killpg(os.getpgid(pid), sig)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def _force_kill(pid: int):
    """强制杀死进程（树）"""
    sig = getattr(signal, "SIGKILL", signal.SIGTERM)
    _kill_tree(pid, sig)


class ProcessManager:
    """管理 Celery Worker 和 Beat 子进程"""

    def __init__(self):
        self._worker_proc: Optional[subprocess.Popen] = None
        self._beat_proc: Optional[subprocess.Popen] = None
        self._worker_concurrency: int = 3
        self._lock = threading.Lock()
        self._project_dir = str(Path(__file__).resolve().parent.parent.parent)
        # 使用当前 Python 解释器（确保在 venv 内）
        self._python = sys.executable

    # ---------- Worker ----------

    def start_worker(self, concurrency: Optional[int] = None) -> dict:
        with self._lock:
            if self._worker_proc and self._worker_proc.poll() is None:
                return {"success": False, "message": "Worker 已在运行中"}

            if concurrency is not None:
                self._worker_concurrency = max(1, min(concurrency, 20))

            cmd = [
                self._python, "-m", "celery",
                "-A", "app.tasks.celery_app", "worker",
                "--pool=prefork",
                f"--concurrency={self._worker_concurrency}",
                "--loglevel=info",
                "-Q", "celery",
                "--max-tasks-per-child=50",
                "--without-heartbeat",
            ]

            log_dir = os.path.join(self._project_dir, "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_file = open(os.path.join(log_dir, "celery_worker.log"), "a")

            self._worker_proc = subprocess.Popen(
                cmd,
                cwd=self._project_dir,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # 独立进程组，便于批量 kill
            )

            return {
                "success": True,
                "message": f"Worker 已启动 (PID={self._worker_proc.pid}, concurrency={self._worker_concurrency})",
                "pid": self._worker_proc.pid,
                "concurrency": self._worker_concurrency,
            }

    def stop_worker(self) -> dict:
        with self._lock:
            if not self._worker_proc or self._worker_proc.poll() is not None:
                self._worker_proc = None
                return {"success": True, "message": "Worker 未在运行"}

            pid = self._worker_proc.pid
            # 向整个进程组发送 SIGTERM（prefork 主进程 + 子进程）
            _kill_tree(pid, signal.SIGTERM)

            # 等待最多 10 秒
            for _ in range(20):
                if self._worker_proc.poll() is not None:
                    break
                time.sleep(0.5)

            # 还没死就强杀
            if self._worker_proc.poll() is None:
                _force_kill(pid)
                try:
                    self._worker_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

            self._worker_proc = None
            return {"success": True, "message": f"Worker 已停止 (PID={pid})"}

    def restart_worker(self, concurrency: Optional[int] = None) -> dict:
        self.stop_worker()
        return self.start_worker(concurrency)

    # ---------- Beat ----------

    def start_beat(self) -> dict:
        with self._lock:
            if self._beat_proc and self._beat_proc.poll() is None:
                return {"success": False, "message": "Beat 已在运行中"}

            cmd = [
                self._python, "-m", "celery",
                "-A", "app.tasks.celery_app", "beat",
                "--loglevel=info",
            ]

            log_dir = os.path.join(self._project_dir, "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_file = open(os.path.join(log_dir, "celery_beat.log"), "a")

            self._beat_proc = subprocess.Popen(
                cmd,
                cwd=self._project_dir,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

            return {
                "success": True,
                "message": f"Beat 已启动 (PID={self._beat_proc.pid})",
                "pid": self._beat_proc.pid,
            }

    def stop_beat(self) -> dict:
        with self._lock:
            if not self._beat_proc or self._beat_proc.poll() is not None:
                self._beat_proc = None
                return {"success": True, "message": "Beat 未在运行"}

            pid = self._beat_proc.pid
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass

            for _ in range(10):
                if self._beat_proc.poll() is not None:
                    break
                time.sleep(0.5)

            if self._beat_proc.poll() is None:
                _force_kill(pid)
                try:
                    self._beat_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

            self._beat_proc = None
            return {"success": True, "message": f"Beat 已停止 (PID={pid})"}

    # ---------- 状态 ----------

    def get_status(self) -> dict:
        wp = self._worker_proc
        bp = self._beat_proc
        worker_alive = wp is not None and wp.poll() is None
        beat_alive = bp is not None and bp.poll() is None
        return {
            "worker": {
                "running": worker_alive,
                "pid": wp.pid if wp and worker_alive else None,
                "concurrency": self._worker_concurrency,
            },
            "beat": {
                "running": beat_alive,
                "pid": bp.pid if bp and beat_alive else None,
            },
        }

    @property
    def worker_concurrency(self) -> int:
        return self._worker_concurrency

    @worker_concurrency.setter
    def worker_concurrency(self, value: int):
        self._worker_concurrency = max(1, min(value, 20))

    def shutdown_all(self):
        """关闭所有子进程（FastAPI 退出时调用）"""
        self.stop_worker()
        self.stop_beat()


# 全局单例
process_manager = ProcessManager()
