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
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

# 仓库根目录 = 本文件的上上上级（app/core/updater.py -> 项目根）
REPO_DIR = str(Path(__file__).resolve().parent.parent.parent)

GIT_TIMEOUT = 90

_GIT_BIN: Optional[str] = None


class GitUpdateError(Exception):
    """git 操作失败时抛出，API 层据此返回可读错误。"""


def _git_bin() -> str:
    """定位 git 可执行文件。gunicorn 等托管进程的 PATH 可能较精简，需兜底常见路径。"""
    global _GIT_BIN
    if _GIT_BIN:
        return _GIT_BIN
    found = shutil.which("git")
    if not found:
        for cand in ("/usr/bin/git", "/usr/local/bin/git", "/bin/git", "/opt/git/bin/git"):
            if os.path.exists(cand):
                found = cand
                break
    _GIT_BIN = found or "git"
    return _GIT_BIN


def _git_env() -> dict:
    env = dict(os.environ)
    # 禁止任何交互式凭据/提示，缺凭据时直接失败而不是挂起
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    return env


def _git_cmd(args: list) -> list:
    # -c safe.directory=REPO_DIR：规避 gunicorn 运行用户(如 www)与仓库属主(如 root)
    # 不一致时 git 抛出的 "detected dubious ownership in repository" 报错。
    return [_git_bin(), "-c", f"safe.directory={REPO_DIR}", *args]


def _run_git_via_system(args: list, timeout: int = GIT_TIMEOUT):
    """
    回退执行方式：通过 os.system(libc system()) 运行 git。

    某些托管环境（如宝塔 gunicorn，服务运行在 asyncio.to_thread 子线程且叠加
    事件循环/gevent 对子进程的接管）下，标准 subprocess 在子线程中监视子进程会
    失败并抛出空的 SubprocessError。os.system 走 libc，不依赖 Python 的子进程
    监视机制，可稳定执行。用临时文件收集输出，timeout 命令提供硬超时。
    """
    if os.name != "posix":
        raise GitUpdateError("git 子进程执行失败，且当前系统不支持回退执行方式")

    full = _git_cmd(args)
    out_fd, out_path = tempfile.mkstemp(prefix="dy_git_out_")
    err_fd, err_path = tempfile.mkstemp(prefix="dy_git_err_")
    os.close(out_fd)
    os.close(err_fd)
    try:
        inner = " ".join(shlex.quote(a) for a in full)
        timeout_prefix = f"timeout {int(timeout)} " if shutil.which("timeout") else ""
        cmd = (
            f"cd {shlex.quote(REPO_DIR)} && "
            f"GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never "
            f"{timeout_prefix}{inner} "
            f"> {shlex.quote(out_path)} 2> {shlex.quote(err_path)}"
        )
        raw = os.system(cmd)
        if hasattr(os, "waitstatus_to_exitcode"):
            try:
                code = os.waitstatus_to_exitcode(raw)
            except ValueError:
                code = raw
        elif os.WIFEXITED(raw):
            code = os.WEXITSTATUS(raw)
        else:
            code = raw or 1
        with open(out_path, encoding="utf-8", errors="replace") as f:
            out = f.read().strip()
        with open(err_path, encoding="utf-8", errors="replace") as f:
            err = f.read().strip()
        return code, out, err
    finally:
        for p in (out_path, err_path):
            try:
                os.remove(p)
            except OSError:
                pass


def _run_git(args: list, timeout: int = GIT_TIMEOUT):
    """执行 git 命令，返回 (returncode, stdout, stderr)。"""
    full = _git_cmd(args)
    try:
        proc = subprocess.run(
            full,
            cwd=REPO_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_git_env(),
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except FileNotFoundError:
        raise GitUpdateError("未找到 git 可执行文件，请确认服务器已安装 git 且在 PATH 中")
    except subprocess.TimeoutExpired:
        raise GitUpdateError(f"git {args[0] if args else ''} 执行超时（{timeout}s），可能是网络问题")
    except (subprocess.SubprocessError, OSError):
        # 标准 subprocess 在该环境的子线程中无法创建/监视子进程（典型为空的
        # SubprocessError）。回退到 os.system 执行，规避子进程监视问题。
        return _run_git_via_system(args, timeout)


def _git_ok(args: list, timeout: int = GIT_TIMEOUT) -> str:
    code, out, err = _run_git(args, timeout)
    if code != 0:
        raise GitUpdateError(_redact(err or out or f"git {' '.join(args)} 失败"))
    return out


def _redact(text: str) -> str:
    """去掉 URL 中的 user:token@ 凭据，避免泄露。"""
    return re.sub(r"//[^/@\s]*@", "//", text or "")


def _ensure_repo():
    code, out, err = _run_git(["rev-parse", "--is-inside-work-tree"], timeout=10)
    if code != 0 or out != "true":
        detail = _redact(err or out).strip()
        raise GitUpdateError(
            f"部署目录不是有效的 git 仓库或无法访问（{REPO_DIR}）"
            + (f"：{detail}" if detail else "")
        )


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
            "repo_dir": REPO_DIR,
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
        "repo_dir": REPO_DIR,
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


def diagnose() -> dict:
    """
    诊断信息：暴露检测到的项目目录、git 环境、运行用户与关键 git 命令的原始输出，
    用于排查"检查更新出错"到底发生在哪一步、检测的是哪个目录。
    本函数保证不抛异常，任何一步失败都以字段形式返回。
    """
    result = {
        "repo_dir": REPO_DIR,
        "repo_dir_exists": os.path.isdir(REPO_DIR),
        "dot_git_exists": os.path.exists(os.path.join(REPO_DIR, ".git")),
        "git_bin": _git_bin(),
    }

    try:
        import getpass
        result["process_user"] = getpass.getuser()
    except Exception:
        result["process_user"] = ""
    try:
        result["process_uid"] = os.getuid()  # type: ignore[attr-defined]
    except Exception:
        result["process_uid"] = None

    def _step(label: str, args: list, timeout: int = 15):
        try:
            code, out, err = _run_git(args, timeout=timeout)
            result[label] = {
                "code": code,
                "out": _redact(out)[:600],
                "err": _redact(err)[:600],
            }
        except GitUpdateError as e:
            result[label] = {"error": str(e)[:400]}
        except Exception as e:
            result[label] = {"error": f"{type(e).__name__}: {str(e)[:300]}"}

    _step("git_version", ["--version"], timeout=10)
    _step("is_work_tree", ["rev-parse", "--is-inside-work-tree"], timeout=10)
    _step("remote", ["remote", "-v"], timeout=10)
    _step("status", ["status", "-sb"], timeout=15)
    return result


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
