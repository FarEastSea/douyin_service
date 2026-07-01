"""
Git 版本更新服务（纯 Python，不依赖 shell/exec）

为什么这样设计：
1. 项目常部署在宝塔面板，其"系统加固/进程防护"会拦截 Web 进程(www)执行任何
   shell/系统命令（返回 "Tips from BT security"）。因此不能用 subprocess/os.system
   调用 git，必须全程纯 Python 完成。
2. 检查更新：直接读取本地 .git 文件拿到当前分支与提交，再调用 GitHub API 与远程
   对比，得到落后提交数与最新提交信息。只依赖 requests（项目已有）。
3. 执行更新：使用纯 Python 的 dulwich 通过 HTTPS 拉取（公共仓库免认证），正确更新
   .git 对象与工作区，不破坏仓库，之后仍可正常用命令行 git。
4. 远程地址中的凭据一律脱敏，不写入响应。
"""

import os
import re
import datetime
from pathlib import Path
from typing import Optional

import requests

# 仓库根目录 = 本文件的上上上级（app/core/updater.py -> 项目根）
REPO_DIR = str(Path(__file__).resolve().parent.parent.parent)

HTTP_TIMEOUT = 20
FETCH_TIMEOUT = 120
_GH_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "douyin-service-updater",
}


class GitUpdateError(Exception):
    """更新相关操作失败时抛出，API 层据此返回可读错误。"""


def _redact(text: str) -> str:
    """去掉 URL 中的 user:token@ 凭据，避免泄露。"""
    return re.sub(r"//[^/@\s]*@", "//", text or "")


# ---------- 本地 .git 解析（不 exec） ----------

def _git_dir() -> str:
    p = os.path.join(REPO_DIR, ".git")
    if os.path.isdir(p):
        return p
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                line = f.read().strip()
            if line.startswith("gitdir:"):
                gd = line.split(":", 1)[1].strip()
                return gd if os.path.isabs(gd) else os.path.normpath(os.path.join(REPO_DIR, gd))
        except Exception:
            pass
    return p


def _ensure_repo():
    if not os.path.exists(_git_dir()):
        raise GitUpdateError(f"部署目录不是 git 仓库（未找到 .git）：{REPO_DIR}")


def _read_head_and_sha():
    """返回 (branch, sha)。detached HEAD 时 branch 为 None。全程读文件，不 exec。"""
    git_dir = _git_dir()
    head_path = os.path.join(git_dir, "HEAD")
    try:
        with open(head_path, encoding="utf-8", errors="replace") as f:
            head = f.read().strip()
    except FileNotFoundError:
        raise GitUpdateError(f"无法读取 {head_path}")

    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()          # refs/heads/main
        branch = ref.split("/")[-1]
        sha = _read_ref(git_dir, ref)
        return branch, sha
    # detached HEAD：HEAD 内容即为 sha
    return None, head or None


