"""管理端鉴权与动态 CORS 中间件。"""

from __future__ import annotations

import asyncio
import secrets
from typing import Callable, TypeVar

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute, iter_route_contexts
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Match

from app.core.env_config import parse_cors_origins, read_env_file


AUTH_COOKIE_NAME = "douyin_admin_media"
_PUBLIC_PAGE_PATHS = {"/", "/legacy", "/docs", "/redoc", "/docs/oauth2-redirect"}
_Endpoint = TypeVar("_Endpoint", bound=Callable)
_MEDIA_COOKIE_ATTRIBUTE = "_allows_media_cookie"
_REQUEST_CONFIG_KEY = "douyin.security_configuration"


def media_cookie(endpoint: _Endpoint) -> _Endpoint:
    """在只读媒体端点上声明 Cookie 权限，不包装函数或改变接口签名。"""
    setattr(endpoint, _MEDIA_COOKIE_ATTRIBUTE, True)
    return endpoint


def register_media_cookie_routes(app) -> None:
    """从实际路由生成权限；任何写方法上的误用都会使注册失败。"""
    routes = []
    for route in iter_route_contexts(app.router.routes):
        allowed = isinstance(route.original_route, APIRoute) and getattr(route.endpoint, _MEDIA_COOKIE_ATTRIBUTE, False)
        if allowed and (not route.methods or not route.methods <= {"GET", "HEAD"}):
            raise ValueError(f"媒体 Cookie 仅允许只读路由：{route.path}")
        routes.append((route.matches, allowed))
    app.state.media_cookie_routes = tuple(routes)


async def _request_configuration(request: Request) -> dict[str, str]:
    """同一请求的鉴权与 CORS 共用一次读取；下一次请求重新获取持久化值。"""
    if _REQUEST_CONFIG_KEY not in request.scope:
        request.scope[_REQUEST_CONFIG_KEY] = await asyncio.to_thread(read_env_file)
    return request.scope[_REQUEST_CONFIG_KEY]


def _unauthorized(message: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "success": False,
            "code": code,
            "message": message,
            "detail": message,
            "suggestion": "请在登录提示中输入当前管理 Token。",
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


def _bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return ""
    return token.strip()


def _tokens_equal(left: str, right: str) -> bool:
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _is_public_page_or_asset(path: str) -> bool:
    return path in _PUBLIC_PAGE_PATHS or path == "/static" or path.startswith("/static/")


def _is_media_request(request: Request) -> bool:
    if request.method not in {"GET", "HEAD"}:
        return False
    # 保持实际路由的匹配顺序；后面的媒体声明不能给前面的普通接口授权。
    for matches, allowed in getattr(request.app.state, "media_cookie_routes", ()):
        match, _ = matches(request.scope)
        if match == Match.FULL:
            return allowed
    return False


class AdminAuthMiddleware(BaseHTTPMiddleware):
    """每次请求读取网页持久化 Token，使修改在下一次调用立即生效。"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in {"/api/health", "/api/ready"} or _is_public_page_or_asset(path):
            return await call_next(request)

        values = await _request_configuration(request)
        admin_token = str(values.get("ADMIN_TOKEN") or "").strip()
        if not admin_token:
            if path.startswith("/api/bootstrap/"):
                return await call_next(request)
            return _unauthorized(
                "管理员 Token 尚未配置，当前仅开放初始化配置流程",
                "ADMIN_TOKEN_NOT_CONFIGURED",
            )

        supplied_token = _bearer_token(request)
        bearer_authenticated = bool(supplied_token) and _tokens_equal(supplied_token, admin_token)
        media_cookie = request.cookies.get(AUTH_COOKIE_NAME, "") if _is_media_request(request) else ""
        media_authenticated = bool(media_cookie) and _tokens_equal(media_cookie, admin_token)
        if not bearer_authenticated and not media_authenticated:
            return _unauthorized("管理 Token 缺失或不正确", "ADMIN_TOKEN_INVALID")

        response = await call_next(request)
        if bearer_authenticated and path != "/api/auth/logout" and request.cookies.get(AUTH_COOKIE_NAME) != admin_token:
            # img/video/a 元素不能附加 Authorization；该 HttpOnly 严格同站
            # Cookie 仅被上面的只读媒体路由接受，不可用于业务 API 或写操作。
            response.set_cookie(
                AUTH_COOKIE_NAME,
                admin_token,
                httponly=True,
                secure=request.url.scheme == "https",
                samesite="strict",
                path="/api",
            )
        return response


def _append_vary(response: Response, value: str) -> None:
    existing = response.headers.get("Vary", "")
    values = [item.strip() for item in existing.split(",") if item.strip()]
    if value not in values:
        values.append(value)
    response.headers["Vary"] = ", ".join(values)


class DynamicCORSMiddleware(BaseHTTPMiddleware):
    """从网页持久化值动态读取显式来源，不缓存为启动快照。"""

    _ALLOWED_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    _ALLOWED_HEADERS = "Authorization, Content-Type, Cache-Control, Pragma"

    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin", "").rstrip("/")
        try:
            values = await _request_configuration(request) if origin else {}
            allowed_origins = set(parse_cors_origins(values.get("CORS_ALLOWED_ORIGINS", "")))
        except ValueError:
            # 手工损坏该配置时按“不开放跨域”降级，避免管理页本身失联。
            allowed_origins = set()
        origin_allowed = bool(origin) and origin in allowed_origins

        if request.method == "OPTIONS" and request.headers.get("access-control-request-method"):
            if not origin_allowed:
                return JSONResponse(
                    status_code=403,
                    content={
                        "success": False,
                        "code": "CORS_ORIGIN_DENIED",
                        "message": "当前网页来源未被允许访问管理接口",
                        "detail": "当前网页来源未被允许访问管理接口",
                    },
                )
            response: Response = Response(status_code=204)
        else:
            response = await call_next(request)

        if origin_allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Methods"] = self._ALLOWED_METHODS
            response.headers["Access-Control-Allow-Headers"] = self._ALLOWED_HEADERS
            response.headers["Access-Control-Expose-Headers"] = "X-Error-ID, X-Request-ID"
            _append_vary(response, "Origin")
        return response
