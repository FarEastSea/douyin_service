"""由项目启动脚本托管的可选服务协调器，独立于 Web worker 重载。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.request import urlopen

import psutil

from app.core.config import settings
from app.core.config_transaction import atomic_json

ROOT = Path(__file__).resolve().parents[2]


def service_status() -> dict:
    path = ROOT / ".runtime" / "xhs-service.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - state.get("checked_at", 0) > 20:
            return {**state, "ready": False, "message": "可选服务协调器未响应"}
        return state
    except (OSError, ValueError):
        return {"ready": False, "running": False, "message": "可选服务协调器尚未启动"}


def _process() -> psutil.Process | None:
    try:
        pid = int((ROOT / "logs" / "xhs-api.pid").read_text().strip())
        process = psutil.Process(pid)
        command = " ".join(process.cmdline())
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        if "xhs-api" in command and str(ROOT / ".xhs-engine") in command:
            return process
    except (OSError, ValueError, psutil.Error):
        pass
    return None


def _stop() -> None:
    process = _process()
    if process is not None:
        try:
            process.terminate()
            process.wait(timeout=15)
        except psutil.NoSuchProcess:
            pass
    (ROOT / "logs" / "xhs-api.pid").unlink(missing_ok=True)


def _publish(state: dict) -> None:
    path = ROOT / ".runtime" / "xhs-service.json"
    atomic_json(path, {**state, "checked_at": time.time()})
    if os.name != "nt" and os.getuid() == 0:
        owner = path.parent.stat()
        os.chown(path, owner.st_uid, owner.st_gid)


def reconcile_service(failures: int, retry_after: float) -> tuple[int, float]:
    enabled = settings.XHS_SERVICE_ENABLED
    process = _process()
    state_dir = ROOT / ".runtime"
    try:
        if not enabled:
            if process is not None:
                _stop()
            _publish({"desired": False, "running": False, "ready": True, "message": "小红书隔离服务已关闭"})
            return 0, 0.0
        if (state_dir / "deployment-maintenance").exists() and not (state_dir / "deployment-verifying").exists():
            _publish({"desired": True, "running": process is not None, "ready": False, "message": "正在发布，等待恢复"})
            return failures, retry_after
        if process is None and time.time() < retry_after:
            # 等待期间不能再次延后重试时间，否则服务永远不会重启。
            _publish({"desired": True, "running": False, "ready": False, "message": "隔离服务正在等待自动重试"})
            return failures, retry_after
        executable = ROOT / ".xhs-engine" / "current" / "source" / ".venv" / "bin" / "xhs-api"
        if not executable.is_file():
            raise RuntimeError("隔离引擎尚未安装；下次项目部署将自动预置锁定版本")
        if process is None:
            environment = {**os.environ, "VENV_DIR": str(Path(sys.executable).parent.parent)}
            completed = subprocess.run(["bash", str(ROOT / "start.sh"), "--xhs-start-once"],
                                       cwd=ROOT, env=environment, timeout=45, check=False)
            if completed.returncode:
                raise RuntimeError("隔离服务启动失败，将自动重试")
            process = _process()
        if process is None:
            raise RuntimeError("隔离服务进程未能接管，将自动重试")
        with urlopen("http://127.0.0.1:5556/health", timeout=2) as response:
            if response.status != 200:
                raise RuntimeError("隔离服务尚未就绪")
        _publish({"desired": True, "running": True, "ready": True, "pid": process.pid,
                  "message": "小红书隔离服务已就绪"})
        return 0, 0.0
    except Exception as exc:
        failures += 1
        if process is not None and failures >= 3:
            try:
                _stop()
                process = None
            except (OSError, psutil.Error):
                pass
        _publish({"desired": enabled, "running": process is not None, "ready": False,
                  "message": str(exc) if isinstance(exc, RuntimeError) else "隔离服务暂不可用，将自动重试"})
        return failures, time.time() + min(60, 2 ** min(failures, 6))


def run() -> None:
    import fcntl
    state_dir = ROOT / ".runtime"
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "optional-services.lock", "a+b") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        stopping = False

        def shutdown(*_args):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        (ROOT / "logs" / "optional-services.pid").write_text(str(os.getpid()), encoding="utf-8")
        failures = 0
        retry_after = 0.0
        try:
            while not stopping:
                failures, retry_after = reconcile_service(failures, retry_after)
                for _ in range(10):
                    if stopping:
                        break
                    time.sleep(0.5)
        finally:
            _stop()
            (ROOT / "logs" / "optional-services.pid").unlink(missing_ok=True)


if __name__ == "__main__":
    run()
