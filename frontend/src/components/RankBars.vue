<script setup lang="ts">
import { computed } from "vue";
import type { RouteLocationRaw } from "vue-router";

export interface RankDatum {
  key: string;
  label: string;
  hint?: string;
  value: number;
  display: string;
  to?: RouteLocationRaw;
}

const props = defineProps<{ items: RankDatum[]; label: string }>();
const peak = computed(() => Math.max(0, ...props.items.map((item) => item.value)));
function width(value: number) {
  return peak.value > 0 && value > 0 ? `${Math.max(1.5, (value / peak.value) * 100)}%` : "0%";
}
</script>

<template>
  <ol class="rank-bars" :aria-label="label">
    <li v-for="item in items" :key="item.key">
      <component :is="item.to ? 'RouterLink' : 'div'" :to="item.to" class="rank-row">
        <span class="rank-name" :title="item.label">{{ item.label }}<small v-if="item.hint">{{ item.hint }}</small></span>
        <span class="rank-value">{{ item.display }}</span>
        <span class="rank-track" aria-hidden="true"><i :style="{ width: width(item.value) }" /></span>
      </component>
    </li>
  </ol>
</template>