def _read_ref(git_dir: str, ref: str) -> Optional[str]:
    ref_path = os.path.join(git_dir, *ref.split("/"))
    if os.path.exists(ref_path):
        try:
            with open(ref_path, encoding="utf-8", errors="replace") as f:
                return f.read().strip()
        except Exception:
            return None
    # packed-refs 兜底
    packed = os.path.join(git_dir, "packed-refs")
    if os.path.exists(packed):
        try:
            with open(packed, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("^"):
                        continue
                    parts = line.split(" ", 1)
                    if len(parts) == 2 and parts[1] == ref:
                        return parts[0]
        except Exception:
            return None
    return None


def _read_remote_url() -> str:
    """从 .git/config 读取 origin 的 url（不 exec）。"""
    cfg = os.path.join(_git_dir(), "config")
    try:
        with open(cfg, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:
        return ""
    m = re.search(r'\[remote\s+"origin"\][^\[]*?url\s*=\s*(\S+)', text, re.S)
    return m.group(1).strip() if m else ""


def _parse_github(url: str):
    """从 https/ssh 的 GitHub 地址解析 (owner, repo)。非 GitHub 返回 None。"""
    if not url or "github.com" not in url:
        return None
    m = re.search(r"github\.com[/:]([^/]+)/([^/\s]+?)(?:\.git)?/?$", url)
    return (m.group(1), m.group(2)) if m else None


def _to_https(url: str) -> str:
    """把 git@github.com:owner/repo(.git) 转成 https 形式（公共仓库免认证拉取）。"""
    gh = _parse_github(url)
    if gh:
        return f"https://github.com/{gh[0]}/{gh[1]}.git"
    return url


# ---------- 提交信息组织 ----------

def _gh_commit_to_info(node: dict) -> dict:
    node = node or {}
    commit = node.get("commit", {}) or {}
    author = commit.get("author", {}) or {}
    sha = node.get("sha", "") or ""
    msg = commit.get("message", "") or ""
    return {
        "hash": sha,
        "short": sha[:7],
        "subject": msg.splitlines()[0] if msg else "",
        "date": author.get("date", ""),
        "author": author.get("name", ""),
    }


def _local_commit_detail(sha: Optional[str]) -> dict:
    """尽力读取本地提交详情；无 dulwich 时仅返回 sha。"""
    info = {"hash": sha or "", "short": (sha or "")[:7], "subject": "", "date": "", "author": ""}
    if not sha:
        return info
    try:
        from dulwich.repo import Repo
        r = Repo(REPO_DIR)
        try:
            c = r[sha.encode()]
            if c.message:
                info["subject"] = c.message.decode("utf-8", "replace").splitlines()[0]
            if c.author:
                info["author"] = c.author.decode("utf-8", "replace").split("<")[0].strip()
            info["date"] = datetime.datetime.fromtimestamp(c.commit_time).isoformat()
        finally:
            r.close()
    except Exception:
        pass
    return info


# ---------- 对外主函数 ----------

def check_update(do_fetch: bool = True) -> dict:
    """
    检查是否有可用更新。

    do_fetch=False：仅返回本地当前版本信息（不联网）。
    do_fetch=True ：调用 GitHub API 与远程对比，得到落后提交数与远程最新提交。
    """
    _ensure_repo()
    branch, local_sha = _read_head_and_sha()
    remote_url = _read_remote_url()
    base = {
        "success": True,
        "is_repo": True,
        "repo_dir": REPO_DIR,
        "branch": branch,
        "remote_url": _redact(remote_url),
    }

    if not branch or not local_sha:
        base.update({
            "current": _local_commit_detail(local_sha),
            "remote": None, "behind": 0, "ahead": 0,
            "has_update": False, "has_local_changes": False,
            "message": "当前处于游离 HEAD 或无法确定分支，无法比较更新",
        })
        return base

    if not do_fetch:
        base.update({
            "current": _local_commit_detail(local_sha),
            "remote": None, "behind": 0, "ahead": 0,
            "has_update": False, "has_local_changes": False,
            "message": "点击「检查更新」与远程仓库对比",
        })
        return base

    gh = _parse_github(remote_url)
    if not gh:
        raise GitUpdateError(
            "目前仅支持 GitHub 远程仓库的在线检查。当前远程：" + (_redact(remote_url) or "未配置")
        )

    owner, repo = gh
    api = f"https://api.github.com/repos/{owner}/{repo}/compare/{local_sha}...{branch}"
    try:
        resp = requests.get(api, headers=_GH_HEADERS, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise GitUpdateError(f"连接 GitHub 失败：{str(e)[:200]}")

    if resp.status_code == 404:
        raise GitUpdateError(
            "GitHub 对比失败(404)：本地提交可能未推送到远程，或仓库/分支名有误。"
        )
    if resp.status_code == 403:
        raise GitUpdateError("GitHub API 访问受限(403)，可能触发匿名访问频率限制，请稍后再试。")
    if resp.status_code != 200:
        raise GitUpdateError(f"GitHub API 请求失败（HTTP {resp.status_code}）：{resp.text[:200]}")

    data = resp.json()
    ahead_by = int(data.get("ahead_by", 0) or 0)     # 远程比本地多出的提交数 = 可更新数
    behind_by = int(data.get("behind_by", 0) or 0)   # 本地比远程多出的提交数 = 分叉/未推送
    commits = data.get("commits") or []
    base_commit = data.get("base_commit")

    current = _gh_commit_to_info(base_commit) if base_commit else _local_commit_detail(local_sha)
    remote = _gh_commit_to_info(commits[-1]) if commits else current
    diverged = ahead_by > 0 and behind_by > 0

    base.update({
        "current": current,
        "remote": remote,
        "behind": ahead_by,
        "ahead": behind_by,
        "has_update": ahead_by > 0,
        "has_local_changes": behind_by > 0,
        "diverged": diverged,
        "message": (f"有 {ahead_by} 个新提交可更新" if ahead_by > 0 else "已是最新版本"),
    })
    return base


def apply_update() -> dict:
    """
    通过纯 Python 的 dulwich 从远程 HTTPS 拉取并更新工作区（不 exec）。

    使用快进方式：若本地与远程分叉会报错而非强推，避免污染/丢失服务端提交。
    """
    _ensure_repo()
    try:
        from dulwich import porcelain
        from dulwich.repo import Repo
        from dulwich.errors import NotGitRepository
        try:
            from dulwich.porcelain import DivergedBranches
        except Exception:  # 兼容不同版本的异常位置
            DivergedBranches = Exception
    except ImportError:
        raise GitUpdateError(
            "缺少 dulwich，无法在受限环境内执行更新。请在服务器虚拟环境执行："
            "pip install dulwich，然后重启服务；或在宝塔放行本项目执行 git。"
        )

    branch, before_sha = _read_head_and_sha()
    if not branch:
        raise GitUpdateError("当前处于游离 HEAD，无法自动更新")

    remote_url = _read_remote_url()
    fetch_url = _to_https(remote_url)
    if not fetch_url:
        raise GitUpdateError("未找到远程仓库地址（origin）")

    before = _local_commit_detail(before_sha)
    refspec = ("refs/heads/" + branch).encode()

    try:
        repo = Repo(REPO_DIR)
    except NotGitRepository:
        raise GitUpdateError(f"部署目录不是有效的 git 仓库：{REPO_DIR}")

    try:
        porcelain.pull(repo, fetch_url, refspecs=refspec, fast_forward=True)
    except DivergedBranches:
        raise GitUpdateError("本地与远程已分叉，无法快进更新。请先在服务器手动处理本地改动。")
    except PermissionError as e:
        raise GitUpdateError(
            f"写入仓库失败（权限不足）：{e}。请确认运行用户对 {REPO_DIR} 有写权限"
            "（如 chown -R www:www 该目录）。"
        )
    except Exception as e:
        raise GitUpdateError(f"拉取更新失败：{_redact(str(e))[:300]}")
    finally:
        try:
            repo.close()
        except Exception:
            pass

    _, after_sha = _read_head_and_sha()
    after = _local_commit_detail(after_sha)
    updated = bool(before_sha and after_sha and before_sha != after_sha)
    return {
        "success": True,
        "updated": updated,
        "before": before,
        "after": after,
        "message": "已更新到最新版本" if updated else "本地已是最新，无需更新",
    }


def diagnose() -> dict:
    """诊断信息：检测到的项目目录、.git 状态、远程、本地提交、dulwich 可用性、GitHub 可达性。全程不 exec。"""
    result = {
        "repo_dir": REPO_DIR,
        "repo_dir_exists": os.path.isdir(REPO_DIR),
        "git_dir": _git_dir(),
        "dot_git_exists": os.path.exists(_git_dir()),
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

    # dulwich 可用性
    try:
        import importlib.util
        result["dulwich_available"] = importlib.util.find_spec("dulwich") is not None
    except Exception:
        result["dulwich_available"] = False

    # 本地分支/提交、远程地址
    try:
        branch, sha = _read_head_and_sha()
        result["branch"] = branch
        result["local_sha"] = sha
    except GitUpdateError as e:
        result["head_error"] = str(e)
    remote_url = _read_remote_url()
    result["remote_url"] = _redact(remote_url)
    gh = _parse_github(remote_url)
    result["github_repo"] = f"{gh[0]}/{gh[1]}" if gh else None

    # 仓库可写性（apply 需要）
    result["repo_writable"] = os.access(_git_dir(), os.W_OK)

    # GitHub API 可达性
    if gh:
        try:
            r = requests.get(
                f"https://api.github.com/repos/{gh[0]}/{gh[1]}/branches/{result.get('branch') or 'main'}",
                headers=_GH_HEADERS, timeout=HTTP_TIMEOUT,
            )
            result["github_api"] = {"code": r.status_code}
        except Exception as e:
            result["github_api"] = {"error": str(e)[:200]}
    return result
