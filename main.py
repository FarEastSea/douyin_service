"""
抖音下载管理系统 - 主应用入口

为什么这样设计：
1. 使用 FastAPI 的 lifespan 上下文管理器处理启动/关闭事件
2. 注册所有 API 路由
3. 配置管理 Token 鉴权与显式 CORS 来源
4. 提供静态文件服务（用于前端页面）
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
import os
import asyncio

from app.api import bootstrap
from app.core.diagnostics import get_runtime_errors
from app.core.env_config import validate_env
from app.core.error_handling import register_exception_handlers
from app.core.security import AdminAuthMiddleware, DynamicCORSMiddleware


BOOTSTRAP_STATUS = validate_env()
BOOTSTRAP_MODE = not BOOTSTRAP_STATUS["ready"]
settings = None

if not BOOTSTRAP_MODE:
    try:
        from app.models.database import init_db
        from app.core.config import settings, ensure_download_dir
        from app.core.process_manager import process_manager
        from app.api import tasks, authors, system, x_tasks, works, platforms, platform_downloads
        from app.services.douyin_account import migrate_legacy_account_sync
        config_errors = get_runtime_errors()
        if config_errors:
            BOOTSTRAP_MODE = True
            BOOTSTRAP_STATUS = {
                **BOOTSTRAP_STATUS,
                "ready": False,
                "errors": [
                    *BOOTSTRAP_STATUS.get("errors", []),
                    *config_errors,
                ],
            }
    except Exception as e:
        BOOTSTRAP_MODE = True
        BOOTSTRAP_STATUS = {
            **BOOTSTRAP_STATUS,
            "ready": False,
            "errors": [
                *BOOTSTRAP_STATUS.get("errors", []),
                {
                    "key": "APP_STARTUP",
                    "label": "应用启动",
                    "group": "应用",
                    "message": f"{type(e).__name__}: {str(e)[:300]}",
                },
            ],
        }


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    if BOOTSTRAP_MODE:
        missing = ", ".join(item["key"] for item in BOOTSTRAP_STATUS["missing"])
        print(f"配置未完成，进入初始化模式。缺失配置: {missing}")
        yield
        return

    # 启动时执行
    print("🚀 启动媒体下载管理系统...")
    
    # 初始化数据库
    try:
        await init_db()
        migrated_account = await asyncio.to_thread(migrate_legacy_account_sync)
        print("✅ 数据库初始化完成")
        if migrated_account:
            print("✅ 旧抖音 Cookie 已迁移到加密账号档案")
    except Exception as e:
        BOOTSTRAP_STATUS["ready"] = False
        BOOTSTRAP_STATUS.setdefault("errors", []).append({
            "key": "DATABASE_INIT",
            "label": "数据库初始化",
            "group": "数据库",
            "message": f"{type(e).__name__}: {str(e)[:300]}",
        })
        app.state.degraded_mode = True
        app.state.bootstrap_status = BOOTSTRAP_STATUS
        print(f"❌ 数据库初始化失败，进入配置维护模式: {e}")
        yield
        return
    
    # 下载目录属于可在维护页修复的配置。权限或路径错误不能让 Web
    # worker 退出，否则用户将失去再次进入维护模式的机会。
    try:
        download_dir = ensure_download_dir()
        print(f"✅ 下载目录: {download_dir}")
    except Exception as e:
        BOOTSTRAP_STATUS["ready"] = False
        BOOTSTRAP_STATUS.setdefault("errors", []).append({
            "key": "DOWNLOAD_DIRECTORY",
            "label": "下载目录",
            "group": "下载目录",
            "message": f"{type(e).__name__}: {str(e)[:300]}",
        })
        app.state.degraded_mode = True
        app.state.bootstrap_status = BOOTSTRAP_STATUS
        print(f"❌ 下载目录初始化失败，进入配置维护模式: {e}")
        yield
        return
    
    # 检查 Redis 连接
    from app.core import redis_client as rc
    if rc.check_connection():
        print("✅ Redis 连接正常")
        rc.append_activity_log("info", "system", "系统启动", f"下载目录={download_dir}")
    else:
        print("❌ Redis 连接失败！活动日志和进度追踪将不可用")
    
    # 自动启动 Celery Worker 和 Beat
    concurrency = settings.MAX_CONCURRENT_DOWNLOADS
    worker_result = process_manager.start_worker(concurrency)
    print(f"✅ {worker_result['message']}")
    beat_result = process_manager.start_beat()
    print(f"✅ {beat_result['message']}")
    
    yield
    
    # 关闭时执行
    print("👋 关闭媒体下载管理系统...")
    process_manager.shutdown_all()
    print("✅ Celery 进程已停止")


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
- 🌍 TikTok 用户主页媒体下载（gallery-dl）
- 🔴 微博用户主页媒体下载（gallery-dl）

## API 文档

- Swagger UI: /docs
- ReDoc: /redoc
    """,
    version="2.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

app.state.bootstrap_status = BOOTSTRAP_STATUS
app.state.degraded_mode = BOOTSTRAP_MODE
register_exception_handlers(app)
# 禁用 API 响应缓存，避免页面显示过时数据
@app.middleware("http")
async def no_cache_api_responses(request: Request, call_next):
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
    }


@app.get("/api/ready", include_in_schema=False)
async def public_readiness():
    """就绪检查：发布验收必须等待所有核心依赖可用。"""
    from app.core.health import build_readiness

    payload = await build_readiness(degraded_mode=bool(app.state.degraded_mode))
    return JSONResponse(status_code=200 if payload["ready"] else 503, content=payload)

app.include_router(bootstrap.router, prefix="/api")

if not BOOTSTRAP_MODE:
    # 注册 API 路由
    app.include_router(tasks.router, prefix="/api")
    app.include_router(authors.router, prefix="/api")
    app.include_router(system.router, prefix="/api")
    app.include_router(x_tasks.router, prefix="/api")
    app.include_router(works.router, prefix="/api")
    app.include_router(platforms.router, prefix="/api")
    app.include_router(platform_downloads.router, prefix="/api")


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
