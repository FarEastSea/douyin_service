<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Clock3, RefreshCw, Sparkles, Users, Zap } from '@lucide/vue'
import { api } from '../api'
import { useAppStore } from '../stores/app'
import { reportStatusLabel } from '../localization'

const store = useAppStore(), reports = ref<any[]>([]), cycle = ref<any>({}), loading = ref(false), timer = ref<number>()
const latest = computed(() => reports.value[0])
async function load(silent = false) {
  if (!silent) loading.value = true
  try { const data = await api<any>('/authors/reports/subscriptions?limit=20'); reports.value = data.items || []; cycle.value = data.cycle || {} }
  catch (error: any) { store.notify(error.message || '加载自动更新报告失败', 'error') }
  finally { if (!silent) loading.value = false }
}
async function run() {
  if (store.risk.active) return store.notify('抖音接口正在冷却', 'error')
  try { const result = await api<any>('/authors/check-all', { method: 'POST' }); store.notify(result.message) }
  catch (error: any) { store.notify(error.message || '提交失败', 'error') }
}
onMounted(() => { load(); timer.value = window.setInterval(() => load(true), 5000) })
onBeforeUnmount(() => clearInterval(timer.value))
</script>

<template>
  <section class="workspace-card update-workspace">
    <header class="workspace-header"><div><p class="eyebrow">AUTOMATION</p><h2>自动更新中心</h2><span>查看订阅检查周期、断点与新作品发现情况</span></div><div class="header-actions"><button class="btn ghost" @click="load()"><RefreshCw :size="16" />刷新</button><button class="btn primary" :disabled="store.risk.active" @click="run"><Zap :size="16" />立即检查全部</button></div></header>
    <div class="metric-grid cycle-metrics"><article><Users /><div><strong>{{ cycle.total_authors || latest?.total_authors || store.stats.subscribed_authors }}</strong><span>订阅作者</span></div></article><article><Clock3 /><div><strong>{{ latest?.checked_authors || 0 }}</strong><span>本轮已检查</span></div></article><article><Clock3 /><div><strong>{{ cycle.checked_authors || 0 }}</strong><span>已检查</span></div></article><article><Sparkles /><div><strong>{{ cycle.new_works || latest?.new_works || 0 }}</strong><span>发现新作品</span></div></article><article><RefreshCw /><div><strong>{{ cycle.remaining_authors ?? latest?.remaining_authors ?? 0 }}</strong><span>等待续检</span></div></article></div>
    <div class="timeline" :class="{ loading }">
      <article v-for="report in reports" :key="report.id" class="timeline-item"><i :data-tone="report.status" /><div class="timeline-head"><strong>{{ report.summary || '订阅检查' }}</strong><span class="status subtle" :data-tone="report.status">{{ reportStatusLabel(report.status) }}</span></div><p>{{ report.checked_authors }} 位已检查 · {{ report.success_authors }} 位成功 · {{ report.warning_authors + report.failed_authors }} 位异常</p><footer><time>{{ report.started_at ? new Date(report.started_at).toLocaleString() : '时间未知' }}</time><span>{{ report.trigger_type === 'manual' ? '手动触发' : '自动调度' }}</span></footer></article>
      <div v-if="!loading && !reports.length" class="empty-state"><Clock3 /><strong>暂无检查报告</strong><span>运行一次订阅检查后将在这里形成报告</span></div>
    </div>
  </section>
</template>

<style scoped>
.metric-grid.cycle-metrics { grid-template-columns: repeat(5, 1fr); }
@media (max-width: 900px) { .metric-grid.cycle-metrics { grid-template-columns: repeat(2, 1fr); } }
</style>
