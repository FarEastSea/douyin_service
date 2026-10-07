<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  Eye,
  RefreshCw,
  RotateCcw,
  Search,
  Square,
  TrendingUp,
  X,
  Pause,
  Play,
  Trash2,
} from "@lucide/vue";
import { api, jsonBody } from "../api";
import Pager from "../components/Pager.vue";
import MoreActions from "../components/MoreActions.vue";
import FilterToolbar from "../components/FilterToolbar.vue";
import JobReport from "../components/JobReport.vue";
import IssueDetail from "../components/IssueDetail.vue";
import PageHeader from "../components/PageHeader.vue";
import StatusIndicator from "../components/StatusIndicator.vue";
import StateView from "../components/StateView.vue";
import TaskInspector from "../components/TaskInspector.vue";
import { confirmAction, dateTime, useContextActions } from "../workspace";
import { openMedia } from "../media";
import { focusFirst, restoreFocus, trapFocus } from "../focus";
import { useAppStore } from "../stores/app";
import type {
  MediaItem,
  UnifiedTask,
  UnifiedTaskActionResult,
  UnifiedTaskPage,
} from "../types";

const store = useAppStore();
const route = useRoute(),
  router = useRouter();
const queryText = (value: unknown) => (typeof value === "string" ? value : "");
const queryPage = (value: unknown) =>
  Math.max(1, Number.parseInt(queryText(value), 10) || 1);
const tasks = ref<UnifiedTask[]>([]),
  page = ref(queryPage(route.query.page)),
  pages = ref(1),
  total = ref(0);
const platform = ref(queryText(route.query.platform)),
  status = ref(queryText(route.query.status)),
  search = ref(queryText(route.query.q)),
  loading = ref(false);
const sort = ref(queryText(route.query.sort) || "created_desc"),
  updatedAt = ref(""),
  inspector = ref<InstanceType<typeof TaskInspector> | null>(null);
const inspectedKey = computed(() => queryText(route.query.task_key));
const loadError = ref(""),
  summaryLoaded = ref(false);
const statusSummary = ref<Record<string, number>>({}),
  selectedKeys = ref<string[]>([]);
const actionBusy = ref(false),
  rowBusy = ref<string[]>([]);
interface RetryJob {
  job_id?: string;
  platform?: string;
  status: string;
  total?: number;
  processed?: number;
  succeeded?: number;
  skipped?: number;
  failed?: number;
  error?: string;
  failures?: Array<{ task_key: string; message: string }>;
}
const retryJob = ref<RetryJob>({ status: "idle" }),
  retryAllBusy = ref(false),
  retryJobError = ref("");
const retryJobActive = computed(() =>
  ["queued", "running"].includes(retryJob.value.status),
);
const deleteJob = ref<RetryJob>({ status: "idle" }),
  deleteAllBusy = ref(false),
  deleteJobError = ref("");
const deleteJobActive = computed(() =>
  ["queued", "running"].includes(deleteJob.value.status),
);
const deleteJobNames: Record<string, string> = {
  queued: "等待后台删除",
  running: "正在删除失败任务",
  completed: "失败任务删除完成",
  partial: "失败任务部分删除",
  interrupted: "删除作业中断",
};
let deleteJobSequence = 0,
  deletePollInFlight = false;
const retryJobNames: Record<string, string> = {
  queued: "等待后台执行",
  running: "正在提交重试",
  completed: "重试提交完成",
  partial: "部分提交完成",
  interrupted: "作业中断",
};
let retryJobSequence = 0,
  retryPollInFlight = false;
const actionFailures = ref<
  Array<{ task_key: string; message: string; status_code: number }>
>([]);
let searchTimer: number | undefined;
let pollTimer: number | undefined;
let loadSequence = 0;
let pollInFlight = false;
type TaskAction =
  "retry" | "cancel" | "pause" | "resume" | "refresh_retry" | "delete";
