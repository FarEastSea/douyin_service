"""Instagram 的稳定身份与分流订阅状态。"""

import json
from pathlib import Path

from sqlalchemy import func, select

from app.core.config import settings
from app.models.models import (
    InstagramCollection,
    InstagramCollectionItem,
    PlatformAuthor,
    PlatformDownloadTask,
    PlatformMediaAsset,
    PlatformSubscriptionState,
    PlatformWork,
)
from app.services.instagram_runtime import utcnow
from app.services.platform_task_service import create_platform_task

KINDS = ("posts", "reels", "stories", "highlights")


def interval_for(kind):
    current = settings.snapshot()
    return (
        current.INSTAGRAM_STORIES_INTERVAL
        if kind == "stories"
        else current.INSTAGRAM_HIGHLIGHTS_INTERVAL
        if kind == "highlights"
        else current.INSTAGRAM_CHECK_INTERVAL
    )


def upsert_author(db, user):
    external_id = str(user.get("pk") or user.get("id") or "")
    username = str(user.get("username") or "")
    if not external_id.isdecimal() or not username:
        raise ValueError("Instagram 作者响应缺少稳定 ID 或用户名")
    author = db.scalar(
        select(PlatformAuthor).where(
            PlatformAuthor.platform == "instagram",
            PlatformAuthor.external_user_id == external_id,
        )
    )
    if not author:
        author = PlatformAuthor(
            platform="instagram",
            external_user_id=external_id,
            profile_url=f"https://www.instagram.com/{username}/",
        )
        db.add(author)
    author.username, author.profile_url = (
        username,
        f"https://www.instagram.com/{username}/",
    )
    author.nickname = str(user.get("full_name") or author.nickname or username)[:255]
    if "biography" in user:
        author.description = str(user.get("biography") or "")[:4000]
    if user.get("profile_pic_url_hd") or user.get("profile_pic_url"):
        author.avatar_url = user.get("profile_pic_url_hd") or user.get(
            "profile_pic_url"
        )
    author.account_status, author.last_error = "active", None
    db.flush()
    return author


def configure_subscription(db, author, *, subscribed, kinds=None, backfill=True):
    kinds = set(kinds if kinds is not None else ("posts", "reels"))
    if not kinds or kinds - set(KINDS):
        raise ValueError("至少选择一种有效的 Instagram 内容类型")
    author.is_subscribed = subscribed
    states = {
        (row.content_kind, row.scan_mode): row
        for row in db.scalars(
            select(PlatformSubscriptionState).where(
                PlatformSubscriptionState.author_id == author.id
            )
        )
    }
    previously_selected = {
        row.content_kind
        for row in states.values()
        if row.scan_mode == "recent" and row.enabled
    }
    for kind in KINDS:
        for mode in ("recent",) if kind == "stories" else ("recent", "history"):
            row = states.get((kind, mode))
            enabled = kind in kinds and (mode == "recent" or backfill)
            if row is None:
                row = PlatformSubscriptionState(
                    author_id=author.id,
                    content_kind=kind,
                    scan_mode=mode,
                    enabled=enabled,
                )
                db.add(row)
            elif mode == "history" and kind in kinds and kind in previously_selected:
                # 保存订阅设置不能恢复用户单独暂停的历史补采。
                continue
            elif row.enabled != enabled:
                row.enabled = enabled
                if enabled:
                    row.next_check_at = utcnow()
    db.flush()


def work_identity(data):
    story = data.get("type") in {"story", "highlight"}
    external_id = str(data["media_id"] if story else data["post_id"]).split("_")[0]
    if not external_id.isdecimal():
        raise ValueError("Instagram 作品缺少有效稳定 ID")
    return ("story:" if story else "post:") + external_id


