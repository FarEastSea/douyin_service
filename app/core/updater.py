"""
Git 版本更新服务

为什么这样设计：
1. 项目部署在服务器的 git 仓库目录内，更新只需 git fetch/pull
2. 把"检查更新"和"执行更新"封装为安全的同步函数，供 API 以线程方式调用
3. 全部使用参数列表调用 git，不用 shell=True，避免命令注入
4. 通过 GIT_TERMINAL_PROMPT=0 禁止 git 交互式提示，避免凭据缺失时挂起
5. 远程地址中的凭据会被脱敏，避免泄露到接口响应
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

# 仓库根目录 = 本文件的上上上级（app/core/updater.py -> 项目根）
REPO_DIR = str(Path(__file__).resolve().parent.parent.parent)

GIT_TIMEOUT = 90


class GitUpdateError(Exception):
    """git 操作失败时抛出，API 层据此返回可读错误。"""


def _git_env() -> dict:
    env = dict(os.environ)
    # 禁止任何交互式凭据/提示，缺凭据时直接失败而不是挂起
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    return env


def _run_git(args: list, timeout: int = GIT_TIMEOUT):
    """执行 git 命令，返回 (returncode, stdout, stderr)。"""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=REPO_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_git_env(),
        )
    except FileNotFoundError:
        raise GitUpdateError("服务器未安装 git，无法检查或执行更新")
    except subprocess.TimeoutExpired:
        raise GitUpdateError(f"git {args[0] if args else ''} 执行超时（{timeout}s），可能是网络问题")
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _git_ok(args: list, timeout: int = GIT_TIMEOUT) -> str:
    code, out, err = _run_git(args, timeout)
    if code != 0:
        raise GitUpdateError(_redact(err or out or f"git {' '.join(args)} 失败"))
    return out


def _redact(text: str) -> str:
    """去掉 URL 中的 user:token@ 凭据，避免泄露。"""
    return re.sub(r"//[^/@\s]*@", "//", text or "")


def _ensure_repo():
    code, out, _ = _run_git(["rev-parse", "--is-inside-work-tree"], timeout=10)
    if code != 0 or out != "true":
        raise GitUpdateError(f"部署目录不是 git 仓库：{REPO_DIR}")


def _commit_info(ref: str) -> Optional[dict]:
    # 用 \x1f 作为字段分隔符，避免提交信息里出现分隔冲突
    fmt = "%H%x1f%h%x1f%s%x1f%cI%x1f%an"
    code, out, _ = _run_git(["show", "-s", f"--format={fmt}", ref], timeout=15)
    if code != 0 or not out:
        return None
    parts = out.split("\x1f")
    return {
        "hash": parts[0] if len(parts) > 0 else "",
        "short": parts[1] if len(parts) > 1 else "",
        "subject": parts[2] if len(parts) > 2 else "",
        "date": parts[3] if len(parts) > 3 else "",
        "author": parts[4] if len(parts) > 4 else "",
    }


def _current_branch() -> str:
    return _git_ok(["rev-parse", "--abbrev-ref", "HEAD"], timeout=10)


def _upstream_ref(branch: str) -> Optional[str]:
    code, out, _ = _run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], timeout=10
    )
    if code == 0 and out:
        return out
    # 回退：若配置了 origin/<branch> 也可用于比较
    candidate = f"origin/{branch}"
    code, _, _ = _run_git(["rev-parse", "--verify", candidate], timeout=10)
    return candidate if code == 0 else None


def _has_local_changes() -> bool:
    code, out, _ = _run_git(["status", "--porcelain"], timeout=15)
    return bool(out.strip()) if code == 0 else False


def _remote_url() -> str:
    code, out, _ = _run_git(["remote", "get-url", "origin"], timeout=10)
    return _redact(out) if code == 0 else ""


def check_update(do_fetch: bool = True) -> dict:
    """
    检查是否有可用更新。

    do_fetch=True 时会联网 git fetch 拉取远程最新引用（不改动工作区）；
    do_fetch=False 时仅基于本地已有的远程引用做比较（用于快速展示当前版本）。
    """
    _ensure_repo()
    branch = _current_branch()
    remote_url = _remote_url()

    if do_fetch:
        code, out, err = _run_git(["fetch", "--prune", "origin"], timeout=GIT_TIMEOUT)
        if code != 0:
            raise GitUpdateError(f"git fetch 失败：{_redact(err or out)}")

    current = _commit_info("HEAD")
    upstream = _upstream_ref(branch)

    if not upstream:
        return {
            "success": True,
            "is_repo": True,
            "branch": branch,
            "remote_url": remote_url,
            "current": current,
            "remote": None,
            "behind": 0,
            "ahead": 0,
            "has_update": False,
            "has_local_changes": _has_local_changes(),
            "message": "未找到远程追踪分支，无法比较更新",
        }

    remote = _commit_info(upstream)

    # left=本地领先(ahead)，right=本地落后(behind)
    counts = _git_ok(["rev-list", "--left-right", "--count", f"HEAD...{upstream}"], timeout=20)
    ahead, behind = 0, 0
    try:
        a, b = counts.split()
        ahead, behind = int(a), int(b)
    except ValueError:
        pass

    return {
        "success": True,
        "is_repo": True,
        "branch": branch,
        "upstream": upstream,
        "remote_url": remote_url,
        "current": current,
        "remote": remote,
        "behind": behind,
        "ahead": ahead,
        "has_update": behind > 0,
        "has_local_changes": _has_local_changes(),
        "message": f"有 {behind} 个新提交可更新" if behind > 0 else "已是最新版本",
    }


def apply_update() -> dict:
    """
    执行更新：git pull --ff-only origin <branch>。

    使用 --ff-only 保证只做快进合并，不会产生合并提交；若本地与远程发生分叉
    或存在冲突改动，会失败并返回错误，避免污染或丢失服务端数据。
    """
    _ensure_repo()
    branch = _current_branch()
    before = _commit_info("HEAD")

    code, out, err = _run_git(["pull", "--ff-only", "origin", branch], timeout=GIT_TIMEOUT)
    combined = (out + "\n" + err).strip()
    if code != 0:
        raise GitUpdateError(f"git pull 失败：{_redact(combined)}")

    after = _commit_info("HEAD")
    updated = bool(before and after and before.get("hash") != after.get("hash"))
    return {
        "success": True,
        "updated": updated,
        "before": before,
        "after": after,
        "output": _redact(combined),
        "message": "已更新到最新版本" if updated else "本地已是最新，无需更新",
    }
