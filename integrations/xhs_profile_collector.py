"""通过受管 Chromium 采集小红书作者主页；仅从 stdin 接收敏感参数。

主页状态字段遵循 ``xhs-engine.lock`` 固定版本公开的用户主页解析契约，
采集、校验与增量停止逻辑由本项目独立实现。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import sys
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from playwright.async_api import Browser, Page, async_playwright

_PROFILE_PATH = re.compile(r"^/user/profile/([0-9A-Za-z._-]{1,128})/?$")
_POSTED_PATH = "/api/sns/web/v1/user_posted"


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, (str, int)) else ""


def _unwrap(value: Any) -> Any:
    item = _record(value)
    if "value" in item:
        return item["value"]
    if "_value" in item:
        return item["_value"]
    return value


def _flatten(values: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for value in _list(values):
        if isinstance(value, list):
            result.extend(_flatten(value))
        elif isinstance(value, dict):
            result.append(value)
    return result


def _media_url(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("urlDefault") or value.get("urlPre") or value.get("url")
    raw = _text(value)
    if raw.startswith("//"):
        raw = f"https:{raw}"
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        host == "xiaohongshu.com"
        or host.endswith(".xiaohongshu.com")
        or host == "xhscdn.com"
        or host.endswith(".xhscdn.com")
        or host == "xhsimg.com"
        or host.endswith(".xhsimg.com")
    ):
        return None
    return raw


def _normalize_note(raw: dict[str, Any], expected_user_id: str) -> dict[str, Any] | None:
    note = _record(raw.get("noteCard") or raw.get("note_card") or raw)
    note_id = _text(
        raw.get("noteId") or raw.get("note_id") or raw.get("id")
        or note.get("noteId") or note.get("note_id") or note.get("id")
    )
    if not note_id or len(note_id) > 128:
        return None
    author = _record(note.get("user") or note.get("author"))
    author_id = _text(author.get("userId") or author.get("user_id") or author.get("id"))
    if author_id and author_id != expected_user_id:
        return None
    token = _text(
        raw.get("xsecToken") or raw.get("xsec_token")
        or note.get("xsecToken") or note.get("xsec_token")
    )
    title = _text(
        note.get("displayTitle") or note.get("display_title")
        or note.get("title") or note.get("desc")
    )[:500]
    note_type = _text(note.get("type") or note.get("noteType") or note.get("note_type")).lower()
    if "video" in note_type:
        normalized_type = "video"
    elif note_type in {"normal", "image", "images"}:
        normalized_type = "image"
    else:
        normalized_type = "unknown"
    cover = _record(note.get("cover"))
    cover_url = _media_url(
        cover.get("urlDefault") or cover.get("url_default") or cover.get("url")
    )
    published = note.get("time") or note.get("publishTime") or note.get("publish_time")
    try:
        published_at = int(published) if published is not None else None
        if published_at and published_at > 10_000_000_000:
            published_at //= 1000
    except (TypeError, ValueError):
        published_at = None
    query = urlencode({"xsec_token": token, "xsec_source": "pc_user"}) if token else ""
    url = f"https://www.xiaohongshu.com/explore/{quote(note_id, safe='')}"
    return {
        "note_id": note_id,
        "xsec_token": token,
        "xsec_source": "pc_user",
        "source_url": f"{url}?{query}" if query else url,
        "title": title,
        "work_type": normalized_type,
        "cover_url": cover_url,
        "published_at": published_at,
        "is_pinned": bool(raw.get("isPinned") or raw.get("is_pinned")),
    }


def _profile_from_state(state: dict[str, Any], expected_user_id: str) -> dict[str, Any]:
    user = _record(state.get("user"))
    page_data = _record(_unwrap(user.get("userPageData")))
    basic = _record(page_data.get("basicInfo"))
    actual_id = _text(basic.get("userId") or basic.get("user_id"))
    if actual_id and actual_id != expected_user_id:
        raise RuntimeError("作者主页身份与请求不一致")
    nickname = _text(basic.get("nickname"))[:200]
    red_id = _text(basic.get("redId") or basic.get("red_id"))[:200]
    if not actual_id and not nickname and not red_id:
        raise RuntimeError("作者主页未返回可验证身份；可能需要重新登录或人工验证")
    avatar = _media_url(basic.get("imageb") or basic.get("images"))
    return {
        "user_id": actual_id or expected_user_id,
        "nickname": nickname,
        "red_id": red_id,
        "description": _text(basic.get("desc"))[:5000],
        "avatar_url": avatar,
    }


def _response_notes(payload: Any) -> tuple[list[dict[str, Any]], bool | None]:
    root = _record(payload)
    data = _record(root.get("data"))
    notes = data.get("notes") or data.get("items") or root.get("notes") or root.get("items")
    if notes is None:
        return [], None
    return _flatten(notes), bool(data.get("has_more", data.get("hasMore", False)))


async def _read_state(page: Page) -> dict[str, Any]:
    value = await page.evaluate(
        """() => {
          const root = window.__INITIAL_STATE__ || {};
          const seen = new WeakSet();
          return JSON.parse(JSON.stringify(root, (key, value) => {
            if (key.startsWith('__v_') || key === 'dep' || key === 'effect') return undefined;
            if (value && typeof value === 'object') {
              if (seen.has(value)) return undefined;
              seen.add(value);
            }
            return value;
          }));
        }"""
    )
    return _record(value)


async def collect_profile(payload: dict[str, Any]) -> dict[str, Any]:
    raw_url = _text(payload.get("url"))
    parsed = urlsplit(raw_url)
    match = _PROFILE_PATH.fullmatch(parsed.path)
    if parsed.scheme != "https" or parsed.hostname != "www.xiaohongshu.com" or not match:
        raise RuntimeError("作者主页地址无效")
    user_id = match.group(1)
    query = parse_qs(parsed.query)
    token = _text((query.get("xsec_token") or [""])[0])
    if token:
        raw_url = (
            f"https://www.xiaohongshu.com/user/profile/{quote(user_id, safe='')}?"
            + urlencode({"xsec_token": token, "xsec_source": "pc_user"})
        )
    else:
        raw_url = f"https://www.xiaohongshu.com/user/profile/{quote(user_id, safe='')}"
    port = int(payload.get("cdp_port") or 0)
    if not 1 <= port <= 65535:
        raise RuntimeError("受管浏览器端口无效")
    max_items = min(max(int(payload.get("max_items") or 100), 1), 500)
    max_scrolls = min(max(int(payload.get("max_scrolls") or 40), 1), 200)
    known_streak_limit = min(max(int(payload.get("known_streak") or 5), 1), 100)
    known = {_text(item) for item in _list(payload.get("known_ids")) if _text(item)}
    delay = min(max(float(payload.get("scroll_delay") or 2.0), 0.5), 10.0)

    notes: dict[str, dict[str, Any]] = {}
    response_queue: asyncio.Queue[tuple[list[dict[str, Any]], bool | None]] = asyncio.Queue()
    page: Page | None = None
    async with async_playwright() as playwright:
        browser: Browser = await playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{port}", timeout=10_000, is_local=True,
        )
        if not browser.contexts:
            raise RuntimeError("受管浏览器没有持久化上下文")
        page = await browser.contexts[0].new_page()

        async def capture(response: Any) -> None:
            try:
                response_url = urlsplit(response.url)
                if (
                    response_url.scheme != "https"
                    or response_url.hostname != "edith.xiaohongshu.com"
                    or response_url.path != _POSTED_PATH
                    or response.status != 200
                ):
                    return
                await response_queue.put(_response_notes(await response.json()))
            except Exception:
                return

        page.on("response", capture)
        try:
            await page.goto(raw_url, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(1500)
            if "login" in urlsplit(page.url).path or await page.locator(
                ".login-container, [class*='login-container']"
            ).count():
                raise RuntimeError("浏览器尚未登录小红书")
            state = await _read_state(page)
            profile = _profile_from_state(state, user_id)
            initial = _unwrap(_record(state.get("user")).get("notes"))
            for raw in _flatten(initial):
                if item := _normalize_note(raw, user_id):
                    notes.setdefault(item["note_id"], item)

            idle_rounds = 0
            has_more: bool | None = None
            stop_reason = "max_scrolls"
            for scroll_index in range(max_scrolls):
                while not response_queue.empty():
                    batch, batch_has_more = response_queue.get_nowait()
                    has_more = batch_has_more if batch_has_more is not None else has_more
                    for raw in batch:
                        if item := _normalize_note(raw, user_id):
                            notes.setdefault(item["note_id"], item)
                ordered = list(notes.values())
                if len(ordered) >= max_items:
                    stop_reason = "max_items"
                    break
                if scroll_index >= 2 and known:
                    streak = 0
                    for item in ordered:
                        if item["is_pinned"]:
                            continue
                        streak = streak + 1 if item["note_id"] in known else 0
                        if streak >= known_streak_limit:
                            break
                    if streak >= known_streak_limit:
                        stop_reason = "known_streak"
                        break
                before = len(notes)
                await page.evaluate(
                    """() => {
                      const target = document.scrollingElement || document.documentElement;
                      target.scrollTo({top: target.scrollHeight, behavior: 'instant'});
                      window.dispatchEvent(new Event('scroll'));
                    }"""
                )
                await page.wait_for_timeout(int(random.uniform(delay, delay * 1.35) * 1000))
                live_state = await _read_state(page)
                live_notes = _unwrap(_record(live_state.get("user")).get("notes"))
                for raw in _flatten(live_notes):
                    if item := _normalize_note(raw, user_id):
                        notes.setdefault(item["note_id"], item)
                if len(notes) == before:
                    idle_rounds += 1
                else:
                    idle_rounds = 0
                if has_more is False and response_queue.empty():
                    stop_reason = "end"
                    break
                if idle_rounds >= 3:
                    stop_reason = "idle"
                    break
            items = list(notes.values())[:max_items]
            return {
                "author": profile,
                "items": items,
                "stop_reason": stop_reason,
                "has_more": bool(has_more),
            }
        finally:
            if page:
                await page.close()


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            raise RuntimeError("采集参数格式无效")
        result = asyncio.run(collect_profile(payload))
        sys.stdout.write(json.dumps({"ok": True, **result}, ensure_ascii=False))
    except Exception as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": str(exc)[:500]}, ensure_ascii=False))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
