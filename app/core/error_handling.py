"""统一的中文异常响应、排障编号和服务端错误日志。"""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


ERROR_LOG_PATH = Path(os.getenv("ERROR_LOG_PATH", "logs/application-error.log"))
logger = logging.getLogger("douyin_service.errors")

_SENSITIVE_PATTERN = re.compile(
    r"(?i)(password|passwd|pwd|cookie|authorization|token|secret|api[_-]?key)"
    r"(\s*[=:]\s*)([^\s,;]+)"
)
_URL_PASSWORD_PATTERN = re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^:/@\s]+:)([^@\s]+)(@)")

_DEFAULT_HTTP_MESSAGES = {
    400: "请求参数不正确",
    401: "身份验证失败",
    403: "当前操作没有权限",
    404: "请求的资源不存在",
    405: "当前请求方式不受支持",
    409: "当前数据状态存在冲突",
    413: "上传或请求内容过大",
    415: "请求内容格式不受支持",
    422: "请求参数校验失败",
    429: "请求过于频繁",
    500: "服务器处理请求时发生异常",
    502: "上游服务响应异常",
    503: "服务暂时不可用",
    504: "上游服务响应超时",
}

_ENGLISH_FRAMEWORK_MESSAGES = {
    "Internal Server Error": "服务器处理请求时发生异常",
    "Not Found": "请求的资源不存在",
    "Method Not Allowed": "当前请求方式不受支持",
    "Unauthorized": "身份验证失败",
    "Forbidden": "当前操作没有权限",
    "Bad Request": "请求参数不正确",
}


class RedactingFormatter(logging.Formatter):
    """在保留异常堆栈的同时清理其中的敏感信息。"""

    def formatException(self, exc_info) -> str:
        return sanitize_text(super().formatException(exc_info), limit=50000)


def configure_error_logging() -> None:
    """配置独立的滚动错误日志；文件不可写时仍保留控制台日志。"""
    if logger.handlers:
        return

    logger.setLevel(logging.INFO)
    formatter = RedactingFormatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    try:
        ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            ERROR_LOG_PATH,
            maxBytes=10 * 1024 * 1024,
            backupCount=10,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning("无法创建错误日志文件，将仅输出到控制台：%s", sanitize_text(str(exc)))

    logger.propagate = False


def sanitize_text(value: Any, limit: int = 1000) -> str:
    """清理可能包含在异常消息中的密码、Cookie、令牌和连接串口令。"""
    text = str(value or "")
    text = _SENSITIVE_PATTERN.sub(r"\1\2***", text)
    text = _URL_PASSWORD_PATTERN.sub(r"\1***\3", text)
    return text[:limit]


def _error_id(request: Request) -> str:
    existing = getattr(request.state, "error_id", "")
    if existing:
        return existing
    error_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
    request.state.error_id = error_id
    return error_id


def _request_context(request: Request) -> str:
    query_keys = sorted(set(request.query_params.keys()))
    client = request.client.host if request.client else "未知"
    return (
        f"请求方式={request.method} 路径={request.url.path} "
        f"查询字段={query_keys or '无'} 客户端={client}"
    )


def _suggestion(status_code: int, exc: Exception | None = None) -> str:
    text = f"{type(exc).__name__}: {exc}" if exc else ""
    lowered = text.lower()
    if "database" in lowered or "sqlalchemy" in lowered or "psycopg" in lowered:
        return "请检查数据库服务、连接配置和账号权限，并用错误编号查询服务端错误日志。"
    if "redis" in lowered:
        return "请检查 Redis 服务、连接地址和密码，并确认网络连通。"
    if isinstance(exc, PermissionError):
        return "请检查服务账号对下载目录、日志目录或目标文件的读写权限。"
    if isinstance(exc, FileNotFoundError):
        return "请检查配置的文件路径、下载目录以及所需程序是否存在。"
    if isinstance(exc, (TimeoutError, ConnectionError)) or "timeout" in lowered:
        return "请检查网络和上游服务状态，稍后重试；若持续失败，请用错误编号查询日志。"

    suggestions = {
        400: "请核对输入内容和必填项后重试。",
        401: "请更新账号凭据或 Cookie 后重试。",
        403: "请检查账号权限、Cookie 状态或文件访问权限。",
        404: "请刷新页面确认数据是否仍然存在。",
        405: "请刷新页面后重试；若仍失败，请确认前后端版本一致。",
        409: "请刷新页面获取最新状态，确认后再重试。",
        422: "请根据参数详情修正输入内容后重试。",
        429: "请降低操作频率，等待一段时间后重试。",
        500: "请复制错误编号，在服务端错误日志中检索；日志包含完整堆栈和请求位置。",
        502: "请检查网络、抖音/X 上游接口或反向代理状态，稍后重试。",
        503: "请检查数据库、Redis 和后台任务进程是否正常运行。",
        504: "请检查网络和上游服务状态，稍后重试。",
    }
    return suggestions.get(status_code, "请稍后重试；若问题持续，请复制错误编号并查询服务端日志。")


