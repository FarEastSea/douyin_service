<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import {
  AlertCircle,
  ArrowRight,
  CircleCheck,
  Clock3,
  Download,
  HardDrive,
  RefreshCw,
  ShieldAlert,
  Users,
} from "@lucide/vue";
import PageHeader from "../components/PageHeader.vue";
import StatusIndicator from "../components/StatusIndicator.vue";
import StateView from "../components/StateView.vue";
import IssueDetail from "../components/IssueDetail.vue";
import { reportSummary } from "../presentation";
import { api } from "../api";
import { useAppStore } from "../stores/app";
import type { UnifiedTask, UnifiedTaskPage } from "../types";

interface SubscriptionReport {
  status: string;
  summary?: string;
  checked_authors: number;
  failed_authors: number;
  warning_authors: number;
  new_works: number;
  remaining_authors: number;
  started_at?: string;
}
interface SubscriptionReports {
  items: SubscriptionReport[];
  cycle?: Record<string, unknown>;
}
interface StorageState {
  status: string;
  error?: string;
  updated_at?: string;
  result?: { issue_counts?: Record<string, number>; scanned_at?: string };
}

const router = useRouter();
const store = useAppStore();
const tasks = ref<UnifiedTask[]>([]);
const activeTasks = ref<UnifiedTask[]>([]);
const failedTasks = ref<UnifiedTask[]>([]);
const counts = ref<Record<string, number> | null>(null);
const report = ref<SubscriptionReport | null>(null);
const storage = ref<StorageState | null>(null);
const risk = ref<any>(null);
const errors = ref<string[]>([]);
const loading = ref(true);
const refreshedAt = ref<Date | null>(null);
let timer: number | undefined;
let inFlight = false,
  sequence = 0;

const activeCount = computed(() => Number(counts.value?.downloading || 0));
const failedCount = computed(() => Number(counts.value?.failed || 0));
const queuedCount = computed(() => Number(counts.value?.pending || 0));
const storageIssues = computed(() =>
  Object.values(storage.value?.result?.issue_counts || {}).reduce(
    (sum, item) => sum + Number(item || 0),
    0,
  ),
);
const attention = computed(() => {
  const items: Array<{
    title: string;
    detail: string;
    to: string;
    tone: "critical" | "warning";
  }> = [];
  if (risk.value?.active)
    items.push({
      title: "抖音请求已暂停",
      detail: risk.value.requires_account_update
        ? "账号请求上下文需要检查；新抖音请求暂不可用。"
        : "正在保护性冷却，等待后再检查。",
      to: "/settings/account-douyin",
      tone: risk.value.requires_account_update ? "critical" : "warning",
    });
  if (
    report.value &&
    [
      "failed",
      "interrupted",
      "partial_upstream",
      "partial_authentication",
    ].includes(report.value.status)
  )
    items.push({
      title: ["failed", "interrupted"].includes(report.value.status)
        ? "最近一次订阅检查异常终止"
        : "订阅检查需要核对",
      detail: reportSummary(report.value),
      to: "/automation",
      tone: "warning",
    });
  if (
    report.value &&
    report.value.failed_authors + report.value.warning_authors > 0 &&
    ![
      "failed",
      "interrupted",
      "partial_upstream",
      "partial_authentication",
    ].includes(report.value.status)
  )
    items.push({
      title: `最近检查有 ${report.value.failed_authors + report.value.warning_authors} 位作者需核对`,
      detail: "已完成的检查结果会保留；打开运行记录查看具体作者与恢复条件。",
      to: "/automation",
      tone: "warning",
    });
  if (storage.value?.status === "failed")
    items.push({
      title: "存储巡检未完成",
      detail: "巡检未完成，前往运行维护查看诊断并核对结果。",
      to: "/operations/maintenance/storage",
      tone: "warning",
    });
  else if (storageIssues.value)
    items.push({
      title: `上次存储巡检发现 ${storageIssues.value.toLocaleString()} 项问题`,
      detail: "先查看样本与预演，再决定是否处理。",
      to: "/operations/maintenance/storage",
      tone: "warning",
    });
  return items;
});

