import type { SettingsPageDefinition } from "../settings-registry";
export const downloadsPages: Record<string, SettingsPageDefinition> = {
  storage: {
    id: "storage",
    title: "下载目录",
    description: "统一管理下载根目录和各平台的相对子目录。",
    fieldSections: [
      {
        title: "根目录",
        description: "所有平台媒体都位于同一个根目录下。",
        envKeys: ["DOWNLOAD_ROOT"],
      },
      {
        title: "磁盘空间预警",
        description:
          "下载根目录所在磁盘的剩余空间低于预警线时，总览与洞察页会提示，并按通知设置提醒。只提醒，不会暂停下载。",
        envKeys: ["DISK_WARN_FREE_GB"],
      },
      {
        title: "平台子目录",
        description: "仅填写相对目录；保存时系统会同步迁移数据库中的已有路径。",
        envKeys: [
          "DOUYIN_DOWNLOAD_SUBDIR",
          "X_DOWNLOAD_SUBDIR",
          "TIKTOK_DOWNLOAD_SUBDIR",
          "WEIBO_DOWNLOAD_SUBDIR",
          "BILIBILI_DOWNLOAD_SUBDIR",
          "XHS_DOWNLOAD_SUBDIR",
          "INSTAGRAM_DOWNLOAD_SUBDIR",
        ],
      },
    ],
  },
  downloads: {
    id: "downloads",
    title: "下载执行",
    description: "控制下载并发、超时、重试和卡住任务判定。",
    fieldSections: [
      {
        title: "并发与传输",
        description: "限制同时下载数量和每次写入的数据块大小。",
        envKeys: ["MAX_CONCURRENT_DOWNLOADS", "DOWNLOAD_CHUNK_SIZE"],
      },
      {
        title: "失败恢复",
        description: "运行期参数在下一次任务调用时生效。",
        runtimeKeys: [
          "download_timeout",
          "download_retry_count",
          "download_retry_delay",
          "stuck_task_timeout",
        ],
      },
    ],
  },
  subscriptions: {
    id: "subscriptions",
    title: "订阅检查",
    description: "控制自动检查周期、有界增量扫描和定期全量对账。",
    fieldSections: [
      {
        title: "调度周期",
        description: "启用自动检查，并限制允许配置的最短检查周期。",
        envKeys: ["MIN_CHECK_INTERVAL", "SUBSCRIPTION_RECORD_RETENTION_DAYS"],
        runtimeKeys: ["auto_check_enabled", "subscription_check_interval"],
      },
      {
        title: "增量扫描",
        description: "在安全回看窗口内识别已知作品，减少无意义的历史翻页。",
        runtimeKeys: [
          "subscription_known_streak",
          "subscription_max_pages",
          "subscription_safe_lookback_pages",
          "subscription_full_reconcile_interval",
        ],
      },
    ],
  },
  archive: {
    id: "archive",
    title: "归档与导出",
    description: "定义新任务使用的目录、文件名、作品范围和元数据规则。",
  },
};
