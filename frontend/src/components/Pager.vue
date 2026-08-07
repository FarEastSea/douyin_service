<script setup lang="ts">
defineProps<{ page: number; pages: number; total: number }>()
const emit = defineEmits<{ change: [page: number] }>()
</script>

<template>
  <footer class="pager">
    <span>共 {{ total.toLocaleString() }} 条 · 第 {{ page }}/{{ Math.max(1, pages) }} 页</span>
    <div>
      <button class="btn ghost compact" :disabled="page <= 1" @click="emit('change', 1)">首页</button>
      <button class="btn ghost compact" :disabled="page <= 1" @click="emit('change', page - 1)">上一页</button>
      <span class="page-current">{{ page }}</span>
      <button class="btn ghost compact" :disabled="page >= pages" @click="emit('change', page + 1)">下一页</button>
      <button class="btn ghost compact" :disabled="page >= pages" @click="emit('change', pages)">末页</button>
    </div>
  </footer>
</template>
