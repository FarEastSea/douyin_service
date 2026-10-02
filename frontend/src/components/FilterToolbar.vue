<script setup lang="ts">
import { ref, useId } from 'vue'
import { SlidersHorizontal, X } from '@lucide/vue'
defineProps<{ active?: string }>()
defineEmits<{ clear: [] }>()
const open = ref(false), id = useId()
</script>
<template>
  <div class="filter-toolbar">
    <div class="filter-search"><slot name="search" /></div>
    <button class="btn ghost filter-trigger" type="button" :aria-expanded="open" :aria-controls="id" aria-label="展开筛选条件" @click="open = !open"><SlidersHorizontal :size="17" />筛选</button>
    <div :id="id" class="filter-fields" :class="{ open }"><slot /></div>
    <button v-if="active" class="filter-chip" type="button" aria-label="清除当前筛选" @click="$emit('clear')">{{ active }}<X :size="14" /></button>
  </div>
</template>