const statusNames: Record<string, string> = {
  pending: "等待中",
  downloading: "下载中",
  paused: "已暂停",
  completed: "已完成",
  skipped: "已跳过",
  failed: "失败",
  cancelled: "已取消",
};
const platformNames: Record<string, string> = {
  douyin: "抖音",
  x: "X",
  tiktok: "TikTok",
  weibo: "微博",
  bilibili: "B站",
  xhs: "小红书",
};
function formatTime(value?: string) {
  if (!value) return "暂无记录";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "时间未知"
    : date.toLocaleString("zh-CN", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
}
function windowNewDownload() {
  window.dispatchEvent(new CustomEvent("app:new-download"));
}
function onVisibilityChange() {
  if (!document.hidden) void load();
}

async function load() {
  if (inFlight) return;
  inFlight = true;
  loading.value = true;
  const current = ++sequence;
  const results = await Promise.allSettled([
    api<UnifiedTaskPage>(
      "/operations/tasks?page=1&page_size=5&status=completed",
    ),
    api<UnifiedTaskPage>("/operations/tasks?page=1&page_size=4&status=failed"),
    api<SubscriptionReports>("/authors/reports/subscriptions?limit=1"),
    api<StorageState>("/operations/storage-audit"),
    api<any>("/system/douyin-risk-state"),
    api<UnifiedTaskPage>(
      "/operations/tasks?page=1&page_size=5&status=downloading",
    ),
  ]);
  inFlight = false;
  if (current !== sequence) return;
  const messages = [
    "最近完成",
    "失败任务",
    "订阅检查",
    "存储巡检",
    "抖音风控",
    "活动队列",
  ];
  activeTasks.value =
    results[5].status === "fulfilled"
      ? results[5].value.items
      : activeTasks.value;
  errors.value = results.flatMap((item, index) =>
    item.status === "rejected" ? [messages[index]] : [],
  );
  tasks.value =
    results[0].status === "fulfilled"
      ? results[0].value.items || []
      : tasks.value;
  counts.value =
    results[0].status === "fulfilled"
      ? results[0].value.status_summary || {}
      : null;
  failedTasks.value =
    results[1].status === "fulfilled"
      ? results[1].value.items || []
      : failedTasks.value;
  report.value =
    results[2].status === "fulfilled"
      ? results[2].value.items?.[0] || null
      : null;
  storage.value = results[3].status === "fulfilled" ? results[3].value : null;
  risk.value = results[4].status === "fulfilled" ? results[4].value : null;
  refreshedAt.value = new Date();
  loading.value = false;
}

onMounted(() => {
  void load();
  timer = window.setInterval(() => {
    if (!document.hidden) void load();
  }, 30_000);
  document.addEventListener("visibilitychange", onVisibilityChange);
});
onBeforeUnmount(() => {
  sequence++;
  if (timer) window.clearInterval(timer);
  document.removeEventListener("visibilitychange", onVisibilityChange);
});
</script>

<template>
  <div class="workspace-page dashboard-page">
    <PageHeader
      title="工作台"
      description="先处理阻塞，再关注正在进行的工作。"
      :busy="loading"
      :updated-at="refreshedAt?.toISOString()"
      refreshable
      @refresh="load"
      ><button class="btn primary" @click="windowNewDownload">
        新建下载
      </button></PageHeader
    >
    <div v-if="errors.length" class="load-error-banner" role="status">
      <AlertCircle :size="17" /><span
        >{{
          errors.join("、")
        }}暂不可用；部分内容为上次读取结果，未知状态以“—”表示。</span
      ><button class="text-button" @click="load">重试</button>
    </div>
    <StateView v-if="loading && !refreshedAt" loading />
    <template v-else>
      <section class="attention-section">
        <div class="section-heading">
          <h2>当前需要处理</h2>
          <span>{{ attention.length ? attention.length + " 项" : "" }}</span>
        </div>
        <div v-if="attention.length" class="attention-list">
          <RouterLink
            v-for="item in attention"
            :key="item.title"
            :to="item.to"
            :data-tone="item.tone"
            ><ShieldAlert :size="18" /><span
              ><strong>{{ item.title }}</strong
              ><small>{{ item.detail }}</small></span
            ><ArrowRight :size="16"
          /></RouterLink>
        </div>
        <p v-else class="normal-note">
          <CircleCheck :size="16" />{{
            errors.length
              ? "部分状态未确认，刷新后再核对。"
              : "当前没有运行阻塞，自动化会继续在后台执行。"
          }}
        </p>
      </section>
      <section
        v-if="failedTasks.length || errors.includes('失败任务')"
        class="dashboard-section"
      >
        <div class="section-heading">
          <h2>
            失败任务待复核
            <span class="error-count">{{
              counts === null ? "—" : failedCount.toLocaleString()
            }}</span>
          </h2>
          <RouterLink to="/operations/tasks?status=failed"
            >查看全部 <ArrowRight :size="14"
          /></RouterLink>
        </div>
        <p class="section-description">
          这些是历史下载失败记录；重试前先核对原因。
        </p>
        <div class="compact-records">
          <RouterLink
            v-for="task in failedTasks"
            :key="task.key"
            :to="`/operations/tasks?status=failed&task_key=${encodeURIComponent(task.key)}`"
            ><span class="task-title"
              ><strong>{{ task.source_label || "任务 " + task.id }}</strong
              ><small
                >{{ platformNames[task.platform] }} ·
                {{
                  task.author_name || task.error_code || "失败原因见详情"
                }}</small
              ></span
            ><StatusIndicator status="failed" /><ArrowRight :size="14"
          /></RouterLink>
        </div>
      </section>
      <section class="dashboard-section">
        <div class="section-heading">
          <h2>自动化进度</h2>
          <RouterLink to="/automation"
            >运行历史 <ArrowRight :size="14"
          /></RouterLink>
        </div>
        <div v-if="report" class="automation-overview">
          <StatusIndicator :status="report.status" /><strong>{{
            reportSummary(report)
          }}</strong
          ><time>{{ formatTime(report.started_at) }}</time
          ><RouterLink
            v-if="report.failed_authors + report.warning_authors"
            to="/automation"
            class="danger"
            >{{
              report.failed_authors + report.warning_authors
            }}
            位作者需核对</RouterLink
          >
        </div>
        <p v-else class="inline-note">
          {{
            errors.includes("订阅检查")
              ? "检查状态未确认"
              : "暂无订阅检查记录。订阅作者后将在这里显示进度。"
          }}
        </p>
      </section>
      <section class="dashboard-section">
        <div class="section-heading">
          <h2>活动队列</h2>
          <span
            >{{ counts === null ? "—" : activeCount }} 正在下载 ·
            {{ counts === null ? "—" : queuedCount }} 等待中</span
          ><RouterLink to="/operations/tasks?status=pending"
            >查看队列 <ArrowRight :size="14"
          /></RouterLink>
        </div>
        <div class="compact-records">
          <RouterLink
            v-for="task in activeTasks"
            :key="task.key"
            :to="`/operations/tasks?task_key=${encodeURIComponent(task.key)}`"
            ><span class="task-title"
              ><strong>{{ task.source_label }}</strong
              ><small
                >{{ platformNames[task.platform] }} ·
                {{ task.author_name }}</small
              ></span
            ><span>{{ Number(task.progress_percent || 0).toFixed(0) }}%</span
            ><StatusIndicator :status="task.status"
          /></RouterLink>
        </div>
        <p v-if="!activeTasks.length" class="inline-note">
          {{
            errors.includes("活动队列")
              ? "活动队列状态未确认"
              : "当前没有正在下载的任务。"
          }}
        </p>
      </section>
      <section class="dashboard-section">
        <div class="section-heading">
          <h2>最近完成</h2>
          <RouterLink to="/operations/tasks?status=completed"
            >查看全部 <ArrowRight :size="14"
          /></RouterLink>
        </div>
        <div class="compact-records">
          <RouterLink
            v-for="task in tasks"
            :key="task.key"
            :to="`/operations/tasks?task_key=${encodeURIComponent(task.key)}`"
            ><span class="task-title"
              ><strong>{{ task.source_label || "任务 " + task.id }}</strong
              ><small
                >{{ platformNames[task.platform] }} ·
                {{ task.author_name }}</small
              ></span
            ><span class="muted">{{ task.file_count }} 个文件</span
            ><time>{{ formatTime(task.completed_at || task.created_at) }}</time
            ><ArrowRight :size="14"
          /></RouterLink>
        </div>
        <p v-if="!tasks.length" class="inline-note">
          {{
            errors.includes("最近完成")
              ? "最近完成记录未确认"
              : "还没有完成的任务。"
          }}
        </p>
      </section>
    </template>
    <footer class="dashboard-footer">
      <HardDrive :size="15" /><span
        >存储巡检{{
          errors.includes("存储巡检")
            ? "状态未确认"
            : storage?.status === "completed"
              ? "更新于 " + formatTime(storage.updated_at)
              : storage?.status === "running"
                ? "正在运行"
                : storage?.status === "failed"
                  ? "需要检查"
                  : "尚无完成记录"
        }}</span
      ><RouterLink to="/operations/maintenance/storage"
        >打开存储维护</RouterLink
      >
    </footer>
  </div>
</template>
