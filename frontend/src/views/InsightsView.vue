<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { AlertCircle, ArrowRight, CircleCheck, HardDrive, Images, TrendingUp } from "@lucide/vue";
import { api } from "../api";
import { bytes, usePlatformNames, statusPresentation, useContextActions } from "../workspace";
import KpiCard from "../components/KpiCard.vue";
import SectionCard from "../components/SectionCard.vue";
import PageHeader from "../components/PageHeader.vue";
import StateView from "../components/StateView.vue";
import BarChart, { type BarDatum } from "../components/BarChart.vue";
import RankBars, { type RankDatum } from "../components/RankBars.vue";
const platformNames = usePlatformNames();

interface Volume {
  files: number;
  bytes: number;
}
interface TrendDay extends Volume {
  date: string;
  by_platform: Record<string, Volume>;
}
interface DiskState {
  available: boolean;
  total_bytes: number | null;
  used_bytes: number | null;
  free_bytes: number | null;
  used_percent: number | null;
  warn_free_bytes: number;
  low: boolean;
}
interface Insights {
  days: number;
  generated_at: string;
  stale?: boolean;
  library: Volume & {
    orphaned_files: number;
    orphaned_bytes: number;
    by_platform: Array<Volume & { platform: string }>;
    by_type: Array<Volume & { media_type: string }>;
  };
  period: Volume & { previous_files: number; previous_bytes: number };
  trend: TrendDay[];
  top_authors: Array<Volume & { platform: string; author_name: string }>;
  tasks: {
    totals: Record<string, number>;
    success_rate: number | null;
    by_platform: Array<{ platform: string; total: number; statuses: Record<string, number> }>;
    failure_reasons: Array<{ platform: string; code: string; label: string; count: number }>;
  };
  subscriptions: {
    runs: number;
    new_works: number;
    attention: number;
    saved_files: number;
    daily: Array<{
      date: string;
      runs: number;
      new_works: number;
      attention: number;
      saved_files: number;
    }>;
  };
  disk: DiskState;
}

const WINDOWS = [7, 30, 90, 365] as const;
const typeNames: Record<string, string> = {
  video: "视频",
  image: "图片",
  live: "实况",
  unknown: "其他",
};
const route = useRoute(),
  router = useRouter();
const queryDays = () => {
  const value = Number(route.query.days);
  return (WINDOWS as readonly number[]).includes(value) ? value : 30;
};
const days = ref(queryDays());
const metric = ref<"files" | "bytes">("files");
const data = ref<Insights | null>(null);
const loading = ref(true),
  error = ref("");
let sequence = 0,
  controller: AbortController | undefined;

const count = (value: number) => value.toLocaleString("zh-CN");
const platformLabel = (id: string) => platformNames.value[id] || id;
function shortDate(value: string) {
  const [, month, day] = value.split("-");
  return `${Number(month)}/${Number(day)}`;
}

async function load(force = false) {
  const current = ++sequence;
  controller?.abort();
  controller = new AbortController();
  loading.value = true;
  try {
    const result = await api<Insights>(
      `/operations/insights?days=${days.value}${force ? "&refresh=true" : ""}`,
      {
        signal: controller.signal,
        timeoutMs: 30000,
      },
    );
    if (current !== sequence) return;
    data.value = result;
    error.value = "";
  } catch (reason: unknown) {
    if (current !== sequence || (reason instanceof DOMException && reason.name === "AbortError"))
      return;
    error.value = reason instanceof Error ? reason.message : "洞察数据读取失败";
  } finally {
    if (current === sequence) loading.value = false;
  }
}
function selectWindow(value: number) {
  if (days.value === value) return;
  days.value = value;
  void router.replace({
    query: { ...route.query, days: value === 30 ? undefined : String(value) },
  });
  void load();
}

