<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Clipboard, Download, Eye, MoreHorizontal, Pause, Play, RefreshCw, RotateCcw, Search, Trash2 } from '@lucide/vue'
import { useRouter } from 'vue-router'
import { api, jsonBody } from '../api'
import { openMedia } from '../media'
import { useAppStore } from '../stores/app'
import type { PageData, Task } from '../types'
import Pager from '../components/Pager.vue'

const store = useAppStore()
const router = useRouter()
const tasks = ref<Task[]>([])
const total = ref(0), pages = ref(1), page = ref(1)
const loading = ref(false), status = ref(''), query = ref(''), shareUrl = ref('')
const loadError = ref('')
const timer = ref<number>(), queryTimer = ref<number>()
const statusCounts = ref<Record<string, number>>({})
const createBusy = ref(false), bulkBusy = ref(false)
const busyTaskIds = ref(new Set<number>())
const statuses = [
  ['', '全部'], ['pending', '待处理'], ['downloading', '下载中'], ['paused', '已暂停'], ['completed', '已完成'], ['skipped', '规则跳过'], ['failed', '失败'], ['cancelled', '已取消'],
]
const failedCount = computed(() => statusCounts.value.failed || 0)

function bytes(value = 0) {
  if (!value) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']; let size = value; let unit = 0
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit++ }
  return `${size.toFixed(unit > 1 ? 1 : 0)} ${units[unit]}`
}
function statusLabel(value: string) {
  return ({ pending: '等待中', downloading: '下载中', paused: '已暂停', completed: '已完成', skipped: '规则跳过', failed: '失败', cancelled: '已取消' } as Record<string, string>)[value] || value
}
function transferLabel(task: Task) {
  if (task.status === 'completed') return '传输完成'
  if (task.status === 'paused') return '已暂停传输'
  if (task.status === 'skipped') return '无需传输'
  if (task.status === 'failed') return '传输失败'
  if (task.status === 'cancelled') return '已取消传输'
  return task.download_speed > 0 ? `${bytes(task.download_speed)}/s` : '等待传输'
}
async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    const params = new URLSearchParams({ page: String(page.value), page_size: '20' })
    if (status.value) params.set('status', status.value)
    if (query.value.trim()) params.set('q', query.value.trim())
    const data = await api<PageData<Task> & { status_counts: Record<string, number> }>(`/tasks/?${params}`)
    tasks.value = data.items; total.value = data.total; pages.value = data.pages; statusCounts.value = data.status_counts || {}; loadError.value = ''
  } catch (error: any) { loadError.value = error.message || '加载任务失败'; if (!silent) store.notify(loadError.value, 'error') }
  finally { loading.value = false }
}
async function createTask() {
  if (!shareUrl.value.trim()) return store.notify('请粘贴抖音分享链接', 'error')
  if (store.risk.active) return store.notify('抖音接口正在冷却，请等待倒计时结束', 'error')
  createBusy.value = true
  try {
    const result = await api<any>('/tasks/download', { method: 'POST', ...jsonBody({ share_url: shareUrl.value.trim(), start_index: 1, wait_time: 1 }) })
    shareUrl.value = ''
    if (result.url_type === 'author' && result.author_already_exists && result.author_id) {
      store.notify('作者已存在，已定位到作者管理中的对应记录', 'info')
      await router.push({
        path: '/douyin/authors',
        query: { focus: String(result.author_id), position: String(result.author_position || 0) },
      })
      return
    }
    store.notify(result.url_type === 'author' ? '作者下载任务已提交' : `已创建 ${result.created_tasks || 0} 个任务`)
    await load(); await store.refreshStatus()
  } catch (error: any) { store.notify(error.message || '创建任务失败', 'error') }
  finally { createBusy.value = false }
}
async function action(task: Task, verb: string, method = 'POST') {
  if (busyTaskIds.value.has(task.id)) return
  const path = verb === 'refresh-retry' ? `/tasks/refresh-retry/${task.id}` : `/tasks/${task.id}/${verb}`
  busyTaskIds.value.add(task.id)
  try { const result = await api<any>(path, { method }); store.notify(result.message || '操作成功'); await load() }
  catch (error: any) { store.notify(error.message || '操作失败', 'error') }
  finally { busyTaskIds.value.delete(task.id) }
}
async function remove(task: Task) {
  if (!confirm(`确定删除任务 #${task.id}？`)) return
  try { await api(`/tasks/${task.id}`, { method: 'DELETE' }); store.notify('任务已删除'); await load(); await store.refreshStatus() }
  catch (error: any) { store.notify(error.message || '删除失败', 'error') }
}
async function bulk(endpoint: string, confirmText?: string) {
  if (bulkBusy.value) return
  if (confirmText && !confirm(confirmText)) return
  bulkBusy.value = true
  try { const result = await api<any>(`/tasks/${endpoint}`, { method: 'POST' }); store.notify(result.message || '操作完成'); await load() }
  catch (error: any) { store.notify(error.message || '批量操作失败', 'error') }
  finally { bulkBusy.value = false }
}
async function copyErrors() {
  try {
    const result = await api<any>('/tasks/failed/errors')
    if (!result.data?.errors?.length) return store.notify('没有失败原因可复制', 'info')
    await navigator.clipboard.writeText(result.data.errors.join('\n\n')); store.notify(`已复制 ${result.data.count} 条失败原因`)
  } catch (error: any) { store.notify(error.message || '复制失败', 'error') }
}
async function copyTaskError(task: Task) {
  try {
    await navigator.clipboard.writeText(`抖音任务 #${task.id}\n作品：${task.work_title || task.file_name || '未知'}\n错误代码：${task.error_code || '未提供'}\n失败原因：${task.error_message || '未提供'}\n建议操作：${task.error_action || '请检查任务详情'}`)
    store.notify('任务诊断已复制')
  } catch { store.notify('复制失败，请检查浏览器剪贴板权限', 'error') }
}
function preview(task: Task) {
  if (!task.preview_url) return
  openMedia([{ url: task.preview_url, type: task.preview_media_type === 'video' ? 'video' : 'image', title: task.work_title || task.file_name }])
}
function setStatus(value: string) { status.value = value; page.value = 1 }
function statusCount(value: string) { return value ? (statusCounts.value[value] || 0) : Object.values(statusCounts.value).reduce((sum, count) => sum + count, 0) }
watch([status, page], () => load())
watch(query, () => {
  if (queryTimer.value != null) window.clearTimeout(queryTimer.value)
  queryTimer.value = window.setTimeout(() => {
    if (page.value === 1) load()
    else page.value = 1
  }, 350)
})
onMounted(() => { void load(); timer.value = window.setInterval(() => { if (!document.hidden) void load(true) }, 5000) })
onBeforeUnmount(() => { clearInterval(timer.value); if (queryTimer.value != null) window.clearTimeout(queryTimer.value) })
</script>

