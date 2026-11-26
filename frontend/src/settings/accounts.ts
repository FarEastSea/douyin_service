import type { SettingsPageDefinition } from "../settings-registry";
export const accountsPages: Record<string, SettingsPageDefinition> = {
  risk: {
    id: "risk",
    title: "抖音请求与风控",
    description: "控制抖音接口节奏、作者间隔和风控后的恢复策略。",
    fieldSections: [
      {
        title: "请求节奏",
        description: "所有 Worker 共享请求节奏，避免多个任务瞬间集中访问。",
        runtimeKeys: ["douyin_request_delay", "author_check_delay"],
      },
      {
        title: "风控恢复",
        description: "进入冷却后暂停接口请求，满足条件后再恢复。",
        runtimeKeys: ["douyin_risk_cooldown_seconds", "douyin_risk_auto_retry"],
      },
    ],
  },
  "account-douyin": {
    id: "account-douyin",
    title: "抖音账号",
    description: "管理 Cookie、浏览器身份、User-Agent 和代理绑定。",
  },
  "account-x": {
    id: "account-x",
    title: "X 账号",
    description: "管理 gallery-dl 引擎、Cookie 和任务日志保留。",
    fieldSections: [
      {
        title: "下载选项",
        description: "Cookie 内容在下方凭据区域单独保存。",
        envKeys: [
          "X_DOWNLOAD_ENGINE",
          "X_COOKIE_FILE",
          "X_TASK_LOG_MAX_LINES",
          "X_TASK_LOG_TTL_SECONDS",
          "X_TASK_STATE_TTL_SECONDS",
        ],
      },
    ],
  },
  "account-tiktok": {
    id: "account-tiktok",
    title: "TikTok 账号",
    description: "管理 TikTok 主页与单条作品下载凭据。",
    fieldSections: [
      {
        title: "下载选项",
        description: "网页加密 Cookie 优先于可选的服务器 Cookie 文件。",
        envKeys: ["TIKTOK_DOWNLOAD_ENGINE", "TIKTOK_COOKIE_FILE"],
      },
    ],
  },
  "account-weibo": {
    id: "account-weibo",
    title: "微博账号",
    description: "管理微博主页与单条动态下载凭据。",
    fieldSections: [
      {
        title: "下载选项",
        description: "网页加密 Cookie 优先于可选的服务器 Cookie 文件。",
        envKeys: ["WEIBO_DOWNLOAD_ENGINE", "WEIBO_COOKIE_FILE"],
      },
    ],
  },
  "account-bilibili": {
    id: "account-bilibili",
    title: "B站账号",
    description: "管理 B站用户主页与单条视频下载凭据。",
    fieldSections: [
      {
        title: "下载选项",
        description: "网页加密 Cookie 优先于可选的服务器 Cookie 文件。",
        envKeys: ["BILIBILI_DOWNLOAD_ENGINE", "BILIBILI_COOKIE_FILE"],
      },
    ],
  },
  "account-instagram": {
    id: "account-instagram",
    title: "Instagram 账号",
    description: "导入正常浏览器会话，管理固定出口、请求预算和低频订阅。",
    fieldSections: [
      {
        title: "采集开关",
        description: "先导入并检测会话，再启用采集。保存后在下一次请求或调度时生效。",
        envKeys: ["INSTAGRAM_ENABLED"],
      },
      {
        title: "请求节奏与预算",
        description: "所有任务共用一个通道。预算是保守的工程默认值，不能保证不触发平台验证。",
        envKeys: [
          "INSTAGRAM_REQUEST_DELAY_MIN",
          "INSTAGRAM_REQUEST_DELAY_MAX",
          "INSTAGRAM_HOURLY_BUDGET",
          "INSTAGRAM_DAILY_BUDGET",
        ],
      },
      {
        title: "订阅与历史分片",
        description:
          "新增和即将过期的 Stories 优先；预算不足时延后检查。作者增长后请关注实际延迟。",
        envKeys: [
          "INSTAGRAM_CHECK_INTERVAL",
          "INSTAGRAM_STORIES_INTERVAL",
          "INSTAGRAM_HIGHLIGHTS_INTERVAL",
          "INSTAGRAM_BATCH_ITEMS",
          "INSTAGRAM_BATCH_SECONDS",
        ],
      },
    ],
  },
  "account-xhs": {
    id: "account-xhs",
    title: "小红书账号",
    description: "仅用于单条图文、视频和实况笔记，不开放作者主页批量采集。",
    fieldSections: [
      {
        title: "下载选项",
        description: "隔离服务按需启用，不影响主服务启动；Cookie 用于单条笔记。",
        envKeys: ["XHS_SERVICE_ENABLED", "XHS_DOWNLOAD_ENGINE", "XHS_COOKIE_FILE"],
      },
    ],
  },
};
