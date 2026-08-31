<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Clipboard, Download, Eye, MoreHorizontal, Pause, Play, RefreshCw, RotateCcw, Search, Trash2 } from '@lucide/vue'
import { api, jsonBody } from '../api'
import { openMedia } from '../media'
import { useAppStore } from '../stores/app'
import type { PageData, Task } from '../types'
import Pager from '../components/Pager.vue'

const store = useAppStore()
const tasks = ref<Task[]>([])
const total = ref(0), pages = ref(1), page = ref(1)
const loading = ref(false), status = ref(''), query = ref(''), shareUrl = ref('')
const timer = ref<number>(), queryTimer = ref<number>()
const statusCounts = ref<Record<string, number>>({})
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
async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    const params = new URLSearchParams({ page: String(page.value), page_size: '20' })
    if (status.value) params.set('status', status.value)
    if (query.value.trim()) params.set('q', query.value.trim())
    const data = await api<PageData<Task> & { status_counts: Record<string, number> }>(`/tasks/?${params}`)
    tasks.value = data.items; total.value = data.total; pages.value = data.pages; statusCounts.value = data.status_counts || {}
  } catch (error: any) { if (!silent) store.notify(error.message || '加载任务失败', 'error') }
  finally { loading.value = false }
}
async function createTask() {
  if (!shareUrl.value.trim()) return store.notify('请粘贴抖音分享链接', 'error')
  if (store.risk.active) return store.notify('抖音接口正在冷却，请等待倒计时结束', 'error')
  try {
    const result = await api<any>('/tasks/download', { method: 'POST', ...jsonBody({ share_url: shareUrl.value.trim(), start_index: 1, wait_time: 1 }) })
    shareUrl.value = ''; store.notify(result.url_type === 'author' ? '作者下载任务已提交' : `已创建 ${result.created_tasks || 0} 个任务`)
    await load(); await store.refreshStatus()
  } catch (error: any) { store.notify(error.message || '创建任务失败', 'error') }
}
async function action(task: Task, verb: string, method = 'POST') {
  const path = verb === 'refresh-retry' ? `/tasks/refresh-retry/${task.id}` : `/tasks/${task.id}/${verb}`
  try { const result = await api<any>(path, { method }); store.notify(result.message || '操作成功'); await load() }
  catch (error: any) { store.notify(error.message || '操作失败', 'error') }
}
async function remove(task: Task) {
  if (!confirm(`确定删除任务 #${task.id}？`)) return
  try { await api(`/tasks/${task.id}`, { method: 'DELETE' }); store.notify('任务已删除'); await load(); await store.refreshStatus() }
  catch (error: any) { store.notify(error.message || '删除失败', 'error') }
}
async function bulk(endpoint: string, confirmText?: string) {
  if (confirmText && !confirm(confirmText)) return
  try { const result = await api<any>(`/tasks/${endpoint}`, { method: 'POST' }); store.notify(result.message || '操作完成'); await load() }
  catch (error: any) { store.notify(error.message || '批量操作失败', 'error') }
}
async function copyErrors() {
  try {
    const result = await api<any>('/tasks/failed/errors')
    if (!result.data?.errors?.length) return store.notify('没有失败原因可复制', 'info')
    await navigator.clipboard.writeText(result.data.errors.join('\n\n')); store.notify(`已复制 ${result.data.count} 条失败原因`)
  } catch (error: any) { store.notify(error.message || '复制失败', 'error') }
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
onMounted(() => { load(); timer.value = window.setInterval(() => load(true), 5000) })
onBeforeUnmount(() => { clearInterval(timer.value); if (queryTimer.value != null) window.clearTimeout(queryTimer.value) })
</script>

<template>
  <section class="workspace-card task-workspace">
    <header class="workspace-header">
      <div><p class="eyebrow">DOUYIN TASKS</p><h2>下载任务</h2><span>管理队列、进度、失败诊断与媒体资源</span></div>
      <div class="header-actions">
        <button v-if="total && (status === 'failed' || failedCount)" class="btn ghost" @click="copyErrors"><Clipboard :size="16" />复制所有失败原因</button>
        <button class="btn ghost" @click="load()"><RefreshCw :size="16" />刷新</button>
        <details class="menu"><summary class="btn ghost"><MoreHorizontal :size="18" />批量操作</summary><div class="menu-popover">
          <button @click="bulk('pause-all', '确定暂停全部等待中和下载中的任务？')"><Pause :size="15" />全部暂停</button>
          <button @click="bulk('redispatch-pending')"><Play :size="15" />分发待处理</button>
          <button @click="bulk('retry-all-failed')"><RotateCcw :size="15" />重试失败</button>
          <button @click="bulk('refresh-retry-all-failed')"><RefreshCw :size="15" />刷新链接后重试</button>
        </div></details>
      </div>
    </header>

    <form class="command-bar" @submit.prevent="createTask">
      <Download :size="18" /><input v-model="shareUrl" placeholder="粘贴作者主页或单个作品分享链接…" autocomplete="off" /><button class="btn primary" :disabled="store.risk.active">开始下载</button>
    </form>

    <div class="filter-row">
      <nav class="segmented"><button v-for="item in statuses" :key="item[0]" :class="{ active: status === item[0] }" @click="setStatus(item[0])">{{ item[1] }} <small>{{ statusCount(item[0]) }}</small></button></nav>
      <label class="search compact-search"><Search :size="15" /><input v-model="query" placeholder="搜索全部任务、作品或作者" /></label>
    </div>

    <div class="table-shell" :class="{ loading }">
      <table class="data-table task-table">
        <thead><tr><th>任务</th><th>状态与进度</th><th>传输</th><th>时间</th><th class="actions-col">操作</th></tr></thead>
        <tbody>
          <tr v-for="task in tasks" :key="task.id">
            <td><div class="media-cell"><span class="media-icon">{{ task.work_type === 'images' ? 'IMG' : 'VID' }}</span><div><strong :title="task.file_name || task.work_title">{{ task.file_name || task.work_title || `任务 #${task.id}` }}</strong><span>{{ task.author_nickname || '未知作者' }} · #{{ task.id }}</span><p v-if="task.error_message" :class="task.status === 'skipped' ? 'inline-note' : 'inline-error'" :title="task.error_message">{{ task.error_message }}</p></div></div></td>
            <td><div class="status-line"><span class="status" :data-tone="task.status">{{ statusLabel(task.status) }}</span><b>{{ Number(task.progress_percent || 0).toFixed(1) }}%</b></div><div class="progress"><i :style="{ width: `${Math.min(100, task.progress_percent || 0)}%` }" /></div><small v-if="task.error_action">{{ task.error_action }}</small></td>
            <td><strong>{{ bytes(task.downloaded_bytes) }} / {{ bytes(task.total_bytes) }}</strong><span>{{ task.download_speed ? `${bytes(task.download_speed)}/s` : '等待传输' }}</span></td>
            <td><span>{{ new Date(task.created_at).toLocaleDateString() }}</span><small>{{ new Date(task.created_at).toLocaleTimeString() }}</small></td>
            <td><div class="row-actions">
              <button v-if="task.preview_url" class="icon-btn" title="预览" @click="preview(task)"><Eye :size="17" /></button>
              <button v-if="task.status === 'downloading' || task.status === 'pending'" class="icon-btn" title="暂停" @click="action(task, 'pause')"><Pause :size="17" /></button>
              <button v-if="task.status === 'paused'" class="icon-btn" title="恢复" @click="action(task, 'resume')"><Play :size="17" /></button>
              <button v-if="task.status === 'failed' || task.status === 'cancelled'" class="icon-btn" title="重试" @click="action(task, task.error_category === 'risk_control' ? 'refresh-retry' : 'retry')"><RotateCcw :size="17" /></button>
              <button class="icon-btn danger" title="删除" @click="remove(task)"><Trash2 :size="17" /></button>
            </div></td>
          </tr>
        </tbody>
      </table>
      <div v-if="!loading && !tasks.length" class="empty-state"><Download /><strong>暂无任务</strong><span>粘贴分享链接即可开始</span></div>
    </div>
    <Pager :page="page" :pages="pages" :total="total" @change="page = $event" />
  </section>
</template>
