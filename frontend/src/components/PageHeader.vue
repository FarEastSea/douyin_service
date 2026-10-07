<script setup lang="ts">
import { RefreshCw } from "@lucide/vue";
import { dateTime } from "../workspace";
defineProps<{
  title: string;
  description?: string;
  busy?: boolean;
  updatedAt?: string;
  refreshable?: boolean;
}>();
defineEmits<{ refresh: [] }>();
</script>
<template>
  <header class="page-header">
    <div class="page-heading">
      <h1>{{ title }}</h1>
      <p v-if="description">{{ description }}</p>
    </div>
    <div class="page-actions">
      <span v-if="updatedAt" class="freshness"
        >更新于 {{ dateTime(updatedAt) }}</span
      ><slot /><button
        v-if="refreshable"
        class="icon-btn"
        type="button"
        aria-label="刷新当前页面"
        :disabled="busy"
        @click="$emit('refresh')"
      >
        <RefreshCw :size="16" :class="{ spinning: busy }" />
      </button>
    </div>
  </header>
</template>
