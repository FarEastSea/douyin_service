"""Bounded sibling paths for artifacts owned by one download attempt."""

import hashlib
from pathlib import Path
from typing import Literal


def build_download_attempt_path(
    file_path: str,
    attempt_id: str | None = None,
    *,
    suffix: Literal[".downloading", ".previous"] = ".downloading",
) -> str:
    """Keep titles out of temporary names without sharing concurrent writers.

    The longest component is 78 ASCII bytes (83 with resume metadata's .json),
    independent of the title, UTF-8 width, or attempt identifier length.
    Sibling paths preserve atomic publication and existing resume validation.
    """
    if suffix not in {".downloading", ".previous"}:
        raise ValueError("Unsupported download artifact suffix")
    target = Path(file_path)
    destination = hashlib.sha256(target.name.encode("utf-8")).hexdigest()[:32]
    attempt = (
        "." + hashlib.sha256(str(attempt_id).encode("utf-8")).hexdigest()[:32]
        if attempt_id else ""
    )
    return str(target.with_name(f".{destination}{attempt}{suffix}"))
