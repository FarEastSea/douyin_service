<script setup lang="ts">
import { ref, watch, onBeforeUnmount, onMounted } from "vue";
import { api } from "../api";
import { openMedia } from "../media";
import { useAppStore } from "../stores/app";
import { bytes, confirmAction, dateTime, platformNames } from "../workspace";
import type { TaskDetail } from "../types";
import InspectorPanel from "./InspectorPanel.vue";
import StatusIndicator from "./StatusIndicator.vue";
import StateView from "./StateView.vue";
import IssueDetail from "./IssueDetail.vue";
import TrendHistory from "./TrendHistory.vue";
const props = defineProps<{ taskKey: string; busy?: boolean }>();
const emit = defineEmits<{
  close: [];
  action: [
    task: TaskDetail,
    action:
      "retry" | "pause" | "resume" | "cancel" | "refresh_retry" | "delete",
  ];
  changed: [];
}>();
const deleting = ref(false);
const store = useAppStore(),
  task = ref<TaskDetail>(),
  loading = ref(false),
  error = ref(""),
  tab = ref("overview"),
  files = ref<any[]>([]),
  logs = ref<string[]>([]),
  resourceBusy = ref(false),
  resourceError = ref("");
let sequence = 0,
  resourceSequence = 0;
let pollTimer: number | undefined,
  pollInFlight = false;
