<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Clipboard, Search, Zap } from "@lucide/vue";
import { api } from "../api";
import { useAppStore } from "../stores/app";
import PageHeader from "../components/PageHeader.vue";
import StatusIndicator from "../components/StatusIndicator.vue";
import StateView from "../components/StateView.vue";
import InspectorPanel from "../components/InspectorPanel.vue";
import MoreActions from "../components/MoreActions.vue";
import Pager from "../components/Pager.vue";
import IssueDetail from "../components/IssueDetail.vue";
import {
  confirmAction,
  dateTime,
  duration,
  useContextActions,
} from "../workspace";
import type { AutomationRun, AutomationCycle, AutomationPage } from "../types";
const store = useAppStore(),
  route = useRoute(),
  router = useRouter();
const reports = ref<AutomationRun[]>([]),
  cycle = ref<AutomationCycle>({}),
  page = ref(Number(route.query.page) || 1),
  pages = ref(1),
  total = ref(0);
const loading = ref(false),
  loaded = ref(false),
  loadError = ref(""),
  updatedAt = ref(""),
  busy = ref(false),
  detail = ref<AutomationRun>(),
  detailBusy = ref(false),
  detailError = ref("");
const runId = computed(() => Number(route.query.run) || 0),
  authorSearch = ref(""),
  onlyIssues = ref(false);
const results = computed(() =>
  (detail.value?.details || []).filter(
    (item) =>
      (!onlyIssues.value ||
        ["failed", "warning", "error"].includes(item.status) ||
        item.error ||
        item.warning) &&
      (!authorSearch.value ||
        (item.nickname || String(item.author_id)).includes(authorSearch.value)),
  ),
);
let timer: number | undefined,
  sequence = 0,
  detailSequence = 0,
  inFlight = false;
