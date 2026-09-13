<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Eye, RefreshCw, RotateCcw, Search, Square, TrendingUp, X } from '@lucide/vue'
import { api, jsonBody } from '../api'
import Pager from '../components/Pager.vue'
import { openMedia } from '../media'
import { useAppStore } from '../stores/app'
import type { MediaItem, UnifiedTask, UnifiedTaskActionResult, UnifiedTaskPage } from '../types'

const store = useAppStore()
const tasks = ref<UnifiedTask[]>([]), page = ref(1), pages = ref(1), total = ref(0)
const platform = ref(''), status = ref(''), search = ref(''), loading = ref(false)
const statusSummary = ref<Record<string, number>>({}), selectedKeys = ref<string[]>([])
const actionBusy = ref(false), rowBusy = ref<string[]>([])
const actionFailures = ref<Array<{ task_key: string; message: string; status_code: number }>>([])
const statsTask = ref<UnifiedTask>(), statsData = ref<any>({ snapshots: [] }), statsBusy = ref(false)
const statsMetric = ref<'view_count' | 'like_count' | 'comment_count' | 'share_count'>('view_count')
let searchTimer: number | undefined
const platformNames: Record<string, string> = { douyin: '抖音', x: 'X', tiktok: 'TikTok', weibo: '微博', bilibili: 'B站', xhs: '小红书' }
const statusNames: Record<string, string> = { pending: '等待中', downloading: '下载中', paused: '已暂停', completed: '已完成', skipped: '已跳过', failed: '失败', cancelled: '已取消' }
const phaseNames: Record<string, string> = { queued: '排队中', preparing: '准备中', downloading: '下载中', completed: '已完成', failed: '失败', cancelled: '已取消' }
const statusOrder = ['pending', 'downloading', 'paused', 'failed', 'cancelled', 'completed', 'skipped']
const visibleSummaries = computed(() => statusOrder.filter(item => statusSummary.value[item] || status.value === item))
const allPageSelected = computed(() => tasks.value.length > 0 && tasks.value.every(task => selectedKeys.value.includes(task.key)))
const selectedTasks = computed(() => tasks.value.filter(task => selectedKeys.value.includes(task.key)))
const retryableSelected = computed(() => selectedTasks.value.filter(task => ['failed', 'cancelled'].includes(task.status)))
const cancellableSelected = computed(() => selectedTasks.value.filter(task => ['pending', 'downloading', 'paused'].includes(task.status)))
const summaryTotal = computed(() => Object.values(statusSummary.value).reduce((sum, count) => sum + count, 0))
const statsMetrics = [['view_count', '播放'], ['like_count', '点赞'], ['comment_count', '评论'], ['share_count', '分享']] as const
const statsSeries = computed(() => (statsData.value.snapshots || []).filter((item: any) => item[statsMetric.value] != null))
const statsSummary = computed(() => {
  const values = statsSeries.value.map((item: any) => Number(item[statsMetric.value] || 0))
  const latest = values.at(-1) || 0, first = values[0] || 0
  const deltas = values.slice(1).map((value: number, index: number) => value - values[index])
  const latestDelta = deltas.at(-1) || 0
  const baseline = deltas.length > 1 ? deltas.slice(0, -1).reduce((sum: number, value: number) => sum + value, 0) / (deltas.length - 1) : 0
  return { latest, delta: latest - first, latestDelta, unusual: baseline > 0 && latestDelta >= baseline * 3 }
})
const statsPoints = computed(() => {
  const values = statsSeries.value.map((item: any) => Number(item[statsMetric.value] || 0))
  if (!values.length) return ''
  const min = Math.min(...values), max = Math.max(...values), span = Math.max(1, max - min)
  return values.map((value: number, index: number) => `${values.length === 1 ? 50 : index * 100 / (values.length - 1)},${90 - (value - min) * 80 / span}`).join(' ')
})
const formatCount = (value: number) => Number(value || 0).toLocaleString('zh-CN')

