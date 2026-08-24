<script setup lang="ts">
import { AlertTriangle, Clipboard, Settings2 } from '@lucide/vue'
import { useRouter } from 'vue-router'
import { useAppStore } from '../stores/app'
import { riskReasonLabel, riskTypeLabel } from '../localization'

const store = useAppStore()
const router = useRouter()
function format(seconds: number) {
  const m = Math.floor(seconds / 60).toString().padStart(2, '0')
  const s = Math.max(0, seconds % 60).toString().padStart(2, '0')
  return `${m}:${s}`
}
async function copy() {
  await navigator.clipboard.writeText([
    `错误类型：${riskTypeLabel(store.risk.error_type, store.risk.error_type_label)}`,
    store.risk.requires_cookie_update ? '恢复条件：更新包含 UIFID 的完整抖音 Cookie' : `剩余冷却：${store.risk.retry_after} 秒`,
    `最近触发：${store.risk.last_seen_at || '未知'}`,
    `原因：${riskReasonLabel(store.risk.error_type, store.risk.reason, store.risk.reason_label)}`,
  ].join('\n'))
  store.notify('风控诊断信息已复制')
}
</script>

<template>
  <Transition name="slide">
    <section v-if="store.risk.active" class="risk-banner" role="status">
      <AlertTriangle :size="20" />
      <div><strong>{{ store.risk.requires_cookie_update ? '抖音 Cookie 身份信息不完整' : '抖音接口保护性冷却中' }}</strong><span>{{ store.risk.requires_cookie_update ? '系统已停止新的抖音业务请求，请更新包含 UIFID 的完整 Cookie。' : '系统已停止新的抖音业务请求，已有直链下载与 X 功能不受影响。' }}</span></div>
      <time>{{ store.risk.requires_cookie_update ? '等待更新 Cookie' : format(store.risk.retry_after) }}</time>
      <button class="btn ghost compact" @click="router.push('/douyin/settings?tab=account')"><Settings2 :size="15" />更新 Cookie</button>
      <button class="btn ghost compact" @click="copy"><Clipboard :size="15" />复制诊断</button>
    </section>
  </Transition>
</template>
