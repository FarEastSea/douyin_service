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
    store.risk.requires_account_update ? '恢复条件：在设置中心重新保存抖音账号请求上下文' : `剩余冷却：${store.risk.retry_after} 秒`,
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
      <div><strong>{{ store.risk.requires_account_update ? '抖音账号请求上下文不可用' : '抖音接口保护性冷却中' }}</strong><span>{{ store.risk.requires_account_update ? '系统已隔离该账号，请检查 Cookie、User-Agent 与代理后重新保存。' : '系统已停止新的抖音业务请求，已有直链下载与 X 功能不受影响。' }}</span></div>
      <time>{{ store.risk.requires_account_update ? '等待更新账号档案' : format(store.risk.retry_after) }}</time>
      <button class="btn ghost compact" @click="router.push('/douyin/settings?tab=account')"><Settings2 :size="15" />更新账号</button>
      <button class="btn ghost compact" @click="copy"><Clipboard :size="15" />复制诊断</button>
    </section>
  </Transition>
</template>
