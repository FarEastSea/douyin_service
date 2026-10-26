"""
抖音下载管理系统 - 主应用入口

为什么这样设计：
1. 使用 FastAPI 的 lifespan 上下文管理器处理启动/关闭事件
2. 注册所有 API 路由
3. 配置管理 Token 鉴权与显式 CORS 来源
4. 提供静态文件服务（用于前端页面）
"""

from app.core.runtime_identity import deployment_startup_blocked, get_runtime_identity
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
import os
import asyncio
import logging
import time
import traceback

from app.api import bootstrap
from app.core.diagnostics import get_runtime_errors
from app.core.env_config import validate_env
from app.core.error_handling import register_exception_handlers
from app.core.security import AdminAuthMiddleware, DynamicCORSMiddleware


from app.core.config_transaction import recover_config_change, journal_path
from app.core.queue_configuration import recover_queue_change
from app.core.config import settings
from app.core.process_manager import process_manager
import importlib


async def activate_runtime(app: FastAPI) -> bool:
    """初始化和故障修复使用同一入口，修复后自动开放业务路由。"""
    # 外部进程管理器即使在发布期间启动了 Web，也不能在迁移/恢复期间写库或修文件。
    if deployment_startup_blocked():
        app.state.degraded_mode = True
        app.state.bootstrap_status = {"ready": False, "missing": [], "errors": [{
            "key": "DEPLOYMENT_MAINTENANCE", "label": "正在发布", "group": "应用",
            "message": "正在切换运行环境，发布完成后自动恢复。",
        }]}
        return False
    status = await asyncio.to_thread(validate_env)
    app.state.bootstrap_status = status
    if status.get("missing"):
        app.state.degraded_mode = True
        return False
    stage = "configuration_recovery"
    try:
        if journal_path().exists():
            await asyncio.to_thread(recover_config_change)
        await asyncio.to_thread(recover_queue_change)
        from app.models.database import init_db
        from app.core.config import ensure_download_dir
        from app.core import redis_client
        stage = "database_init"
        await init_db()
        stage = "download_directory"
        await asyncio.to_thread(ensure_download_dir)
        stage = "redis_connection"
        if not await asyncio.to_thread(redis_client.check_connection):
            raise RuntimeError("Redis 尚未恢复")
        from app.services.download_publication import recover_media_publications
        from app.services.media_deletion import recover_media_deletions
        from app.services.douyin_account import migrate_legacy_account_sync, recover_legacy_signature_isolation_sync
        stage = "media_publication_recovery"
        await asyncio.to_thread(recover_media_publications)
        stage = "media_deletion_recovery"
        await asyncio.to_thread(recover_media_deletions)
        stage = "account_migration"
        await asyncio.to_thread(migrate_legacy_account_sync)
        stage = "signature_recovery"
        await asyncio.to_thread(recover_legacy_signature_isolation_sync)
        stage = "route_registration"
        if not getattr(app.state, "runtime_routes_loaded", False):
            for name in ("tasks", "authors", "system", "x_tasks", "works", "platforms", "platform_downloads", "operations"):
                module = importlib.import_module("app.api." + name)
                app.include_router(module.router, prefix="/api")
            app.state.runtime_routes_loaded = True
            app.openapi_schema = None
        stage = "process_reconcile"
        await asyncio.to_thread(process_manager.reconcile)
        app.state.degraded_mode = False
        app.state.bootstrap_status = {**status, "ready": True, "missing": [], "errors": []}
        from pathlib import Path
        initialized = Path(__file__).resolve().parent / ".runtime" / "initialized"
        initialized.parent.mkdir(parents=True, exist_ok=True)
        initialized.touch(exist_ok=True)
        return True
    except Exception as exc:
        from app.core.error_handling import sanitize_text
        # 只持久化程序阶段和异常类型，不把配置、业务参数或异常原文写入诊断清单。
        diagnostic = {"stage": stage, "error_type": type(exc).__name__,
                      "checked_at": time.time(), "runtime": get_runtime_identity()}
        diagnostic["frames"] = [{"file": os.path.basename(frame.filename), "line": frame.lineno,
                                 "function": frame.name}
                                for frame in traceback.extract_tb(exc.__traceback__)[-8:]]
        logging.getLogger(__name__).error("APP_STARTUP failed: stage=%s type=%s", stage, type(exc).__name__)
        try:
            from app.core.config_transaction import atomic_json
            from pathlib import Path
            atomic_json(Path(__file__).resolve().parent / ".runtime" / "startup-failure.json", diagnostic)
        except OSError:
            pass
        app.state.degraded_mode = True
        app.state.bootstrap_status = {**status, "ready": False, "errors": [{
            "key": "APP_STARTUP", "label": "运行环境", "group": "应用",
            "stage": stage, "error_type": type(exc).__name__,
            "message": sanitize_text(str(exc), limit=300),
        }]}
        return False


