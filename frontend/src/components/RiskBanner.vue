<script setup lang="ts">
import { AlertTriangle, Clipboard, Settings2 } from '@lucide/vue'
import { useRouter } from 'vue-router'
import { useAppStore } from '../stores/app'

const store = useAppStore()
const router = useRouter()
function format(seconds: number) {
  const m = Math.floor(seconds / 60).toString().padStart(2, '0')
  const s = Math.max(0, seconds % 60).toString().padStart(2, '0')
  return `${m}:${s}`
}
async function copy() {
  await navigator.clipboard.writeText([
    `错误类型：${store.risk.error_type || 'risk_control'}`,
    `剩余冷却：${store.risk.retry_after} 秒`,
    `最近触发：${store.risk.last_seen_at || '未知'}`,
    `原因：${store.risk.reason || '抖音上游安全校验拒绝'}`,
  ].join('\n'))
  store.notify('风控诊断信息已复制')
}
</script>

<template>
  <Transition name="slide">
    <section v-if="store.risk.active" class="risk-banner" role="status">
      <AlertTriangle :size="20" />
      <div><strong>抖音接口保护性冷却中</strong><span>系统已停止新的抖音业务请求，已有直链下载与 X 功能不受影响。</span></div>
      <time>{{ format(store.risk.retry_after) }}</time>
      <button class="btn ghost compact" @click="router.push('/douyin/settings?tab=account')"><Settings2 :size="15" />更新 Cookie</button>
      <button class="btn ghost compact" @click="copy"><Clipboard :size="15" />复制诊断</button>
    </section>
  </Transition>
</template>
