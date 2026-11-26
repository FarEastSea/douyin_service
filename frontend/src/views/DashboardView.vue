<script setup lang="ts">
import { usePagePolling } from "../composables/usePagePolling";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import {
  Activity,
  AlertCircle,
  ArrowRight,
  CircleCheck,
  Clock3,
  Download,
  HardDrive,
  ShieldAlert,
  Users,
} from "@lucide/vue";
import KpiCard from "../components/KpiCard.vue";
import SectionCard from "../components/SectionCard.vue";
import PageHeader from "../components/PageHeader.vue";
import StatusIndicator from "../components/StatusIndicator.vue";
import StateView from "../components/StateView.vue";
import MediaThumbnail from "../components/MediaThumbnail.vue";
import { mediaPreviewType, openMedia } from "../media";
import { reportSummary } from "../presentation";
import { api } from "../api";
import { bytes, usePlatformNames } from "../workspace";
import { useAppStore } from "../stores/app";
import type { UnifiedTask, UnifiedTaskPage, LibraryMedia, PageData } from "../types";
const platformNames = usePlatformNames();

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

const store = useAppStore();
const tasks = ref<UnifiedTask[]>([]);
const activeTasks = ref<UnifiedTask[]>([]);
const media = ref<LibraryMedia[]>([]);
const freshness = ref<Record<string, string>>({});
const endpoint = (value?: string) =>
  value ? (value.startsWith("/api/") ? value : `/api${value}`) : "";
function previewMedia(item: LibraryMedia) {
  const available = media.value.filter((entry) => entry.available !== false);
  openMedia(
    available.map((entry) => ({
      url: endpoint(entry.preview_endpoint),
      type: mediaPreviewType(entry.media_type),
      title: entry.title || entry.filename,
    })),
    Math.max(
      0,
      available.findIndex((entry) => entry.key === item.key),
    ),
  );
}
const failedTasks = ref<UnifiedTask[]>([]);
const counts = ref<Record<string, number> | null>(null);
const report = ref<SubscriptionReport | null>(null);
const storage = ref<StorageState | null>(null);
const risk = computed(() => store.risk);
interface DiskState {
  available: boolean;
  free_bytes: number | null;
  used_percent: number | null;
  warn_free_bytes: number;
  low: boolean;
}
const disk = ref<DiskState | null>(null);
// 接口只返回有记录的状态；已读取到摘要时缺失的状态就是 0，只有摘要本身未读取到才显示“-”。
const statusCount = (status: string) =>
  counts.value === null ? "-" : Number(counts.value[status] || 0).toLocaleString();
const loadErrors = ref<string[]>([]);
const errors = computed(() =>
  store.riskState === "failed" ? [...loadErrors.value, "抖音风控"] : loadErrors.value,
);
function refreshDashboard() {
  void store.refreshHealth();
  void load();
}
const loading = ref(true);
const refreshedAt = ref<Date | null>(null);

let inFlight = false,
  sequence = 0;