// 超过 90 天时按周汇总，避免柱子细到无法分辨和悬停。
const trendBars = computed<BarDatum[]>(() => {
  const series = data.value?.trend || [];
  if (series.length <= 90)
    return series.map((item) => ({
      key: item.date,
      label: shortDate(item.date),
      value: item[metric.value],
    }));
  const weeks: BarDatum[] = [];
  for (let start = 0; start < series.length; start += 7) {
    const chunk = series.slice(start, start + 7);
    const first = chunk[0]!,
      last = chunk[chunk.length - 1]!;
    weeks.push({
      key: first.date,
      label:
        first === last ? shortDate(first.date) : `${shortDate(first.date)}–${shortDate(last.date)}`,
      tick: shortDate(first.date),
      value: chunk.reduce((sum, item) => sum + item[metric.value], 0),
    });
  }
  return weeks;
});
const trendFormat = computed(() =>
  metric.value === "bytes" ? (value: number) => bytes(value) : count,
);
const trendCaption = computed(
  () =>
    `最近 ${days.value} 天${trendBars.value.length < (data.value?.trend.length || 0) ? "每周" : "每日"}入库${metric.value === "bytes" ? "体积" : "文件数"}`,
);
const busiestDay = computed(() => {
  const series = data.value?.trend || [];
  return series.reduce<TrendDay | null>(
    (best, item) => (item.files > (best?.files || 0) ? item : best),
    null,
  );
});
const periodDelta = computed(() => {
  const period = data.value?.period;
  if (!period || !period.previous_files) return null;
  return Math.round(((period.files - period.previous_files) / period.previous_files) * 100);
});
const platformBars = computed<RankDatum[]>(() =>
  (data.value?.library.by_platform || []).map((item) => ({
    key: item.platform,
    label: platformLabel(item.platform),
    hint: `${count(item.files)} 个文件`,
    value: item.bytes,
    display: bytes(item.bytes),
    to: { path: "/library", query: { platform: item.platform } },
  })),
);
const typeBars = computed<RankDatum[]>(() =>
  (data.value?.library.by_type || []).map((item) => ({
    key: item.media_type,
    label: typeNames[item.media_type] || item.media_type,
    hint: `${count(item.files)} 个文件`,
    value: item.bytes,
    display: bytes(item.bytes),
    to:
      typeNames[item.media_type] && item.media_type !== "unknown"
        ? { path: "/library", query: { type: item.media_type } }
        : undefined,
  })),
);
const authorBars = computed<RankDatum[]>(() =>
  (data.value?.top_authors || []).map((item) => ({
    key: `${item.platform}:${item.author_name}`,
    label: item.author_name,
    hint: `${platformLabel(item.platform)} · ${count(item.files)} 个文件`,
    value: item.bytes,
    display: bytes(item.bytes),
    to: { path: "/library", query: { platform: item.platform, author: item.author_name } },
  })),
);
const subscriptionBars = computed<BarDatum[]>(() => {
  const series = data.value?.subscriptions.daily || [];
  const size = series.length > 90 ? 7 : 1;
  const bars: BarDatum[] = [];
  for (let start = 0; start < series.length; start += size) {
    const chunk = series.slice(start, start + size);
    const first = chunk[0]!,
      last = chunk[chunk.length - 1]!;
    bars.push({
      key: first.date,
      label:
        first === last ? shortDate(first.date) : `${shortDate(first.date)}–${shortDate(last.date)}`,
      tick: shortDate(first.date),
      value: chunk.reduce((sum, item) => sum + item.new_works + item.saved_files, 0),
    });
  }
  return bars;
});
// 成功率低于该值时才用警示色，避免少量失败就把整张卡片标红。
const LOW_SUCCESS_RATE = 90;
const lowSuccess = computed(() => {
  const rate = data.value?.tasks.success_rate;
  return rate !== null && rate !== undefined && rate < LOW_SUCCESS_RATE;
});
const taskStatuses = ["completed", "downloading", "pending", "failed"] as const;
const disk = computed(() => data.value?.disk);
const diskTone = computed(() =>
  !disk.value?.available ? "unknown" : disk.value.low ? "low" : "ok",
);

useContextActions(() => [
  { id: "refresh-insights", label: "重新统计洞察数据", run: () => load(true) },
]);
watch(
  () => route.query.days,
  () => {
    const next = queryDays();
    if (next !== days.value) {
      days.value = next;
      void load();
    }
  },
);
onMounted(() => load());
onBeforeUnmount(() => {
  sequence++;
  controller?.abort();
});
</script>

