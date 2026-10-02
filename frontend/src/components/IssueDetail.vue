<script setup lang="ts">
import { computed } from 'vue'
import { issuePresentation } from '../presentation'
import { useAppStore } from '../stores/app'
const props = withDefaults(defineProps<{ message?: string; code?: string; impact?: string }>(), { message: '', code: '', impact: '' })
const issue = computed(() => issuePresentation(props.message, props.code))
const store = useAppStore()
async function copy() {
  try { await navigator.clipboard.writeText([props.code, props.impact, props.message].filter(Boolean).join('\n')); store.notify('完整诊断已复制') }
  catch { store.notify('复制失败，请检查浏览器剪贴板权限', 'error') }
}
</script>
<template>
  <details class="issue-detail">
    <summary>{{ issue.title }}</summary>
    <div class="issue-body"><p v-if="impact">{{ impact }}</p><p>{{ issue.nextAction }}</p><small v-if="code">错误代码：{{ code }}</small><pre>{{ message }}</pre><button class="text-button" type="button" @click="copy">复制完整诊断</button><slot /></div>
  </details>
</template>