const failedCount = computed(() => Number(counts.value?.failed || 0));
const queuedCount = computed(() => Number(counts.value?.pending || 0));
const averageProgress = computed(() => {
  if (!activeTasks.value.length) return 0;
  const sum = activeTasks.value.reduce(
    (total, task) => total + Math.min(100, Math.max(0, task.progress_percent || 0)),
    0,
  );
  return Math.round(sum / activeTasks.value.length);
});
const greeting = computed(() => {
  const hour = (refreshedAt.value || new Date()).getHours();
  const period = hour < 6 ? "夜深了" : hour < 12 ? "早上好" : hour < 18 ? "下午好" : "晚上好";
  return `${period}，这是当前的下载、订阅与入库概况。`;
});
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
    tone: "danger" | "warning";
  }> = [];
  if (risk.value?.active)
    items.push({
      title: "抖音请求已暂停",
      detail: risk.value.requires_account_update
        ? "账号请求上下文需要检查；新抖音请求暂不可用。"
        : "正在保护性冷却，等待后再检查。",
      to: "/settings/account-douyin",
      tone: risk.value.requires_account_update ? "danger" : "warning",
    });
  if (
    report.value &&
    ["failed", "interrupted", "partial_upstream", "partial_authentication"].includes(
      report.value.status,
    )
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
    !["failed", "interrupted", "partial_upstream", "partial_authentication"].includes(
      report.value.status,
    )
  )
    items.push({
      title: `最近检查有 ${report.value.failed_authors + report.value.warning_authors} 位作者需核对`,
      detail: "已完成的检查结果会保留；打开运行记录查看具体作者与恢复条件。",
      to: "/automation",
      tone: "warning",
    });
  if (disk.value?.available && disk.value.low)
    items.push({
      title: `下载磁盘仅剩 ${bytes(disk.value.free_bytes)}`,
      detail: `已低于预警线 ${bytes(disk.value.warn_free_bytes)}；空间耗尽会导致下载失败，请清理媒体或扩容。`,
      to: "/insights",
      tone: "danger",
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

async function load() {
  if (inFlight) return;
  inFlight = true;
  loading.value = true;
  const current = ++sequence;
  const results = await Promise.allSettled([
    api<UnifiedTaskPage>("/operations/tasks?page=1&page_size=5&status=completed"),
    api<UnifiedTaskPage>("/operations/tasks?page=1&page_size=4&status=failed"),
    api<SubscriptionReports>("/authors/reports/subscriptions?limit=1"),
    api<StorageState>("/operations/storage-audit"),
    api<UnifiedTaskPage>("/operations/tasks?page=1&page_size=5&status=downloading"),
    api<PageData<LibraryMedia>>(
      "/operations/media?page=1&page_size=6&sort_by=created_desc&with_counts=false",
    ),
    api<DiskState>("/operations/insights/disk"),
  ]);
  inFlight = false;
  if (current !== sequence) return;
  const messages = [
    "最近完成",
    "失败任务",
    "订阅检查",
    "存储巡检",
    "活动队列",
    "媒体库",
    "磁盘空间",
  ];
  if (results[6].status === "fulfilled") disk.value = results[6].value;
  results.forEach((result, index) => {
    if (result.status === "fulfilled") freshness.value[messages[index]!] = new Date().toISOString();
  });
  if (results[5].status === "fulfilled") media.value = results[5].value.items;
  activeTasks.value =
    results[4].status === "fulfilled" ? results[4].value.items : activeTasks.value;
  loadErrors.value = results.flatMap((item, index) =>
    item.status === "rejected" ? [messages[index]] : [],
  );
  tasks.value = results[0].status === "fulfilled" ? results[0].value.items || [] : tasks.value;
  counts.value =
    results[0].status === "fulfilled" ? results[0].value.status_summary || {} : counts.value;
  failedTasks.value =
    results[1].status === "fulfilled" ? results[1].value.items || [] : failedTasks.value;
  report.value =
    results[2].status === "fulfilled" ? results[2].value.items?.[0] || null : report.value;
  storage.value = results[3].status === "fulfilled" ? results[3].value : storage.value;
  refreshedAt.value = new Date();
  loading.value = false;
}

onMounted(load);
usePagePolling(30_000, load);
onBeforeUnmount(() => {
  sequence++;
});
</script>

<template>
  <div class="workspace-page dashboard-page">
    <PageHeader
      title="总览"
      :description="greeting"
      :busy="loading"
      :updated-at="refreshedAt?.toISOString()"
      refreshable
      @refresh="refreshDashboard"
    >
      <button
        class="btn primary workspace-create"
        @click="windowNewDownload"
      >
        <Download :size="16" />
        新建下载
      </button>
    </PageHeader>
    <div
      v-if="errors.length"
      class="load-error-banner"
      role="status"
    >
      <AlertCircle :size="17" />
      <span>{{ errors.join("、") }}暂不可用，当前显示上次读取的结果。未知状态以“-”表示。</span>
      <button
        class="btn ghost compact"
        @click="refreshDashboard"
      >
        重试
      </button>
    </div>
    <StateView
      v-if="loading && !refreshedAt"
      loading
    />
    <template v-else>
      <section
        class="kpi-grid"
        aria-label="全部平台任务摘要"
      >
        <KpiCard
          to="/operations/tasks?status=downloading"
          tone="info"
          :progress="activeTasks.length ? averageProgress : null"
          :icon="Activity"
          :value="statusCount('downloading')"
        >
          <template #label>正在下载</template>
          {{
            errors.includes("活动队列")
              ? "队列状态未确认"
              : activeTasks.length
                ? `平均进度 ${averageProgress}%`
                : "队列当前空闲"
          }}
        </KpiCard>
        <KpiCard
          to="/operations/tasks?status=pending"
          :icon="Clock3"
          :value="statusCount('pending')"
        >
          <template #label>等待下载</template>
          按调度规则依次执行
        </KpiCard>
        <KpiCard
          to="/operations/tasks?status=failed"
          :tone="failedCount > 0 ? 'danger' : undefined"
          :icon="AlertCircle"
          :value="statusCount('failed')"
        >
          <template #label>失败待复核</template>
          {{
            counts === null
              ? "任务状态未确认"
              : failedCount > 0
                ? "打开查看失败原因"
                : "没有失败任务"
          }}
        </KpiCard>
        <KpiCard
          to="/authors/douyin"
          :icon="Users"
          :value="store.statsKnown ? store.stats.subscribed_authors.toLocaleString() : '—'"
        >
          <template #label>订阅作者</template>
          累计下载
          {{ store.statsKnown ? store.stats.total_downloads.toLocaleString() : "—" }} 个作品
        </KpiCard>
      </section>
      <SectionCard
        title="需要处理"
        class="attention-section"
      >
        <template #actions>
          <span
            v-if="attention.length"
            class="error-count"
          >
            {{ attention.length }} 项
          </span>
          <span
            v-else-if="errors.length || store.riskState === 'pending'"
            class="muted"
          >
            部分状态未确认
          </span>
          <span
            v-else
            class="all-clear"
          >
            <CircleCheck :size="15" />
            一切正常
          </span>
        </template>
        <div
          v-if="attention.length"
          class="attention-list"
        >
          <RouterLink
            v-for="item in attention"
            :key="item.title"
            :to="item.to"
            :data-tone="item.tone"
          >
            <ShieldAlert :size="17" />
            <span>
              <strong>{{ item.title }}</strong>
              <small>{{ item.detail }}</small>
            </span>
            <ArrowRight :size="14" />
          </RouterLink>
        </div>
        <p
          v-else
          class="normal-note"
        >
          {{
            errors.length || store.riskState === "pending"
              ? "部分状态未确认，刷新后再核对。"
              : "当前没有运行阻塞。"
          }}
        </p>
      </SectionCard>
      <div class="dashboard-lead">
        <SectionCard
          title="下载队列"
          class="overview-queue"
        >
          <template #description>实时传输中的任务</template>
          <template #actions>
            <RouterLink
              class="btn ghost compact"
              to="/operations/tasks?status=downloading"
            >
              查看队列
              <ArrowRight :size="14" />
            </RouterLink>
          </template>
          <div
            v-if="activeTasks.length"
            class="compact-records active-download-records"
          >
            <RouterLink
              v-for="task in activeTasks.slice(0, 4)"
              :key="task.key"
              :to="`/operations/tasks?task_key=${encodeURIComponent(task.key)}`"
            >
              <MediaThumbnail
                :src="task.thumbnail_endpoint || task.cover_url"
                :type="task.media_type"
              />
              <span class="task-title">
                <strong>{{ task.source_label || "任务 " + task.id }}</strong>
                <small>
                  {{ platformNames[task.platform] }}
                  <span v-if="task.author_name">· {{ task.author_name }}</span>
                </small>
                <span
                  class="overview-transfer"
                  :style="{
                    '--progress': Math.min(100, Math.max(0, task.progress_percent || 0)) + '%',
                  }"
                />
              </span>
              <span class="download-percent">
                {{ Number(task.progress_percent || 0).toFixed(0) }}
                <small>%</small>
              </span>
            </RouterLink>
          </div>
          <div
            v-else
            class="queue-empty"
          >
            <span class="queue-empty-icon"><Download :size="20" /></span>
            <div>
              <strong>
                {{
                  errors.includes("活动队列") ? "下载队列暂时无法读取" : "当前没有正在下载的任务"
                }}
              </strong>
              <p>
                {{
                  errors.includes("活动队列")
                    ? "刷新后核对当前运行情况。"
                    : queuedCount
                      ? "等待中的任务会按调度规则继续执行。"
                      : "添加作品链接，或让作者订阅自动发现新内容。"
                }}
              </p>
            </div>
            <button
              class="btn"
              @click="windowNewDownload"
            >
              添加下载
            </button>
          </div>
        </SectionCard>

        <SectionCard
          title="订阅检查"
          class="overview-subscriptions"
        >
          <template #actions>
            <RouterLink
              class="btn ghost compact"
              to="/automation"
            >
              运行记录
              <ArrowRight :size="14" />
            </RouterLink>
          </template>
          <div
            v-if="report"
            class="automation-overview"
          >
            <div class="automation-overview-head">
              <StatusIndicator :status="report.status" />
              <time>{{ formatTime(report.started_at) }}</time>
            </div>
            <p>{{ reportSummary(report) }}</p>
            <RouterLink
              v-if="report.failed_authors + report.warning_authors"
              to="/automation"
              class="btn compact danger"
            >
              {{ report.failed_authors + report.warning_authors }} 位作者需核对
            </RouterLink>
          </div>
          <p
            v-else
            class="inline-note"
          >
            {{
              errors.includes("订阅检查")
                ? "检查状态未确认"
                : "暂无运行记录，订阅作者后会定期检查更新。"
            }}
          </p>
          <RouterLink
            class="subscription-manage"
            to="/authors/douyin"
          >
            <Users :size="15" />
            管理作者订阅
            <ArrowRight :size="14" />
          </RouterLink>
        </SectionCard>
      </div>
      <SectionCard
        title="最近入库"
        class="recent-library"
      >
        <template #description>
          {{
            errors.includes("媒体库")
              ? "读取失败，显示上次结果"
              : freshness["媒体库"]
                ? "更新于 " + formatTime(freshness["媒体库"])
                : "已保存的媒体文件"
          }}
        </template>
        <template #actions>
          <RouterLink
            class="btn ghost compact"
            to="/library"
          >
            打开媒体库
            <ArrowRight :size="14" />
          </RouterLink>
        </template>
        <div
          v-if="media.length"
          class="recent-media-grid"
        >
          <article
            v-for="item in media"
            :key="item.key"
          >
            <button
              class="recent-cover"
              :disabled="item.available === false"
              :aria-label="'预览 ' + (item.title || item.filename)"
              @click="previewMedia(item)"
            >
              <MediaThumbnail
                :src="
                  endpoint(item.thumbnail_endpoint) ||
                  (item.media_type === 'image' ? endpoint(item.preview_endpoint) : '')
                "
                :type="item.media_type"
              />
              <span
                v-if="item.available === false"
                class="media-unavailable"
              >
                文件不可用
              </span>
              <span class="recent-caption">
                <strong :title="item.title || item.filename">
                  {{ item.title || item.filename }}
                </strong>
                <small>{{ item.author_name || platformNames[item.platform] }}</small>
              </span>
            </button>
          </article>
        </div>
        <StateView
          v-else
          :error="errors.includes('媒体库') ? '媒体库暂时无法读取' : ''"
          title="还没有入库媒体"
          description="下载完成的内容会出现在这里。"
          :rows="2"
          @retry="load"
        >
          <button
            class="btn"
            @click="windowNewDownload"
          >
            添加下载
          </button>
        </StateView>
      </SectionCard>
      <div class="dashboard-history">
        <SectionCard
          title="失败待复核"
          class="overview-failures"
        >
          <template #count>
            <span class="error-count">
              {{ counts === null ? "-" : failedCount.toLocaleString() }}
            </span>
          </template>
          <template #actions>
            <RouterLink
              class="btn ghost compact"
              to="/operations/tasks?status=failed"
            >
              查看全部
              <ArrowRight :size="14" />
            </RouterLink>
          </template>
          <div
            v-if="failedTasks.length"
            class="compact-records"
          >
            <RouterLink
              v-for="task in failedTasks.slice(0, 3)"
              :key="task.key"
              :to="`/operations/tasks?status=failed&task_key=${encodeURIComponent(task.key)}`"
            >
              <MediaThumbnail
                :src="task.thumbnail_endpoint || task.cover_url"
                :type="task.media_type"
              />
              <span class="task-title">
                <strong>{{ task.source_label || "任务 " + task.id }}</strong>
                <small class="danger-text">
                  {{
                    task.error_message || task.author_name || task.error_code || "失败原因见详情"
                  }}
                </small>
              </span>
              <span class="row-recovery">
                查看原因
                <ArrowRight :size="14" />
              </span>
            </RouterLink>
          </div>
          <p
            v-else
            class="inline-note"
          >
            {{ errors.includes("失败任务") ? "失败记录暂时无法读取。" : "没有待复核的失败任务。" }}
          </p>
        </SectionCard>
        <SectionCard
          title="最近完成"
          class="overview-completed"
        >
          <template #actions>
            <RouterLink
              class="btn ghost compact"
              to="/operations/tasks?status=completed"
            >
              查看全部
              <ArrowRight :size="14" />
            </RouterLink>
          </template>
          <div
            v-if="tasks.length"
            class="compact-records"
          >
            <RouterLink
              v-for="task in tasks.slice(0, 3)"
              :key="task.key"
              :to="`/operations/tasks?task_key=${encodeURIComponent(task.key)}`"
            >
              <span class="completed-check"><CircleCheck :size="16" /></span>
              <span class="task-title">
                <strong>{{ task.source_label || "任务 " + task.id }}</strong>
                <small>
                  {{ platformNames[task.platform] }}
                  <span v-if="task.author_name">· {{ task.author_name }}</span>
                  · {{ task.file_count }} 个文件
                </small>
              </span>
              <time>{{ formatTime(task.completed_at || task.created_at) }}</time>
            </RouterLink>
          </div>
          <p
            v-else
            class="inline-note"
          >
            {{ errors.includes("最近完成") ? "最近完成记录未确认。" : "还没有完成的任务。" }}
          </p>
        </SectionCard>
      </div>
    </template>
    <footer class="dashboard-footer">
      <HardDrive :size="15" />
      <span>
        存储巡检{{
          errors.includes("存储巡检")
            ? "状态未确认"
            : storage?.status === "completed"
              ? "更新于 " + formatTime(storage.updated_at)
              : storage?.status === "running"
                ? "正在运行"
                : storage?.status === "failed"
                  ? "需要检查"
                  : "尚无完成记录"
        }}
      </span>
      <RouterLink
        class="btn ghost compact"
        to="/operations/maintenance/storage"
      >
        打开存储维护
        <ArrowRight :size="14" />
      </RouterLink>
      <span
        v-if="disk?.available"
        class="dashboard-disk"
        :class="{ 'danger-text': disk.low }"
      >
        磁盘剩余 {{ bytes(disk.free_bytes) }}（已用 {{ disk.used_percent }}%）
      </span>
      <RouterLink
        class="btn ghost compact"
        to="/insights"
      >
        查看洞察
        <ArrowRight :size="14" />
      </RouterLink>
    </footer>
  </div>
</template>
