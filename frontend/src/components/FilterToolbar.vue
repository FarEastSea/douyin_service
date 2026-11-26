<script setup lang="ts">
import { computed, ref, useId } from "vue";
import { SlidersHorizontal, X } from "@lucide/vue";
const props = defineProps<{ active: (string | false | undefined)[] }>();
const filters = computed(() => props.active.filter(Boolean));
defineEmits<{ clear: [] }>();
const open = ref(false),
  id = useId();
</script>
<template>
  <div class="filter-toolbar">
    <div class="filter-search"><slot name="search" /></div>
    <button
      class="btn ghost filter-trigger"
      type="button"
      :aria-expanded="open"
      :aria-controls="id"
      :aria-label="
        (open ? '收起筛选条件' : '展开筛选条件') +
        (filters.length ? `，已选 ${filters.length} 项` : '')
      "
      @click="open = !open"
    >
      <SlidersHorizontal :size="17" />
      筛选
      <span
        v-if="filters.length"
        class="filter-count"
      >
        {{ filters.length }}
      </span>
    </button>
    <div
      :id="id"
      class="filter-fields"
      :class="{ open }"
    >
      <slot />
    </div>
    <button
      v-if="filters.length"
      class="filter-chip"
      type="button"
      aria-label="清除当前筛选"
      @click="$emit('clear')"
    >
      {{ filters.join(" · ") }}
      <X :size="14" />
    </button>
  </div>
</template>
