import { applicationPages } from "./settings/application";
import { downloadsPages } from "./settings/downloads";
import { accountsPages } from "./settings/accounts";
import { notificationsPages } from "./settings/notifications";
export type SettingsMode = "settings" | "maintenance";

export interface SettingsNavItem {
  id: string;
  label: string;
}

export interface SettingsNavGroup {
  id: string;
  label: string;
  items: SettingsNavItem[];
}

export interface SettingsFieldSection {
  title: string;
  description: string;
  envKeys?: string[];
  runtimeKeys?: string[];
}

export interface SettingsPageDefinition {
  id: string;
  title: string;
  description: string;
  fieldSections?: SettingsFieldSection[];
}

export const settingsPages = {
  ...applicationPages,
  ...downloadsPages,
  ...accountsPages,
  ...notificationsPages,
};

export const maintenancePages: Record<string, SettingsPageDefinition> = {
  services: {
    id: "services",
    title: "进程与依赖",
    description: "检查 Web 依赖、下载 Worker 和定时调度器。",
  },
  platforms: {
    id: "platforms",
    title: "平台就绪",
    description: "核对平台引擎、Cookie、FFmpeg、目录和真实任务证据。",
  },
  storage: {
    id: "storage",
    title: "存储巡检与维护",
    description: "扫描数据库与媒体文件，并采用可恢复方式处理问题。",
  },
  exclusions: {
    id: "exclusions",
    title: "排除项目",
    description: "管理删除后不再下载的作品与媒体，需要时恢复下载。",
  },
  logs: {
    id: "logs",
    title: "活动日志",
    description: "查看可操作事件、失败证据和关联请求。",
  },
  update: {
    id: "update",
    title: "更新与系统信息",
    description: "检查版本、复制诊断并查看系统入口。",
  },
};

export const settingsNavigation: SettingsNavGroup[] = [
  {
    id: "downloads",
    label: "下载与文件",
    items: [
      { id: "downloads", label: "下载执行" },
      { id: "storage", label: "下载目录" },
      { id: "archive", label: "归档与导出" },
    ],
  },
  {
    id: "automation",
    label: "自动化",
    items: [{ id: "subscriptions", label: "订阅检查" }],
  },
  {
    id: "accounts",
    label: "平台连接",
    items: [
      { id: "account-douyin", label: "抖音账号" },
      { id: "risk", label: "抖音请求与风控" },
      { id: "account-x", label: "X 账号" },
      { id: "account-tiktok", label: "TikTok" },
      { id: "account-weibo", label: "微博" },
      { id: "account-bilibili", label: "B站" },
      { id: "account-xhs", label: "小红书" },
      { id: "account-instagram", label: "Instagram" },
    ],
  },
  {
    id: "notifications",
    label: "通知",
    items: [{ id: "notifications", label: "通知渠道与规则" }],
  },
  {
    id: "application",
    label: "应用与安全",
    items: [{ id: "application", label: "应用与访问安全" }],
  },
  {
    id: "advanced",
    label: "高级连接",
    items: [
      { id: "connections", label: "数据库与 Redis" },
      { id: "jobs", label: "后台任务" },
      { id: "other", label: "其他配置" },
    ],
  },
];

export const maintenanceNavigation: SettingsNavGroup[] = [
  {
    id: "service",
    label: "服务状态",
    items: [
      { id: "services", label: "进程与依赖" },
      { id: "platforms", label: "平台就绪" },
    ],
  },
  {
    id: "storage",
    label: "存储",
    items: [
      { id: "storage", label: "存储巡检与维护" },
      { id: "exclusions", label: "排除项目" },
    ],
  },
  {
    id: "diagnostics",
    label: "诊断",
    items: [
      { id: "logs", label: "活动日志" },
      { id: "update", label: "更新与系统信息" },
    ],
  },
];

export const platformCredentials = [
  {
    id: "instagram",
    pageId: "account-instagram",
    name: "Instagram",
    cookieKey: "INSTAGRAM_COOKIE",
  },
  {
    id: "tiktok",
    pageId: "account-tiktok",
    name: "TikTok",
    cookieKey: "TIKTOK_COOKIE",
  },
  {
    id: "weibo",
    pageId: "account-weibo",
    name: "微博",
    cookieKey: "WEIBO_COOKIE",
  },
  {
    id: "bilibili",
    pageId: "account-bilibili",
    name: "B站",
    cookieKey: "BILIBILI_COOKIE",
  },
  { id: "xhs", pageId: "account-xhs", name: "小红书", cookieKey: "XHS_COOKIE" },
] as const;

export const knownCredentialKeys = new Set([
  "DOUYIN_COOKIE",
  "X_COOKIE",
  ...platformCredentials.map((item) => item.cookieKey),
]);

export const assignedEnvKeys = new Set([
  ...Object.values(settingsPages).flatMap(
    (page) => page.fieldSections?.flatMap((section) => section.envKeys || []) || [],
  ),
  ...knownCredentialKeys,
]);

export const settingsSectionIds = new Set(Object.keys(settingsPages));
export const maintenanceSectionIds = new Set(Object.keys(maintenancePages));

export function sectionPath(mode: SettingsMode, section: string) {
  return mode === "maintenance" ? `/operations/maintenance/${section}` : `/settings/${section}`;
}
