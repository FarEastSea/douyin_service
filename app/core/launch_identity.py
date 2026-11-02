"""托管 Celery 子进程的启动标识。

令牌通过解释器 ``-X`` 选项传递，而不是显式环境变量：宝塔安全模块会拦截
www 用户携带自定义环境的 execve，导致 Popen(env=...) 在子进程 exec 前失败。
命令行对本机所有用户可见，因此只放随机令牌；配置代际由托管清单按令牌记录。
旧代进程仍可能通过环境变量携带标识，读取时保留该回退。
"""
from __future__ import annotations

import os
import sys
from collections.abc import Sequence

TOKEN_OPTION = "media_process_token"
LEGACY_TOKEN_ENVIRONMENT = "MEDIA_PROCESS_TOKEN"
LEGACY_CONFIGURATION_ENVIRONMENT = "MEDIA_PROCESS_CONFIGURATION"


def interpreter_options(token: str) -> list[str]:
    """生成插入在解释器与 ``-m`` 之间的启动参数。"""
    return ["-X", f"{TOKEN_OPTION}={token}"]


def current_token() -> str:
    """当前进程自身的启动令牌，用于写入就绪证明。"""
    value = sys._xoptions.get(TOKEN_OPTION)
    if isinstance(value, str) and value:
        return value
    return os.environ.get(LEGACY_TOKEN_ENVIRONMENT, "")


def token_from_command(command: Sequence[str]) -> str | None:
    """从目标进程命令行中读取 ``-X media_process_token=...``。"""
    prefix = f"{TOKEN_OPTION}="
    for flag, value in zip(command, command[1:]):
        if flag == "-X" and value.startswith(prefix):
            return value[len(prefix):]
    return None
