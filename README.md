# 媒体下载管理系统

抖音媒体下载与作者管理服务，支持视频、图集下载，任务管理，作者订阅检查，以及 X/Twitter 下载链路。

当前文档按现状整理：

- 服务端部署使用 PostgreSQL，不再使用 SQLite。
- 运行时依赖 Redis、Celery、FastAPI。
- Web 界面已支持任务媒体预览、作者作品预览，以及从作者视图联动查看关联下载任务。

---

## 当前能力

### 抖音下载

- 支持单作品下载和作者主页批量下载。
- 支持视频与图集下载。
- 图集按单张图片拆分为独立下载任务。
- 支持暂停、恢复、重试、强制重试、删除、批量重试。

### 预览与管理

- 已完成视频任务支持本地预览。
- 已完成图片任务支持本地预览。
- 未完成图片任务支持使用作品原始地址预览。
- 作者管理支持查看该作者关联的全部作品预览。
- 作者作品中的已下载视频优先使用本地任务预览地址，避免浏览器直接加载源站视频失败。
- 作者作品视图支持查看视频、图集，并联动筛选该作者的下载任务。

### 自动化与运行控制

- 作者订阅自动检查新作品。
- 内置请求节流与限流保护配置。
- 应用启动时自动拉起 Celery Worker 与 Beat。
- 提供服务状态、日志、数据库配置、运行时参数等页面入口。

### 其他平台

- 包含 X/Twitter 下载链路与用户管理页面。

---

## 技术栈

- FastAPI
- SQLAlchemy 2.x
- PostgreSQL
- Redis
- Celery
- 原生静态页面前端

---

## 环境要求

### 服务端依赖

- Python 3.11 或 3.12
- PostgreSQL
- Redis

### 本地开发说明

当前仓库依赖版本在本地 Python 3.14 环境下存在兼容性问题，至少已确认 `sqlalchemy==2.0.25` 在 Python 3.14 下导入失败。

建议本地开发使用：

- Python 3.11
- 或 Python 3.12

---

## 环境变量

参考 [env.example.txt](env.example.txt)。核心配置如下：

```env
DEBUG=false

DOWNLOAD_DIR=/downloads

DB_TYPE=postgresql
DB_HOST=localhost
DB_PORT=5432
DB_USER=postgres
DB_PASSWORD=your_db_password
DB_NAME=douyin_service

REDIS_URL=redis://localhost:6379/0
REDIS_PASSWORD=your_redis_password

CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/0

MAX_CONCURRENT_DOWNLOADS=3
DEFAULT_CHECK_INTERVAL=21600
MIN_CHECK_INTERVAL=3600
AUTO_CHECK_ENABLED=true
REQUEST_DELAY=3.0
AUTHOR_CHECK_DELAY=30.0
```

说明：

- 数据库默认就是 PostgreSQL。
- 应用会基于 `DB_TYPE` 自动拼接同步与异步数据库连接。
- 抖音 Cookie 可通过 `.env` 提供，也可通过 Web 页面配置。

安全约定：

- README 和 `env.example.txt` 仅保留占位示例，不应提交真实数据库口令、Redis 口令、Cookie、内网地址或运维账号信息。
- 生产连接信息建议只保存在 `.env`、密钥管理系统或服务器侧配置中。

---

## 本地启动

### 1. 创建虚拟环境

推荐使用 Python 3.11 或 3.12：

```bash
python -m venv .venv
```

Windows:

```bash
.venv\Scripts\activate
```

Linux:

