<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { AlertCircle, Clipboard, Clock3, RefreshCw, Sparkles, Users, Zap } from '@lucide/vue'
import { api } from '../api'
import { useAppStore } from '../stores/app'
import { reportStatusLabel } from '../localization'

const store = useAppStore(), reports = ref<any[]>([]), cycle = ref<any>({}), loading = ref(false), copying = ref(false), timer = ref<number>()
const runBusy = ref(false), reconcileBusy = ref(false), loadError = ref(''), reportLoaded = ref(false)
const latest = computed(() => reports.value[0])
function metric(value: unknown) { return reportLoaded.value && value != null ? Number(value).toLocaleString('zh-CN') : '—' }
async function load(silent = false) {
  if (!silent) loading.value = true
  try { const data = await api<any>('/authors/reports/subscriptions?limit=20'); reports.value = data.items || []; cycle.value = data.cycle || {}; reportLoaded.value = true; loadError.value = '' }
  catch (error: any) { loadError.value = error.message || '加载自动更新报告失败'; if (!silent) store.notify(loadError.value, 'error') }
  finally { if (!silent) loading.value = false }
}
async function run() {
  if (store.risk.active) return store.notify('抖音接口正在冷却', 'error')
  if (runBusy.value) return
  runBusy.value = true
  try { const result = await api<any>('/authors/check-all', { method: 'POST' }); store.notify(result.message || '检查已提交，结果会在此页更新'); await load(true) }
  catch (error: any) { store.notify(error.message || '提交失败', 'error') }
  finally { runBusy.value = false }
}
async function reconcile() {
  if (store.risk.active) return store.notify('抖音接口正在冷却', 'error')
  if (reconcileBusy.value) return
  if (!confirm('全量对账会读取每位订阅作者的全部作品，耗时和请求量都高于日常检查，确定继续？')) return
  reconcileBusy.value = true
  try { const result = await api<any>('/authors/reconcile-all', { method: 'POST' }); store.notify(result.message) }
  catch (error: any) { store.notify(error.message || '提交全量对账失败', 'error') }
  finally { reconcileBusy.value = false }
}
async function copyDiagnostic() {
  copying.value = true
  try {
    const diagnostic = await api<any>('/authors/reports/subscriptions/diagnostic')
    await navigator.clipboard.writeText(JSON.stringify(diagnostic, null, 2))
    store.notify('自动更新诊断已复制，敏感凭据已排除')
  } catch (error: any) { store.notify(error.message || '复制诊断失败', 'error') }
  finally { copying.value = false }
}
function triggerLabel(trigger: string) {
  if (trigger === 'reconcile') return '全量对账'
  return trigger === 'manual' ? '手动触发' : '自动调度'
}
onMounted(() => { void load(); timer.value = window.setInterval(() => { if (!document.hidden) void load(true) }, 10_000) })
onBeforeUnmount(() => clearInterval(timer.value))
</script>

<template>
  <section class="workspace-card update-workspace">
    <header class="workspace-header"><div><h2>自动更新</h2><span>抖音订阅检查周期、待续检作者与失败证据</span></div><div class="header-actions"><button class="btn ghost" :disabled="copying" @click="copyDiagnostic"><Clipboard :size="16" />{{ copying ? '整理中…' : '复制诊断' }}</button><button class="btn ghost" @click="load()"><RefreshCw :size="16" />刷新</button><button class="btn ghost" :disabled="store.risk.active || reconcileBusy" @click="reconcile"><RefreshCw :size="16" />{{ reconcileBusy ? '提交中…' : '全量对账' }}</button><button class="btn primary" :disabled="store.risk.active || runBusy" @click="run"><Zap :size="16" />{{ runBusy ? '提交中…' : '立即检查全部' }}</button></div></header>
    <div v-if="loadError" class="update-load-error" role="alert"><AlertCircle :size="18" /><span>检查报告暂不可用：{{ loadError }}{{ reportLoaded ? '；下方保留上次读取结果。' : '；当前数字未确认。' }}</span><button class="text-button" @click="load()">重试</button></div>
    <div class="metric-grid cycle-metrics"><article><Users /><div><strong>{{ metric(cycle.total_authors ?? latest?.total_authors) }}</strong><span>抖音订阅作者</span></div></article><article><Clock3 /><div><strong>{{ metric(cycle.checked_authors ?? latest?.checked_authors) }}</strong><span>本周期已检查</span></div></article><article><Sparkles /><div><strong>{{ metric(cycle.new_works ?? latest?.new_works) }}</strong><span>本周期新作品</span></div></article><article><RefreshCw /><div><strong>{{ metric(cycle.remaining_authors ?? latest?.remaining_authors) }}</strong><span>等待续检</span></div></article></div>
    <div class="timeline" :class="{ loading }">
      <article v-for="report in reports" :key="report.id" class="timeline-item"><i :data-tone="report.status" /><div class="timeline-head"><strong>{{ report.summary || '订阅检查' }}</strong><span class="status subtle" :data-tone="report.status">{{ reportStatusLabel(report.status) }}</span></div><p>{{ report.checked_authors }} 位已检查 · {{ report.success_authors }} 位成功 · {{ report.warning_authors + report.failed_authors }} 位异常</p><details v-if="report.details?.some((item:any) => item.status === 'failed')" class="report-errors"><summary>查看本轮失败证据</summary><ul><li v-for="item in report.details.filter((entry:any) => entry.status === 'failed').slice(0, 10)" :key="`${report.id}-${item.author_id}`"><strong>{{ item.nickname || `作者 ${item.author_id}` }}</strong><span>{{ item.message || item.error || '未知错误' }}</span><small v-if="item.http_status || item.diagnostics?.request_id">HTTP {{ item.http_status || '—' }} · 请求 {{ item.diagnostics?.request_id || '未记录' }}</small></li></ul></details><footer><time>{{ report.started_at ? new Date(report.started_at).toLocaleString() : '时间未知' }}</time><span>{{ triggerLabel(report.trigger_type) }}</span></footer></article>
      <div v-if="!loading && reportLoaded && !reports.length" class="empty-state"><Clock3 /><strong>暂无检查报告</strong><span>运行一次订阅检查后将在这里形成报告</span></div>
    </div>
  </section>
</template>

<style scoped>
.metric-grid.cycle-metrics { grid-template-columns: repeat(4, 1fr); }
.report-errors { margin-top: 8px; color: var(--muted); font-size: 12px; }
.update-load-error { margin: 14px 20px 0; padding: 11px 13px; display: flex; align-items: center; gap: 8px; border: 1px solid var(--line-strong); border-radius: 8px; color: var(--amber); }
.update-load-error button { margin-left: auto; }
.report-errors summary { cursor: pointer; }
.report-errors ul { margin: 8px 0 0; padding-left: 18px; display: grid; gap: 7px; }
.report-errors li { display: grid; gap: 2px; }
.report-errors span, .report-errors small { color: var(--muted); overflow-wrap: anywhere; }
@media (max-width: 900px) { .metric-grid.cycle-metrics { grid-template-columns: repeat(2, 1fr); } }
</style>