async function load() {
  loading.value = true
  const params = new URLSearchParams({ page: String(page.value), page_size: '20' })
  if (platform.value) params.set('platform', platform.value)
  if (status.value) params.set('status', status.value)
  if (search.value.trim()) params.set('q', search.value.trim())
  try {
    const data = await api<UnifiedTaskPage>(`/operations/tasks?${params}`)
    tasks.value = data.items
    pages.value = data.pages
    total.value = data.total
    statusSummary.value = data.status_summary || {}
    selectedKeys.value = []
  } catch (error: any) { store.notify(error.message || '加载统一任务失败', 'error') }
  finally { loading.value = false }
}
function resetAndLoad() { page.value = 1; selectedKeys.value = []; void load() }
function queueSearch() { window.clearTimeout(searchTimer); searchTimer = window.setTimeout(resetAndLoad, 350) }
function toggleTask(taskKey: string) {
  selectedKeys.value = selectedKeys.value.includes(taskKey)
    ? selectedKeys.value.filter(key => key !== taskKey)
    : [...selectedKeys.value, taskKey]
}
function togglePage() { selectedKeys.value = allPageSelected.value ? [] : tasks.value.map(task => task.key) }
function setStatus(next: string) { status.value = next === status.value ? '' : next; resetAndLoad() }
async function runAction(taskKeys: string[], action: 'retry' | 'cancel') {
  if (!taskKeys.length || actionBusy.value) return
  actionBusy.value = true
  try {
    const result = await api<UnifiedTaskActionResult>('/operations/tasks/actions', {
      method: 'POST', ...jsonBody({ action, task_keys: taskKeys }),
    })
    const failed = result.data?.failed || []
    actionFailures.value = failed
    store.notify(result.message || '操作完成', failed.length ? (result.success ? 'info' : 'error') : 'success')
    await load()
  } catch (error: any) { store.notify(error.message || '任务操作失败', 'error') }
  finally { actionBusy.value = false }
}
async function action(task: UnifiedTask, actionName: 'retry' | 'cancel') {
  if (rowBusy.value.includes(task.key)) return
  rowBusy.value = [...rowBusy.value, task.key]
  try { await runAction([task.key], actionName) }
  finally { rowBusy.value = rowBusy.value.filter(key => key !== task.key) }
}
async function preview(task: UnifiedTask) {
  try {
    if (task.preview_endpoint) return openMedia([{ url: `/api${task.preview_endpoint}`, type: task.media_type === 'image' ? 'image' : 'video', title: task.source_label }])
    if (!task.media_endpoint) return
    const assets = await api<any[]>(task.media_endpoint)
    const items: MediaItem[] = assets.map(item => ({ url: item.preview_url, type: item.media_type === 'video' ? 'video' : 'image', title: item.title || item.filename }))
    if (items.length) openMedia(items); else store.notify('该任务没有可预览资源', 'info')
  } catch (error: any) { store.notify(error.message || '预览失败', 'error') }
}
async function showStats(task: UnifiedTask) {
  if (!task.stats_endpoint) return
  statsTask.value = task; statsBusy.value = true; statsData.value = { snapshots: [] }
  try { statsData.value = await api<any>(task.stats_endpoint) }
  catch (error: any) { store.notify(error.message || '互动趋势加载失败', 'error') }
  finally { statsBusy.value = false }
}
function changePage(value: number) { page.value = value; void load() }
onMounted(load)
onBeforeUnmount(() => window.clearTimeout(searchTimer))
</script>