<template>
  <section class="workspace-card task-workspace">
    <header class="workspace-header">
      <div><h2>抖音任务</h2><span>专项队列、进度和失败诊断；跨平台任务请使用“全部任务”。</span></div>
      <div class="header-actions">
        <button v-if="total && (status === 'failed' || failedCount)" class="btn ghost" @click="copyErrors"><Clipboard :size="16" />复制所有失败原因</button>
        <button class="btn ghost" @click="load()"><RefreshCw :size="16" />刷新</button>
        <details class="menu"><summary class="btn ghost"><MoreHorizontal :size="18" />批量操作</summary><div class="menu-popover">
          <button :disabled="bulkBusy" @click="bulk('pause-all', '确定暂停全部等待中和下载中的任务？')"><Pause :size="15" />全部暂停</button>
          <button :disabled="bulkBusy" @click="bulk('redispatch-pending')"><Play :size="15" />分发待处理</button>
          <button :disabled="bulkBusy" @click="bulk('retry-all-failed')"><RotateCcw :size="15" />重试失败</button>
          <button :disabled="bulkBusy" @click="bulk('refresh-retry-all-failed')"><RefreshCw :size="15" />{{ bulkBusy ? '正在提交…' : '刷新链接后重试' }}</button>
        </div></details>
      </div>
    </header>
    <div v-if="loadError" class="load-error-banner" role="alert">抖音任务暂不可用：{{ loadError }}{{ tasks.length ? '；下方为上次读取的结果。' : '' }}<button class="text-button" @click="load()">重试</button></div>

    <form class="command-bar" @submit.prevent="createTask">
      <Download :size="18" /><input v-model="shareUrl" aria-label="抖音作者主页或单个作品分享链接" placeholder="粘贴作者主页或单个作品分享链接…" autocomplete="off" /><button class="btn primary" :disabled="store.risk.active || createBusy">{{ createBusy ? '正在提交…' : '开始下载' }}</button>
    </form>

    <div class="filter-row">
      <nav class="segmented"><button v-for="item in statuses" :key="item[0]" :class="{ active: status === item[0] }" @click="setStatus(item[0])">{{ item[1] }} <small>{{ loadError && !tasks.length ? '—' : statusCount(item[0]) }}</small></button></nav>
      <label class="search compact-search"><Search :size="15" /><input v-model="query" aria-label="搜索抖音任务" placeholder="搜索全部任务、作品或作者" /></label>
    </div>

    <div class="table-shell" :class="{ loading }">
      <table class="data-table task-table">
        <thead><tr><th>任务</th><th>状态与进度</th><th>传输</th><th>时间</th><th class="actions-col">操作</th></tr></thead>
        <tbody>
          <tr v-for="task in tasks" :key="task.id">
            <td data-label="任务"><div class="media-cell"><span class="media-icon">{{ task.work_type === 'images' ? 'IMG' : 'VID' }}</span><div><strong :title="task.file_name || task.work_title">{{ task.file_name || task.work_title || `任务 #${task.id}` }}</strong><span>{{ task.author_nickname || '未知作者' }} · #{{ task.id }}</span><details v-if="task.error_message" class="task-error-detail"><summary>{{ task.status === 'skipped' ? '查看跳过原因' : '查看失败原因' }}</summary><p>{{ task.error_message }}</p><small v-if="task.error_action">建议：{{ task.error_action }}</small><button class="text-button" @click="copyTaskError(task)">复制诊断</button></details></div></div></td>
            <td data-label="状态与进度"><div class="status-line"><span class="status" :data-tone="task.status">{{ statusLabel(task.status) }}</span><b>{{ Number(task.progress_percent || 0).toFixed(1) }}%</b></div><div class="progress"><i :style="{ width: `${Math.min(100, task.progress_percent || 0)}%` }" /></div></td>
            <td data-label="传输"><strong>{{ bytes(task.downloaded_bytes) }} / {{ bytes(task.total_bytes) }}</strong><span>{{ transferLabel(task) }}</span></td>
            <td data-label="时间"><span>{{ new Date(task.created_at).toLocaleDateString() }}</span><small>{{ new Date(task.created_at).toLocaleTimeString() }}</small></td>
            <td data-label="操作"><div class="row-actions">
              <button v-if="task.preview_url" class="icon-btn" title="预览" :aria-label="`预览任务 ${task.id}`" @click="preview(task)"><Eye :size="17" /></button>
               <button v-if="task.status === 'downloading' || task.status === 'pending'" class="icon-btn" title="暂停" :aria-label="`暂停任务 ${task.id}`" :disabled="busyTaskIds.has(task.id)" @click="action(task, 'pause')"><Pause :size="17" /></button>
               <button v-if="task.status === 'paused'" class="icon-btn" title="恢复" :aria-label="`恢复任务 ${task.id}`" :disabled="busyTaskIds.has(task.id)" @click="action(task, 'resume')"><Play :size="17" /></button>
               <button v-if="task.status === 'failed' || task.status === 'cancelled'" class="icon-btn" :title="task.error_category === 'risk_control' ? '刷新链接后重试' : '重试'" :aria-label="`${task.error_category === 'risk_control' ? '刷新链接后重试' : '重试'}任务 ${task.id}`" :disabled="busyTaskIds.has(task.id)" @click="action(task, task.error_category === 'risk_control' ? 'refresh-retry' : 'retry')"><RotateCcw :size="17" /></button>
              <button class="icon-btn danger" title="删除" :aria-label="`删除任务 ${task.id}`" @click="remove(task)"><Trash2 :size="17" /></button>
            </div></td>
          </tr>
        </tbody>
      </table>
      <div v-if="!loading && !tasks.length && !loadError" class="empty-state"><Download /><strong>暂无任务</strong><span>粘贴分享链接即可开始</span></div>
    </div>
    <Pager v-if="!loadError || tasks.length" :page="page" :pages="pages" :total="total" @change="page = $event" />
  </section>
</template>
