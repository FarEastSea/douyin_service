<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Eye, RefreshCw, RotateCcw, Search, Square, TrendingUp, X, Pause, Play, Trash2 } from '@lucide/vue'
import { api, jsonBody } from '../api'
import Pager from '../components/Pager.vue'
import MoreActions from '../components/MoreActions.vue'
import FilterToolbar from '../components/FilterToolbar.vue'
import JobReport from '../components/JobReport.vue'
import IssueDetail from '../components/IssueDetail.vue'
import { openMedia } from '../media'
import { focusFirst, restoreFocus, trapFocus } from '../focus'
import { useAppStore } from '../stores/app'
import type { MediaItem, UnifiedTask, UnifiedTaskActionResult, UnifiedTaskPage } from '../types'

const store = useAppStore()
const route = useRoute(), router = useRouter()
const queryText = (value: unknown) => typeof value === 'string' ? value : ''
const queryPage = (value: unknown) => Math.max(1, Number.parseInt(queryText(value), 10) || 1)
const tasks = ref<UnifiedTask[]>([]), page = ref(queryPage(route.query.page)), pages = ref(1), total = ref(0)
const platform = ref(queryText(route.query.platform)), status = ref(queryText(route.query.status)), search = ref(queryText(route.query.q)), loading = ref(false)
const loadError = ref(''), summaryLoaded = ref(false)
const statusSummary = ref<Record<string, number>>({}), selectedKeys = ref<string[]>([])
const actionBusy = ref(false), rowBusy = ref<string[]>([])
interface RetryJob {
  job_id?: string; platform?: string; status: string; total?: number; processed?: number
  succeeded?: number; skipped?: number; failed?: number; error?: string
  failures?: Array<{ task_key: string; message: string }>
}
const retryJob = ref<RetryJob>({ status: 'idle' }), retryAllBusy = ref(false), retryJobError = ref('')
const retryJobActive = computed(() => ['queued', 'running'].includes(retryJob.value.status))
const deleteJob = ref<RetryJob>({ status: 'idle' }), deleteAllBusy = ref(false), deleteJobError = ref('')
const deleteJobActive = computed(() => ['queued', 'running'].includes(deleteJob.value.status))
const deleteJobNames: Record<string, string> = { queued: '等待后台删除', running: '正在删除失败任务', completed: '失败任务删除完成', partial: '失败任务部分删除', interrupted: '删除作业中断' }
let deleteJobSequence = 0, deletePollInFlight = false
const retryJobNames: Record<string, string> = { queued: '等待后台执行', running: '正在提交重试', completed: '重试提交完成', partial: '部分提交完成', interrupted: '作业中断' }
let retryJobSequence = 0, retryPollInFlight = false
const actionFailures = ref<Array<{ task_key: string; message: string; status_code: number }>>([])
const statsTask = ref<UnifiedTask>(), statsData = ref<any>({ snapshots: [] }), statsBusy = ref(false)
const statsDialog = ref<HTMLElement | null>(null)
const statsMetric = ref<'view_count' | 'like_count' | 'comment_count' | 'share_count'>('view_count')
let searchTimer: number | undefined
let pollTimer: number | undefined
let loadSequence = 0
let pollInFlight = false
type TaskAction = 'retry' | 'cancel' | 'pause' | 'resume' | 'refresh_retry' | 'delete'
let statsReturnFocus: HTMLElement | null = null
const platformNames: Record<string, string> = { douyin: '抖音', x: 'X', tiktok: 'TikTok', weibo: '微博', bilibili: 'B站', xhs: '小红书' }
const statusNames: Record<string, string> = { pending: '等待中', downloading: '下载中', paused: '已暂停', completed: '已完成', skipped: '已跳过', failed: '失败', cancelled: '已取消' }
const phaseNames: Record<string, string> = { queued: '排队中', preparing: '准备中', downloading: '下载中', completed: '已完成', failed: '失败', cancelled: '已取消' }
const statusOrder = ['pending', 'downloading', 'paused', 'failed', 'cancelled', 'completed', 'skipped']
const visibleSummaries = computed(() => statusOrder.filter(item => statusSummary.value[item] || status.value === item))
const allPageSelected = computed(() => tasks.value.length > 0 && tasks.value.every(task => selectedKeys.value.includes(task.key)))
const selectedTasks = computed(() => tasks.value.filter(task => selectedKeys.value.includes(task.key)))
const retryableSelected = computed(() => selectedTasks.value.filter(task => ['failed', 'cancelled'].includes(task.status)))
const deletableSelected = computed(() => selectedTasks.value.filter(task => ['failed', 'cancelled'].includes(task.status)))
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

