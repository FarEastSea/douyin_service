"""抖音上游错误分类与可操作提示。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class DouyinErrorInfo:
    code: str
    category: str
    message: str
    action: str
    recoverable: bool = True


ERROR_INFO = {
    "browser_identity_missing": DouyinErrorInfo(
        "browser_identity_missing", "authentication",
        "抖音请求缺少浏览器身份标识，系统已停止继续请求。",
        "请在设置中心更新包含 UIFID 的完整抖音 Cookie 后重试。",
        False,
    ),
    "argus_blocked": DouyinErrorInfo(
        "argus_blocked", "risk_control",
        "抖音安全校验未通过，系统已暂停新的抖音接口请求。",
        "请等待冷却结束；若再次失败，请在设置中更新 Cookie 后手动重试。",
    ),
    "rate_limited": DouyinErrorInfo(
        "rate_limited", "risk_control",
        "抖音请求过于频繁，系统已进入保护性冷却。",
        "请等待倒计时结束，避免连续点击重试。",
    ),
    "cookie_invalid": DouyinErrorInfo(
        "cookie_invalid", "authentication", "抖音登录状态可能已失效。",
        "请在设置中心更新抖音 Cookie 后重试。",
    ),
    "content_unavailable": DouyinErrorInfo(
        "content_unavailable", "content", "目标作品或作者当前不可访问。",
        "请确认链接有效，并检查内容是否已删除、设为私密或受地区限制。", False,
    ),
    "network_error": DouyinErrorInfo(
        "network_error", "network", "访问抖音服务时发生网络异常。",
        "请检查服务器网络并稍后重试。",
    ),
    "upstream_error": DouyinErrorInfo(
        "upstream_error", "upstream", "抖音服务返回了异常响应。",
        "请稍后重试；若持续失败，请复制诊断信息查看服务端日志。",
    ),
}


class DouyinRequestError(ValueError):
    """可被 API、Celery 和前端统一识别的抖音请求错误。"""

    def __init__(self, code: str, *, detail: str = "", status_code: Optional[int] = None,
                 retry_after: int = 0) -> None:
        self.info = ERROR_INFO.get(code, ERROR_INFO["upstream_error"])
        self.code = self.info.code
        self.category = self.info.category
        self.user_message = self.info.message
        self.action = self.info.action
        self.recoverable = self.info.recoverable
        self.detail = str(detail or "")[:1000]
        self.status_code = status_code
        self.retry_after = max(0, int(retry_after or 0))
        super().__init__(self.user_message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "category": self.category, "message": self.user_message,
            "action": self.action, "recoverable": self.recoverable,
            "retry_after": self.retry_after,
        }


class DouyinCooldownError(DouyinRequestError):
    def __init__(self, *, retry_after: int, reason: str = "argus_blocked") -> None:
        code = reason if reason in {
            "browser_identity_missing", "argus_blocked", "rate_limited",
        } else "argus_blocked"
        super().__init__(code, detail="global cooldown active", retry_after=retry_after)


def _flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def classify_douyin_error(*, status_code: Optional[int], body: Any = None,
                           data: Any = None) -> Optional[DouyinRequestError]:
    """从状态码、正文和 JSON 业务字段中识别抖音错误。"""
    text = " ".join((_flatten_text(body), _flatten_text(data))).strip()
    lowered = text.lower()
    if "uifid not found" in lowered:
        return DouyinRequestError(
            "browser_identity_missing", detail=text, status_code=status_code
        )
    if "argussecurityplugin" in lowered or (
        "blocked by argus" in lowered and "validate error" in lowered
    ):
        return DouyinRequestError("argus_blocked", detail=text, status_code=status_code)

    rate_tokens: Iterable[str] = (
        "too many requests", "rate limit", "请求频繁", "访问频繁", "操作频繁",
        "风控", "反爬", "captcha", "verify failed", "verify error", "验证码",
    )
    if status_code == 429 or any(token in lowered for token in rate_tokens):
        return DouyinRequestError("rate_limited", detail=text, status_code=status_code)

    cookie_tokens: Iterable[str] = (
        "cookie invalid", "cookie expired", "cookie失效", "cookie 失效", "cookie过期",
        "login required", "not login", "未登录", "登录失效", "登录过期",
        "session expired", "passport",
    )
    if any(token in lowered for token in cookie_tokens):
        return DouyinRequestError("cookie_invalid", detail=text, status_code=status_code)

    content_tokens: Iterable[str] = (
        "not found", "不存在", "已删除", "作品已下架", "私密", "不可见", "无权限",
    )
    if status_code in {404, 410} or any(token in lowered for token in content_tokens):
        return DouyinRequestError("content_unavailable", detail=text, status_code=status_code)

    if status_code is not None and status_code >= 400:
        code = "network_error" if status_code in {502, 503, 504} else "upstream_error"
        return DouyinRequestError(code, detail=text, status_code=status_code)
    return None


def parse_douyin_json_response(response: Any, *, expected_keys: Iterable[str] = ()) -> dict[str, Any]:
    """解析 JSON，并优先暴露风控/鉴权错误而不是通用 ValueError。"""
    body = str(getattr(response, "text", "") or "")
    status_code = getattr(response, "status_code", None)
    preliminary = classify_douyin_error(status_code=status_code, body=body)
    # HTTP 200 的正常作品正文可能碰巧包含“风控”“验证码”等文字。只有精确的
    # Argus 文本可以在 JSON 解析前直接判定；其他文本分类仅用于 HTTP 错误响应。
    if preliminary and (preliminary.code == "argus_blocked" or (status_code or 0) >= 400):
        raise preliminary
    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        classified = preliminary or classify_douyin_error(status_code=status_code, body=body)
        if classified:
            raise classified from exc
        raise DouyinRequestError(
            "cookie_invalid" if status_code in {200, 401, 403} else "upstream_error",
            detail=body[:500] or "empty non-json response", status_code=status_code,
        ) from exc

    if not isinstance(data, dict):
        raise DouyinRequestError("upstream_error", detail=f"unexpected JSON type: {type(data).__name__}")
    diagnostic = {
        key: data.get(key)
        for key in ("status_code", "status_msg", "message", "description", "detail")
        if data.get(key) not in {None, "", 0, "0"}
    }
    classified = classify_douyin_error(status_code=status_code, data=diagnostic)
    if classified:
        raise classified
    business_status = data.get("status_code", 0)
    has_expected_payload = any(bool(data.get(key)) for key in expected_keys)
    if business_status not in {None, 0, "0"} and not has_expected_payload:
        raise DouyinRequestError("upstream_error", detail=_flatten_text(data)[:1000], status_code=status_code)
    return data


def classify_stored_task_error(value: Optional[str]) -> dict[str, Any]:
    """为历史 error_message 计算前端展示字段，无需数据库迁移。"""
    if not value:
        return {"error_code": None, "error_category": None, "error_action": None}
    if "安全校验未通过" in value:
        classified = DouyinRequestError("argus_blocked", detail=value)
    else:
        classified = classify_douyin_error(status_code=None, body=value)
    if not classified:
        return {"error_code": "task_error", "error_category": "task",
                "error_action": "请复制错误详情并查看活动日志。"}
    return {"error_code": classified.code, "error_category": classified.category,
            "error_action": classified.action}


def http_status_for_douyin_error(exc: DouyinRequestError) -> int:
    if exc.code in {"argus_blocked", "rate_limited"}:
        return 429
    if exc.code in {"browser_identity_missing", "cookie_invalid"}:
        return 401
    if exc.code == "content_unavailable":
        return 404
    if exc.code == "network_error":
        return 503
    return 502


def douyin_error_type_label(code: Optional[str]) -> str:
    return {
        "browser_identity_missing": "浏览器身份信息缺失",
        "argus_blocked": "抖音安全校验拦截",
        "rate_limited": "抖音请求频率受限",
        "cookie_invalid": "抖音登录状态失效",
    }.get(str(code or ""), "抖音请求保护")


def localize_douyin_reason(code: Optional[str], reason: Optional[str]) -> str:
    lowered = str(reason or "").lower()
    if code == "browser_identity_missing" or "uifid not found" in lowered:
        return "请求缺少或未识别 UIFID 浏览器身份标识，抖音安全校验拒绝了本次请求。"
    if code == "argus_blocked" or "argussecurityplugin" in lowered:
        return "抖音安全校验拒绝了本次请求。"
    if code == "rate_limited":
        return "抖音判定请求过于频繁，系统已暂停新的业务请求。"
    return str(reason or "抖音上游安全校验拒绝了本次请求。")
