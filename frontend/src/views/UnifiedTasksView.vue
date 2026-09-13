<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Eye, RefreshCw, RotateCcw, Search, Square } from '@lucide/vue'
import { api } from '../api'
import Pager from '../components/Pager.vue'
import { openMedia } from '../media'
import { useAppStore } from '../stores/app'
import type { MediaItem, PageData, UnifiedTask } from '../types'

const store = useAppStore()
const tasks = ref<UnifiedTask[]>([]), page = ref(1), pages = ref(1), total = ref(0)
const platform = ref(''), status = ref(''), search = ref(''), loading = ref(false)
let searchTimer: number | undefined
const platformNames: Record<string, string> = { douyin: '抖音', x: 'X', tiktok: 'TikTok', weibo: '微博', bilibili: 'B站', xhs: '小红书' }

async function load() {
  loading.value = true
  const params = new URLSearchParams({ page: String(page.value), page_size: '20' })
  if (platform.value) params.set('platform', platform.value)
  if (status.value) params.set('status', status.value)
  if (search.value.trim()) params.set('q', search.value.trim())
  try {
    const data = await api<PageData<UnifiedTask>>(`/operations/tasks?${params}`)
    tasks.value = data.items; pages.value = data.pages; total.value = data.total
  } catch (error: any) { store.notify(error.message || '加载统一任务失败', 'error') }
  finally { loading.value = false }
}
function resetAndLoad() { page.value = 1; void load() }
function queueSearch() { window.clearTimeout(searchTimer); searchTimer = window.setTimeout(resetAndLoad, 350) }
async function action(task: UnifiedTask, endpoint?: string) {
  if (!endpoint) return
  try { const result = await api<any>(endpoint, { method: 'POST' }); store.notify(result.message || '操作完成'); await load() }
  catch (error: any) { store.notify(error.message || '操作失败', 'error') }
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
onMounted(load)
</script>

<template>
  <section class="workspace-card">
    <header class="workspace-header"><div><p class="eyebrow">UNIFIED QUEUE</p><h2>全部平台任务</h2><span>统一查看状态、错误、重试、取消和本地预览</span></div><button class="btn ghost" @click="load"><RefreshCw :size="16" />刷新</button></header>
    <div class="filter-row unified-filters">
      <select v-model="platform" aria-label="平台" @change="resetAndLoad"><option value="">全部平台</option><option v-for="item in store.platforms" :key="item.id" :value="item.id">{{ item.name }}</option></select>
      <select v-model="status" aria-label="状态" @change="resetAndLoad"><option value="">全部状态</option><option value="pending">等待中</option><option value="downloading">下载中</option><option value="completed">已完成</option><option value="failed">失败</option><option value="cancelled">已取消</option></select>
      <label class="search"><Search :size="15" /><input v-model="search" placeholder="搜索作者、标题或来源" @input="queueSearch" /></label>
    </div>
    <div class="table-shell" :class="{ loading }"><table class="data-table"><thead><tr><th>平台与来源</th><th>元数据</th><th>状态</th><th>进度</th><th>结果</th><th class="actions-col">操作</th></tr></thead><tbody>
      <tr v-for="task in tasks" :key="task.key"><td><div class="media-cell"><span class="media-icon">{{ platformNames[task.platform] || task.platform }}</span><div><strong>{{ task.source_label }}</strong><span>{{ task.source_type === 'profile' ? '作者主页' : '单条作品' }} · #{{ task.id }}</span></div></div></td><td><strong>{{ task.author_name || '作者未知' }}</strong><span>{{ task.published_at ? new Date(task.published_at).toLocaleString() : (task.media_type || '元数据待采集') }}</span></td><td><span class="status" :data-tone="task.status">{{ task.status }}</span><small>{{ task.phase || '—' }}</small></td><td><strong>{{ Number(task.progress_percent || 0).toFixed(1) }}%</strong><span>{{ task.file_count }} 个文件</span></td><td><span :class="{ 'inline-error': task.error_message }">{{ task.error_message || '—' }}</span></td><td><div class="row-actions"><button v-if="task.preview_count" class="icon-btn" title="预览" @click="preview(task)"><Eye :size="17" /></button><button v-if="['failed','cancelled'].includes(task.status)" class="icon-btn" title="重试" @click="action(task, task.retry_endpoint)"><RotateCcw :size="17" /></button><button v-if="['pending','downloading'].includes(task.status)" class="icon-btn" title="取消" @click="action(task, task.cancel_endpoint)"><Square :size="17" /></button></div></td></tr>
    </tbody></table><div v-if="!loading && !tasks.length" class="empty-state"><strong>暂无符合条件的任务</strong></div></div>
    <Pager :page="page" :pages="pages" :total="total" @change="value => { page = value; load() }" />
  </section>
</template>

<style scoped>
.unified-filters { grid-template-columns: 160px 160px minmax(240px, 1fr); }
.unified-filters select { min-height: 40px; padding: 0 12px; border: 1px solid var(--line); border-radius: 9px; background: var(--surface-2); color: var(--text); }
.media-icon { min-width: 48px; padding: 7px; border-radius: 8px; font-size: 10px; text-align: center; }
@media (max-width: 760px) { .unified-filters { grid-template-columns: 1fr; } }
</style>