const platformNames: Record<string, string> = {
  douyin: "抖音",
  x: "X",
  tiktok: "TikTok",
  weibo: "微博",
  bilibili: "B站",
  xhs: "小红书",
};
const statusNames: Record<string, string> = {
  pending: "等待中",
  downloading: "下载中",
  paused: "已暂停",
  completed: "已完成",
  skipped: "已跳过",
  failed: "失败",
  cancelled: "已取消",
};
const phaseNames: Record<string, string> = {
  queued: "排队中",
  preparing: "准备中",
  downloading: "下载中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};
const statusOrder = [
  "pending",
  "downloading",
  "paused",
  "failed",
  "cancelled",
  "completed",
  "skipped",
];
const visibleSummaries = computed(() =>
  statusOrder.filter(
    (item) => statusSummary.value[item] || status.value === item,
  ),
);
const allPageSelected = computed(
  () =>
    tasks.value.length > 0 &&
    tasks.value.every((task) => selectedKeys.value.includes(task.key)),
);
const selectedTasks = computed(() =>
  tasks.value.filter((task) => selectedKeys.value.includes(task.key)),
);
const retryableSelected = computed(() =>
  selectedTasks.value.filter((task) =>
    ["failed", "cancelled"].includes(task.status),
  ),
);
const deletableSelected = computed(() =>
  selectedTasks.value.filter((task) =>
    ["failed", "cancelled"].includes(task.status),
  ),
);
const cancellableSelected = computed(() =>
  selectedTasks.value.filter((task) =>
    ["pending", "downloading", "paused"].includes(task.status),
  ),
);
const summaryTotal = computed(() =>
  Object.values(statusSummary.value).reduce((sum, count) => sum + count, 0),
);
async function load(silent = false) {
  if (silent && pollInFlight) return;
  if (silent) pollInFlight = true;
  const sequence = ++loadSequence;
  if (!silent) loading.value = true;
  const params = new URLSearchParams({
    page: String(page.value),
    page_size: "20",
  });
  if (platform.value) params.set("platform", platform.value);
  if (status.value) params.set("status", status.value);
  if (search.value.trim()) params.set("q", search.value.trim());
  params.set("sort_by", sort.value);
  try {
    const data = await api<UnifiedTaskPage>(`/operations/tasks?${params}`);
    if (sequence !== loadSequence) return;
    if (page.value > Math.max(1, data.pages)) {
      page.value = Math.max(1, data.pages);
      syncQuery();
      void load();
      return;
    }
    tasks.value = data.items;
    pages.value = data.pages;
    total.value = data.total;
    statusSummary.value = data.status_summary || {};
    summaryLoaded.value = true;
    loadError.value = "";
    updatedAt.value = new Date().toISOString();
    selectedKeys.value = selectedKeys.value.filter((key) =>
      data.items.some((task) => task.key === key),
    );
  } catch (error: any) {
    if (sequence !== loadSequence) return;
    loadError.value = error.message || "加载统一任务失败";
    if (!silent) store.notify(loadError.value, "error");
  } finally {
    if (silent) pollInFlight = false;
    if (sequence === loadSequence) loading.value = false;
  }
}
function syncQuery() {
  const query: Record<string, string> = {};
  if (inspectedKey.value) query.task_key = inspectedKey.value;
  if (sort.value !== "created_desc") query.sort = sort.value;
  if (platform.value) query.platform = platform.value;
  if (status.value) query.status = status.value;
  if (search.value.trim()) query.q = search.value.trim();
  if (page.value > 1) query.page = String(page.value);
  void router.replace({ path: "/operations/tasks", query });
}
function resetAndLoad() {
  page.value = 1;
  selectedKeys.value = [];
  syncQuery();
  void load();
}
function queueSearch() {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(resetAndLoad, 350);
}
function toggleTask(taskKey: string) {
  selectedKeys.value = selectedKeys.value.includes(taskKey)
    ? selectedKeys.value.filter((key) => key !== taskKey)
    : [...selectedKeys.value, taskKey];
}
function togglePage() {
  selectedKeys.value = allPageSelected.value
    ? []
    : tasks.value.map((task) => task.key);
}
function setStatus(next: string) {
  status.value = next === status.value ? "" : next;
  resetAndLoad();
}
async function loadRetryJob() {
  if (retryPollInFlight || retryAllBusy.value) return;
  retryPollInFlight = true;
  const sequence = ++retryJobSequence;
  try {
    const data = await api<RetryJob>("/operations/tasks/retry-all-failed");
    if (sequence === retryJobSequence) {
      retryJob.value = data;
      retryJobError.value = "";
    }
  } catch (error: any) {
    if (sequence === retryJobSequence)
      retryJobError.value =
        error.message || "全部重试进度暂不可用，请刷新核对；不要重复提交。";
  } finally {
    retryPollInFlight = false;
  }
}
async function retryAllFailed() {
  if (
    retryAllBusy.value ||
    retryJobActive.value ||
    deleteAllBusy.value ||
    deleteJobActive.value ||
    actionBusy.value
  )
    return;
  retryAllBusy.value = true;
  ++retryJobSequence;
  const targetPlatform = platform.value;
  const scope = targetPlatform
    ? platformNames[targetPlatform] || targetPlatform
    : "全部平台";
  try {
    const preview = await api<{ total: number }>(
      `/operations/tasks/retry-all-failed/preview?platform=${encodeURIComponent(targetPlatform)}`,
    );
    if (!preview.total) {
      store.notify(`${scope}没有失败任务`, "info");
      return;
    }
    if (
      !(await confirmAction(
        `重试${scope}的全部 ${preview.total.toLocaleString()} 个失败任务？\n跨所有分页，不受搜索和当前选择限制，不包含已取消任务。\n采用普通重试；抖音直链返回 403、404、410 时自动尝试刷新，不强制刷新所有链接。\n后台逐项重新排队，下载仍遵循并发限制；提交时数量可能变化。`,
      ))
    )
      return;
    const result = await api<{ message: string; data: RetryJob }>(
      "/operations/tasks/retry-all-failed",
      {
        method: "POST",
        ...jsonBody({ platform: targetPlatform }),
      },
    );
    retryJob.value = result.data;
    retryJobError.value = "";
    store.notify(result.message, "info");
    await load();
  } catch (error: any) {
    store.notify(
      error.message || "全部重试提交失败，请核对后台作业状态",
      "error",
    );
  } finally {
    retryAllBusy.value = false;
    void loadRetryJob();
  }
}
async function runAction(taskKeys: string[], action: TaskAction) {
  if (!taskKeys.length || actionBusy.value) return;
  if (
    action === "cancel" &&
    !(await confirmAction(
      `确定取消 ${taskKeys.length} 个任务？已保存的文件不会删除。`,
    ))
  )
    return;
  if (
    action === "delete" &&
    !(await confirmAction(
      `删除 ${taskKeys.length} 个失败或已取消任务？\n删除任务及关联下载历史，不删除磁盘文件、作者或作品。\n后续订阅扫描若再次发现该作品，会补建缺失任务并重新下载；本次不立即重试。\n其他平台已保存部分媒体的任务会保留并说明原因。任务记录删除不可撤销。`,
    ))
  )
    return;
  actionBusy.value = true;
  try {
    const result = await api<UnifiedTaskActionResult>(
      "/operations/tasks/actions",
      {
        method: "POST",
        ...jsonBody({ action, task_keys: taskKeys }),
      },
    );
    const failed = result.data?.failed || [];
    actionFailures.value = failed;
    store.notify(
      result.message || "操作完成",
      failed.length ? (result.success ? "info" : "error") : "success",
    );
    await load();
  } catch (error: any) {
    store.notify(error.message || "任务操作失败", "error");
  } finally {
    actionBusy.value = false;
  }
}
async function loadDeleteJob() {
  if (deletePollInFlight || deleteAllBusy.value) return;
  deletePollInFlight = true;
  const sequence = ++deleteJobSequence;
  try {
    const data = await api<RetryJob>("/operations/tasks/delete-all-failed");
    if (sequence === deleteJobSequence) {
      deleteJob.value = data;
      deleteJobError.value = "";
    }
  } catch (error: any) {
    if (sequence === deleteJobSequence)
      deleteJobError.value =
        error.message || "删除进度暂不可用，请刷新核对，不要重复提交。";
  } finally {
    deletePollInFlight = false;
  }
}
async function deleteAllFailed() {
  if (
    deleteAllBusy.value ||
    deleteJobActive.value ||
    retryAllBusy.value ||
    retryJobActive.value ||
    actionBusy.value
  )
    return;
  deleteAllBusy.value = true;
  ++deleteJobSequence;
  const targetPlatform = platform.value;
  const scope = targetPlatform
    ? platformNames[targetPlatform] || targetPlatform
    : "全部平台";
  try {
    const preview = await api<{ total: number }>(
      `/operations/tasks/delete-all-failed/preview?platform=${encodeURIComponent(targetPlatform)}`,
    );
    if (!preview.total) {
      store.notify(`${scope}没有失败任务`, "info");
      return;
    }
    if (
      !(await confirmAction(
        `删除${scope}的全部 ${preview.total.toLocaleString()} 个失败任务？\n跨所有分页，不受搜索和当前选择限制，不包含已取消任务。\n删除任务及关联下载历史，保留磁盘文件、作者和作品；任务记录删除不可撤销。\n后续订阅再次发现作品时，会补建缺失任务重新下载。状态已变化或保存部分媒体的任务会跳过并说明原因。`,
      ))
    )
      return;
    const result = await api<{ message: string; data: RetryJob }>(
      "/operations/tasks/delete-all-failed",
      {
        method: "POST",
        ...jsonBody({ platform: targetPlatform }),
      },
    );
    deleteJob.value = result.data;
    deleteJobError.value = "";
    store.notify(result.message, "info");
    await load();
  } catch (error: any) {
    store.notify(
      error.message || "全部删除提交失败，请核对后台作业状态",
      "error",
    );
  } finally {
    deleteAllBusy.value = false;
    void loadDeleteJob();
  }
}
async function action(task: UnifiedTask, actionName: TaskAction) {
  if (rowBusy.value.includes(task.key)) return;
  rowBusy.value = [...rowBusy.value, task.key];
  try {
    await runAction([task.key], actionName);
  } finally {
    rowBusy.value = rowBusy.value.filter((key) => key !== task.key);
  }
}
async function preview(task: UnifiedTask) {
  try {
    if (task.preview_endpoint)
      return openMedia([
        {
          url: `/api${task.preview_endpoint}`,
          type: task.media_type === "image" ? "image" : "video",
          title: task.source_label,
        },
      ]);
    if (!task.media_endpoint) return;
    const assets = await api<any[]>(task.media_endpoint);
    const items: MediaItem[] = assets.map((item) => ({
      url: item.preview_url,
      type: item.media_type === "video" ? "video" : "image",
      title: item.title || item.filename,
    }));
    if (items.length) openMedia(items);
    else store.notify("该任务没有可预览资源", "info");
  } catch (error: any) {
    store.notify(error.message || "预览失败", "error");
  }
}
function changePage(value: number) {
  page.value = value;
  syncQuery();
  void load();
}
async function copyFailure(task: UnifiedTask) {
  try {
    await navigator.clipboard.writeText(
      `平台：${platformNames[task.platform] || task.platform}\n任务：${task.key}\n来源：${task.source_label}\n错误代码：${task.error_code || "未提供"}\n失败原因：${task.error_message || "未提供"}`,
    );
    store.notify("失败信息已复制");
  } catch {
    store.notify("复制失败，请检查浏览器剪贴板权限", "error");
  }
}
onMounted(() => {
  void load();
  void loadRetryJob();
  void loadDeleteJob();
  pollTimer = window.setInterval(() => {
    if (!document.hidden && !loading.value && !actionBusy.value)
      void load(true);
    if (!document.hidden) void loadRetryJob();
    if (!document.hidden) void loadDeleteJob();
  }, 5000);
});
onBeforeUnmount(() => {
  loadSequence++;
  retryJobSequence++;
  deleteJobSequence++;
  window.clearInterval(pollTimer);
  window.clearTimeout(searchTimer);
});
function newDownload() {
  window.dispatchEvent(new CustomEvent("app:new-download"));
}
function openTask(task: UnifiedTask) {
  void router.replace({ query: { ...route.query, task_key: task.key } });
}
function closeTask() {
  void router.replace({ query: { ...route.query, task_key: undefined } });
}
async function inspectorAction(task: UnifiedTask, name: TaskAction) {
  await action(task, name);
  await inspector.value?.refresh();
}
async function platformAction(endpoint: string) {
  if (actionBusy.value || platform.value !== "douyin") return;
  if (
    !(await confirmAction(
      "操作范围为全部抖音任务，跨所有分页，不受当前搜索、状态或选择限制。",
      "平台批处理",
      "继续操作",
      false,
    ))
  )
    return;
  actionBusy.value = true;
  try {
    const data = await api<any>("/tasks/" + endpoint, { method: "POST" });
    store.notify(data.message || "操作已提交");
    await load();
  } catch (e: any) {
    store.notify(e.message, "error");
  } finally {
    actionBusy.value = false;
  }
}
useContextActions(() => [
  ...(inspector.value?.task
    ? [
        { id: "inspect-current", label: "关闭当前任务详情", run: closeTask },
        ...(["failed", "cancelled"].includes(inspector.value.task.status)
          ? [
              {
                id: "retry-current",
                label: "重试当前任务",
                run: () => inspectorAction(inspector.value!.task!, "retry"),
                disabled: actionBusy.value,
              },
            ]
          : []),
      ]
    : []),
  { id: "refresh-tasks", label: "刷新下载任务", run: () => load() },
  {
    id: "retry-failures",
    label: "重试全部失败任务",
    run: retryAllFailed,
    disabled: actionBusy.value || retryJobActive.value,
  },
]);
watch(
  () => route.fullPath,
  () => {
    const nextPlatform = queryText(route.query.platform),
      nextStatus = queryText(route.query.status),
      nextSearch = queryText(route.query.q),
      nextPage = queryPage(route.query.page),
      nextSort = queryText(route.query.sort) || "created_desc";
    if (
      nextPlatform === platform.value &&
      nextStatus === status.value &&
      nextSearch === search.value.trim() &&
      nextPage === page.value &&
      nextSort === sort.value
    )
      return;
    platform.value = nextPlatform;
    status.value = nextStatus;
    search.value = nextSearch;
    page.value = nextPage;
    sort.value = nextSort;
    selectedKeys.value = [];
    void load();
  },
);
</script>

<template>
  <section class="workspace-page unified-workspace">
    <PageHeader
      title="下载任务"
      description="跨平台管理队列、结果与失败记录。"
      :busy="loading"
      :updated-at="updatedAt"
      refreshable
      @refresh="
        load();
        loadRetryJob();
        loadDeleteJob();
      "
    >
      <MoreActions label="任务批处理"
        ><button
          :disabled="
            retryAllBusy || retryJobActive || deleteJobActive || actionBusy
          "
          @click="retryAllFailed"
        >
          重试全部失败</button
        ><button
          class="danger"
          :disabled="
            deleteAllBusy || deleteJobActive || retryJobActive || actionBusy
          "
          @click="deleteAllFailed"
        >
          删除全部失败记录</button
        ><template v-if="platform === 'douyin'"
          ><button :disabled="actionBusy" @click="platformAction('pause-all')">
            暂停全部抖音任务</button
          ><button
            :disabled="actionBusy"
            @click="platformAction('redispatch-pending')"
          >
            分发抖音待处理任务</button
          ><button
            :disabled="actionBusy || store.risk.active"
            @click="platformAction('refresh-retry-all-failed')"
          >
            刷新链接后重试全部抖音失败
          </button></template
        ></MoreActions
      ><button class="btn primary" @click="newDownload">新建下载</button>
    </PageHeader>
    <nav class="view-tabs" aria-label="任务状态视图">
      <button
        :class="{ active: !status }"
        :aria-pressed="!status"
        @click="
          status = '';
          resetAndLoad();
        "
      >
        全部
        <span>{{
          summaryLoaded ? summaryTotal.toLocaleString() : "—"
        }}</span></button
      ><button
        v-for="item in [
          'downloading',
          'pending',
          'failed',
          'completed',
          'paused',
        ]"
        :key="item"
        :class="{ active: status === item }"
        :aria-pressed="status === item"
        @click="setStatus(item)"
      >
        {{ statusNames[item] }}
        <span
          :class="{ danger: item === 'failed' && statusSummary[item] > 0 }"
          >{{ statusSummary[item] ?? "—" }}</span
        >
      </button>
    </nav>
    <div v-if="retryJobError" class="load-error-banner" role="alert">
      <IssueDetail :message="retryJobError" /><button
        class="text-button"
        @click="loadRetryJob"
      >
        核对进度
      </button>
    </div>
    <JobReport
      v-if="retryJob.status !== 'idle'"
      :title="
        (retryJobNames[retryJob.status] || retryJob.status) +
        ' · ' +
        (retryJob.platform ? platformNames[retryJob.platform] : '全部平台')
      "
      :status="retryJob.status"
      :processed="retryJob.processed"
      :total="retryJob.total"
      :failed="retryJob.failed"
    >
      <span
        >已处理 {{ retryJob.processed || 0 }}/{{ retryJob.total || 0 }} · 已提交
        {{ retryJob.succeeded || 0 }} · 跳过 {{ retryJob.skipped || 0 }} ·
        未提交 {{ retryJob.failed || 0 }} · 未处理
        {{
          Math.max(0, (retryJob.total || 0) - (retryJob.processed || 0))
        }}</span
      >
      <small
        >这是重试投递结果，不代表下载已完成。仅处理提交时的失败任务；状态已变化的任务会跳过。</small
      >
      <IssueDetail v-if="retryJob.error" :message="retryJob.error" />
      <details v-if="retryJob.failures?.length">
        <summary>查看未提交与待核对原因（最多 200 条）</summary>
        <ul>
          <li v-for="item in retryJob.failures" :key="item.task_key">
            <b>{{ item.task_key }}</b> {{ item.message }}
          </li>
        </ul>
      </details>
    </JobReport>
    <div v-if="deleteJobError" class="load-error-banner" role="alert">
      <IssueDetail :message="deleteJobError" /><button
        class="text-button"
        @click="loadDeleteJob"
      >
        核对进度
      </button>
    </div>
    <JobReport
      v-if="deleteJob.status !== 'idle'"
      :title="
        (deleteJobNames[deleteJob.status] || deleteJob.status) +
        ' · ' +
        (deleteJob.platform ? platformNames[deleteJob.platform] : '全部平台')
      "
      :status="deleteJob.status"
      :processed="deleteJob.processed"
      :total="deleteJob.total"
      :failed="deleteJob.failed"
    >
      <span
        >已处理 {{ deleteJob.processed || 0 }}/{{ deleteJob.total || 0 }} ·
        已删除 {{ deleteJob.succeeded || 0 }} · 跳过
        {{ deleteJob.skipped || 0 }} · 删除失败 {{ deleteJob.failed || 0 }} ·
        未处理
        {{
          Math.max(0, (deleteJob.total || 0) - (deleteJob.processed || 0))
        }}</span
      >
      <small
        >只处理提交时的失败任务，不删除磁盘文件。状态已变化的任务会跳过；订阅仍可补建缺失任务。</small
      >
      <IssueDetail v-if="deleteJob.error" :message="deleteJob.error" />
      <details v-if="deleteJob.failures?.length">
        <summary>查看删除失败与待核对原因（最多 200 条）</summary>
        <ul>
          <li v-for="item in deleteJob.failures" :key="item.task_key">
            <b>{{ item.task_key }}</b> {{ item.message }}
          </li>
        </ul>
      </details>
    </JobReport>
    <div
      v-if="loadError && tasks.length"
      class="load-error-banner"
      role="alert"
    >
      <IssueDetail
        :message="loadError"
        impact="显示上次读取的数据，当前状态未确认。"
      /><button class="text-button" @click="load()">重试</button>
    </div>
    <FilterToolbar
      :active="
        [platform && platformNames[platform], status && statusNames[status]]
          .filter(Boolean)
          .join(' · ')
      "
      @clear="
        platform = '';
        status = '';
        resetAndLoad();
      "
      ><template #search
        ><label class="search"
          ><Search :size="16" /><input
            v-model="search"
            aria-label="搜索任务"
            placeholder="搜索作者、标题、作品 ID"
            @input="queueSearch" /></label></template
      ><select v-model="platform" aria-label="平台" @change="resetAndLoad">
        <option value="">全部平台</option>
        <option v-for="(name, id) in platformNames" :key="id" :value="id">
          {{ name }}
        </option></select
      ><select v-model="status" aria-label="任务状态" @change="resetAndLoad">
        <option value="">全部状态</option>
        <option v-for="item in statusOrder" :key="item" :value="item">
          {{ statusNames[item] }}
        </option></select
      ><select v-model="sort" aria-label="任务排序" @change="resetAndLoad">
        <option value="created_desc">最新创建</option>
        <option value="created_asc">最早创建</option>
      </select></FilterToolbar
    >
    <div v-if="selectedKeys.length" class="selection-bar">
      <span>已选择 {{ selectedKeys.length }} 个当前页任务</span>
      <div>
        <button
          class="btn compact"
          :disabled="actionBusy || !retryableSelected.length"
          @click="
            runAction(
              retryableSelected.map((task) => task.key),
              'retry',
            )
          "
        >
          重试 {{ retryableSelected.length }}</button
        ><button
          class="btn compact"
          :disabled="actionBusy || !cancellableSelected.length"
          @click="
            runAction(
              cancellableSelected.map((task) => task.key),
              'cancel',
            )
          "
        >
          取消 {{ cancellableSelected.length }}</button
        ><button
          class="btn compact danger"
          :disabled="actionBusy || !deletableSelected.length"
          @click="
            runAction(
              deletableSelected.map((task) => task.key),
              'delete',
            )
          "
        >
          删除记录 {{ deletableSelected.length }}</button
        ><button class="text-button" @click="selectedKeys = []">
          取消选择
        </button>
      </div>
    </div>
    <details v-if="actionFailures.length" class="action-report">
      <summary>{{ actionFailures.length }} 个任务未处理</summary>
      <ul>
        <li v-for="item in actionFailures" :key="item.task_key">
          {{ item.task_key }} · {{ item.message }}
        </li>
      </ul>
    </details>
    <StateView
      v-if="!tasks.length"
      :loading="loading"
      :error="loadError"
      title="没有符合条件的任务"
      description="调整筛选条件，或新建一个下载任务。"
      @retry="load()"
    />
    <div v-else class="table-shell">
      <table class="data-table unified-table">
        <thead>
          <tr>
            <th class="select-col">
              <label class="selection-hit"
                ><input
                  type="checkbox"
                  aria-label="选择当前页全部任务"
                  :checked="allPageSelected"
                  @change="togglePage"
              /></label>
            </th>
            <th>任务与来源</th>
            <th>状态与进度</th>
            <th>文件</th>
            <th>创建时间</th>
            <th class="actions-col">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="task in tasks"
            :key="task.key"
            :class="{
              selected:
                selectedKeys.includes(task.key) || inspectedKey === task.key,
            }"
          >
            <td class="select-col">
              <label class="selection-hit"
                ><input
                  type="checkbox"
                  :aria-label="'选择任务 ' + task.key"
                  :checked="selectedKeys.includes(task.key)"
                  @change="toggleTask(task.key)"
              /></label>
            </td>
            <td class="task-source">
              <button
                class="record-link"
                :title="task.source_label"
                @click="openTask(task)"
              >
                {{ task.source_label || "任务 #" + task.id }}</button
              ><span
                >{{ platformNames[task.platform] }} ·
                {{
                  task.author_name ||
                  (task.source_type === "profile" ? "作者主页" : "单条作品")
                }}
                · #{{ task.id }}</span
              >
            </td>
            <td data-label="状态">
              <StatusIndicator :status="task.status" /><small
                v-if="task.status === 'downloading'"
                >{{ Number(task.progress_percent || 0).toFixed(1) }}%</small
              ><button
                v-if="task.error_message"
                class="error-excerpt"
                :title="task.error_message"
                @click="openTask(task)"
              >
                {{ task.error_message }}
              </button>
            </td>
            <td data-label="文件">{{ task.file_count }}</td>
            <td data-label="创建时间">
              <time>{{ dateTime(task.created_at) }}</time>
            </td>
            <td class="actions-col">
              <MoreActions :label="'任务 ' + task.id"
                ><button @click="openTask(task)">查看详情</button
                ><button v-if="task.preview_count" @click="preview(task)">
                  预览文件</button
                ><button
                  v-if="['failed', 'cancelled'].includes(task.status)"
                  :disabled="actionBusy"
                  @click="action(task, 'retry')"
                >
                  重试</button
                ><button
                  v-if="
                    task.platform === 'douyin' &&
                    ['pending', 'downloading'].includes(task.status)
                  "
                  :disabled="actionBusy"
                  @click="action(task, 'pause')"
                >
                  暂停</button
                ><button
                  v-if="task.platform === 'douyin' && task.status === 'paused'"
                  :disabled="actionBusy"
                  @click="action(task, 'resume')"
                >
                  恢复</button
                ><button
                  v-if="
                    ['pending', 'downloading', 'paused'].includes(task.status)
                  "
                  :disabled="actionBusy"
                  @click="action(task, 'cancel')"
                >
                  取消任务</button
                ><button v-if="task.error_message" @click="copyFailure(task)">
                  复制失败信息</button
                ><button
                  v-if="['failed', 'cancelled'].includes(task.status)"
                  class="danger"
                  :disabled="actionBusy"
                  @click="action(task, 'delete')"
                >
                  删除任务记录
                </button></MoreActions
              >
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <Pager
      v-if="summaryLoaded"
      :page="page"
      :pages="pages"
      :total="total"
      @change="changePage"
    />
    <TaskInspector
      ref="inspector"
      :task-key="inspectedKey"
      :busy="actionBusy"
      @close="closeTask"
      @action="inspectorAction"
      @changed="load()"
    />
  </section>
</template>
