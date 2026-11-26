import type { SettingsPageDefinition } from "../settings-registry";
export const applicationPages: Record<string, SettingsPageDefinition> = {
  application: {
    id: "application",
    title: "应用与安全",
    description: "管理服务名称、调试开关和管理端访问边界。",
    fieldSections: [
      {
        title: "应用身份",
        description: "用于识别当前服务实例，不影响已有媒体数据。",
        envKeys: ["APP_NAME", "DEBUG"],
      },
      {
        title: "访问安全",
        description: "管理 Token 和跨域来源属于敏感边界，修改后立即使用新值。",
        envKeys: ["ADMIN_TOKEN", "CORS_ALLOWED_ORIGINS"],
      },
    ],
  },
  connections: {
    id: "connections",
    title: "数据库与 Redis",
    description: "配置持久化数据库、缓存和消息连接。",
    fieldSections: [
      {
        title: "数据库",
        description: "保存后系统自动切换数据库连接，密码不回显。",
        envKeys: ["DB_TYPE", "DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"],
      },
      {
        title: "Redis",
        description: "保存后系统自动重建连接。如果后台任务正在运行，旧队列排空后再切换。",
        envKeys: ["REDIS_URL", "REDIS_PASSWORD"],
      },
    ],
  },
  jobs: {
    id: "jobs",
    title: "后台任务",
    description: "配置 Celery 使用的消息队列与结果存储。",
    fieldSections: [
      {
        title: "任务基础设施",
        description:
          "默认继承 Redis。忙碌时保存为待生效变更，暂停新投递，旧队列排空后自动切换；可在上方撤回待生效变更。",
        envKeys: ["CELERY_CONNECTION_MODE", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"],
      },
    ],
  },
  other: {
    id: "other",
    title: "其他配置",
    description: "后端新增但尚未归入固定分类的配置。",
  },
};
