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
const loadError = ref('')
let searchTimer: number | undefined
let loadSequence = 0
let switchingPlatform = false
const loadedPlatform = ref('')
const definition = computed(() => store.platforms.find(item => item.id === props.platform))
const platformNames: Record<string, string> = { tiktok: 'TikTok', weibo: '微博', bilibili: 'B站', xhs: '小红书' }
const platformIcons: Record<string, string> = { tiktok: 'T', weibo: '微', bilibili: '哔', xhs: '红' }
const platformName = computed(() => definition.value?.name || platformNames[props.platform] || props.platform)
const inputHint = computed(() => props.platform === 'bilibili'
  ? '输入 B站 UP 空间 UID、主页、BV/av、分P或含视频的动态链接…'
  : props.platform === 'xhs'
    ? '粘贴小红书图文、视频或实况笔记分享文本/链接…'
  : `输入 ${platformName.value} 用户主页、@用户名或单条作品链接…`)
const workspaceDescription = computed(() => props.platform === 'xhs'
  ? '支持单条图文、视频与实况笔记下载；作者主页批量采集暂不开放'
  : '统一处理用户主页与单条视频/动态')
const statusLabels: Record<string, string> = { pending: '等待中', downloading: '下载中', completed: '已完成', failed: '失败', cancelled: '已取消', paused: '已暂停' }
const phaseLabels: Record<string, string> = { queued: '排队中', preparing: '准备中', downloading: '下载中', completed: '已完成', failed: '失败', cancelled: '已取消' }

async function load(silent: boolean | Event = false) {
  const requestedPlatform = props.platform
  const requestSequence = ++loadSequence
  try {
    const params = new URLSearchParams({ page: String(page.value), page_size: '20' })
    if (status.value) params.set('status', status.value)
    if (search.value.trim()) params.set('q', search.value.trim())
    const data = await api<PageData<PlatformTask>>(`/platform-downloads/${requestedPlatform}/tasks?${params}`)
    if (requestSequence !== loadSequence || requestedPlatform !== props.platform) return
    tasks.value = data.items; pages.value = data.pages; total.value = data.total; loadError.value = ''
    loadedPlatform.value = requestedPlatform
  } catch (error: any) {
    if (requestSequence !== loadSequence || requestedPlatform !== props.platform) return
    loadError.value = error.message || `加载 ${platformName.value} 任务失败`
    if (silent !== true) store.notify(loadError.value, 'error')
  }
}
async function create() {
  if (!input.value.trim()) return store.notify(props.platform === 'xhs' ? '请输入小红书单条笔记链接' : `请输入 ${platformName.value} 用户主页、用户名或单条作品链接`, 'error')
  const requestedPlatform = props.platform
  const requestedSource = input.value.trim()
  try {
    await api(`/platform-downloads/${requestedPlatform}/download`, { method: 'POST', ...jsonBody({ source: requestedSource }) })
    if (requestedPlatform !== props.platform) return
    if (input.value.trim() === requestedSource) input.value = ''
    store.notify(`${platformName.value} 下载任务已提交`); await load()
  } catch (error: any) { if (requestedPlatform === props.platform) store.notify(error.message || '提交失败', 'error') }
}
async function action(task: PlatformTask, verb: string, method = 'POST') {
  const requestedPlatform = props.platform
  if (loadedPlatform.value !== requestedPlatform || !tasks.value.includes(task)) {
    store.notify('页面已切换，请刷新后再操作', 'error')
    return
  }
  const path = `/platform-downloads/${requestedPlatform}/tasks/${task.id}${verb ? `/${verb}` : ''}`
  try {
    const result = await api<any>(path, { method })
    store.notify(result.message || '操作完成')
    if (requestedPlatform === props.platform) await load()
  }
  catch (error: any) { if (requestedPlatform === props.platform) store.notify(error.message || '操作失败', 'error') }
}
async function remove(task: PlatformTask) {
  const label = task.source_type === 'work' ? '这条作品' : `@${task.source_key}`
  if (confirm(`确定删除 ${label} 的任务记录？本地媒体文件会保留。`)) await action(task, '', 'DELETE')
}
async function preview(task: PlatformTask) {
  const requestedPlatform = props.platform
  if (loadedPlatform.value !== requestedPlatform || !tasks.value.includes(task)) return store.notify('页面已切换，请刷新后再操作', 'error')
  try {
    const assets = await api<any[]>(`/platform-downloads/${requestedPlatform}/tasks/${task.id}/media`)
    if (requestedPlatform !== props.platform || !tasks.value.includes(task)) return
    const items: MediaItem[] = assets.map(item => ({ url: item.preview_url, type: item.media_type === 'video' ? 'video' : 'image', title: item.filename }))
    if (items.length) openMedia(items); else store.notify('该任务没有可预览资源', 'info')
  } catch (error: any) { if (requestedPlatform === props.platform) store.notify(error.message || '预览失败', 'error') }
}
async function copyLog(task: PlatformTask) {
  const requestedPlatform = props.platform
  if (loadedPlatform.value !== requestedPlatform || !tasks.value.includes(task)) return store.notify('页面已切换，请刷新后再操作', 'error')
  try {
    const data = await api<any>(`/platform-downloads/${requestedPlatform}/tasks/${task.id}/log?start=0`)
    if (requestedPlatform !== props.platform || !tasks.value.includes(task)) return
    await navigator.clipboard.writeText((data.lines || []).join('\n')); store.notify('任务日志已复制')
  } catch (error: any) { if (requestedPlatform === props.platform) store.notify(error.message || '复制日志失败', 'error') }
}
watch([page, status], () => { if (!switchingPlatform) void load() })
watch(search, () => {
  if (switchingPlatform) return
  page.value = 1
  if (searchTimer != null) window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => load(), 250)
})
watch(() => props.platform, () => {
  switchingPlatform = true
  loadSequence += 1
  loadedPlatform.value = ''
  tasks.value = []
  page.value = 1
  pages.value = 1
  total.value = 0
  status.value = ''
  input.value = ''
  search.value = ''
  loadError.value = ''
  if (searchTimer != null) window.clearTimeout(searchTimer)
  switchingPlatform = false
  void load()
}, { immediate: true, flush: 'sync' })
onMounted(() => { timer.value = window.setInterval(() => { if (!document.hidden) void load(true) }, 10000) })
onBeforeUnmount(() => { clearInterval(timer.value); if (searchTimer != null) clearTimeout(searchTimer) })
</script>

