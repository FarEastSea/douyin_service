# AGENTS.md

## Project Summary

- 项目名称：媒体下载管理系统
- 主要能力：抖音作品下载、作者订阅检查、任务管理、媒体预览、X/Twitter 下载链路
- 后端技术栈：FastAPI、SQLAlchemy 2.x、PostgreSQL、Redis、Celery
- 前端形态：单页静态界面，核心文件位于 `static/index.html`

## Repo Layout

- `main.py`：FastAPI 应用入口
- `app/api/`：接口路由
- `app/core/`：配置、Redis、运行时参数、进程管理
- `app/models/`：ORM 模型与响应 Schema
- `app/services/`：下载与业务逻辑
- `app/tasks/`：Celery 任务定义
- `static/index.html`：前端界面、样式与交互逻辑
- `env.example.txt`：环境变量模板
- `start.sh` / `stop.sh`：Linux 环境下的启停脚本

## Development Notes

- 当前本地工作区 `.venv` 可能是 Python 3.14，完整运行验证会受到 `sqlalchemy==2.0.25` 兼容性限制。
- 本地开发与服务端部署优先使用 Python 3.11 或 3.12。
- 所有测试文件、测试脚本、测试工作流只允许留存在本地，并且必须加入 git ignore；不得提交到 git 记录，不得上传到服务端。
- git 记录只保留生产运行所需程序与配置，内容应优先适配生产环境，而不是本地开发或 CI 验证环境。
- 临时测试文件只允许用于当次验证，验证完成后必须删除，不得保留在仓库中。
- 前端是单文件页面，`static/index.html` 的 CSS/JS 回归会直接影响首页展示与交互。

## Deployment Target Placeholders

以下字段仅允许使用占位符，不得写入真实值：

- `REMOTE_HOST=<REMOTE_HOST>`
- `REMOTE_PORT=<SSH_PORT>`
- `REMOTE_USER=<SSH_USER>`
- `REMOTE_KEY_PATH=<LOCAL_PRIVATE_KEY_PATH>`
- `REMOTE_SERVICE_DIR=<REMOTE_SERVICE_DIR>`
- `REMOTE_PYTHON_ENV=<REMOTE_PYTHON_ENV>`
- `REMOTE_WEB_URL=<REMOTE_WEB_URL>`

真实连接信息必须只保存在本地敏感文件、运行时环境变量或受控记忆中，不能提交到仓库。

## Deployment Rules

- 发布到服务端时，只同步本项目目录下需要变更的文件。
- 不覆盖服务端 `.env`、下载目录、日志目录和其他运行期数据。
- 如果需要重启服务，优先遵循服务端当前进程管理方式；若现场仍使用仓库脚本，可参考 `start.sh` / `stop.sh`。
- 发布后至少验证首页、`/docs`、任务列表、作者管理和媒体预览。

## Secret Handling

- 严禁把账号、口令、Cookie、数据库连接串、Redis 密码、SSH 私钥路径、内网地址写入仓库。
- 本地敏感连接文件使用 `.remote-access.local.ps1`，该文件必须保持被 git 忽略。
- README、AGENTS.md、示例配置文件只能保留脱敏占位符。