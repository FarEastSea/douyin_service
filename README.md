# 媒体下载管理系统

媒体下载管理系统是一个面向日常下载运营场景的管理服务，提供抖音作品下载、作者订阅巡检、任务管理、媒体预览，以及 X/Twitter 下载链路。项目同时提供 Web 管理界面和 API，适合用于持续下载、集中管理和简单排查。

## 功能特性

- 支持抖音单作品下载与作者主页批量下载。
- 支持视频与图集下载，图集可拆分为独立图片任务。
- 支持任务暂停、恢复、重试、强制重试、删除和批量重试。
- 支持已完成视频和图片的本地预览，未完成图片可回退使用源地址预览。
- 支持作者订阅、自动巡检新作品，以及从作者视图联动查看相关任务。
- 提供 X/Twitter 下载链路与配套管理能力。

## 技术栈

- FastAPI
- SQLAlchemy 2.x
- PostgreSQL
- Redis
- Celery
- Web 管理界面

## 运行环境

- Python 3.11 或 3.12
- PostgreSQL
- Redis

当前部署环境实际使用 PostgreSQL。代码层保留了 MySQL 支持，但不是当前项目的实际使用形态。

## 快速开始

### 安装依赖

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

```bash
pip install -r requirements.txt
```

### 启动服务

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

也可以直接运行：

```bash
python main.py
```

启动后可访问：

- 首页：/
- Swagger 文档：/docs
- ReDoc：/redoc

应用启动时会自动初始化数据库，并自动拉起 Celery Worker 和 Beat。

### 首次使用

首次启动后，建议先通过 Web 管理页面完成业务侧配置，例如抖音 Cookie、自动巡检开关、巡检间隔、下载超时和重试参数。数据库连接、Redis 连接和下载目录这类基础运行参数仍应由部署环境提供。

## 简要部署指引

常见部署方式如下：

1. 准备 Python 3.11 或 3.12、PostgreSQL 和 Redis。
2. 安装项目依赖并提供基础运行参数。
3. 使用 uvicorn、systemd、supervisor、容器或现有进程管理方式启动服务。
4. 通过反向代理暴露 Web 管理页面和 API。
5. 首次进入管理页面后完成 Cookie 和运行参数配置。

如果当前环境仍沿用仓库脚本，可参考 [start.sh](start.sh) 和 [stop.sh](stop.sh)。这两个脚本适用于 Linux 环境，且默认由应用自身管理 Worker 与 Beat。

## 开发说明

当前依赖组合在 Python 3.14 下存在兼容性问题，已确认 sqlalchemy==2.0.25 在该版本下无法正常导入。本地开发和调试建议统一使用 Python 3.11 或 3.12。