<template>
  <section class="workspace-card">
    <header class="workspace-header">
      <div><p class="eyebrow">UNIFIED QUEUE</p><h2>全部平台任务</h2><span>统一检索、批量操作、错误追踪和本地预览</span></div>
      <button class="btn ghost" :disabled="loading" @click="load"><RefreshCw :size="16" />{{ loading ? '刷新中…' : '刷新' }}</button>
    </header>
    <div class="filter-row unified-filters">
      <select v-model="platform" aria-label="平台" @change="resetAndLoad"><option value="">全部平台</option><option v-for="item in store.platforms" :key="item.id" :value="item.id">{{ item.name }}</option></select>
      <select v-model="status" aria-label="状态" @change="resetAndLoad"><option value="">全部状态</option><option value="pending">等待中</option><option value="downloading">下载中</option><option value="paused">已暂停</option><option value="completed">已完成</option><option value="failed">失败</option><option value="cancelled">已取消</option></select>
      <label class="search"><Search :size="15" /><input v-model="search" placeholder="搜索作者、标题、作品 ID、文件名或来源链接" @input="queueSearch" /></label>
    </div>
    <div class="status-strip" aria-label="当前筛选结果状态汇总">
      <button :class="{ active: !status }" @click="setStatus('')"><span>全部</span><b>{{ summaryTotal.toLocaleString() }}</b></button>
      <button v-for="item in visibleSummaries" :key="item" :class="{ active: status === item }" @click="setStatus(item)"><span>{{ statusNames[item] || item }}</span><b>{{ statusSummary[item].toLocaleString() }}</b></button>
    </div>
    <div v-if="selectedKeys.length" class="selection-bar" role="status">
      <span>已选择 <b>{{ selectedKeys.length }}</b> 个当前页任务</span>
      <div>
        <button class="btn ghost compact" :disabled="actionBusy || !retryableSelected.length" @click="runAction(retryableSelected.map(task => task.key), 'retry')"><RotateCcw :size="15" />{{ actionBusy ? '处理中…' : `重试 ${retryableSelected.length}` }}</button>
        <button class="btn ghost compact" :disabled="actionBusy || !cancellableSelected.length" @click="runAction(cancellableSelected.map(task => task.key), 'cancel')"><Square :size="15" />{{ actionBusy ? '处理中…' : `取消 ${cancellableSelected.length}` }}</button>
      </div>
    </div>
    <details v-if="actionFailures.length" class="action-report"><summary>{{ actionFailures.length }} 个任务未处理，查看原因</summary><ul><li v-for="item in actionFailures" :key="item.task_key"><b>{{ item.task_key }}</b><span>{{ item.message }}</span></li></ul></details>
    <div class="table-shell" :class="{ loading }">
      <table class="data-table">
        <thead><tr><th class="select-col"><input type="checkbox" aria-label="选择当前页全部任务" :checked="allPageSelected" @change="togglePage" /></th><th>平台与来源</th><th>元数据</th><th>状态</th><th>进度</th><th>结果</th><th class="actions-col">操作</th></tr></thead>
        <tbody><tr v-for="task in tasks" :key="task.key" :class="{ selected: selectedKeys.includes(task.key) }">
          <td class="select-col"><input type="checkbox" :aria-label="`选择任务 ${task.id}`" :checked="selectedKeys.includes(task.key)" @change="toggleTask(task.key)" /></td>
          <td><div class="media-cell"><span class="media-icon">{{ platformNames[task.platform] || task.platform }}</span><div><strong :title="task.source_label">{{ task.source_label }}</strong><span>{{ task.source_type === 'profile' ? '作者主页' : '单条作品' }} · #{{ task.id }}</span></div></div></td>
          <td><strong :title="task.author_name || ''">{{ task.author_name || '作者未知' }}</strong><span>{{ task.published_at ? new Date(task.published_at).toLocaleString() : (task.media_type || '元数据待采集') }}</span></td>
          <td><span class="status" :data-tone="task.status">{{ statusNames[task.status] || task.status }}</span><small>{{ phaseNames[task.phase || ''] || task.phase || '—' }}</small></td>
          <td><strong>{{ Number(task.progress_percent || 0).toFixed(1) }}%</strong><span>{{ task.file_count }} 个文件</span></td>
          <td class="result-cell"><span :class="{ 'inline-error': task.error_message }" :title="task.error_message || ''">{{ task.error_message || '—' }}</span><small v-if="task.error_code">{{ task.error_code }}</small></td>
          <td><div class="row-actions"><button v-if="task.preview_count" class="icon-btn" title="预览" :disabled="rowBusy.includes(task.key)" @click="preview(task)"><Eye :size="17" /></button><button v-if="task.has_stats" class="icon-btn" title="互动趋势" @click="showStats(task)"><TrendingUp :size="17" /></button><button v-if="['failed','cancelled'].includes(task.status)" class="icon-btn" title="重试" :disabled="actionBusy || rowBusy.includes(task.key)" @click="action(task, 'retry')"><RotateCcw :size="17" /></button><button v-if="['pending','downloading','paused'].includes(task.status)" class="icon-btn" title="取消" :disabled="actionBusy || rowBusy.includes(task.key)" @click="action(task, 'cancel')"><Square :size="17" /></button></div></td>
        </tr></tbody>
      </table>
      <div v-if="!loading && !tasks.length" class="empty-state"><strong>暂无符合条件的任务</strong><span>可调整平台、状态或搜索条件后重试</span></div>
    </div>
    <Pager :page="page" :pages="pages" :total="total" @change="changePage" />
    <Teleport to="body"><div v-if="statsTask" class="trend-overlay" @click.self="statsTask = undefined"><section class="trend-dialog"><header><div><p class="eyebrow">CROSS-PLATFORM ANALYTICS</p><h3>{{ statsData.label || statsTask.source_label }}</h3><span>{{ platformNames[statsTask.platform] }} · {{ statsSeries.length }} 个统计快照</span></div><button class="icon-btn" @click="statsTask = undefined"><X /></button></header><nav><button v-for="metric in statsMetrics" :key="metric[0]" :class="{ active: statsMetric === metric[0] }" @click="statsMetric = metric[0]">{{ metric[1] }}</button></nav><div v-if="statsBusy" class="empty-state">正在读取趋势…</div><template v-else-if="statsSeries.length"><div class="trend-kpis"><article><strong>{{ formatCount(statsSummary.latest) }}</strong><span>当前值</span></article><article><strong>+{{ formatCount(statsSummary.delta) }}</strong><span>区间增长</span></article><article :data-alert="statsSummary.unusual"><strong>+{{ formatCount(statsSummary.latestDelta) }}</strong><span>最近增量{{ statsSummary.unusual ? ' · 异常增长' : '' }}</span></article></div><svg class="trend-chart" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="跨平台互动数据变化曲线"><line x1="0" y1="90" x2="100" y2="90" /><line x1="0" y1="50" x2="100" y2="50" /><line x1="0" y1="10" x2="100" y2="10" /><polyline :points="statsPoints" /></svg><div class="trend-table"><article v-for="(snapshot, index) in [...statsSeries].reverse().slice(0, 30)" :key="snapshot.id || index"><time>{{ new Date(snapshot.observed_at).toLocaleString() }}</time><strong>{{ formatCount(snapshot[statsMetric]) }}</strong><span>{{ snapshot.source }}</span></article></div></template><div v-else class="empty-state"><TrendingUp /><strong>该平台尚未返回互动统计</strong><span>后续下载取得统计字段时会自动开始记录</span></div></section></div></Teleport>
  </section>
