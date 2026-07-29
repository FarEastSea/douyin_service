"""
抖音下载管理系统 - 主应用入口

为什么这样设计：
1. 使用 FastAPI 的 lifespan 上下文管理器处理启动/关闭事件
2. 注册所有 API 路由
3. 配置 CORS 支持前端跨域访问
4. 提供静态文件服务（用于前端页面）
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os

from app.api import bootstrap
from app.core.env_config import validate_env
from app.core.error_handling import register_exception_handlers


BOOTSTRAP_STATUS = validate_env()
BOOTSTRAP_MODE = not BOOTSTRAP_STATUS["ready"]
settings = None

if not BOOTSTRAP_MODE:
    try:
        from app.models.database import init_db
        from app.core.config import settings, ensure_download_dir
        from app.core.process_manager import process_manager
        from app.api import tasks, authors, system, x_tasks, works
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
        print("✅ 数据库初始化完成")
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
    
    # 确保下载目录存在
    download_dir = ensure_download_dir()
    print(f"✅ 下载目录: {download_dir}")
    
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
    title=settings.APP_NAME if settings is not None else "媒体下载管理系统",
    description="""
## 功能特性

- 🎬 抖音视频/图集下载
- ⏸️ 支持暂停/恢复（断点续传）
- 📊 实时下载进度
- 📚 下载历史记录
- 🔔 作者订阅，自动检查新作品
- 🐦 X/Twitter 媒体下载（gallery-dl）

## API 文档

- Swagger UI: /docs
- ReDoc: /redoc
    """,
    version="2.0.0",
    lifespan=lifespan
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

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应限制具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(bootstrap.router, prefix="/api")

if not BOOTSTRAP_MODE:
    # 注册 API 路由
    app.include_router(tasks.router, prefix="/api")
    app.include_router(authors.router, prefix="/api")
    app.include_router(system.router, prefix="/api")
    app.include_router(x_tasks.router, prefix="/api")
    app.include_router(works.router, prefix="/api")


# 静态文件服务（前端页面）
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    """根路径 - 返回前端页面或 API 信息"""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    
    return {
        "name": settings.APP_NAME if settings is not None else "媒体下载管理系统",
        "version": "2.0.0",
        "docs": "/docs",
        "api": "/api"
    }


# 用于宝塔面板运行
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False if BOOTSTRAP_MODE or settings is None else settings.DEBUG
    )