```bash
source .venv/bin/activate
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 准备配置

```bash
cp env.example.txt .env
```

根据实际环境填写 PostgreSQL、Redis、下载目录和 Cookie 配置。

### 4. 启动服务

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

或直接：

```bash
python main.py
```

说明：

- FastAPI 应用启动时会自动初始化数据库。
- 应用生命周期内会自动拉起 Worker 和 Beat。
- 首页默认返回前端页面，接口文档在 `/docs`。

---

## 服务器部署

项目已部署在服务器，推荐按“代码更新 + 依赖确认 + 环境变量确认 + 服务重启”的方式发布。

### 标准发布步骤

1. 备份 PostgreSQL 数据库。
2. 备份 `.env`、下载目录和日志目录。
3. 上传或拉取最新代码。
4. 安装或更新依赖。
5. 确认 PostgreSQL、Redis、下载目录配置正确。
6. 重启应用服务。
7. 打开 Web 页面和 `/docs` 做发布后验证。

### 如果你使用现有脚本

仓库里保留了 [start.sh](start.sh) 和 [stop.sh](stop.sh) 对应的 Linux 启停脚本，但脚本中的虚拟环境路径需要与你服务器实际环境保持一致。

在使用前请确认：

- 虚拟环境目录名是否为 `venv`。
- `uvicorn` 是否在该虚拟环境中可用。
- 服务器进程管理方式是否仍然采用脚本启动。

如果生产环境已经切换到 supervisor、systemd、宝塔、容器或其他方式，请以线上实际进程管理方案为准。

---

## 数据备份与恢复

### PostgreSQL 备份

```bash
PGPASSWORD="你的密码" pg_dump -h 127.0.0.1 -U postgres -d douyin_service -Fc -f backups/douyin_service_$(date +%Y%m%d_%H%M%S).dump
```

### PostgreSQL 恢复

```bash
PGPASSWORD="你的密码" pg_restore -h 127.0.0.1 -U postgres -d douyin_service --clean --if-exists backups/douyin_service_xxx.dump
```

### 其他需要保留的数据

- `.env`
- 下载目录
- `logs/`

不再存在 `douyin.db` 这类 SQLite 文件备份流程。

---

## 日志与排查

常用日志查看命令：

```bash
tail -f logs/download_tasks.log
tail -f logs/downloader.log
grep -i error logs/*.log
```

如果已配置系统级 logrotate，可继续使用 [logrotate.conf](logrotate.conf) 做轮转。

---

## API 概览

### 抖音任务

- `POST /api/tasks/download` 创建下载任务
- `GET /api/tasks/` 获取任务列表，支持分页、状态过滤、作者过滤
- `GET /api/tasks/{id}` 获取任务详情
- `GET /api/tasks/{id}/preview` 预览本地媒体文件
- `POST /api/tasks/{id}/pause` 暂停任务
- `POST /api/tasks/{id}/resume` 恢复任务
- `POST /api/tasks/{id}/retry` 重试任务
- `POST /api/tasks/{id}/force-retry` 强制重试任务
- `DELETE /api/tasks/{id}` 删除任务

### 抖音作者

- `POST /api/authors/` 添加作者
- `GET /api/authors/` 获取作者列表
- `GET /api/authors/{id}` 获取作者详情
- `GET /api/authors/{id}/works` 获取作者关联作品与预览信息
- `POST /api/authors/{id}/subscribe` 订阅作者
- `POST /api/authors/{id}/unsubscribe` 取消订阅
- `POST /api/authors/{id}/download` 下载作者作品
- `POST /api/authors/check-all` 检查所有订阅作者更新

### 系统与配置

- `GET /api/status` 获取系统状态
- `GET /api/config/cookie` 获取 Cookie 状态
- `POST /api/config/cookie` 更新 Cookie
- `GET /api/config/runtime` 获取运行时配置
- `POST /api/config/runtime` 更新运行时配置

完整接口文档：

- `/docs`
- `/redoc`

---

## 项目结构

```text
douyin_service/
├── app/
│   ├── api/              # API 路由
│   ├── core/             # 配置、Redis、进程管理
│   ├── models/           # ORM 与 Schema
│   ├── services/         # 下载与业务逻辑
│   └── tasks/            # Celery 任务
├── static/               # 前端页面
├── main.py               # FastAPI 入口
├── requirements.txt      # Python 依赖
├── env.example.txt       # 环境变量示例
├── start.sh              # Linux 启动脚本
├── stop.sh               # Linux 停止脚本
└── logrotate.conf        # 日志轮转配置
```

---

## 本地验证提示

当前依赖组合在 Python 3.14 下存在兼容性限制，至少已确认 `sqlalchemy==2.0.25` 无法在该版本下正常导入。

因此如果需要做本地完整启动验证，建议：

1. 使用 Python 3.11 或 3.12 创建虚拟环境。
2. 重新安装依赖后再执行应用启动、任务下载和页面预览联调。

如果只做文档修改、静态检查或前端结构调整，则不一定需要切换本地解释器。

---

## 发布验证清单

建议每次发布后至少检查：

1. 首页与 `/docs` 可以正常打开。
2. 任务列表分页、状态筛选、作者筛选可正常工作。
3. 已完成视频和图片任务可以打开本地预览。
4. 作者作品预览与“关联任务”联动功能正常。
5. Worker、Beat、Redis 和数据库状态正常。

---

最后更新：2026-04-28