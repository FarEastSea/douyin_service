"""整个订阅分片共享的单调时间预算，不为重试或重定向重新计时。"""
import time


class DouyinScanDeadlineExceeded(RuntimeError):
    pass


def remaining_timeout(deadline: float | None, timeout: float, *, reserve: float = 0) -> float:
    if deadline is None:
        return float(timeout)
    remaining = deadline - time.monotonic() - reserve
    if remaining <= 1:
        raise DouyinScanDeadlineExceeded("本轮时间预算耗尽，保留作者断点等待续检")
    return min(float(timeout), remaining)


def budget_sleep(seconds: float, deadline: float | None) -> None:
    if seconds > 0:
        remaining_timeout(deadline, seconds + 1)
        if deadline is not None and seconds >= deadline - time.monotonic() - 1:
            raise DouyinScanDeadlineExceeded("等待请求间隔将超出本轮时间预算，等待续检")
        time.sleep(seconds)
