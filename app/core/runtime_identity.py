"""Web 装载代次证明及发布阶段的启动边界，不读取业务配置。"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

import psutil


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def deployment_startup_blocked() -> bool:
    runtime = PROJECT_ROOT / ".runtime"
    return ((runtime / "deployment-maintenance").exists()
            and not (runtime / "deployment-verifying").exists())


def _read_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=3,
        )
        revision = result.stdout.strip()
        return revision if re.fullmatch(r"[0-9a-f]{40,64}", revision) else None
    except (OSError, subprocess.SubprocessError):
        return None


# 必须在 main 装载时冻结。按请求重新读取磁盘 HEAD 会让旧进程冒充新代码。
LOADED_REVISION = _read_revision()


@lru_cache(maxsize=1)
def _capture_identity(pid: int) -> dict:
    # 读取已装载模块，而非 dist-info；pip 替换后者不能证明进程实际运行的版本。
    dependencies = {}
    for name in ("fastapi", "starlette", "pydantic", "sqlalchemy", "celery", "kombu",
                 "redis", "gunicorn", "uvicorn", "psutil"):
        version = getattr(sys.modules.get(name), "__version__", None)
        if isinstance(version, str):
            dependencies[name] = version
    try:
        created_at = psutil.Process(pid).create_time()
    except psutil.Error:
        created_at = None
    return {
        "revision": LOADED_REVISION,
        "pid": pid,
        "created_at": created_at,
        "instance": uuid4().hex,
        "python": ".".join(map(str, sys.version_info[:3])),
        "dependencies": dependencies,
    }


def get_runtime_identity() -> dict:
    # Gunicorn preload 后 fork 也按真实 worker 身份生成，调用方不能改写缓存。
    return deepcopy(_capture_identity(os.getpid()))