async def supervise_runtime(app: FastAPI) -> None:
    while True:
        try:
            if app.state.degraded_mode:
                await activate_runtime(app)
            else:
                await asyncio.to_thread(process_manager.reconcile)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 受管状态保留；短暂故障不终止后续恢复周期。
            pass
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.runtime_identity = get_runtime_identity()
    app.state.degraded_mode = True
    app.state.runtime_routes_loaded = False
    await activate_runtime(app)
    from app.core.queue_configuration import resume_pending_changes
    runtime_supervisor = asyncio.create_task(supervise_runtime(app))
    queue_supervisor = asyncio.create_task(resume_pending_changes())
    try:
        yield
    finally:
        for task in (runtime_supervisor, queue_supervisor):
            task.cancel()
        await asyncio.gather(runtime_supervisor, queue_supervisor, return_exceptions=True)
        # Celery 属于项目而不是某一代 Web worker。重载由新 Web 接管；
        # 项目停止/部署通过 stop.sh 显式停止并核验这些进程。


# 创建 FastAPI 应用
app = FastAPI(
    title="媒体下载管理系统",
    description="""
## 功能特性

- 🎬 抖音视频/图集下载
- ⏸️ 支持暂停/恢复（断点续传）
- 📊 实时下载进度
- 📚 下载历史记录
- 🔔 作者订阅，自动检查新作品
- 🐦 X/Twitter 媒体下载（gallery-dl）
- 🌍 TikTok 用户主页及单条作品下载（gallery-dl）
- 🔴 微博用户主页及单条动态下载（gallery-dl）
- 📺 B站 UP 主空间、视频、分P及含视频动态下载（yt-dlp）
- 📕 小红书单条图文、视频与实况笔记下载（作者主页批量采集已搁置）

## API 文档

- Swagger UI: /docs
- ReDoc: /redoc
    """,
    version="2.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

app.state.bootstrap_status = {"ready": False, "missing": [], "errors": []}
app.state.degraded_mode = True
register_exception_handlers(app)
# 禁用 API 响应缓存，避免页面显示过时数据
@app.middleware("http")
async def no_cache_api_responses(request: Request, call_next):
    from pathlib import Path
    maintenance = Path(__file__).resolve().parent / ".runtime" / "deployment-maintenance"
    if maintenance.exists() and request.method not in {"GET", "HEAD", "OPTIONS"}:
        return JSONResponse(status_code=503, content={"success": False, "detail": "系统正在发布，请稍后重试"},
                            headers={"Retry-After": "10"})
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# 鉴权读取最新网页持久化 Token；CORS 位于外层，确保 401 也带正确的
# 显式来源响应头。两者都不把配置固化为启动时快照。
app.add_middleware(AdminAuthMiddleware)
app.add_middleware(DynamicCORSMiddleware)


@app.get("/api/health", include_in_schema=False)
async def public_health():
    """存活检查：只证明 Web 进程仍能响应。"""
    return {
        "status": "healthy",
        "alive": True,
        "bootstrap_mode": bool(app.state.degraded_mode),
        "runtime": get_runtime_identity(),
    }


@app.get("/api/ready", include_in_schema=False)
async def public_readiness():
    """就绪检查：发布验收必须等待所有核心依赖可用。"""
    from app.core.health import build_readiness

    payload = await build_readiness(degraded_mode=bool(app.state.degraded_mode))
    payload["runtime"] = get_runtime_identity()
    return JSONResponse(status_code=200 if payload["ready"] else 503, content=payload)

app.include_router(bootstrap.router, prefix="/api")

# 静态文件服务（前端页面）
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/docs", include_in_schema=False)
async def swagger_docs():
    """公开文档外壳；OpenAPI 描述和实际请求仍需管理 Token。"""
    response = get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=f"{app.title} - API 文档",
        oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
    )
    html = response.body.decode("utf-8")
    auth_interceptors = """
        requestInterceptor: (request) => {
            let token = window.localStorage.getItem('douyinAdminToken') || '';
            if (!token) {
                token = window.prompt('请输入管理 Token 以加载受保护的 API 文档：') || '';
                if (token) window.localStorage.setItem('douyinAdminToken', token.trim());
            }
            if (token) request.headers.Authorization = `Bearer ${token.trim()}`;
            return request;
        },
        responseInterceptor: (response) => {
            if (response.status === 401) {
                window.localStorage.removeItem('douyinAdminToken');
            }
            return response;
        },
    """
    html = html.replace(
        "const ui = SwaggerUIBundle({",
        f"const ui = SwaggerUIBundle({{{auth_interceptors}",
        1,
    )
    return HTMLResponse(html)


@app.get("/docs/oauth2-redirect", include_in_schema=False)
async def swagger_ui_redirect():
    return get_swagger_ui_oauth2_redirect_html()


@app.get("/redoc", include_in_schema=False)
async def redoc_redirect():
    return RedirectResponse(url="/docs", status_code=307)


@app.get("/")
async def root():
    """根路径 - 优先返回 Vue 3 生产界面。"""
    index_path = os.path.join(static_dir, "app", "index.html")
    if not os.path.exists(index_path):
        index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    
    return {
        "name": "媒体下载管理系统",
        "version": "2.0.0",
        "docs": "/docs",
        "api": "/api"
    }


@app.get("/legacy", include_in_schema=False)
async def legacy_frontend():
    """新界面验收期间保留的旧版回退入口。"""
    legacy_path = os.path.join(static_dir, "legacy.html")
    if not os.path.exists(legacy_path):
        legacy_path = os.path.join(static_dir, "index.html")
    if not os.path.exists(legacy_path):
        return RedirectResponse(url="/", status_code=307)
    return FileResponse(legacy_path)


# 用于宝塔面板运行
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