def persist_work(db, author, data, expected):
    identity = work_identity(data)
    work = db.scalar(
        select(PlatformWork).where(
            PlatformWork.platform == "instagram",
            PlatformWork.external_work_id == identity,
        )
    )
    is_new = work is None
    story = identity.startswith("story:")
    source_url = (
        f"https://www.instagram.com/stories/{author.username}/{identity[6:]}/"
        if story
        else f"https://www.instagram.com/p/{data['post_shortcode']}/"
    )
    if work is None:
        work = PlatformWork(
            platform="instagram",
            external_work_id=identity,
            author_id=author.id,
            source_url=source_url,
        )
        db.add(work)
    work.source_url = source_url
    work.title = str(data.get("description") or data.get("highlight_title") or "")[
        :4000
    ]
    # 同一持久帖文在 Posts/Reels 中保留同一个作品身份。
    work.content_kind = (
        "story"
        if story
        else "reel"
        if data.get("type") == "reel" or data.get("is_reel")
        else work.content_kind or "post"
    )
    work.work_type = (
        "carousel" if expected > 1 else "video" if data.get("video_url") else "image"
    )
    work.expected_media_count = expected
    work.published_at = data.get("post_date") or data.get("date")
    work.expires_at = (
        data.get("expires") if story and data.get("type") != "highlight" else None
    )
    work.last_seen_at = utcnow()
    work.is_pinned = bool(data.get("pinned"))
    db.flush()
    if data.get("type") == "highlight":
        collection_id = str(data["post_id"]).removeprefix("highlight:")
        collection = db.scalar(
            select(InstagramCollection).where(
                InstagramCollection.external_id == collection_id
            )
        )
        if collection is None:
            collection = InstagramCollection(
                external_id=collection_id, author_id=author.id
            )
            db.add(collection)
            db.flush()
        collection.title = str(data.get("highlight_title") or collection_id)[:255]
        collection.last_seen_at = utcnow()
        if db.get(InstagramCollectionItem, (collection.id, work.id)) is None:
            db.add(
                InstagramCollectionItem(
                    collection_id=collection.id,
                    work_id=work.id,
                    position=int(
                        data.get("collection_position") or data.get("num") or 0
                    ),
                )
            )
    return work, is_new


def ensure_work_task(db, work, root_task):
    task = (
        db.get(PlatformDownloadTask, work.download_task_id)
        if work.download_task_id
        else None
    )
    if task is None:
        if root_task.source_type == "work":
            task = root_task
        else:
            key = work.external_work_id.replace(":", "-")
            task = db.scalar(
                select(PlatformDownloadTask)
                .where(
                    PlatformDownloadTask.platform == "instagram",
                    PlatformDownloadTask.source_key == key,
                    PlatformDownloadTask.source_type == "work",
                )
                .order_by(PlatformDownloadTask.id.desc())
                .limit(1)
            )
            if task is None:
                task = create_platform_task("instagram", key, work.source_url, "work")
                task.phase = "discovered"
                db.add(task)
                db.flush()
        work.download_task_id = task.id
    if work.external_work_id.startswith("story:"):
        collection = db.scalar(
            select(InstagramCollection)
            .join(InstagramCollectionItem)
            .where(InstagramCollectionItem.work_id == work.id)
        )
        if collection:
            task.source_url = f"https://www.instagram.com/stories/highlights/{collection.external_id}/"
            task.instagram_options = json.dumps({"story_id": work.external_work_id[6:]})
    return task


def work_complete(db, work):
    assets = list(
        db.scalars(
            select(PlatformMediaAsset).where(
                PlatformMediaAsset.platform == "instagram",
                PlatformMediaAsset.work_id == work.id,
            )
        )
    )
    return bool(
        work.expected_media_count
        and len(assets) == work.expected_media_count
        and all(
            Path(asset.file_path).is_file()
            and Path(asset.file_path).stat().st_size == asset.size_bytes
            and asset.size_bytes > 0
            for asset in assets
        )
    )


def author_payload(db, author):
    states = list(
        db.scalars(
            select(PlatformSubscriptionState)
            .where(PlatformSubscriptionState.author_id == author.id)
            .order_by(
                PlatformSubscriptionState.content_kind,
                PlatformSubscriptionState.scan_mode,
            )
        )
    )
    indexed = {}
    for kind in KINDS:
        condition = PlatformWork.author_id == author.id
        if kind == "highlights":
            ids = (
                select(InstagramCollectionItem.work_id)
                .join(InstagramCollection)
                .where(InstagramCollection.author_id == author.id)
            )
            condition = condition & PlatformWork.id.in_(ids)
        else:
            condition = condition & PlatformWork.content_kind.in_(
                {"posts": ("post", "reel"), "reels": ("reel",), "stories": ("story",)}[
                    kind
                ]
            )
        indexed[kind] = int(
            db.scalar(select(func.count(PlatformWork.id)).where(condition)) or 0
        )
    return {
        **{
            key: getattr(author, key)
            for key in (
                "id",
                "platform",
                "external_user_id",
                "username",
                "nickname",
                "profile_url",
                "avatar_url",
                "is_subscribed",
                "account_status",
                "last_error",
                "last_check_time",
                "last_success_at",
                "total_works",
            )
        },
        "streams": [
            {
                "indexed_works": indexed[row.content_kind],
                "estimated_total": row.estimated_total,
                "estimated_remaining": max(
                    0, row.estimated_total - indexed[row.content_kind]
                )
                if row.estimated_total is not None
                else None,
                **{
                    key: getattr(row, key)
                    for key in (
                        "id",
                        "content_kind",
                        "scan_mode",
                        "enabled",
                        "next_check_at",
                        "last_check_at",
                        "last_success_at",
                        "history_complete",
                        "deferred_reason",
                        "task_id",
                    )
                },
            }
            for row in states
        ],
    }
