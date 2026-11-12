"""按缓存资源串行发布；不同资源可并行，锁同时覆盖线程和服务进程。"""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import threading
import time
from weakref import WeakValueDictionary


_guard = threading.Lock()
_locks = WeakValueDictionary()


@contextmanager
def cache_resource_lock(directory: Path, resource: str):
    directory = directory.resolve()
    key = str(directory / resource)
    with _guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
    with lock:
        lock_directory = directory / ".locks"
        lock_directory.mkdir(parents=True, exist_ok=True)
        path = lock_directory / f"{resource}.lock"
        if path.is_symlink():
            raise ValueError("缓存锁不能使用符号链接")
        with path.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt

                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"0")
                    handle.flush()
                while True:
                    handle.seek(0)
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                            raise
                        time.sleep(0.05)
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