async function load(silent = false) {
  if (silent && pollInFlight) return
  if (silent) pollInFlight = true
  const sequence = ++loadSequence
  if (!silent) loading.value = true
  const params = new URLSearchParams({ page: String(page.value), page_size: '20' })
  if (platform.value) params.set('platform', platform.value)
  if (status.value) params.set('status', status.value)
  if (search.value.trim()) params.set('q', search.value.trim())
  if (queryText(route.query.task_key)) params.set('task_key', queryText(route.query.task_key))
  try {
    const data = await api<UnifiedTaskPage>(`/operations/tasks?${params}`)
    if (sequence !== loadSequence) return
    if (page.value > Math.max(1, data.pages)) {
      page.value = Math.max(1, data.pages)
      syncQuery()
      void load()
      return
    }
    tasks.value = data.items
    pages.value = data.pages
    total.value = data.total
    statusSummary.value = data.status_summary || {}
    summaryLoaded.value = true
    loadError.value = ''
    selectedKeys.value = selectedKeys.value.filter(key => data.items.some(task => task.key === key))
  } catch (error: any) {
    if (sequence !== loadSequence) return
    loadError.value = error.message || '加载统一任务失败'
    if (!silent) store.notify(loadError.value, 'error')
  }
  finally { if (silent) pollInFlight = false; if (sequence === loadSequence) loading.value = false }
}
function syncQuery() {
  const query: Record<string, string> = {}
  if (platform.value) query.platform = platform.value
  if (status.value) query.status = status.value
  if (search.value.trim()) query.q = search.value.trim()
  if (page.value > 1) query.page = String(page.value)
  void router.replace({ path: '/operations/tasks', query })
}
function resetAndLoad() { page.value = 1; selectedKeys.value = []; syncQuery(); void load() }
function queueSearch() { window.clearTimeout(searchTimer); searchTimer = window.setTimeout(resetAndLoad, 350) }
function toggleTask(taskKey: string) {
  selectedKeys.value = selectedKeys.value.includes(taskKey)
    ? selectedKeys.value.filter(key => key !== taskKey)
    : [...selectedKeys.value, taskKey]
}
function togglePage() { selectedKeys.value = allPageSelected.value ? [] : tasks.value.map(task => task.key) }
function setStatus(next: string) { status.value = next === status.value ? '' : next; resetAndLoad() }
async function loadRetryJob() {
  if (retryPollInFlight || retryAllBusy.value) return
  retryPollInFlight = true
  const sequence = ++retryJobSequence
  try {
    const data = await api<RetryJob>('/operations/tasks/retry-all-failed')
    if (sequence === retryJobSequence) { retryJob.value = data; retryJobError.value = '' }
  } catch (error: any) {
    if (sequence === retryJobSequence) retryJobError.value = error.message || '全部重试进度暂不可用，请刷新核对；不要重复提交。'
  } finally { retryPollInFlight = false }
}
async function retryAllFailed() {
  if (retryAllBusy.value || retryJobActive.value || deleteAllBusy.value || deleteJobActive.value || actionBusy.value) return
  retryAllBusy.value = true
  ++retryJobSequence
  const targetPlatform = platform.value
  const scope = targetPlatform ? (platformNames[targetPlatform] || targetPlatform) : '全部平台'
  try {
    const preview = await api<{ total: number }>(`/operations/tasks/retry-all-failed/preview?platform=${encodeURIComponent(targetPlatform)}`)
    if (!preview.total) { store.notify(`${scope}没有失败任务`, 'info'); return }
    if (!confirm(`重试${scope}的全部 ${preview.total.toLocaleString()} 个失败任务？\n跨所有分页，不受搜索和当前选择限制，不包含已取消任务。\n采用普通重试；抖音直链返回 403、404、410 时自动尝试刷新，不强制刷新所有链接。\n后台逐项重新排队，下载仍遵循并发限制；提交时数量可能变化。`)) return
    const result = await api<{ message: string; data: RetryJob }>('/operations/tasks/retry-all-failed', {
      method: 'POST', ...jsonBody({ platform: targetPlatform }),
    })
    retryJob.value = result.data
    retryJobError.value = ''
    store.notify(result.message, 'info')
    await load()
  } catch (error: any) {
    store.notify(error.message || '全部重试提交失败，请核对后台作业状态', 'error')
  } finally { retryAllBusy.value = false; void loadRetryJob() }
}
async function runAction(taskKeys: string[], action: TaskAction) {
  if (!taskKeys.length || actionBusy.value) return
  if (action === 'cancel' && !confirm(`确定取消 ${taskKeys.length} 个任务？已保存的文件不会删除。`)) return
  if (action === 'delete' && !confirm(`删除 ${taskKeys.length} 个失败或已取消任务？\n删除任务及关联下载历史，不删除磁盘文件、作者或作品。\n后续订阅扫描若再次发现该作品，会补建缺失任务并重新下载；本次不立即重试。\n其他平台已保存部分媒体的任务会保留并说明原因。任务记录删除不可撤销。`)) return
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
async function loadDeleteJob() {
  if (deletePollInFlight || deleteAllBusy.value) return
  deletePollInFlight = true
  const sequence = ++deleteJobSequence
  try {
    const data = await api<RetryJob>('/operations/tasks/delete-all-failed')
    if (sequence === deleteJobSequence) { deleteJob.value = data; deleteJobError.value = '' }
  } catch (error: any) {
    if (sequence === deleteJobSequence) deleteJobError.value = error.message || '删除进度暂不可用，请刷新核对，不要重复提交。'
  } finally { deletePollInFlight = false }
}
async function deleteAllFailed() {
  if (deleteAllBusy.value || deleteJobActive.value || retryAllBusy.value || retryJobActive.value || actionBusy.value) return
  deleteAllBusy.value = true
  ++deleteJobSequence
  const targetPlatform = platform.value
  const scope = targetPlatform ? (platformNames[targetPlatform] || targetPlatform) : '全部平台'
  try {
    const preview = await api<{ total: number }>(`/operations/tasks/delete-all-failed/preview?platform=${encodeURIComponent(targetPlatform)}`)
    if (!preview.total) { store.notify(`${scope}没有失败任务`, 'info'); return }
    if (!confirm(`删除${scope}的全部 ${preview.total.toLocaleString()} 个失败任务？\n跨所有分页，不受搜索和当前选择限制，不包含已取消任务。\n删除任务及关联下载历史，保留磁盘文件、作者和作品；任务记录删除不可撤销。\n后续订阅再次发现作品时，会补建缺失任务重新下载。状态已变化或保存部分媒体的任务会跳过并说明原因。`)) return
    const result = await api<{ message: string; data: RetryJob }>('/operations/tasks/delete-all-failed', {
      method: 'POST', ...jsonBody({ platform: targetPlatform }),
    })
    deleteJob.value = result.data; deleteJobError.value = ''
    store.notify(result.message, 'info'); await load()
  } catch (error: any) { store.notify(error.message || '全部删除提交失败，请核对后台作业状态', 'error') }
  finally { deleteAllBusy.value = false; void loadDeleteJob() }
}
async function action(task: UnifiedTask, actionName: TaskAction) {
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
  statsReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
  statsTask.value = task; statsBusy.value = true; statsData.value = { snapshots: [] }
  document.body.classList.add('modal-open')
  void nextTick(() => focusFirst(statsDialog.value))
  try { statsData.value = await api<any>(task.stats_endpoint) }
  catch (error: any) { store.notify(error.message || '互动趋势加载失败', 'error') }
  finally { statsBusy.value = false }
}
function closeStats() {
  statsTask.value = undefined
  document.body.classList.remove('modal-open')
  const target = statsReturnFocus
  statsReturnFocus = null
  void nextTick(() => restoreFocus(target))
}
function statsKeydown(event: KeyboardEvent) {
  if (!statsTask.value) return
  if (event.key === 'Escape') { event.preventDefault(); closeStats() }
  else trapFocus(event, statsDialog.value)
}
function changePage(value: number) { page.value = value; syncQuery(); void load() }
async function copyFailure(task: UnifiedTask) {
  try {
    await navigator.clipboard.writeText(`平台：${platformNames[task.platform] || task.platform}\n任务：${task.key}\n来源：${task.source_label}\n错误代码：${task.error_code || '未提供'}\n失败原因：${task.error_message || '未提供'}`)
    store.notify('失败信息已复制')
  } catch { store.notify('复制失败，请检查浏览器剪贴板权限', 'error') }
}
onMounted(() => {
  void load()
  void loadRetryJob()
  void loadDeleteJob()
  pollTimer = window.setInterval(() => {
    if (!document.hidden && !loading.value && !actionBusy.value) void load(true)
    if (!document.hidden) void loadRetryJob()
    if (!document.hidden) void loadDeleteJob()
  }, 5000)
  document.addEventListener('keydown', statsKeydown)
})
onBeforeUnmount(() => { loadSequence++; retryJobSequence++; deleteJobSequence++; window.clearInterval(pollTimer); window.clearTimeout(searchTimer); document.removeEventListener('keydown', statsKeydown); document.body.classList.remove('modal-open') })
watch(() => route.fullPath, (path, previousPath) => {
  const nextPlatform = queryText(route.query.platform), nextStatus = queryText(route.query.status)
  const nextSearch = queryText(route.query.q), nextPage = queryPage(route.query.page)
  if (nextPlatform === platform.value && nextStatus === status.value && nextSearch === search.value.trim() && nextPage === page.value && path === previousPath) return
  platform.value = nextPlatform; status.value = nextStatus; search.value = nextSearch; page.value = nextPage
  void load()
})
</script>

<template>
  <section class="workspace-card unified-workspace">
    <header class="workspace-header">
      <div><h2>全部任务</h2><span>跨平台检索、批量处理与失败诊断；筛选会保留在链接中。</span></div>
      <div class="header-actions">
        <MoreActions label="跨全部分页操作">
        <button class="btn ghost" title="普通重试；抖音直链返回 403、404、410 时自动尝试刷新。需强制刷新请使用任务行的刷新链接重试。" :disabled="retryAllBusy || retryJobActive || deleteAllBusy || deleteJobActive || actionBusy" @click="retryAllFailed"><RotateCcw :size="16" />{{ retryAllBusy ? '正在核对…' : retryJobActive ? '后台重试中…' : '重试全部失败' }}</button>
        <button class="btn danger" :disabled="deleteAllBusy || deleteJobActive || retryAllBusy || retryJobActive || actionBusy" @click="deleteAllFailed"><Trash2 :size="16" />{{ deleteAllBusy ? '正在核对…' : deleteJobActive ? '后台删除中…' : '删除全部失败任务' }}</button>
        </MoreActions>
        <button class="btn ghost" :disabled="loading" @click="load(); loadRetryJob(); loadDeleteJob()"><RefreshCw :size="16" />{{ loading ? '刷新中…' : '刷新' }}</button>
      </div>
    </header>
    <div v-if="retryJobError" class="load-error-banner" role="alert"><IssueDetail :message="retryJobError" /><button class="text-button" @click="loadRetryJob">核对进度</button></div>
    <JobReport v-if="retryJob.status !== 'idle'" :title="(retryJobNames[retryJob.status] || retryJob.status) + ' · ' + (retryJob.platform ? platformNames[retryJob.platform] : '全部平台')" :status="retryJob.status" :processed="retryJob.processed" :total="retryJob.total" :failed="retryJob.failed">

      <span>已处理 {{ retryJob.processed || 0 }}/{{ retryJob.total || 0 }} · 已提交 {{ retryJob.succeeded || 0 }} · 跳过 {{ retryJob.skipped || 0 }} · 未提交 {{ retryJob.failed || 0 }} · 未处理 {{ Math.max(0, (retryJob.total || 0) - (retryJob.processed || 0)) }}</span>
      <small>这是重试投递结果，不代表下载已完成。仅处理提交时的失败任务；状态已变化的任务会跳过。</small>
      <IssueDetail v-if="retryJob.error" :message="retryJob.error" />
      <details v-if="retryJob.failures?.length"><summary>查看未提交与待核对原因（最多 200 条）</summary><ul><li v-for="item in retryJob.failures" :key="item.task_key"><b>{{ item.task_key }}</b> {{ item.message }}</li></ul></details>
    </JobReport>
    <div v-if="deleteJobError" class="load-error-banner" role="alert"><IssueDetail :message="deleteJobError" /><button class="text-button" @click="loadDeleteJob">核对进度</button></div>
    <JobReport v-if="deleteJob.status !== 'idle'" :title="(deleteJobNames[deleteJob.status] || deleteJob.status) + ' · ' + (deleteJob.platform ? platformNames[deleteJob.platform] : '全部平台')" :status="deleteJob.status" :processed="deleteJob.processed" :total="deleteJob.total" :failed="deleteJob.failed">

      <span>已处理 {{ deleteJob.processed || 0 }}/{{ deleteJob.total || 0 }} · 已删除 {{ deleteJob.succeeded || 0 }} · 跳过 {{ deleteJob.skipped || 0 }} · 删除失败 {{ deleteJob.failed || 0 }} · 未处理 {{ Math.max(0, (deleteJob.total || 0) - (deleteJob.processed || 0)) }}</span>
      <small>只处理提交时的失败任务，不删除磁盘文件。状态已变化的任务会跳过；订阅仍可补建缺失任务。</small>
      <IssueDetail v-if="deleteJob.error" :message="deleteJob.error" />
      <details v-if="deleteJob.failures?.length"><summary>查看删除失败与待核对原因（最多 200 条）</summary><ul><li v-for="item in deleteJob.failures" :key="item.task_key"><b>{{ item.task_key }}</b> {{ item.message }}</li></ul></details>
    </JobReport>
    <div v-if="loadError" class="load-error-banner" role="alert"><IssueDetail :message="loadError" impact="当前数据读取失败；下方如有列表，为上次读取的结果。" /><button class="text-button" @click="load()">重试</button></div>
    <div v-if="route.query.task_key" class="selection-bar" role="status"><span>当前定位任务 {{ route.query.task_key }}</span><button class="text-button" @click="resetAndLoad">返回任务列表</button></div>
    <FilterToolbar :active="[platform && platformNames[platform], status && statusNames[status]].filter(Boolean).join(' · ')" @clear="platform = ''; status = ''; resetAndLoad()">
      <template #search><label class="search"><Search :size="16" /><input v-model="search" aria-label="搜索任务" placeholder="搜索作者、标题、作品 ID" @input="queueSearch" /></label></template>
      <select v-model="platform" aria-label="平台" @change="resetAndLoad"><option value="">全部平台</option><option v-for="item in store.platforms" :key="item.id" :value="item.id">{{ item.name }}</option></select>
      <select v-model="status" aria-label="状态" @change="resetAndLoad"><option value="">全部状态{{ summaryLoaded ? ' · ' + summaryTotal.toLocaleString() : '' }}</option><option v-for="item in statusOrder" :key="item" :value="item">{{ statusNames[item] }} · {{ statusSummary[item] ?? '—' }}</option></select>
    </FilterToolbar>
    <div v-if="selectedKeys.length" class="selection-bar" role="status">
      <span>已选择 <b>{{ selectedKeys.length }}</b> 个当前页任务</span>
      <div>
        <button class="btn ghost compact" :disabled="actionBusy || !retryableSelected.length" @click="runAction(retryableSelected.map(task => task.key), 'retry')"><RotateCcw :size="15" />{{ actionBusy ? '处理中…' : `重试 ${retryableSelected.length}` }}</button>
        <button class="btn ghost compact" :disabled="actionBusy || !cancellableSelected.length" @click="runAction(cancellableSelected.map(task => task.key), 'cancel')"><Square :size="15" />{{ actionBusy ? '处理中…' : `取消 ${cancellableSelected.length}` }}</button>
        <button class="btn ghost compact danger" :disabled="actionBusy || !deletableSelected.length" @click="runAction(deletableSelected.map(task => task.key), 'delete')"><Trash2 :size="15" />{{ actionBusy ? '处理中…' : `删除 ${deletableSelected.length}` }}</button>
      </div>
    </div>
    <details v-if="actionFailures.length" class="action-report"><summary>{{ actionFailures.length }} 个任务未处理，查看原因</summary><ul><li v-for="item in actionFailures" :key="item.task_key"><b>{{ item.task_key }}</b><span>{{ item.message }}</span></li></ul></details>
    <div class="table-shell" :class="{ loading }">
      <table class="data-table unified-table">
        <thead><tr><th class="select-col"><label class="selection-hit"><input type="checkbox" aria-label="选择当前页全部任务" :checked="allPageSelected" @change="togglePage" /></label></th><th>任务与来源</th><th>状态与结果</th><th class="actions-col">操作</th></tr></thead>
        <tbody><tr v-for="task in tasks" :key="task.key" :class="{ selected: selectedKeys.includes(task.key) }">
          <td class="select-col" data-label="选择"><label class="selection-hit"><input type="checkbox" :aria-label="`选择任务 ${task.id}`" :checked="selectedKeys.includes(task.key)" @change="toggleTask(task.key)" /></label></td>
          <td class="task-source"><div class="media-cell"><div><strong :title="task.source_label">{{ task.source_label || '任务 #' + task.id }}</strong><span>{{ platformNames[task.platform] || task.platform }} · {{ task.author_name || (task.source_type === 'profile' ? '作者主页' : '单条作品') }} · #{{ task.id }}</span><small v-if="task.published_at">{{ new Date(task.published_at).toLocaleDateString() }}</small></div></div></td>
          <td class="task-state"><span class="status" :data-tone="task.status">{{ statusNames[task.status] || task.status }}</span><span v-if="task.status === 'completed'">{{ task.file_count }} 个文件</span><span v-else-if="task.status === 'downloading'">{{ Number(task.progress_percent || 0).toFixed(1) }}%</span><small v-else-if="task.phase && phaseNames[task.phase] !== statusNames[task.status]">{{ phaseNames[task.phase] || task.phase }}</small><IssueDetail v-if="task.error_message" :message="task.error_message" :code="task.error_code || ''"><button class="text-button" @click="copyFailure(task)">复制任务上下文</button></IssueDetail></td>
          <td data-label="操作"><div class="row-actions"><button v-if="task.preview_count" class="icon-btn" title="预览" :aria-label="`预览任务 ${task.id}`" :disabled="rowBusy.includes(task.key)" @click="preview(task)"><Eye :size="17" /></button><MoreActions v-if="task.has_stats || ['pending','downloading','paused','failed','cancelled'].includes(task.status)" :label="'任务操作：' + task.id"><button v-if="task.platform === 'douyin' && ['pending','downloading'].includes(task.status)" class="icon-btn" :aria-label="'暂停任务 ' + task.id" :disabled="actionBusy" @click="action(task, 'pause')"><Pause :size="17" /></button><button v-if="task.platform === 'douyin' && task.status === 'paused'" class="icon-btn" :aria-label="'恢复任务 ' + task.id" :disabled="actionBusy" @click="action(task, 'resume')"><Play :size="17" /></button><button v-if="task.platform === 'douyin' && ['failed','cancelled'].includes(task.status)" class="icon-btn" :aria-label="'刷新链接重试任务 ' + task.id" title="刷新链接重试" :disabled="actionBusy || rowBusy.includes(task.key)" @click="action(task, 'refresh_retry')"><RefreshCw :size="17" /></button><button v-if="task.has_stats" class="icon-btn" title="互动趋势" :aria-label="`查看任务 ${task.id} 互动趋势`" @click="showStats(task)"><TrendingUp :size="17" /></button><button v-if="['failed','cancelled'].includes(task.status)" class="icon-btn" title="重试" :aria-label="`重试任务 ${task.id}`" :disabled="actionBusy || rowBusy.includes(task.key)" @click="action(task, 'retry')"><RotateCcw :size="17" /></button><button v-if="['failed','cancelled'].includes(task.status)" class="icon-btn danger" title="删除任务（保留磁盘文件）" :aria-label="`删除任务 ${task.id}`" :disabled="actionBusy || rowBusy.includes(task.key)" @click="action(task, 'delete')"><Trash2 :size="17" /></button><button v-if="['pending','downloading','paused'].includes(task.status)" class="icon-btn" title="取消" :aria-label="`取消任务 ${task.id}`" :disabled="actionBusy || rowBusy.includes(task.key)" @click="action(task, 'cancel')"><Square :size="17" /></button></MoreActions></div></td>
        </tr></tbody>
      </table>
      <div v-if="!loading && !tasks.length && !loadError" class="empty-state"><strong>暂无符合条件的任务</strong><span>可调整平台、状态或搜索条件后重试</span></div>
    </div>
    <Pager v-if="!loadError" :page="page" :pages="pages" :total="total" @change="changePage" />
    <Teleport to="body"><div v-if="statsTask" class="trend-overlay" @click.self="closeStats"><section ref="statsDialog" class="trend-dialog" role="dialog" aria-modal="true" aria-labelledby="task-trend-title" tabindex="-1"><header><div><h3 id="task-trend-title">{{ statsData.label || statsTask.source_label }}</h3><span>{{ platformNames[statsTask.platform] }} · {{ statsSeries.length }} 个统计快照</span></div><button class="icon-btn" aria-label="关闭互动趋势" @click="closeStats"><X /></button></header><nav aria-label="趋势指标"><button v-for="metric in statsMetrics" :key="metric[0]" :class="{ active: statsMetric === metric[0] }" :aria-pressed="statsMetric === metric[0]" @click="statsMetric = metric[0]">{{ metric[1] }}</button></nav><div v-if="statsBusy" class="empty-state">正在读取趋势…</div><template v-else-if="statsSeries.length"><div class="trend-kpis"><article><strong>{{ formatCount(statsSummary.latest) }}</strong><span>当前值</span></article><article><strong>+{{ formatCount(statsSummary.delta) }}</strong><span>区间增长</span></article><article :data-alert="statsSummary.unusual"><strong>+{{ formatCount(statsSummary.latestDelta) }}</strong><span>最近增量{{ statsSummary.unusual ? ' · 异常增长' : '' }}</span></article></div><svg class="trend-chart" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="跨平台互动数据变化曲线"><line x1="0" y1="90" x2="100" y2="90" /><line x1="0" y1="50" x2="100" y2="50" /><line x1="0" y1="10" x2="100" y2="10" /><polyline :points="statsPoints" /></svg><div class="trend-table"><article v-for="(snapshot, index) in [...statsSeries].reverse().slice(0, 30)" :key="snapshot.id || index"><time>{{ new Date(snapshot.observed_at).toLocaleString() }}</time><strong>{{ formatCount(snapshot[statsMetric]) }}</strong><span>{{ snapshot.source }}</span></article></div></template><div v-else class="empty-state"><TrendingUp /><strong>该平台尚未返回互动统计</strong><span>后续下载取得统计字段时会自动开始记录</span></div></section></div></Teleport>
  </section>
</template>