<template>
  <section class="workspace-card">
    <header class="workspace-header"><div><h2>{{ platformName }} 下载任务</h2><span>{{ workspaceDescription }}</span></div><button class="btn ghost" @click="load"><RefreshCw :size="16" />刷新</button></header>
    <div v-if="loadError" class="load-error-banner" role="alert">任务状态暂不可用：{{ loadError }}{{ tasks.length ? '；下方为上次读取的结果。' : '' }}<button class="text-button" @click="load">重试</button></div>
    <form class="command-bar" @submit.prevent="create"><span class="media-icon">{{ definition?.icon_text || platformIcons[platform] || 'M' }}</span><input v-model="input" :aria-label="`${platformName}下载来源`" :placeholder="inputHint" /><button class="btn primary">开始下载</button></form>
    <div class="filter-row"><nav class="segmented" aria-label="筛选任务状态"><button v-for="item in [['','全部'],['downloading','下载中'],['completed','已完成'],['failed','失败']]" :key="item[0]" :class="{ active: status === item[0] }" :aria-pressed="status === item[0]" @click="status = item[0]; page = 1">{{ item[1] }}</button></nav><label class="search compact-search"><Search :size="15" /><input v-model="search" aria-label="搜索全部任务" placeholder="搜索全部任务" /></label></div>
    <div class="table-shell"><table class="data-table"><thead><tr><th>来源与任务</th><th>阶段</th><th>文件</th><th>最近状态</th><th class="actions-col">操作</th></tr></thead><tbody><tr v-for="task in tasks" :key="task.id">
      <td data-label="来源与任务"><div class="media-cell"><span class="media-icon">{{ definition?.icon_text || platformIcons[platform] || 'M' }}</span><div><strong>{{ task.source_type === 'work' ? '单条作品' : `@${task.source_key}` }}</strong><span>任务 #{{ task.id }} · {{ platformName }}</span></div></div></td>
      <td data-label="阶段"><span class="status" :data-tone="task.status">{{ statusLabels[task.status] || task.status }}</span><small>{{ phaseLabels[task.phase || ''] || task.phase || '排队中' }}</small></td>
      <td data-label="文件"><strong>{{ task.file_count || 0 }}</strong><span>个媒体文件</span></td>
      <td data-label="最近状态"><details v-if="task.error_message" class="task-error-detail"><summary>{{ task.error_message }}</summary><p>{{ task.error_message }}</p><small v-if="task.error_code">错误代码：{{ task.error_code }}</small><button class="text-button" @click="copyLog(task)">复制任务日志</button></details><span v-else>{{ task.last_log_line || (task.status === 'completed' ? '文件已保存' : '等待更新') }}</span></td>
      <td data-label="操作"><div class="row-actions"><button v-if="task.preview_count" class="icon-btn" :aria-label="`预览任务 ${task.id}`" title="预览" @click="preview(task)"><Eye :size="17" /></button><button class="icon-btn" :aria-label="`复制任务 ${task.id} 日志`" title="复制日志" @click="copyLog(task)"><FileText :size="17" /></button><button v-if="['pending','downloading'].includes(task.status)" class="icon-btn" :aria-label="`取消任务 ${task.id}`" title="取消" @click="action(task, 'cancel')"><Ban :size="17" /></button><button v-if="['failed','cancelled'].includes(task.status)" class="icon-btn" :aria-label="`重试任务 ${task.id}`" title="重试" @click="action(task, 'retry')"><RotateCcw :size="17" /></button><button class="icon-btn danger" :aria-label="`删除任务 ${task.id}`" title="删除记录" @click="remove(task)"><Trash2 :size="17" /></button></div></td>
    </tr></tbody></table><div v-if="!tasks.length && !loadError" class="empty-state"><strong>暂无 {{ platformName }} 下载任务</strong></div></div>
    <Pager v-if="!loadError || tasks.length" :page="page" :pages="pages" :total="total" @change="page = $event" />
  </section>
</template>
