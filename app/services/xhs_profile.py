"""小红书受管浏览器会话与作者主页采集边界。"""

from __future__ import annotations

from typing import Any, Iterable
from uuid import uuid4

import requests

from app.services.xhs_download import XHS_INTERNAL_API_URL

class XhsProfileError(RuntimeError):
    """可安全展示的小红书作者采集错误。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _local_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


def _request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    session = _local_session()
    timeout = kwargs.pop("timeout", (3, 65))
    try:
        response = session.request(
            method, f"{XHS_INTERNAL_API_URL}{path}", timeout=timeout, **kwargs,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise XhsProfileError("invalid_result", "小红书采集服务返回结构无效")
        return payload
    except requests.ConnectionError as exc:
        raise XhsProfileError("engine_unavailable", "小红书隔离采集服务未启动") from exc
    except requests.Timeout as exc:
        raise XhsProfileError("request_timeout", "小红书浏览器操作超时") from exc
    except requests.HTTPError as exc:
        detail = ""
        try:
            body = exc.response.json()
            detail = str(body.get("detail") or body.get("message") or "")
        except (ValueError, AttributeError):
            pass
        raise XhsProfileError(
            "browser_unavailable", detail[:300] or "小红书受管浏览器操作失败",
        ) from exc
    finally:
        session.close()


def ensure_managed_browser() -> dict[str, Any]:
    status = _request("POST", "/browser/managed/start")
    port = status.get("cdp_port")
    if status.get("state") != "running" or not isinstance(port, int):
        raise XhsProfileError(
            "browser_unavailable", str(status.get("message") or "小红书受管浏览器未就绪")[:300],
        )
    return status


def get_browser_login_status() -> dict[str, Any]:
    status = _request("GET", "/browser/managed/status")
    if status.get("state") != "running":
        return {
            "installed": bool(status.get("installed")),
            "state": str(status.get("state") or "stopped"),
            "logged_in": False,
            "nickname": None,
            "message": str(status.get("message") or "受管浏览器尚未启动"),
        }
    task = _request(
        "POST", "/xhs/login/status?wait_seconds=30",
        json={"request_id": f"status-{uuid4().hex}"},
    )
    if task.get("status") == "failed":
        raise XhsProfileError(
            "browser_unavailable",
            str(task.get("message") or "小红书浏览器登录状态检查失败")[:300],
        )
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    return {
        "installed": bool(status.get("installed")),
        "state": str(status.get("state") or "running"),
        "logged_in": bool(result.get("logged_in")),
        "nickname": result.get("nickname"),
        "message": str(task.get("message") or status.get("message") or "状态已读取"),
    }


def get_login_qrcode() -> dict[str, Any]:
    ensure_managed_browser()
    task = _request(
        "POST", "/xhs/login/qrcode?wait_seconds=30",
        json={"request_id": f"qrcode-{uuid4().hex}"},
    )
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    image = result.get("image_data_url")
    if image is not None and not (
        isinstance(image, str) and image.startswith("data:image/") and len(image) <= 512_000
    ):
        raise XhsProfileError("invalid_result", "小红书登录二维码格式无效")
    is_logged_in = bool(result.get("is_logged_in"))
    if not is_logged_in and not image:
        raise XhsProfileError(
            "browser_unavailable",
            str(task.get("message") or "小红书登录二维码未生成")[:300],
        )
    return {
        "is_logged_in": is_logged_in,
        "image_data_url": image,
        "expires_at": result.get("expires_at"),
        "message": str(task.get("message") or "二维码已生成"),
    }


def collect_profile(
    source_url: str,
    *,
    known_ids: Iterable[str] = (),
    max_items: int = 100,
    max_scrolls: int = 40,
    known_streak: int = 5,
    scroll_delay: float = 2.0,
) -> dict[str, Any]:
    """启动受管浏览器并调用隔离环境中的 Playwright 采集脚本。"""
    status = ensure_managed_browser()
    payload = {
        "url": source_url,
        "cdp_port": status["cdp_port"],
        "known_ids": list(dict.fromkeys(str(item) for item in known_ids if str(item)))[:5000],
        "max_items": max_items,
        "max_scrolls": max_scrolls,
        "known_streak": known_streak,
        "scroll_delay": scroll_delay,
    }
    result = _request(
        "POST",
        "/integrations/profile/collect",
        json=payload,
        timeout=(3, max(90, int(max_scrolls * scroll_delay * 2 + 60))),
    )
    if not isinstance(result.get("author"), dict) or not isinstance(result.get("items"), list):
        raise XhsProfileError("invalid_result", "小红书作者采集结果缺少作者或作品列表")
    return result