<template>
  <section class="workspace-page insights-page">
    <PageHeader
      title="洞察"
      description="媒体库规模、入库趋势、任务结果与磁盘空间。统计结果会复用一段时间，点击刷新可立即重算。"
      :busy="loading"
      :updated-at="data?.generated_at"
      refreshable
      @refresh="load(true)"
    />
    <p
      v-if="data?.stale"
      class="inline-note"
      role="status"
    >
      正在重新统计，当前显示的是上一次的结果。
    </p>
    <nav
      class="view-tabs"
      aria-label="统计窗口"
    >
      <button
        v-for="value in WINDOWS"
        :key="value"
        :class="{ active: days === value }"
        :aria-pressed="days === value"
        @click="selectWindow(value)"
      >
        {{ value === 365 ? "近一年" : `近 ${value} 天` }}
      </button>
    </nav>
    <div
      v-if="error && data"
      class="load-error-banner"
      role="alert"
    >
      <AlertCircle :size="17" />
      <span>{{ error }}，当前显示上次读取的结果。</span>
      <button
        class="btn ghost compact"
        @click="load()"
      >
        重试
      </button>
    </div>
    <StateView
      v-if="!data"
      :loading="loading"
      :error="error"
      title="还没有可统计的数据"
      description="下载完成的媒体会计入这里。"
      @retry="load()"
    />
    <template v-else>
      <section
        class="kpi-grid"
        aria-label="关键指标"
      >
        <KpiCard
          to="/library"
          :icon="Images"
          :value="count(data.library.files)"
        >
          <template #label>媒体库</template>
          共 {{ bytes(data.library.bytes) }}
          <template v-if="data.library.orphaned_files">
            · 无索引 {{ count(data.library.orphaned_files) }}
          </template>
        </KpiCard>
        <KpiCard
          tone="info"
          :icon="TrendingUp"
          :value="count(data.period.files)"
        >
          <template #label>{{ days === 365 ? "近一年入库" : `近 ${days} 天入库` }}</template>
          {{ bytes(data.period.bytes) }}
          <template v-if="periodDelta !== null">
            · 较上一周期 {{ periodDelta > 0 ? "+" : "" }}{{ periodDelta }}%
          </template>
          <template v-else-if="data.period.files">· 上一周期无入库</template>
        </KpiCard>
        <KpiCard
          to="/operations/tasks?status=failed"
          :tone="lowSuccess ? 'danger' : undefined"
          :icon="CircleCheck"
          :value="data.tasks.success_rate === null ? '-' : data.tasks.success_rate + '%'"
        >
          <template #label>任务成功率</template>
          {{
            data.tasks.success_rate === null
              ? "还没有已结束的任务"
              : `完成 ${count(data.tasks.totals.completed || 0)} · 失败 ${count(data.tasks.totals.failed || 0)}`
          }}
        </KpiCard>
        <KpiCard
          to="/settings/storage"
          :tone="diskTone === 'low' ? 'danger' : undefined"
          :progress="disk?.available ? disk.used_percent : null"
          :icon="HardDrive"
          :value="disk?.available ? bytes(disk.free_bytes) : '-'"
        >
          <template #label>磁盘剩余</template>
          <template v-if="!disk?.available">下载目录空间读取失败</template>
          <template v-else-if="disk.low">
            低于预警线 {{ bytes(disk.warn_free_bytes) }}，请清理或扩容
          </template>
          <template v-else>
            已用 {{ disk.used_percent }}% · 共 {{ bytes(disk.total_bytes) }}
          </template>
        </KpiCard>
      </section>

      <SectionCard
        title="入库趋势"
        class="insight-trend"
      >
        <template #description>
          {{
            busiestDay
              ? `最多的一天是 ${shortDate(busiestDay.date)}，入库 ${count(busiestDay.files)} 个文件`
              : "这段时间没有新入库"
          }}
        </template>
        <template #actions>
          <div
            class="segmented-control"
            role="group"
            aria-label="趋势指标"
          >
            <button
              :aria-pressed="metric === 'files'"
              @click="metric = 'files'"
            >
              文件数
            </button>
            <button
              :aria-pressed="metric === 'bytes'"
              @click="metric = 'bytes'"
            >
              体积
            </button>
          </div>
        </template>
        <BarChart
          :items="trendBars"
          :format="trendFormat"
          :caption="trendCaption"
        />
        <details class="chart-table">
          <summary>查看数据表</summary>
          <div class="chart-table-scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">日期</th>
                  <th scope="col">文件数</th>
                  <th scope="col">体积</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="item in [...data.trend].reverse().filter((entry) => entry.files)"
                  :key="item.date"
                >
                  <th scope="row">{{ item.date }}</th>
                  <td>{{ count(item.files) }}</td>
                  <td>{{ bytes(item.bytes) }}</td>
                </tr>
              </tbody>
            </table>
            <p
              v-if="!data.period.files"
              class="inline-note"
            >
              窗口内没有入库记录。
            </p>
          </div>
        </details>
      </SectionCard>

      <div class="insight-columns">
        <SectionCard title="平台占用">
          <template #description>按已保存文件的体积排序</template>
          <RankBars
            v-if="platformBars.length"
            :items="platformBars"
            label="各平台占用"
          />
          <p
            v-else
            class="inline-note"
          >
            媒体库还没有内容。
          </p>
        </SectionCard>
        <SectionCard title="媒体类型">
          <template #description>视频、图片与实况各占多少</template>
          <RankBars
            v-if="typeBars.length"
            :items="typeBars"
            label="各媒体类型占用"
          />
          <p
            v-else
            class="inline-note"
          >
            媒体库还没有内容。
          </p>
        </SectionCard>
      </div>

      <div class="insight-columns">
        <SectionCard title="占用最多的作者">
          <template #description>全部时间，点击查看该作者的媒体</template>
          <template #actions>
            <RouterLink
              class="btn ghost compact"
              to="/library?sort=size_desc"
            >
              最大的文件
              <ArrowRight :size="14" />
            </RouterLink>
          </template>
          <RankBars
            v-if="authorBars.length"
            :items="authorBars"
            label="占用最多的作者"
          />
          <p
            v-else
            class="inline-note"
          >
            还没有带作者信息的媒体。
          </p>
        </SectionCard>
        <SectionCard title="订阅产出">
          <template #description>订阅检查发现的新作品与保存的文件</template>
          <template #actions>
            <RouterLink
              class="btn ghost compact"
              to="/automation"
            >
              运行记录
              <ArrowRight :size="14" />
            </RouterLink>
          </template>
          <dl class="insight-facts">
            <div>
              <dt>抖音检查</dt>
              <dd>{{ count(data.subscriptions.runs) }} 次</dd>
            </div>
            <div>
              <dt>新作品</dt>
              <dd>{{ count(data.subscriptions.new_works) }}</dd>
            </div>
            <div>
              <dt>分片保存</dt>
              <dd>{{ count(data.subscriptions.saved_files) }}</dd>
            </div>
            <div>
              <dt>需核对</dt>
              <dd :class="{ 'danger-text': data.subscriptions.attention > 0 }">
                {{ count(data.subscriptions.attention) }}
              </dd>
            </div>
          </dl>
          <BarChart
            v-if="data.subscriptions.new_works + data.subscriptions.saved_files"
            :items="subscriptionBars"
            :format="count"
            :caption="`最近 ${days} 天订阅带来的新内容数量`"
            compact
          />
          <p
            v-else
            class="inline-note"
          >
            这段时间订阅没有带来新内容。
          </p>
        </SectionCard>
      </div>

      <div class="insight-columns">
        <SectionCard title="任务结果">
          <template #description>各平台当前保留的任务记录</template>
          <template #actions>
            <RouterLink
              class="btn ghost compact"
              to="/operations/tasks"
            >
              全部任务
              <ArrowRight :size="14" />
            </RouterLink>
          </template>
          <div
            v-if="data.tasks.by_platform.length"
            class="chart-table-scroll"
          >
            <table class="insight-table">
              <thead>
                <tr>
                  <th scope="col">平台</th>
                  <th
                    v-for="status in taskStatuses"
                    :key="status"
                    scope="col"
                  >
                    {{ statusPresentation(status).label }}
                  </th>
                  <th scope="col">合计</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="item in data.tasks.by_platform"
                  :key="item.platform"
                >
                  <th scope="row">
                    <RouterLink
                      :to="{ path: '/operations/tasks', query: { platform: item.platform } }"
                    >
                      {{ platformLabel(item.platform) }}
                    </RouterLink>
                  </th>
                  <td
                    v-for="status in taskStatuses"
                    :key="status"
                    :class="{ 'danger-text': status === 'failed' && item.statuses[status] }"
                  >
                    {{ count(item.statuses[status] || 0) }}
                  </td>
                  <td>{{ count(item.total) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p
            v-else
            class="inline-note"
          >
            还没有任务记录。
          </p>
        </SectionCard>
        <SectionCard title="失败原因">
          <template #description>当前失败任务按原因归类</template>
          <ul
            v-if="data.tasks.failure_reasons.length"
            class="failure-reasons"
          >
            <li
              v-for="item in data.tasks.failure_reasons"
              :key="item.platform + item.code"
            >
              <RouterLink
                :to="{
                  path: '/operations/tasks',
                  query: { status: 'failed', platform: item.platform },
                }"
              >
                <AlertCircle
                  :size="16"
                  aria-hidden="true"
                />
                <span>
                  <strong>{{ item.label }}</strong>
                  <small>
                    {{ platformLabel(item.platform) }}
                    <template v-if="item.label !== item.code && item.code !== 'unknown'">
                      · {{ item.code }}
                    </template>
                  </small>
                </span>
                <em>{{ count(item.count) }} 个</em>
              </RouterLink>
            </li>
          </ul>
          <p
            v-else
            class="all-clear"
          >
            <CircleCheck :size="15" />
            当前没有失败任务
          </p>
        </SectionCard>
      </div>
    </template>
  </section>
</template>
