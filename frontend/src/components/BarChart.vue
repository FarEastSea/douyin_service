<script setup lang="ts">
import { computed, ref } from "vue";

export interface BarDatum {
  key: string;
  label: string;
  /** 坐标轴上的短标签；缺省时使用 label。 */
  tick?: string;
  value: number;
}

const props = defineProps<{
  items: BarDatum[];
  format: (value: number) => string;
  caption: string;
  tickEvery?: number;
  compact?: boolean;
}>();

const active = ref<number | null>(null);
const peak = computed(() => Math.max(0, ...props.items.map((item) => item.value)));
const step = computed(() => props.tickEvery || Math.max(1, Math.ceil(props.items.length / 6)));
const focused = computed(() => (active.value === null ? null : props.items[active.value] || null));
const tooltipLeft = computed(() =>
  active.value === null || !props.items.length
    ? "50%"
    : `${Math.min(92, Math.max(8, ((active.value + 0.5) / props.items.length) * 100))}%`,
);
function height(value: number) {
  // 有数据的柱至少保留 2% 高度，避免极小值在视觉上等同于零。
  return peak.value > 0 && value > 0 ? `${Math.max(2, (value / peak.value) * 100)}%` : "0%";
}
function move(event: KeyboardEvent) {
  if (!props.items.length) return;
  if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
    event.preventDefault();
    const delta = event.key === "ArrowRight" ? 1 : -1;
    const current = active.value === null ? (delta > 0 ? -1 : props.items.length) : active.value;
    active.value = Math.min(props.items.length - 1, Math.max(0, current + delta));
  } else if (event.key === "Escape") active.value = null;
}
</script>

<template>
  <figure class="bar-chart" :class="{ compact }">
    <div class="bar-chart-scale" aria-hidden="true">
      <span>{{ format(peak) }}</span><span>0</span>
    </div>
    <div
      class="bar-chart-plot"
      role="img"
      tabindex="0"
      :aria-label="`${caption}。使用左右方向键逐项查看。`"
      @keydown="move"
      @pointerleave="active = null"
      @blur="active = null"
    >
      <div
        v-for="(item, index) in items"
        :key="item.key"
        class="bar-chart-column"
        :class="{ active: active === index, dimmed: active !== null && active !== index }"
        @pointerenter="active = index"
        @click="active = index"
      >
        <i :style="{ height: height(item.value) }" />
      </div>
      <div v-if="focused" class="bar-chart-tooltip" :style="{ left: tooltipLeft }" role="status">
        <small>{{ focused.label }}</small><strong>{{ format(focused.value) }}</strong>
      </div>
    </div>
    <div class="bar-chart-axis" aria-hidden="true">
      <span v-for="(item, index) in items" :key="item.key">{{ index % step === 0 ? item.tick ?? item.label : "" }}</span>
    </div>
    <figcaption class="sr-only">{{ caption }}</figcaption>
  </figure>
</template>