async function refresh() {
  if (!props.taskKey || pollInFlight) return;
  pollInFlight = true;
  const current = sequence,
    key = props.taskKey;
  const [platform, id] = key.split(":");
  try {
    const data = await api<TaskDetail>(
      `/operations/tasks/${encodeURIComponent(platform)}/${encodeURIComponent(id)}`,
    );
    if (current === sequence && key === props.taskKey) {
      task.value = data;
      error.value = "";
    }
  } catch (e: any) {
    if (current === sequence) error.value = e.message || "任务状态未确认";
  } finally {
    pollInFlight = false;
  }
}
async function load() {
  const current = ++sequence;
  task.value = undefined;
  error.value = "";
  tab.value = "overview";
  files.value = [];
  logs.value = [];
  ++resourceSequence;
  if (!props.taskKey) return;
  loading.value = true;
  const [platform, id] = props.taskKey.split(":");
  try {
    const data = await api<TaskDetail>(
      `/operations/tasks/${encodeURIComponent(platform)}/${encodeURIComponent(id)}`,
    );
    if (current === sequence) task.value = data;
  } catch (e: any) {
    if (current === sequence) error.value = e.message || "无法读取任务";
  } finally {
    if (current === sequence) loading.value = false;
  }
}
async function loadResource() {
  const current = ++resourceSequence;
  resourceError.value = "";
  resourceBusy.value = true;
  try {
    if (tab.value === "files" && task.value?.media_endpoint) {
      const data = await api<any[]>(task.value.media_endpoint);
      if (current === resourceSequence) files.value = data;
    }
    if (tab.value === "logs" && task.value?.log_endpoint) {
      const data = await api<any>(task.value.log_endpoint + "?start=0");
      if (current === resourceSequence) logs.value = data.lines || [];
    }
  } catch (e: any) {
    if (current === resourceSequence) resourceError.value = e.message;
  } finally {
    if (current === resourceSequence) resourceBusy.value = false;
  }
}
function previewFile(file: any) {
  openMedia([
    {
      url: file.preview_url,
      type: file.media_type === "video" ? "video" : "image",
      title: file.filename,
    },
  ]);
}
function previewTask() {
  if (task.value?.preview_endpoint)
    openMedia([
      {
        url: "/api" + task.value.preview_endpoint,
        type: task.value.media_type === "image" ? "image" : "video",
        title: task.value.source_label,
      },
    ]);
}
async function copyDiagnostic() {
  if (!task.value) return;
  try {
    await navigator.clipboard.writeText(
      JSON.stringify(
        {
          key: task.value.key,
          status: task.value.status,
          error_code: task.value.error_code,
          error: task.value.error_message,
          log: logs.value,
        },
        null,
        2,
      ),
    );
    store.notify("任务诊断已复制");
  } catch {
    store.notify("复制失败", "error");
  }
}
async function deleteRecord() {
  const item = task.value;
  if (!item || props.busy || deleting.value) return;
  if (
    !(await confirmAction(
      `删除任务 ${item.key} 的记录？保留已保存的磁盘文件。记录删除不可撤销。`,
      "删除任务记录",
      "删除记录",
    ))
  )
    return;
  deleting.value = true;
  const endpoint =
    item.platform === "douyin"
      ? `/tasks/${item.id}`
      : item.platform === "x"
        ? `/x/tasks/${item.id}`
        : `/platform-downloads/${item.platform}/tasks/${item.id}`;
  try {
    await api(endpoint, { method: "DELETE" });
    store.notify("任务记录已删除");
    emit("changed");
    emit("close");
  } catch (e: any) {
    store.notify(e.message, "error");
  } finally {
    deleting.value = false;
  }
}
defineExpose({ refresh, task });
onMounted(() => {
  pollTimer = window.setInterval(() => {
    if (
      !document.hidden &&
      ["pending", "downloading", "paused"].includes(task.value?.status || "")
    )
      void refresh();
  }, 5000);
});
watch(() => props.taskKey, load, { immediate: true });
watch(tab, loadResource);
onBeforeUnmount(() => {
  clearInterval(pollTimer);
  sequence++;
  resourceSequence++;
});
</script>
<template>
  <InspectorPanel
    :open="!!taskKey"
    :title="task?.source_label || '任务详情'"
    :subtitle="task ? platformNames[task.platform] + ' · #' + task.id : taskKey"
    @close="emit('close')"
    ><StateView
      v-if="!task"
      :loading="loading"
      :error="error"
      @retry="load"
    /><template v-else
      ><div v-if="error" class="load-error-banner" role="alert">
        <span>{{ error }}。显示上次读取的数据。</span
        ><button class="text-button" @click="refresh">重试</button>
      </div>
      <div class="detail-status">
        <StatusIndicator :status="task.status" /><span
          v-if="task.status === 'downloading'"
          >{{ Number(task.progress_percent || 0).toFixed(1) }}%</span
        >
      </div>
      <nav class="view-tabs" aria-label="任务详情">
        <button
          v-for="item in [
            ['overview', '概览'],
            ['files', '文件'],
            ['logs', '日志'],
            ['stats', '趋势'],
          ]"
          v-show="item[0] !== 'logs' || task.log_endpoint"
          :key="item[0]"
          :class="{ active: tab === item[0] }"
          :aria-pressed="tab === item[0]"
          @click="tab = item[0]"
        >
          {{ item[1] }}
        </button>
      </nav>
      <template v-if="tab === 'overview'"
        ><dl class="detail-fields">
          <div>
            <dt>作者</dt>
            <dd>{{ task.author_name || "未提供" }}</dd>
          </div>
          <div>
            <dt>创建时间</dt>
            <dd>{{ dateTime(task.created_at) }}</dd>
          </div>
          <div>
            <dt>完成时间</dt>
            <dd>{{ dateTime(task.completed_at) }}</dd>
          </div>
          <div>
            <dt>文件</dt>
            <dd>{{ task.file_count }} 个</dd>
          </div>
          <div v-if="task.file_name">
            <dt>文件名</dt>
            <dd>{{ task.file_name }}</dd>
          </div>
          <div v-if="task.transfer">
            <dt>传输</dt>
            <dd>
              {{ bytes(task.transfer.downloaded_bytes) }} /
              {{ bytes(task.transfer.total_bytes) }} ·
              {{ bytes(task.transfer.download_speed) }}/s
            </dd>
          </div>
          <div v-if="task.engine_name">
            <dt>引擎</dt>
            <dd>{{ task.engine_name }}</dd>
          </div>
        </dl>
        <a
          v-if="task.source_url"
          class="text-button"
          :href="task.source_url"
          target="_blank"
          rel="noopener noreferrer"
          >打开来源</a
        ><RouterLink
          v-if="task.work_id && task.author_id"
          class="text-button"
          :to="`/authors/douyin/${task.author_id}/works?work=${task.work_id}`"
          >查看作者作品</RouterLink
        ><IssueDetail
          v-if="task.error_message"
          :message="task.error_message"
          :code="task.error_code"
        />
        <p v-if="task.error_action" class="inline-note">
          {{ task.error_action }}
        </p>
        <button class="text-button" @click="copyDiagnostic">
          复制任务诊断
        </button></template
      ><template v-else-if="tab === 'files'"
        ><button v-if="task.preview_endpoint" class="btn" @click="previewTask">
          预览下载文件</button
        ><StateView
          v-else-if="resourceBusy || resourceError || !files.length"
          :loading="resourceBusy"
          :error="resourceError"
          title="暂无可用文件"
          @retry="loadResource"
        />
        <div class="file-list">
          <article v-for="file in files" :key="file.id">
            <span>{{ file.filename || file.title || "媒体文件" }}</span
            ><button
              v-if="file.preview_url"
              class="text-button"
              @click="previewFile(file)"
            >
              预览</button
            ><a
              v-if="file.download_url"
              class="text-button"
              :href="file.download_url"
              >下载</a
            >
          </article>
        </div></template
      ><template v-else-if="tab === 'logs'"
        ><StateView
          v-if="resourceBusy || resourceError || !logs.length"
          :loading="resourceBusy"
          :error="resourceError"
          title="暂无日志"
          @retry="loadResource"
        /><template v-else
          ><button class="text-button" @click="copyDiagnostic">
            复制日志与诊断
          </button>
          <pre class="log-output">{{ logs.join("\n") }}</pre>
        </template></template
      ><TrendHistory
        v-else-if="tab === 'stats' && task.stats_endpoint"
        :endpoint="task.stats_endpoint" /><StateView
        v-else
        title="暂无统计历史"
        description="平台返回互动统计后会开始记录。" /></template
    ><template #footer
      ><template v-if="task"
        ><button
          v-if="['failed', 'cancelled'].includes(task.status)"
          class="btn primary"
          :disabled="busy || deleting"
          @click="emit('action', task, 'retry')"
        >
          重试</button
        ><button
          v-if="
            task.platform === 'douyin' &&
            ['pending', 'downloading'].includes(task.status)
          "
          class="btn"
          :disabled="busy || deleting"
          @click="emit('action', task, 'pause')"
        >
          暂停</button
        ><button
          v-if="task.platform === 'douyin' && task.status === 'paused'"
          class="btn primary"
          :disabled="busy || deleting"
          @click="emit('action', task, 'resume')"
        >
          恢复</button
        ><button
          v-if="
            task.platform === 'douyin' &&
            ['failed', 'cancelled'].includes(task.status)
          "
          class="btn"
          :disabled="busy || deleting"
          @click="emit('action', task, 'refresh_retry')"
        >
          刷新链接重试</button
        ><button
          v-if="['pending', 'downloading', 'paused'].includes(task.status)"
          class="btn"
          :disabled="busy || deleting"
          @click="emit('action', task, 'cancel')"
        >
          取消任务</button
        ><button
          v-else
          class="btn danger"
          :disabled="busy || deleting"
          @click="deleteRecord"
        >
          删除记录
        </button></template
      ></template
    ></InspectorPanel
  >
</template>
