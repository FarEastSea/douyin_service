"""Read the deployed source revision without spawning a Git subprocess."""

from __future__ import annotations

import os
from pathlib import Path


def runtime_revision() -> str | None:
    for key in ("JENKINS_TARGET_SHA", "GIT_COMMIT"):
        if value := str(os.environ.get(key) or "").strip():
            return value
    try:
        git_path = Path(__file__).resolve().parents[2] / ".git"
        if git_path.is_file():
            line = git_path.read_text(encoding="utf-8").strip()
            git_path = (git_path.parent / line.removeprefix("gitdir:").strip()).resolve()
        head = (git_path / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            return (git_path / head.removeprefix("ref:").strip()).read_text(encoding="utf-8").strip()
        return head
    except (OSError, RuntimeError, ValueError):
        return None
