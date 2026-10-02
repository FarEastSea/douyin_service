<script setup lang="ts">
import { computed } from 'vue'
import IssueDetail from './IssueDetail.vue'
const props = defineProps<{ title: string; status: string; processed?: number; total?: number; failed?: number }>()
const active = computed(() => ['queued', 'running'].includes(props.status))
</script>
<template>
  <details class="job-report" :open="active || undefined">
    <summary :aria-live="active ? 'polite' : undefined"><strong>{{ title }}</strong><span>{{ processed ?? 0 }}/{{ total ?? 0 }}<template v-if="failed"> · {{ failed }} 未成功</template></span></summary>
    <div class="job-report-body"><slot /><IssueDetail v-if="!['completed','queued','running'].includes(status) && !$slots.default" message="后台作业未全部完成，请核对剩余项。" /></div>
  </details>
</template>
