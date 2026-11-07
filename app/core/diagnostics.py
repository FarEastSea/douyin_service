"""进程内运行故障登记，供初始化/维护页面统一展示。"""

from __future__ import annotations

from threading import RLock
from typing import Any, Dict, List
import json
import logging
from pathlib import Path
import time

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


def mark_startup_recovered(path: Path, runtime: dict) -> None:
    """保留最近一次启动失败，并单独记录成功恢复的时间与运行版本。"""
    try:
        diagnostic = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(diagnostic, dict):
            raise ValueError("Invalid startup diagnostic")
        if diagnostic.get("state") == "recovered":
            return
        from app.core.config_transaction import atomic_json
        atomic_json(path, {**diagnostic, "state": "recovered",
                           "recovered_at": time.time(), "recovered_runtime": runtime})
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        # 诊断记录损坏或不可写不能把已恢复的业务运行再次变成启动故障。
        logging.getLogger(__name__).warning("启动已恢复，历史启动诊断暂无法更新")
