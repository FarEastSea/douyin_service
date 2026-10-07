<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { api } from "../api";
import StateView from "./StateView.vue";
import { dateTime } from "../workspace";
const props = defineProps<{ endpoint: string; work?: boolean }>();
const snapshots = ref<any[]>([]),
  loading = ref(false),
  error = ref(""),
  metric = ref(props.work ? "digg_count" : "view_count");
const metrics = computed(() =>
  props.work
    ? [
        ["digg_count", "点赞"],
        ["comment_count", "评论"],
        ["collect_count", "收藏"],
        ["share_count", "分享"],
        ["play_count", "播放"],
      ]
    : [
        ["view_count", "播放"],
        ["like_count", "点赞"],
        ["comment_count", "评论"],
        ["share_count", "分享"],
      ],
);
const series = computed(() =>
  [...snapshots.value]
    .filter((item) => item[metric.value] != null)
    .sort((a, b) => Date.parse(a.observed_at) - Date.parse(b.observed_at)),
);
const points = computed(() => {
  const values = series.value.map((item) => Number(item[metric.value]));
  const min = Math.min(...values),
    span = Math.max(1, Math.max(...values) - min);
  return values
    .map(
      (value, index) =>
        `${values.length === 1 ? 50 : (index * 100) / (values.length - 1)},${90 - ((value - min) * 80) / span}`,
    )
    .join(" ");
});
const increase = computed(() =>
  series.value.length
    ? Number(series.value.at(-1)[metric.value]) -
      Number(series.value[0][metric.value])
    : 0,
);
let sequence = 0;
async function load() {
  const current = ++sequence;
  loading.value = true;
  error.value = "";
  snapshots.value = [];
  try {
    const data = await api<any>(props.endpoint);
    if (current === sequence)
      snapshots.value = Array.isArray(data) ? data : data.snapshots || [];
  } catch (e: any) {
    if (current === sequence) error.value = e.message;
  } finally {
    if (current === sequence) loading.value = false;
  }
}
watch(() => props.endpoint, load, { immediate: true });
</script>
<template>
  <div class="trend-history">
    <nav class="view-tabs" aria-label="统计指标">
      <button
        v-for="item in metrics"
        :key="item[0]"
        :aria-pressed="metric === item[0]"
        :class="{ active: metric === item[0] }"
        @click="metric = item[0]"
      >
        {{ item[1] }}
      </button>
    </nav>
    <StateView
      v-if="loading || error || !series.length"
      :loading="loading"
      :error="error"
      title="暂无统计历史"
      description="取得统计字段后会自动记录快照。"
      @retry="load"
    /><template v-else
      ><p>
        当前
        <strong>{{ Number(series.at(-1)[metric]).toLocaleString() }}</strong> ·
        区间变化 {{ increase > 0 ? "+" : "" }}{{ increase.toLocaleString() }}
      </p>
      <svg
        class="trend-chart"
        viewBox="0 0 100 100"
        preserveAspectRatio="none"
        role="img"
        aria-label="互动数据变化曲线"
      >
        <line x1="0" y1="90" x2="100" y2="90" />
        <polyline :points="points" />
      </svg>
      <div class="trend-table">
        <article
          v-for="(snapshot, index) in [...series].reverse().slice(0, 30)"
          :key="snapshot.id || index"
        >
          <time>{{ dateTime(snapshot.observed_at) }}</time
          ><strong>{{ Number(snapshot[metric]).toLocaleString() }}</strong
          ><span>{{ snapshot.source }}</span>
        </article>
      </div></template
    >
  </div>
</template>
