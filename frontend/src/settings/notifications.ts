import type { SettingsPageDefinition } from "../settings-registry";
export const notificationsPages: Record<string, SettingsPageDefinition> = {
  notifications: {
    id: "notifications",
    title: "通知中心",
    description:
      "选择需要通知的事件，并配置 Webhook、Bark、邮件、Gotify、Telegram 与企业微信、钉钉、飞书机器人。",
    fieldSections: [
      {
        title: "通知规则",
        description: "统一开关、事件范围和相同通知的去重时间。",
        envKeys: [
          "NOTIFY_ENABLED",
          "NOTIFY_ON_NEW_WORKS",
          "NOTIFY_ON_DOWNLOAD_FAILURE",
          "NOTIFY_ON_RISK",
          "NOTIFY_ON_SUBSCRIPTION_FAILURE",
          "NOTIFY_ON_LOW_DISK",
          "NOTIFY_DEDUPE_SECONDS",
        ],
      },
      {
        title: "每日摘要",
        description:
          "每天在设定的整点之后汇总前一天的入库、订阅检查、当前队列与磁盘空间，发送到已启用的渠道。",
        envKeys: ["NOTIFY_DAILY_DIGEST", "NOTIFY_DAILY_DIGEST_HOUR"],
      },
      {
        title: "Webhook",
        description: "向自定义服务发送带可选 HMAC 签名的事件。",
        envKeys: ["WEBHOOK_ENABLED", "WEBHOOK_URL", "WEBHOOK_SECRET"],
      },
      {
        title: "Bark",
        description: "向 Bark 设备推送通知。",
        envKeys: ["BARK_ENABLED", "BARK_SERVER_URL", "BARK_DEVICE_KEY"],
      },
      {
        title: "邮件",
        description: "通过 SMTP 向一个或多个收件地址发送通知。",
        envKeys: [
          "EMAIL_ENABLED",
          "SMTP_HOST",
          "SMTP_PORT",
          "SMTP_USERNAME",
          "SMTP_PASSWORD",
          "SMTP_FROM",
          "SMTP_TO",
          "SMTP_SECURITY",
        ],
      },
      {
        title: "Gotify",
        description: "向自托管 Gotify 服务推送通知。",
        envKeys: ["GOTIFY_ENABLED", "GOTIFY_SERVER_URL", "GOTIFY_TOKEN"],
      },
      {
        title: "Telegram",
        description: "通过 Telegram Bot 向用户、群组或频道发送通知。",
        envKeys: [
          "TELEGRAM_ENABLED",
          "TELEGRAM_BOT_TOKEN",
          "TELEGRAM_CHAT_ID",
          "TELEGRAM_API_BASE",
        ],
      },
      {
        title: "企业微信",
        description: "向企业微信群机器人发送文本通知。",
        envKeys: ["WECOM_ENABLED", "WECOM_WEBHOOK_URL"],
      },
      {
        title: "钉钉",
        description:
          "向钉钉自定义机器人发送文本通知；机器人使用关键词校验时，请确保关键词出现在通知标题中。",
        envKeys: ["DINGTALK_ENABLED", "DINGTALK_WEBHOOK_URL", "DINGTALK_SECRET"],
      },
      {
        title: "飞书",
        description: "向飞书自定义机器人发送文本通知。",
        envKeys: ["FEISHU_ENABLED", "FEISHU_WEBHOOK_URL", "FEISHU_SECRET"],
      },
    ],
  },
};
