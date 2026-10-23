"""只收回本次下载启动的进程树，不依据模糊命令行匹配终止其他服务。"""
import subprocess
import queue
import threading

import psutil


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
        while not exhausted or process.poll() is None:
            if check_running:
                check_running()
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


def close_download_process(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    try:
        if process.poll() is None:
            try:
                children = psutil.Process(process.pid).children(recursive=True)
            except psutil.NoSuchProcess:
                children = []
            for child in reversed(children):
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            _, alive = psutil.wait_procs(children, timeout=3)
            for child in alive:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
    finally:
        if process.stdout:
            process.stdout.close()
