<script setup lang="ts">
import { AlertCircle, Inbox } from "@lucide/vue";
withDefaults(
  defineProps<{
    loading?: boolean;
    error?: string;
    empty?: boolean;
    title?: string;
    description?: string;
    rows?: number;
  }>(),
  { title: "没有符合条件的记录", rows: 5, empty: true },
);
defineEmits<{ retry: [] }>();
</script>
<template>
  <div
    v-if="loading"
    class="list-skeleton"
    aria-label="正在读取数据"
    role="status"
  >
    <div v-for="row in rows" :key="row"><i /><i /><i /></div>
  </div>
  <div v-else-if="error" class="resource-error" role="alert">
    <AlertCircle :size="18" />
    <div>
      <strong>数据读取失败</strong>
      <p>{{ error }}</p>
    </div>
    <button class="btn ghost" @click="$emit('retry')">重试</button>
  </div>
  <div v-else-if="empty" class="empty-state">
    <Inbox :size="22" /><strong>{{ title }}</strong
    ><span v-if="description">{{ description }}</span
    ><slot />
  </div>
</template>
