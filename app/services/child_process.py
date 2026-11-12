"""下载执行拥有独立进程容器；取消只回收经过身份核验的本次执行。"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4

import psutil


_CONTAINER_ENV = "DOUYIN_DOWNLOAD_CONTAINER"
_processes: dict[int, subprocess.Popen] = {}
_processes_lock = threading.Lock()


class DownloadProcessError(RuntimeError):
    pass


class _WindowsJob:
    """持有 Job 句柄；Worker 退出时 Windows 自动终止全部成员。"""

    def __init__(self, name: str, *, create: bool):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "OpenJobObjectW": ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
        }
        for method, (arguments, result) in signatures.items():
            getattr(self.api, method).argtypes = arguments
            getattr(self.api, method).restype = result
        self.handle = (self.api.CreateJobObjectW(None, name) if create
                       else self.api.OpenJobObjectW(0x0004 | 0x0008, False, name))
        if not self.handle:
            if not create and ctypes.get_last_error() == 2:
                return
            raise ctypes.WinError(ctypes.get_last_error())
        if create:
            class BasicLimits(ctypes.Structure):
                _fields_ = [("ProcessUserTime", ctypes.c_int64), ("JobUserTime", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class IoCounters(ctypes.Structure):
                _fields_ = [(field, ctypes.c_uint64) for field in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class ExtendedLimits(ctypes.Structure):
                _fields_ = [("Basic", BasicLimits), ("Io", IoCounters),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            limits = ExtendedLimits()
            limits.Basic.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                error = ctypes.WinError(ctypes.get_last_error())
                self.close()
                raise error

    def assign(self, process: subprocess.Popen) -> None:
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def active(self) -> int:
        if not self.handle:
            return 0
        from ctypes import wintypes
        ctypes = self.ctypes

        class Accounting(ctypes.Structure):
            _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                        ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                        ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                        ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

        value = Accounting()
        if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(value), ctypes.sizeof(value), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(value.ActiveProcesses)

    def terminate(self) -> None:
        if self.handle and not self.api.TerminateJobObject(self.handle, 1):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def members(self) -> list[psutil.Process]:
        if not self.handle:
            return []
        from ctypes import wintypes
        ctypes = self.ctypes
        capacity = max(16, self.active() + 8)
        for _ in range(5):
            class ProcessIds(ctypes.Structure):
                _fields_ = [("Assigned", wintypes.DWORD), ("Listed", wintypes.DWORD),
                            ("Ids", ctypes.c_size_t * capacity)]
            values = ProcessIds()
            if self.api.QueryInformationJobObject(self.handle, 3, ctypes.byref(values), ctypes.sizeof(values), None):
                members = []
                for pid in values.Ids[:values.Listed]:
                    try:
                        members.append(psutil.Process(pid))
                    except psutil.NoSuchProcess:
                        pass
                return members
            if ctypes.get_last_error() != 234:  # ERROR_MORE_DATA: a new child joined the Job.
                raise ctypes.WinError(ctypes.get_last_error())
            capacity = max(capacity * 2, int(values.Assigned) + 8)
        raise DownloadProcessError("下载 Job 成员仍在变化，无法确认完整收尾")

    def close(self) -> None:
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def start_download_process(command: list[str], **kwargs) -> subprocess.Popen:
    """先启动等待登记的 gate；调用方登记成功后才放行实际下载器。"""
    nonce = uuid4().hex
    supplied_environment = kwargs.pop("env", None)
    environment = dict(os.environ if supplied_environment is None else supplied_environment)
    environment[_CONTAINER_ENV] = nonce
    if kwargs.get("text") or kwargs.get("encoding"):
        environment["PYTHONIOENCODING"] = kwargs.get("encoding", "utf-8")
    job = None
    process = None
    try:
        launcher = [sys.executable, str(Path(__file__).resolve()), "--owned-child", json.dumps(command)]
        if os.name == "nt":
            job_name = "Local\\DouyinDownload-" + nonce
            job = _WindowsJob(job_name, create=True)
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            process = subprocess.Popen(launcher, env=environment, stdin=subprocess.PIPE, **kwargs)
            job.assign(process)
        else:
            process = subprocess.Popen(launcher, env=environment, stdin=subprocess.PIPE, start_new_session=True, **kwargs)
        identity = {"version": 1, "pid": process.pid,
                    "created_at": psutil.Process(process.pid).create_time(), "nonce": nonce,
                    "kind": "windows_job" if job else "posix_group",
                    "container": job_name if job else process.pid}
        process._download_identity = identity
        process._download_job = job
        with _processes_lock:
            _processes[process.pid] = process
        return process
    except BaseException:
        if job:
            job.terminate()
            job.close()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            with _processes_lock:
                _processes.pop(process.pid, None)
        raise


def _receipt_directory() -> Path:
    return Path(__file__).resolve().parents[2] / ".runtime" / "download-processes"


def download_process_identity(process_id: int, token: str, *, platform=None, task_id=None) -> dict:
    with _processes_lock:
        process = _processes.get(process_id)
    if process is None or not token:
        raise DownloadProcessError("下载进程没有可验证的执行归属")
    identity = {**process._download_identity, "token": token}
    if platform is not None and task_id is not None:
        from app.services.filesystem_durability import write_journal
        identity.update(platform=platform, task_id=int(task_id))
        previous = getattr(process, "_download_bound_identity", None)
        if previous is not None and previous != identity:
            raise DownloadProcessError("下载进程已登记为其他执行")
        receipt = _receipt_directory() / (identity["nonce"] + ".json")
        write_journal(receipt, identity)
        process._download_bound_identity = identity
    return identity


def release_download_process(process: subprocess.Popen) -> None:
    """所有登记和执行权检查通过后放行；登记异常时 gate 不会写媒体文件。"""
    if process.stdin and not process.stdin.closed:
        process.stdin.write("1" if process.text_mode else b"1")
        process.stdin.flush()
        process.stdin.close()


def find_download_identities(platform: str, task_id: int, token: str | None = None) -> list[dict]:
    identities = []
    for receipt in _receipt_directory().glob("*.json"):
        try:
            identity = json.loads(receipt.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # A completing download may retire its receipt after glob listed it.
            continue
        except (ValueError, OSError) as exc:
            raise DownloadProcessError("下载进程归属凭证暂不可读取") from exc
        if not isinstance(identity, dict):
            raise DownloadProcessError("下载进程归属凭证结构无效")
        if identity.get("platform") == platform and identity.get("task_id") == task_id:
            if token is None or identity.get("token") == token:
                if receipt.stem != identity.get("nonce"):
                    raise DownloadProcessError("下载进程凭证名称与归属不一致")
                identities.append(identity)
    return identities


def retire_download_identity(identity: dict) -> None:
    if not re.fullmatch(r"[0-9a-f]{32}", str(identity.get("nonce", ""))):
        raise DownloadProcessError("下载进程凭证名称无效")
    receipt = _receipt_directory() / (identity["nonce"] + ".json")
    try:
        if json.loads(receipt.read_text(encoding="utf-8")) != identity:
            raise DownloadProcessError("下载进程归属凭证已变化")
        receipt.unlink()
        from app.services.filesystem_durability import sync_directory
        sync_directory(receipt.parent)
    except FileNotFoundError:
        pass


def _posix_members(identity: dict) -> list[psutil.Process]:
    group = int(identity["container"])
    members = []
    for process in psutil.process_iter(["pid", "create_time", "status"]):
        try:
            if os.getpgid(process.pid) != group or process.info["status"] == psutil.STATUS_ZOMBIE:
                continue
            if (process.environ().get(_CONTAINER_ENV) != identity["nonce"]
                    or process.info["create_time"] + .01 < float(identity["created_at"])):
                raise DownloadProcessError("下载进程组身份不匹配，已拒绝发送信号")
            members.append(process)
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue
        except psutil.AccessDenied as exc:
            raise DownloadProcessError("无法校验下载进程组归属") from exc
    return members


def terminate_download_identity(identity: dict | None, token: str) -> None:
    """可由 Web 或 Worker 调用；父进程退出后也核验并清理整个容器。"""
    if not identity:
        return
    if (identity.get("version") != 1 or identity.get("token") != token
            or not isinstance(token, str) or not token
            or not re.fullmatch(r"[0-9a-f]{32}", str(identity.get("nonce", "")))
            or type(identity.get("pid")) is not int or identity["pid"] < 1
            or not isinstance(identity.get("created_at"), (int, float))
            or not math.isfinite(identity["created_at"]) or identity["created_at"] <= 0):
        raise DownloadProcessError("下载进程身份记录无效，已拒绝发送信号")
    if identity.get("kind") == "windows_job" and os.name == "nt":
        if identity.get("container") != "Local\\DouyinDownload-" + identity["nonce"]:
            raise DownloadProcessError("下载 Job 身份不匹配")
        job = _WindowsJob(identity["container"], create=False)
        try:
            if not job.active():
                return
            try:
                if abs(psutil.Process(identity["pid"]).create_time() - identity["created_at"]) > .01:
                    raise DownloadProcessError("下载父进程 PID 已被复用")
            except psutil.NoSuchProcess:
                pass
            members = job.members()
            if job.active():
                job.terminate()
            _, alive = psutil.wait_procs(members, timeout=5)
            deadline = time.monotonic() + 5
            while job.active() and time.monotonic() < deadline:
                time.sleep(.05)
            if alive or job.active():
                raise DownloadProcessError("下载子进程尚未完全结束")
        finally:
            job.close()
    elif identity.get("kind") == "posix_group" and os.name != "nt":
        if identity.get("container") != identity["pid"]:
            raise DownloadProcessError("下载进程组身份不匹配")
        members = _posix_members(identity)
        if not members:
            return
        try:
            if abs(psutil.Process(identity["pid"]).create_time() - identity["created_at"]) > .01:
                raise DownloadProcessError("下载父进程 PID 已被复用")
        except psutil.NoSuchProcess:
            pass
        try:
            os.killpg(identity["container"], signal.SIGTERM)
        except ProcessLookupError:
            return
        psutil.wait_procs(members, timeout=3)
        if _posix_members(identity):
            try:
                os.killpg(identity["container"], signal.SIGKILL)
            except ProcessLookupError:
                pass
            _, alive = psutil.wait_procs(_posix_members(identity), timeout=3)
            if alive:
                raise DownloadProcessError("下载子进程尚未完全结束")
    else:
        raise DownloadProcessError("下载进程容器类型不匹配")


def iter_download_output(process: subprocess.Popen, check_running=None):
    """主执行线程持续检查执行权；安静或阻塞输出的子进程也能及时停止。"""
    lines = queue.Queue(maxsize=256)
    stopped = threading.Event()
    end = object()

    def put(value):
        while not stopped.is_set():
            try:
                lines.put(value, timeout=.2)
                return
            except queue.Full:
                pass

    def read():
        try:
            if process.stdout:
                for line in process.stdout:
                    put(line)
                    if stopped.is_set():
                        return
        except Exception as exc:
            put(exc)
        finally:
            put(end)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        exhausted = False
        container_stopped = False
        while not exhausted or process.poll() is None:
            if check_running:
                check_running()
            if process.poll() is not None and not container_stopped:
                # 后代可能仍持有输出管道；先结束容器才能收到 EOF 并结束迭代。
                _stop_owned_container(process)
                container_stopped = True
            if exhausted:
                stopped.wait(.25)
                continue
            try:
                item = lines.get(timeout=.25)
            except queue.Empty:
                continue
            if item is end:
                exhausted = True
            elif isinstance(item, Exception):
                raise item
            else:
                yield item
        if check_running:
            check_running()
    except BaseException:
        stopped.set()
        close_download_process(process)
        raise
    finally:
        stopped.set()
        if process.poll() is not None:
            reader.join(timeout=1)


def _stop_owned_container(process: subprocess.Popen) -> None:
    identity = getattr(process, "_download_identity", None)
    if identity is None:
        raise DownloadProcessError("拒绝清理没有容器归属的下载进程")
    terminate_download_identity({**identity, "token": "owner"}, "owner")


def close_download_process(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    try:
        _stop_owned_container(process)
        process.wait(timeout=5)
        bound = getattr(process, "_download_bound_identity", None)
        if bound:
            retire_download_identity(bound)
    finally:
        job = getattr(process, "_download_job", None)
        if job:
            job.close()
        with _processes_lock:
            if _processes.get(process.pid) is process:
                _processes.pop(process.pid, None)
        # 容器收尾后才关闭输出，避免存活子进程持有管道时阻塞 close。
        if process.stdout and process.poll() is not None:
            process.stdout.close()
        if process.stdin and not process.stdin.closed and process.poll() is not None:
            process.stdin.close()


def _run_owned_child() -> int:
    if sys.stdin.buffer.read(1) != b"1":
        return 125
    command = json.loads(sys.argv[2])
    if os.name != "nt":
        os.execvpe(command[0], command, os.environ)
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    return process.wait()


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--owned-child":
    raise SystemExit(_run_owned_child())
