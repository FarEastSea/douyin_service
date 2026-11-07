import { onBeforeUnmount, ref, shallowRef, watchEffect } from "vue";

export const platformNames: Record<string, string> = {
  douyin: "抖音",
  x: "X",
  tiktok: "TikTok",
  weibo: "微博",
  bilibili: "B站",
  xhs: "小红书",
  instagram: "Instagram",
};
export const modalDepth = ref(0);
export const inspectorDepth = ref(0);
export interface WorkspaceAction {
  id: string;
  label: string;
  run: () => void | Promise<unknown>;
  disabled?: boolean;
  danger?: boolean;
}
export const contextActions = shallowRef<WorkspaceAction[]>([]);
let actionOwner: symbol | null = null;
export function useContextActions(actions: () => WorkspaceAction[]) {
  const owner = Symbol();
  const stop = watchEffect(() => {
    actionOwner = owner;
    contextActions.value = actions();
  });
  onBeforeUnmount(() => {
    stop();
    if (actionOwner === owner) {
      contextActions.value = [];
      actionOwner = null;
    }
  });
}
export function dateTime(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "时间未确认"
    : date.toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      });
}
export function bytes(value?: number | null) {
  if (value == null) return "—";
  let size = value,
    index = 0;
  const units = ["B", "KB", "MB", "GB", "TB"];
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index++;
  }
  return `${size.toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}
export function duration(start?: string, end?: string) {
  if (!start || !end) return "—";
  const seconds = Math.max(
    0,
    Math.round((Date.parse(end) - Date.parse(start)) / 1000),
  );
  if (!Number.isFinite(seconds)) return "—";
  return seconds < 60
    ? `${seconds} 秒`
    : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}
const states: Record<string, [string, string]> = {
  pending: ["等待中", "neutral"],
  waiting_budget: ["等待接口预算", "neutral"],
  cooling: ["保护性冷却", "warning"],
  checkpoint: ["需要浏览器验证", "warning"],
  auth_required: ["需要更新会话", "warning"],
  session_changed: ["等待会话检测", "warning"],
  batch_limit: ["等待下一分片", "neutral"],
  time_budget: ["等待下一分片", "neutral"],
  queued: ["排队中", "neutral"],
  waiting: ["等待检查", "neutral"],
  downloading: ["下载中", "active"],
  running: ["执行中", "active"],
  preparing: ["准备中", "active"],
  completed: ["已完成", "success"],
  success: ["成功", "success"],
  new_works: ["发现新作品", "success"],
  failed: ["失败", "error"],
  error: ["异常", "error"],
  interrupted: ["已中断", "error"],
  warning: ["需关注", "warning"],
  partial: ["部分完成", "warning"],
  partial_timeout: ["超时 · 等待续检", "warning"],
  partial_rate_limited: ["冷却 · 等待续检", "warning"],
  partial_upstream: ["上游异常 · 等待续检", "warning"],
  partial_authentication: ["需更新账号", "error"],
  paused: ["已暂停", "neutral"],
  cancelled: ["已取消", "neutral"],
  skipped: ["已跳过", "neutral"],
  disabled: ["已禁用", "neutral"],
  not_started: ["尚未下载", "neutral"],
  incomplete: ["未完成", "neutral"],
  active: ["处理中", "active"],
  unsubscribed: ["未订阅", "neutral"],
  idle: ["尚未运行", "neutral"],
  deferred: ["待续检", "neutral"],
  not_due: ["未到检查时间", "neutral"],
  account_warning: ["账号需关注", "warning"],
  banned: ["已封禁", "error"],
  restricted: ["账号受限", "warning"],
  deleted: ["已销号", "warning"],
};
export function statusPresentation(status?: string) {
  const value = states[status || ""];
  return { label: value?.[0] || "状态未确认", tone: value?.[1] || "neutral" };
}

export interface ConfirmationOption {
  label: string;
  help?: string;
}
export interface Confirmation {
  title: string;
  message: string;
  confirmLabel: string;
  danger: boolean;
  option?: ConfirmationOption;
}
export const confirmation = shallowRef<Confirmation | null>(null);
// 确认框附加选项的勾选状态；每次打开都重置为未勾选，避免沿用上次的危险选择。
export const confirmationChecked = ref(false);
let resolveConfirmation: ((value: boolean) => void) | null = null;
function openConfirmation(value: Confirmation): Promise<boolean> {
  if (resolveConfirmation) return Promise.resolve(false);
  confirmationChecked.value = false;
  confirmation.value = value;
  return new Promise((resolve) => {
    resolveConfirmation = resolve;
  });
}
export function confirmAction(
  message: string,
  title = "确认操作",
  confirmLabel = "确认继续",
  danger = true,
): Promise<boolean> {
  return openConfirmation({ title, message, confirmLabel, danger });
}
export async function confirmWithOption(
  message: string,
  option: ConfirmationOption,
  title = "确认操作",
  confirmLabel = "确认继续",
): Promise<{ confirmed: boolean; checked: boolean }> {
  const confirmed = await openConfirmation({ title, message, confirmLabel, danger: true, option });
  return { confirmed, checked: confirmed && confirmationChecked.value };
}
export function settleConfirmation(value: boolean) {
  const resolve = resolveConfirmation;
  resolveConfirmation = null;
  confirmation.value = null;
  resolve?.(value);
}
