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

日常配置在网页设置中心维护。生产部署、进程管理和备份恢复见[部署指南](docs/deployment.md)。

## 开发与支持

前端源码在 `frontend/`，后端入口为 `main.py`，生产资源在 `static/app/`。

- [开发与贡献规范](CONTRIBUTING.md)：本地环境、前端构建和提交约定。
- [配置说明](docs/configuration.md)：配置优先级与敏感信息管理。
- [问题反馈](https://github.com/FarEastSea/douyin_service/issues)：请提供版本、复现步骤和脱敏错误代码。
- [更新记录](CHANGELOG.md)：按版本查阅功能和修复。

## 免责声明与使用边界

本项目面向个人合法备份、本地媒体管理与技术研究，基于多项开源工具构建，与所支持的平台没有隶属、合作或背书关系。项目维护者不提供平台账号、代采集、第三方内容托管或内容分发服务。

- **使用前取得授权**：使用前请先与相应平台、内容作者及其他权利人确认许可范围，取得所需授权，并遵守适用法律及平台规则；仅保存自行创作、已获明确授权或法律允许保存的内容。平台依法享有其服务管理及规则执行权，内容权利仍归相应权利人。公开可见、持有账号或能够下载，均不代表已取得复制、传播、商业利用或处理个人信息的授权。
- **禁止侵权与滥用**：不得盗用账号、绕过访问控制、获取无权访问的私密内容，或将未经授权的内容用于售卖、付费资源库、商业引流、批量传播及其他侵权活动。软件许可不构成维护者对这些行为的授权、委托或认可。
- **独立部署与使用责任**：使用者自行决定采集对象、范围、频率、存储和后续用途，并负责核实所需授权及保护账号和个人信息。由未经授权的采集、传播或其他滥用引发的争议，应由实际实施者依法承担相应责任。
- **账号与可用性风险**：Cookie 属于敏感登录凭据，请勿公开。平台变化或自动请求可能导致下载不完整、限流、验证及账号限制；限频和冷却不能保证账号安全。本软件按现状提供，不作持续可用、适销性、特定用途适用性或不侵权保证。在适用法律允许的最大范围内，作者与贡献者不承担因使用本软件产生的损失或索赔责任。
- **权利人反馈**：如认为仓库中的代码、文档或其他材料侵犯你的合法权益，请通过[项目问题反馈](https://github.com/FarEastSea/douyin_service/issues)说明相关位置、权利依据和诉求，维护者将核实并依法处理。请勿在公开反馈中上传身份证件、Cookie 或其他敏感资料。第三方自行部署的服务及其保存内容，请同时联系相应运营者。

## 开源许可

项目软件代码采用 [GNU AGPL v3](LICENSE)，使用、修改和分发以许可证正文为准。修改后通过网络向用户提供服务的版本，应按第 13 条向相关用户提供对应源码。软件许可不授予任何第三方媒体内容、平台账号或数据的使用权；涉及第三方内容的用途须另行取得相应授权。依赖及引用实现保留各自许可，详见[许可说明](docs/licensing.md)与[第三方声明](THIRD_PARTY_NOTICES.md)。


## 致谢

感谢以下开源项目及其贡献者（排名不分先后）：

- 下载与解析：gallery-dl、yt-dlp、FFmpeg、Johnserf-Seed/f2、Evil0ctal/Douyin_TikTok_Download_API、Andy-SoulShell/xhs-downloader。
- 服务与存储：FastAPI、Uvicorn、Gunicorn、SQLAlchemy、PostgreSQL、Redis、Celery。
- 控制台与基础库：Vue、Vite、Pinia、Vue Router、Lucide、Inter、Requests、HTTPX、Pydantic、Pillow、cryptography、gmssl、Dulwich、Swagger UI。

引用来源和许可见[第三方声明](THIRD_PARTY_NOTICES.md)。
