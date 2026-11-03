# 开发与贡献

## 本地开发

后端使用 Python 3.11 或 3.12，按 [README](README.md#快速开始)准备 PostgreSQL、Redis 和依赖。前端使用 Node.js 22.12+：

```bash
cd frontend
npm ci
npm run dev
```

提交前端改动前执行 `npm run build`，同时更新 `static/app/` 生产资源。页面继续沿用现有控制台组件和样式。

## 修改约定

- 修复根因，保持已有平台的数据与行为兼容。
- 配置须提供网页入口、持久化和动态生效；详细约定见 [AGENTS.md](AGENTS.md)。
- 测试脚本只在本地使用，加入忽略并在验证后删除，不提交或上传到服务端。
- Cookie、账号、密钥、抓包和真实部署地址不得进入代码、日志或提交。报告问题前请先脱敏。
- 提交聚焦一个问题，说明行为变化和验证结果；用户可见的变更记入 [CHANGELOG.md](CHANGELOG.md)。

## 文档与发布

README 只保留项目入口和快速上手。平台范围放在 [平台说明](docs/platforms.md)，发布与回滚步骤放在 [部署指南](docs/deployment.md)。发布使用现有 Jenkins 流程，避免同时启动手动发布。

新增能力以真实验收范围为准，不将本地模拟验证当作生产采集成功。