def _message_from_detail(detail: Any, status_code: int) -> str:
    if isinstance(detail, str):
        translated = _ENGLISH_FRAMEWORK_MESSAGES.get(detail, detail)
        return sanitize_text(translated, 500) or _DEFAULT_HTTP_MESSAGES.get(status_code, "请求处理失败")
    if isinstance(detail, dict):
        candidate = detail.get("message") or detail.get("detail")
        if candidate:
            return sanitize_text(candidate, 500)
    return _DEFAULT_HTTP_MESSAGES.get(status_code, "请求处理失败")


def _payload(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    suggestion: str,
    error_id: str,
    details: Any = None,
    error_type: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": False,
        "code": code,
        "message": message,
        "detail": message,
        "suggestion": suggestion,
        "error_id": error_id,
        "status_code": status_code,
        "path": request.url.path,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if error_type:
        result["error_type"] = error_type
    if details is not None:
        result["details"] = details
    return result


def _response(payload: dict[str, Any], status_code: int, error_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=payload,
        headers={"X-Error-ID": error_id, "X-Request-ID": error_id},
    )


def register_exception_handlers(app: FastAPI) -> None:
    configure_error_logging()

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        error_id = _error_id(request)
        details = [
            {
                "field": ".".join(str(part) for part in error.get("loc", []) if part != "body"),
                "message": _translate_validation_message(error.get("msg", "")),
                "type": error.get("type", "validation_error"),
            }
            for error in exc.errors()
        ]
        logger.warning(
            "参数校验失败 | 错误编号=%s | %s | 详情=%s",
            error_id,
            _request_context(request),
            sanitize_text(details),
        )
        payload = _payload(
            request,
            status_code=422,
            code="REQUEST_VALIDATION_ERROR",
            message="请求参数校验失败",
            suggestion=_suggestion(422),
            error_id=error_id,
            details=details,
            error_type=type(exc).__name__,
        )
        return _response(payload, 422, error_id)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        error_id = _error_id(request)
        status_code = exc.status_code
        message = _message_from_detail(exc.detail, status_code)
        log_method = logger.error if status_code >= 500 else logger.warning
        log_method(
            "HTTP 请求失败 | 错误编号=%s | 状态码=%s | %s | 原因=%s",
            error_id,
            status_code,
            _request_context(request),
            sanitize_text(exc.detail),
        )
        structured_detail = exc.detail if isinstance(exc.detail, dict) else None
        payload = _payload(
            request,
            status_code=status_code,
            code=(structured_detail or {}).get("code") or f"HTTP_{status_code}",
            message=message,
            suggestion=(structured_detail or {}).get("action") or _suggestion(status_code, exc),
            error_id=error_id,
            details=structured_detail,
            error_type=type(exc).__name__,
        )
        return _response(payload, status_code, error_id)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        error_id = _error_id(request)
        logger.error(
            "未处理的服务器异常 | 错误编号=%s | %s | 异常类型=%s | 异常摘要=%s",
            error_id,
            _request_context(request),
            type(exc).__name__,
            sanitize_text(exc),
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        payload = _payload(
            request,
            status_code=500,
            code="INTERNAL_SERVER_ERROR",
            message=_classify_unhandled_message(exc),
            suggestion=_suggestion(500, exc),
            error_id=error_id,
            error_type=type(exc).__name__,
        )
        return _response(payload, 500, error_id)


def _classify_unhandled_message(exc: Exception) -> str:
    name_and_message = f"{type(exc).__name__}: {exc}".lower()
    if "database" in name_and_message or "sqlalchemy" in name_and_message or "psycopg" in name_and_message:
        return "数据库操作发生异常"
    if "redis" in name_and_message:
        return "Redis 操作发生异常"
    if isinstance(exc, PermissionError):
        return "文件或目录访问权限不足"
    if isinstance(exc, FileNotFoundError):
        return "服务所需的文件或目录不存在"
    if isinstance(exc, (TimeoutError, ConnectionError)) or "timeout" in name_and_message:
        return "网络连接或上游服务响应超时"
    return "服务器处理请求时发生异常"


def _translate_validation_message(message: str) -> str:
    lowered = message.lower()
    translations = (
        ("field required", "该字段为必填项"),
        ("input should be a valid integer", "请输入有效的整数"),
        ("input should be a valid number", "请输入有效的数字"),
        ("input should be a valid boolean", "请输入有效的布尔值"),
        ("input should be a valid string", "请输入有效的文本"),
        ("input should be a valid url", "请输入有效的网址"),
        ("value is not a valid", "输入值格式不正确"),
    )
    for source, target in translations:
        if source in lowered:
            return target
    return sanitize_text(message, 300) or "输入内容不符合要求"
