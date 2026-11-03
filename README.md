# 媒体下载管理系统

通过一个 Web 控制台管理媒体下载、作者订阅与本地媒体库。

![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)
![Vue](https://img.shields.io/badge/Console-Vue%203-4FC08D?style=flat-square&logo=vuedotjs&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/Database-PostgreSQL-4169E1?style=flat-square&logo=postgresql&logoColor=white)
![Redis](https://img.shields.io/badge/Queue-Redis%20%2B%20Celery-DC382D?style=flat-square&logo=redis&logoColor=white)
[![License](https://img.shields.io/badge/License-AGPL--3.0-blue?style=flat-square)](LICENSE)

[快速开始](#快速开始) · [平台使用](docs/platforms.md) · [部署指南](docs/deployment.md) · [参与开发](CONTRIBUTING.md) · [更新记录](CHANGELOG.md)

## 可以做什么

- **集中下载**：提交作品或作者链接，统一查看进度、暂停、重试和取消。
- **持续关注**：订阅作者、检查更新，按平台能力补采历史内容。
- **管理媒体**：浏览本地图片与视频，在任务、作品和作者之间跳转。
- **网页配置**：在设置中心管理账号会话、下载目录和运行参数。

支持抖音、X/Twitter、TikTok、微博、哔哩哔哩和小红书，具体入口与范围见[平台说明](docs/platforms.md)。Instagram 提供受控下载和订阅入口，默认关闭，各内容类型仍需真实账号验收。

## 快速开始

准备 Python 3.11 / 3.12、PostgreSQL 和 Redis。在项目目录运行：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 127.0.0.1 --port 8000
```

Windows 使用 `.venv\Scripts\activate` 激活环境。

打开 `http://127.0.0.1:8000`，按初始化向导配置数据库、Redis、管理 Token 和下载目录。随后在设置中心导入需要的平台会话，提交一条作品链接验证下载。API 文档位于 `/docs`。

网页保存的配置是运行时权威来源。生产部署、进程管理和备份恢复请按[部署指南](docs/deployment.md)操作。

## 开发与支持

前端源码在 `frontend/`，后端入口为 `main.py`，生产资源在 `static/app/`。

- [开发与贡献规范](CONTRIBUTING.md)：本地环境、前端构建和提交约定。
- [配置说明](docs/configuration.md)：配置优先级与敏感信息管理。
- [问题反馈](https://github.com/FarEastSea/douyin_service/issues)：请提供版本、复现步骤和脱敏错误代码。
- [更新记录](CHANGELOG.md)：按版本查阅功能和修复。

请仅下载有权访问和保存的内容，遵守相关法律、平台条款及版权要求。项目许可见 [LICENSE](LICENSE)。
