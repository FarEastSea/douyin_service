"""归档路径、筛选和元数据导出规则。"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
from datetime import date, datetime
from pathlib import Path
from string import Formatter
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.models import Author, SystemConfig, Work


CONFIG_KEY = "archive:rules"
RULE_VERSION = 1
DEFAULT_RULES: dict[str, Any] = {
    "version": RULE_VERSION,
    "directory_template": "{author}",
    "filename_template": "{title}_{aweme_id}{index_suffix}.{ext}",
    "work_types": ["video", "images"],
    "published_from": None,
    "published_to": None,
    "min_file_size_mb": 0,
    "max_file_size_mb": 0,
    "metadata_formats": [],
}

_DIRECTORY_FIELDS = {"author", "published_date", "year", "month", "work_type"}
_FILENAME_FIELDS = {
    "author", "title", "aweme_id", "published_date", "year", "month",
    "work_type", "index", "index_suffix", "ext",
}
_INVALID_FILENAME = re.compile(r'[\x00-\x1f\\/:*?"<>|]')


def _template_fields(template: str) -> set[str]:
    try:
        parsed = list(Formatter().parse(template))
    except ValueError as exc:
        raise ValueError(f"模板格式无效：{exc}") from exc
    if any(format_spec or conversion for _, _, format_spec, conversion in parsed):
        raise ValueError("模板变量不支持格式化指令或转换符")
    return {field for _, field, _, _ in parsed if field}


def _parse_date(value: Any, label: str) -> str | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError as exc:
        raise ValueError(f"{label} 必须是 YYYY-MM-DD 日期") from exc


def normalize_archive_rules(value: Any) -> dict[str, Any]:
    """合并默认值并严格校验，避免非法模板越出下载根目录。"""
    source = dict(value or {}) if isinstance(value, dict) else {}
    rules = {**DEFAULT_RULES, **source, "version": RULE_VERSION}

    directory_template = str(rules["directory_template"] or "").strip()
    filename_template = str(rules["filename_template"] or "").strip()
    if not directory_template:
        raise ValueError("归档目录模板不能为空")
    if len(directory_template) > 300 or len(filename_template) > 300:
        raise ValueError("归档模板不能超过 300 个字符")
    if directory_template.replace("\\", "/").count("/") > 8:
        raise ValueError("归档目录最多支持 9 层")
    if Path(directory_template).is_absolute() or ".." in Path(directory_template).parts:
        raise ValueError("归档目录模板必须是下载目录内的相对路径")
    unknown_directory = _template_fields(directory_template) - _DIRECTORY_FIELDS
    if unknown_directory:
        raise ValueError(f"归档目录模板包含未知变量：{', '.join(sorted(unknown_directory))}")
    if not filename_template or Path(filename_template).name != filename_template:
        raise ValueError("文件名模板必须是单个文件名，不能包含目录")
    fields = _template_fields(filename_template)
    unknown_filename = fields - _FILENAME_FIELDS
    if unknown_filename:
        raise ValueError(f"文件名模板包含未知变量：{', '.join(sorted(unknown_filename))}")
    if "aweme_id" not in fields:
        raise ValueError("文件名模板必须包含 {aweme_id}，避免不同作品互相覆盖")
    if "ext" not in fields:
        raise ValueError("文件名模板必须包含 {ext}")
    if not ({"index", "index_suffix"} & fields):
        raise ValueError("文件名模板必须包含 {index} 或 {index_suffix}，避免图集文件互相覆盖")

    work_types = list(dict.fromkeys(str(item) for item in (rules.get("work_types") or [])))
    if not work_types or set(work_types) - {"video", "images"}:
        raise ValueError("作品类型至少选择视频或图集中的一种")
    metadata_formats = list(dict.fromkeys(str(item) for item in (rules.get("metadata_formats") or [])))
    if set(metadata_formats) - {"json", "csv"}:
        raise ValueError("元数据导出格式仅支持 JSON、CSV")

    published_from = _parse_date(rules.get("published_from"), "开始日期")
    published_to = _parse_date(rules.get("published_to"), "结束日期")
    if published_from and published_to and published_from > published_to:
        raise ValueError("开始日期不能晚于结束日期")
    try:
        min_size = float(rules.get("min_file_size_mb") or 0)
        max_size = float(rules.get("max_file_size_mb") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("文件大小必须是数字") from exc
    if not math.isfinite(min_size) or not math.isfinite(max_size):
        raise ValueError("文件大小必须是有限数字")
    if min_size < 0 or max_size < 0:
        raise ValueError("文件大小不能小于 0")
    if max_size and max_size < min_size:
        raise ValueError("最大文件大小不能小于最小文件大小")

    return {
        "version": RULE_VERSION,
        "directory_template": directory_template,
        "filename_template": filename_template,
        "work_types": work_types,
        "published_from": published_from,
        "published_to": published_to,
        "min_file_size_mb": min_size,
        "max_file_size_mb": max_size,
        "metadata_formats": metadata_formats,
    }


def serialize_archive_rules(rules: dict[str, Any]) -> str:
    return json.dumps(normalize_archive_rules(rules), ensure_ascii=False, separators=(",", ":"))


def deserialize_archive_rules(raw: str | None) -> dict[str, Any]:
    if not raw:
        return normalize_archive_rules({})
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("已保存的归档规则不是有效 JSON") from exc
    return normalize_archive_rules(value)


def get_archive_rules_sync(db: Session) -> dict[str, Any]:
    raw = db.execute(select(SystemConfig.value).where(SystemConfig.key == CONFIG_KEY)).scalar_one_or_none()
    return deserialize_archive_rules(raw)


async def get_archive_rules(db: AsyncSession) -> dict[str, Any]:
    raw = (await db.execute(select(SystemConfig.value).where(SystemConfig.key == CONFIG_KEY))).scalar_one_or_none()
    return deserialize_archive_rules(raw)


async def save_archive_rules(db: AsyncSession, value: dict[str, Any]) -> dict[str, Any]:
    rules = normalize_archive_rules(value)
    row = (await db.execute(select(SystemConfig).where(SystemConfig.key == CONFIG_KEY))).scalar_one_or_none()
    serialized = serialize_archive_rules(rules)
    if row:
        row.value = serialized
    else:
        db.add(SystemConfig(key=CONFIG_KEY, value=serialized))
    await db.commit()
    return rules


def work_matches_archive_rules(work: Work, rules: dict[str, Any]) -> tuple[bool, str | None]:
    if work.work_type not in rules["work_types"]:
        return False, "作品类型不在归档规则范围内"
    published = work.published_at.date().isoformat() if work.published_at else None
    if rules["published_from"] and (not published or published < rules["published_from"]):
        return False, "作品发布时间早于归档范围"
    if rules["published_to"] and (not published or published > rules["published_to"]):
        return False, "作品发布时间晚于归档范围"
    return True, None


def archive_size_limits(rules: dict[str, Any]) -> tuple[int, int]:
    mib = 1024 * 1024
    return int(rules["min_file_size_mb"] * mib), int(rules["max_file_size_mb"] * mib)


def _safe_component(value: Any, fallback: str, max_length: int = 120) -> str:
    cleaned = _INVALID_FILENAME.sub(" ", str(value or ""))
    cleaned = " ".join(cleaned.split()).strip(" .")
    return (cleaned or fallback)[:max_length]


def _safe_filename(value: Any, fallback: str, ext: str) -> str:
    cleaned = _safe_component(value, fallback, 1000)
    if len(cleaned) <= 240:
        return cleaned
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:12]
    suffix = f"_{digest}.{ext}"
    return f"{cleaned[:240 - len(suffix)].rstrip(' .')}{suffix}"


def build_archive_file_path(
    download_root: str,
    rules: dict[str, Any],
    author: Author,
    work: Work,
    file_index: int,
    *,
    is_live_photo: bool = False,
) -> str:
    published_at = work.published_at or work.discovered_at or datetime.now()
    index = file_index + 1 if work.work_type == "images" else 0
    ext = "mp4" if work.work_type == "video" or is_live_photo else "jpg"
    values = {
        "author": _safe_component(author.nickname, "未知作者"),
        "title": _safe_component(work.title, "untitled"),
        "aweme_id": _safe_component(work.aweme_id, "unknown"),
        "published_date": published_at.strftime("%Y-%m-%d"),
        "year": published_at.strftime("%Y"),
        "month": published_at.strftime("%m"),
        "work_type": work.work_type,
        "index": str(index),
        "index_suffix": f"_{index}" if index else "",
        "ext": ext,
    }
    directory_text = rules["directory_template"].format_map(values).replace("\\", "/")
    directory_parts = [_safe_component(part, "未分类") for part in directory_text.split("/") if part]
    filename = _safe_filename(
        rules["filename_template"].format_map(values), f"{work.aweme_id}.{ext}", ext,
    )
    root = Path(download_root).expanduser().resolve()
    target = root.joinpath(*directory_parts, filename).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("归档模板生成的路径越出了下载目录") from exc
    return str(target)


def write_metadata_sidecars(
    file_path: str,
    rules: dict[str, Any],
    author: Author,
    work: Work,
    file_index: int,
) -> list[str]:
    formats = rules.get("metadata_formats") or []
    if not formats:
        return []
    data = {
        "aweme_id": work.aweme_id,
        "author": author.nickname,
        "title": work.title,
        "work_type": work.work_type,
        "file_index": file_index,
        "published_at": work.published_at.isoformat() if work.published_at else None,
        "duration_ms": work.duration_ms,
        "width": work.width,
        "height": work.height,
        "music_title": work.music_title,
        "music_author": work.music_author,
        "hashtags": work.hashtags,
        "digg_count": work.digg_count,
        "comment_count": work.comment_count,
        "collect_count": work.collect_count,
        "share_count": work.share_count,
        "play_count": work.play_count,
    }
    written: list[str] = []
    target = Path(file_path)
    for file_format in formats:
        sidecar = target.with_suffix(target.suffix + f".{file_format}")
        temporary = sidecar.with_suffix(sidecar.suffix + ".tmp")
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        if file_format == "json":
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(data))
                writer.writeheader()
                writer.writerow({**data, "hashtags": ",".join(data["hashtags"])})
        os.replace(temporary, sidecar)
        written.append(str(sidecar))
    return written