function metric(value?: number) {
  return loaded.value && value != null ? value.toLocaleString() : "—";
}
async function load(silent = false) {
  if (silent && inFlight) return;
  inFlight = true;
  loading.value = true;
  const current = ++sequence;
  try {
    const data = await api<AutomationPage>(
      `/authors/reports/subscriptions?paginated=true&page=${page.value}&page_size=20`,
    );
    if (current !== sequence) return;
    reports.value = data.items;
    cycle.value = data.cycle;
    pages.value = data.pages;
    total.value = data.total;
    loaded.value = true;
    loadError.value = "";
    updatedAt.value = new Date().toISOString();
    if (page.value > Math.max(1, data.pages))
      changePage(Math.max(1, data.pages));
  } catch (e: any) {
    if (current === sequence) loadError.value = e.message || "无法读取运行记录";
  } finally {
    if (current === sequence) {
      inFlight = false;
      loading.value = false;
    }
  }
}
async function loadDetail(silent = false) {
  const current = ++detailSequence;
  if (!silent) detail.value = undefined;
  detailError.value = "";
  if (!runId.value) return;
  detailBusy.value = true;
  try {
    const data = await api<AutomationRun>(
      `/authors/reports/subscriptions/${runId.value}`,
    );
    if (current === detailSequence) detail.value = data;
  } catch (e: any) {
    if (current === detailSequence)
      detailError.value = e.message || "无法读取运行详情";
  } finally {
    if (current === detailSequence) detailBusy.value = false;
  }
}
function openRun(id: number) {
  authorSearch.value = "";
  onlyIssues.value = false;
  void router.replace({ query: { ...route.query, run: String(id) } });
}
function closeRun() {
  void router.replace({ query: { ...route.query, run: undefined } });
}
function changePage(value: number) {
  page.value = value;
  void router.replace({
    query: { ...route.query, page: value > 1 ? String(value) : undefined },
  });
}
async function run(reconcile = false) {
  if (busy.value || store.risk.active) return;
  if (
    reconcile &&
    !(await confirmAction(
      "全量对账会读取每位订阅作者的全部作品，耗时和请求量高于增量检查。已有检查进度会保留。",
      "提交全量对账",
      "开始对账",
      false,
    ))
  )
    return;
  busy.value = true;
  try {
    const data = await api<any>(
      reconcile ? "/authors/reconcile-all" : "/authors/check-all",
      { method: "POST" },
    );
    store.notify(data.message || "检查已提交");
    await load();
  } catch (e: any) {
    store.notify(e.message || "提交失败", "error");
  } finally {
    busy.value = false;
  }
}
async function copyDiagnostic() {
  try {
    const data = await api("/authors/reports/subscriptions/diagnostic");
    await navigator.clipboard.writeText(JSON.stringify(data, null, 2));
    store.notify("诊断已复制，敏感凭据已排除");
  } catch (e: any) {
    store.notify(e.message || "复制失败", "error");
  }
}
function trigger(value?: string) {
  return value === "manual"
    ? "手动检查"
    : value === "reconcile"
      ? "全量对账"
      : "自动调度";
}
useContextActions(() => [
  {
    id: "check-all",
    label: "检查全部订阅作者",
    run: () => run(),
    disabled: store.risk.active || busy.value,
  },
  { id: "diagnostic", label: "复制自动化诊断", run: copyDiagnostic },
]);
watch(runId, () => loadDetail());
watch(
  () => route.query.page,
  () => {
    page.value = Math.max(1, Number(route.query.page) || 1);
    void load();
  },
);
onMounted(() => {
  void load();
  void loadDetail();
  timer = window.setInterval(() => {
    if (!document.hidden) {
      void load(true);
      if (detail.value?.status === "running") void loadDetail(true);
    }
  }, 10000);
});
onBeforeUnmount(() => {
  sequence++;
  detailSequence++;
  clearInterval(timer);
});
</script>
<template>
  <section class="workspace-page automation-page">
    <PageHeader
      title="自动化"
      description="监督订阅检查，定位异常，再继续未完成的工作。"
      :busy="loading"
      :updated-at="updatedAt"
      refreshable
      @refresh="load"
    >
      <MoreActions label="自动化操作"
        ><button :disabled="busy || store.risk.active" @click="run(true)">
          全量对账</button
        ><button @click="copyDiagnostic">
          <Clipboard :size="15" />复制诊断</button
        ><RouterLink to="/settings/subscriptions"
          >调度设置</RouterLink
        ></MoreActions
      >
      <button
        class="btn primary"
        :disabled="busy || store.risk.active"
        @click="run()"
      >
        <Zap :size="15" />{{ busy ? "提交中…" : "检查全部" }}
      </button>
    </PageHeader>
    <div v-if="loadError && loaded" class="load-error-banner" role="alert">
      <IssueDetail
        :message="loadError"
        impact="显示上次读取的数据；当前状态未确认。"
      /><button class="text-button" @click="load()">重试</button>
    </div>
    <section class="cycle-strip" aria-label="当前检查周期">
      <div>
        <span class="eyebrow">当前周期</span
        ><strong>{{
          !loaded || cycle.active == null
            ? "状态未确认"
            : cycle.active
              ? "检查进行中"
              : "等待下一轮调度"
        }}</strong>
      </div>
      <dl>
        <div>
          <dt>订阅作者</dt>
          <dd>{{ metric(cycle.total_authors) }}</dd>
        </div>
        <div>
          <dt>已检查</dt>
          <dd>{{ metric(cycle.checked_authors) }}</dd>
        </div>
        <div>
          <dt>新作品</dt>
          <dd>{{ metric(cycle.new_works) }}</dd>
        </div>
        <div>
          <dt>待续检</dt>
          <dd>{{ metric(cycle.remaining_authors) }}</dd>
        </div>
      </dl>
      <progress
        v-if="cycle.total_authors"
        :max="cycle.total_authors"
        :value="cycle.checked_authors || 0"
        aria-label="周期检查进度"
      />
    </section>
    <div class="section-heading">
      <h2>运行历史</h2>
      <span
        >{{ loaded ? total.toLocaleString() + " 次运行" : "—" }} ·
        抖音订阅</span
      >
    </div>
    <StateView
      v-if="!reports.length"
      :loading="loading"
      :error="loadError"
      title="还没有运行记录"
      description="订阅作者后，自动检查将在这里留下记录。"
      @retry="load"
    />
    <div v-else class="table-shell">
      <table class="data-table run-table">
        <thead>
          <tr>
            <th>时间 / 来源</th>
            <th>检查人数</th>
            <th>新作品</th>
            <th>待续检</th>
            <th>耗时</th>
            <th>结果</th>
            <th>异常</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="report in reports"
            :key="report.id"
            :class="{ selected: runId === report.id }"
          >
            <td data-label="时间">
              <button class="record-link" @click="openRun(report.id)">
                {{ dateTime(report.started_at) }}</button
              ><span>{{ trigger(report.trigger_type) }}</span>
            </td>
            <td data-label="检查人数">
              {{ report.checked_authors
              }}<small> / {{ report.total_authors }}</small>
            </td>
            <td data-label="新作品">{{ report.new_works }}</td>
            <td data-label="待续检">{{ report.remaining_authors }}</td>
            <td data-label="耗时">
              {{
                report.status === "running"
                  ? "执行中"
                  : duration(report.started_at, report.finished_at)
              }}
            </td>
            <td data-label="结果">
              <StatusIndicator :status="report.status" />
            </td>
            <td data-label="异常">
              <button
                v-if="report.failed_authors + report.warning_authors"
                class="text-button danger"
                @click="
                  openRun(report.id);
                  onlyIssues = true;
                "
              >
                {{ report.failed_authors + report.warning_authors }} 位</button
              ><span v-else class="muted">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <Pager
      v-if="loaded"
      :page="page"
      :pages="pages"
      :total="total"
      @change="changePage"
    />
    <InspectorPanel
      :open="!!runId"
      :title="'运行 #' + runId"
      :subtitle="
        dateTime(detail?.started_at) + ' · ' + trigger(detail?.trigger_type)
      "
      @close="closeRun"
    >
      <StateView
        v-if="!detail"
        :loading="detailBusy"
        :error="detailError"
        @retry="loadDetail"
      />
      <template v-else
        ><StatusIndicator :status="detail.status" />
        <p class="detail-summary">{{ detail.summary || "本次检查结果" }}</p>
        <dl class="detail-grid">
          <div>
            <dt>检查</dt>
            <dd>{{ detail.checked_authors }} 位</dd>
          </div>
          <div>
            <dt>新作品</dt>
            <dd>{{ detail.new_works }} 个</dd>
          </div>
          <div>
            <dt>待续检</dt>
            <dd>{{ detail.remaining_authors }} 位</dd>
          </div>
          <div>
            <dt>耗时</dt>
            <dd>{{ duration(detail.started_at, detail.finished_at) }}</dd>
          </div>
        </dl>
        <p v-if="detail.remaining_authors" class="inline-note">
          未完成的作者会在后续调度中继续检查。账号或上游异常的恢复条件见作者详情。
        </p>
        <div class="section-heading">
          <h3>作者结果</h3>
          <label><input v-model="onlyIssues" type="checkbox" /> 仅异常</label>
        </div>
        <label class="search"
          ><Search :size="15" /><input
            v-model="authorSearch"
            placeholder="在本次结果中查找作者"
            aria-label="搜索本次作者结果"
        /></label>
        <div class="result-list">
          <article
            v-for="(item, index) in results"
            :key="item.author_id || index"
          >
            <div>
              <RouterLink
                v-if="item.author_id"
                :to="`/authors/douyin?focus=${item.author_id}`"
                >{{ item.nickname || "作者 " + item.author_id }}</RouterLink
              ><strong v-else>{{ item.nickname || "作者未确认" }}</strong
              ><StatusIndicator :status="item.status" />
            </div>
            <span v-if="item.new_works">新作品 {{ item.new_works }} 个</span
            ><IssueDetail
              v-if="item.message || item.error || item.warning"
              :message="item.message || item.error || item.warning || ''"
              :code="item.error_code"
            /><small v-if="item.http_status">HTTP {{ item.http_status }}</small>
          </article>
          <p v-if="!results.length" class="inline-note">没有匹配的作者结果。</p>
        </div></template
      >
    </InspectorPanel>
  </section>
</template>
