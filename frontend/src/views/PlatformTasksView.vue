<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Ban, Eye, FileText, RefreshCw, RotateCcw, Search, Trash2 } from '@lucide/vue'
import { api, jsonBody } from '../api'
import Pager from '../components/Pager.vue'
import { openMedia } from '../media'
import { useAppStore } from '../stores/app'
import type { MediaItem, PageData, PlatformTask } from '../types'

const props = defineProps<{ platform: string }>()
const store = useAppStore()
const tasks = ref<PlatformTask[]>([]), page = ref(1), pages = ref(1), total = ref(0)
const status = ref(''), input = ref(''), search = ref(''), timer = ref<number>()
let searchTimer: number | undefined
const definition = computed(() => store.platforms.find(item => item.id === props.platform))
const platformName = computed(() => definition.value?.name || props.platform)

async function load(silent: boolean | Event = false) {
  try {
    const params = new URLSearchParams({ page: String(page.value), page_size: '20' })
    if (status.value) params.set('status', status.value)
    if (search.value.trim()) params.set('q', search.value.trim())
    const data = await api<PageData<PlatformTask>>(`/platform-downloads/${props.platform}/tasks?${params}`)
    tasks.value = data.items; pages.value = data.pages; total.value = data.total
  } catch (error: any) { if (silent !== true) store.notify(error.message || `加载 ${platformName.value} 任务失败`, 'error') }
}
async function create() {
  if (!input.value.trim()) return store.notify(`请输入 ${platformName.value} 用户主页、用户名或单条作品链接`, 'error')
  try {
    await api(`/platform-downloads/${props.platform}/download`, { method: 'POST', ...jsonBody({ source: input.value.trim() }) })
    input.value = ''; store.notify(`${platformName.value} 下载任务已提交`); await load()
  } catch (error: any) { store.notify(error.message || '提交失败', 'error') }
}
async function action(task: PlatformTask, verb: string, method = 'POST') {
  const path = `/platform-downloads/${props.platform}/tasks/${task.id}${verb ? `/${verb}` : ''}`
  try { const result = await api<any>(path, { method }); store.notify(result.message || '操作完成'); await load() }
  catch (error: any) { store.notify(error.message || '操作失败', 'error') }
}
async function remove(task: PlatformTask) {
  const label = task.source_type === 'work' ? '这条作品' : `@${task.source_key}`
  if (confirm(`确定删除 ${label} 的任务记录？本地媒体文件会保留。`)) await action(task, '', 'DELETE')
}
async function preview(task: PlatformTask) {
  try {
    const assets = await api<any[]>(`/platform-downloads/${props.platform}/tasks/${task.id}/media`)
    const items: MediaItem[] = assets.map(item => ({ url: item.preview_url, type: item.media_type === 'video' ? 'video' : 'image', title: item.filename }))
    if (items.length) openMedia(items); else store.notify('该任务没有可预览资源', 'info')
  } catch (error: any) { store.notify(error.message || '预览失败', 'error') }
}
async function copyLog(task: PlatformTask) {
  try {
    const data = await api<any>(`/platform-downloads/${props.platform}/tasks/${task.id}/log?start=0`)
    await navigator.clipboard.writeText((data.lines || []).join('\n')); store.notify('任务日志已复制')
  } catch (error: any) { store.notify(error.message || '复制日志失败', 'error') }
}
watch([page, status], () => load())
watch(search, () => {
  page.value = 1
  if (searchTimer != null) window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => load(), 250)
})
onMounted(() => { load(); timer.value = window.setInterval(() => load(true), 5000) })
onBeforeUnmount(() => { clearInterval(timer.value); if (searchTimer != null) clearTimeout(searchTimer) })
</script>

<template>
  <section class="workspace-card">
    <header class="workspace-header"><div><p class="eyebrow">MULTI-PLATFORM PIPELINE</p><h2>{{ platformName }} 下载任务</h2><span>统一处理用户主页与单条视频/动态</span></div><button class="btn ghost" @click="load"><RefreshCw :size="16" />刷新</button></header>
    <form class="command-bar" @submit.prevent="create"><span class="media-icon">{{ definition?.icon_text || 'M' }}</span><input v-model="input" :placeholder="`输入 ${platformName} 用户主页、@用户名或单条作品链接…`" /><button class="btn primary">开始下载</button></form>
    <div class="filter-row"><nav class="segmented"><button v-for="item in [['','全部'],['downloading','下载中'],['completed','已完成'],['failed','失败']]" :key="item[0]" :class="{ active: status === item[0] }" @click="status = item[0]; page = 1">{{ item[1] }}</button></nav><label class="search compact-search"><Search :size="15" /><input v-model="search" placeholder="搜索全部任务" /></label></div>
    <div class="table-shell"><table class="data-table"><thead><tr><th>来源与任务</th><th>阶段</th><th>文件</th><th>最近状态</th><th class="actions-col">操作</th></tr></thead><tbody><tr v-for="task in tasks" :key="task.id"><td><div class="media-cell"><span class="media-icon">{{ definition?.icon_text || 'M' }}</span><div><strong>{{ task.source_type === 'work' ? '单条作品' : `@${task.source_key}` }}</strong><span>任务 #{{ task.id }} · {{ platformName }}</span></div></div></td><td><span class="status" :data-tone="task.status">{{ task.status }}</span><small>{{ task.phase || 'queued' }}</small></td><td><strong>{{ task.file_count || 0 }}</strong><span>个媒体文件</span></td><td><span :class="{ 'inline-error': task.error_message }">{{ task.error_message || task.last_log_line || '等待更新' }}</span></td><td><div class="row-actions"><button v-if="task.preview_count" class="icon-btn" title="预览" @click="preview(task)"><Eye :size="17" /></button><button class="icon-btn" title="复制日志" @click="copyLog(task)"><FileText :size="17" /></button><button v-if="['pending','downloading'].includes(task.status)" class="icon-btn" title="取消" @click="action(task, 'cancel')"><Ban :size="17" /></button><button v-if="['failed','cancelled'].includes(task.status)" class="icon-btn" title="重试" @click="action(task, 'retry')"><RotateCcw :size="17" /></button><button class="icon-btn danger" title="删除记录" @click="remove(task)"><Trash2 :size="17" /></button></div></td></tr></tbody></table><div v-if="!tasks.length" class="empty-state"><strong>暂无 {{ platformName }} 下载任务</strong></div></div>
    <Pager :page="page" :pages="pages" :total="total" @change="page = $event" />
  </section>
</template>