</template>

<style scoped>
.unified-filters { grid-template-columns:160px 160px minmax(240px,1fr); }
.unified-filters select { min-height:40px; padding:0 12px; border:1px solid var(--line); border-radius:9px; background:var(--surface-2); color:var(--text); }
.status-strip { display:flex; gap:6px; margin:12px 0; overflow-x:auto; scrollbar-width:thin; }
.status-strip button { display:flex; align-items:center; gap:8px; min-height:34px; padding:0 11px; border:1px solid var(--line); border-radius:8px; background:transparent; color:var(--muted); white-space:nowrap; cursor:pointer; }
.status-strip button:hover,.status-strip button.active { border-color:var(--accent); color:var(--text); background:var(--accent-soft); }
.status-strip b { color:var(--text); font-variant-numeric:tabular-nums; }
.selection-bar { display:flex; align-items:center; justify-content:space-between; gap:12px; margin:10px 0; padding:10px 12px; border:1px solid var(--accent); border-radius:9px; background:var(--accent-soft); }
.selection-bar>div { display:flex; gap:8px; }
.action-report { margin:10px 0; padding:10px 12px; border:1px solid color-mix(in srgb,var(--red) 38%,var(--line)); border-radius:9px; color:var(--muted); }
.action-report summary { color:var(--red); cursor:pointer; }
.action-report ul { display:grid; gap:6px; margin:10px 0 0; padding:0; list-style:none; }
.action-report li { display:flex; gap:10px; min-width:0; }
.action-report li b { flex:none; color:var(--text); }
.action-report li span { overflow-wrap:anywhere; }
.select-col { width:42px; text-align:center; }
.select-col input { width:16px; height:16px; accent-color:var(--accent); cursor:pointer; }
.data-table tr.selected td { background:var(--accent-soft); }
.media-icon { min-width:48px; padding:7px; border-radius:8px; font-size:10px; text-align:center; }
.media-cell>div,.result-cell { min-width:0; }
.media-cell strong,.result-cell>span,.data-table td>strong { display:block; max-width:260px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.result-cell small { display:block; margin-top:4px; color:var(--faint); overflow-wrap:anywhere; }
.status[data-tone="paused"],.status[data-tone="cancelled"] { background:rgba(231,169,67,.12); color:var(--amber)!important; }
.trend-overlay { position:fixed; inset:0; z-index:1000; display:grid; place-items:center; padding:24px; background:rgba(5,8,14,.7); backdrop-filter:blur(8px); }
.trend-dialog { width:min(760px,100%); max-height:88vh; overflow:auto; padding:24px; border:1px solid var(--line); border-radius:16px; background:var(--surface); box-shadow:0 24px 80px rgba(0,0,0,.32); }
.trend-dialog>header { display:flex; justify-content:space-between; gap:16px; }
.trend-dialog>header h3 { margin:4px 0; }
.trend-dialog>header span,.trend-table span { color:var(--muted); }
.trend-dialog>nav { display:flex; gap:6px; margin:18px 0; }
.trend-dialog>nav button { padding:7px 11px; border:1px solid var(--line); border-radius:8px; background:transparent; color:var(--muted); cursor:pointer; }
.trend-dialog>nav button.active { border-color:var(--accent); background:var(--accent-soft); color:var(--text); }
.trend-kpis { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }
.trend-kpis article { padding:12px; border:1px solid var(--line); border-radius:10px; background:var(--surface-2); }
.trend-kpis strong,.trend-kpis span { display:block; }
.trend-kpis span { margin-top:4px; color:var(--muted); font-size:10px; }
.trend-kpis article[data-alert="true"] { border-color:var(--amber); }
.trend-chart { width:100%; height:210px; margin:18px 0; overflow:visible; }
.trend-chart line { stroke:var(--line); stroke-width:.5; }
.trend-chart polyline { fill:none; stroke:var(--accent); stroke-width:2.2; vector-effect:non-scaling-stroke; }
.trend-table { display:grid; gap:5px; }
.trend-table article { display:grid; grid-template-columns:1fr auto 90px; gap:12px; padding:8px 10px; border-bottom:1px solid var(--line); }
@media (max-width:760px) { .unified-filters { grid-template-columns:1fr; } .selection-bar { align-items:flex-start; flex-direction:column; } }
</style>
