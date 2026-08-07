"""进程内运行故障登记，供初始化/维护页面统一展示。"""

from __future__ import annotations

from threading import RLock
from typing import Any, Dict, List

from app.core.error_handling import sanitize_text


_errors: Dict[str, Dict[str, str]] = {}
_lock = RLock()


def report_runtime_error(key: str, label: str, group: str, message: Any) -> None:
    normalized = f"{type(message).__name__}: {message}" if isinstance(message, Exception) else str(message)
    with _lock:
        _errors[key] = {
            "key": key,
            "label": label,
            "group": group,
            "message": sanitize_text(normalized, limit=300),
        }


def clear_runtime_error(key: str) -> None:
    with _lock:
        _errors.pop(key, None)


def clear_runtime_errors(prefix: str) -> None:
    with _lock:
        for key in [item for item in _errors if item.startswith(prefix)]:
            _errors.pop(key, None)


def get_runtime_errors() -> List[Dict[str, str]]:
    with _lock:
        return [dict(item) for item in _errors.values()]
