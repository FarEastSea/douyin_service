"""媒体恢复协议共用的文件系统持久化屏障。"""
from __future__ import annotations

import os
from pathlib import Path

from app.core.config_transaction import atomic_json


def sync_directory(path: Path) -> None:
    # Windows 不提供与 POSIX directory fsync 等价的 os 接口；文件内容仍由
    # atomic_json/发布者 fsync，再通过同卷原子替换发布。生产使用 POSIX。
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def ensure_directory(path: Path) -> None:
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    path.mkdir(parents=True, exist_ok=True)
    # 新建日志目录的父目录项也必须先持久化，否则仅 fsync 日志目录仍可能
    # 在机器掉电后丢失整个目录，使数据库 marker 失去对应恢复证据。
    for created in reversed(missing):
        sync_directory(created.parent)


def write_journal(path: Path, entry: dict) -> None:
    ensure_directory(path.parent)
    atomic_json(path, entry)
    sync_directory(path.parent)
