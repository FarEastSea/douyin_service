<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Download, Eye, FileText, RefreshCw, RotateCcw, Search, Trash2, X } from '@lucide/vue'
import { api, jsonBody } from '../api'
import { openMedia } from '../media'
import { useAppStore } from '../stores/app'
import type { MediaItem, PageData, XTask } from '../types'
import Pager from '../components/Pager.vue'

const store = useAppStore(), tasks = ref<XTask[]>([]), page = ref(1), pages = ref(1), total = ref(0), status = ref(''), input = ref(''), search = ref(''), timer = ref<number>()
const loadError = ref('')
const statusLabels: Record<string, string> = { pending: '等待中', downloading: '下载中', completed: '已完成', failed: '失败', cancelled: '已取消', paused: '已暂停' }
const phaseLabels: Record<string, string> = { queued: '排队中', preparing: '准备中', downloading: '下载中', completed: '已完成', failed: '失败', cancelled: '已取消' }
async function load(silent: boolean | Event = false) {
  try { const params = new URLSearchParams({ page: String(page.value), page_size: '20' }); if (status.value) params.set('status', status.value); const data = await api<PageData<XTask>>(`/x/tasks?${params}`); tasks.value = data.items; pages.value = data.pages; total.value = data.total; loadError.value = '' }
  catch (error: any) { loadError.value = error.message || '加载 X 任务失败'; if (silent !== true) store.notify(loadError.value, 'error') }
}
async function create() { if (!input.value.trim()) return store.notify('请输入 X 用户主页、用户名或单条动态链接', 'error'); try { await api('/x/download', { method: 'POST', ...jsonBody({ profile_url: input.value.trim() }) }); input.value = ''; store.notify('X 下载任务已提交'); await load() } catch (e: any) { store.notify(e.message, 'error') } }
async function action(task: XTask, verb: string, method = 'POST') { const path = verb ? `/x/tasks/${task.id}/${verb}` : `/x/tasks/${task.id}`; try { const result = await api<any>(path, { method }); store.notify(result.message || '操作完成'); await load() } catch (e: any) { store.notify(e.message, 'error') } }
async function remove(task: XTask) { if (!confirm(`确定删除 @${task.username} 的任务？`)) return; await action(task, '', 'DELETE') }
async function preview(task: XTask) { try { const assets = await api<any[]>(`/x/tasks/${task.id}/media`); const items: MediaItem[] = assets.map(item => ({ url: item.preview_url, type: item.media_type === 'video' ? 'video' : 'image', title: item.filename })); if (items.length) openMedia(items); else store.notify('该任务没有可预览资源', 'info') } catch (e: any) { store.notify(e.message, 'error') } }
async function copyLog(task: XTask) { try { const data = await api<any>(`/x/tasks/${task.id}/log?start=0`); await navigator.clipboard.writeText((data.lines || []).join('\n')); store.notify('任务日志已复制') } catch (e: any) { store.notify(e.message, 'error') } }
watch([page, status], () => load()); onMounted(() => { load(); timer.value = window.setInterval(() => { if (!document.hidden) void load(true) }, 10000) }); onBeforeUnmount(() => clearInterval(timer.value))
</script>

<template>
  <section class="workspace-card">
    <header class="workspace-header"><div><h2>X 下载任务</h2><span>统一处理用户媒体与单条动态</span></div><button class="btn ghost" @click="load"><RefreshCw :size="16" />刷新</button></header>
    <div v-if="loadError" class="load-error-banner" role="alert">任务状态暂不可用：{{ loadError }}{{ tasks.length ? '；下方为上次读取的结果。' : '' }}<button class="text-button" @click="load">重试</button></div>
    <form class="command-bar" @submit.prevent="create"><X :size="18" /><input v-model="input" aria-label="X 下载来源" placeholder="输入 X 用户主页、@用户名或单条动态链接…" /><button class="btn primary">开始下载</button></form>
    <div class="filter-row"><nav class="segmented" aria-label="筛选任务状态"><button v-for="item in [['','全部'],['downloading','下载中'],['completed','已完成'],['failed','失败']]" :key="item[0]" :class="{ active: status === item[0] }" @click="status = item[0]; page = 1">{{ item[1] }}</button></nav><label class="search compact-search"><Search :size="15" /><input v-model="search" aria-label="筛选当前页 X 用户" placeholder="筛选当前页用户名" /></label></div>
    <div class="table-shell"><table class="data-table"><thead><tr><th>来源与任务</th><th>阶段</th><th>进度</th><th>最近状态</th><th class="actions-col">操作</th></tr></thead><tbody>
      <tr v-for="task in tasks.filter(t => !search || t.username.toLowerCase().includes(search.toLowerCase()))" :key="task.id">
        <td data-label="来源与任务"><div class="media-cell"><span class="media-icon">X</span><div><strong>{{ task.profile_url.includes('/status/') ? '单条动态' : `@${task.username}` }}</strong><span>任务 #{{ task.id }} · {{ task.file_count }} 个文件</span></div></div></td>
        <td data-label="阶段"><span class="status" :data-tone="task.status">{{ statusLabels[task.status] || task.status }}</span><small>{{ phaseLabels[task.phase || ''] || task.phase || '排队中' }}</small></td>
        <td data-label="进度"><div class="status-line"><b>{{ Number(task.progress_percent || 0).toFixed(1) }}%</b></div><div class="progress"><i :style="{ width: `${task.progress_percent || 0}%` }" /></div></td>
        <td data-label="最近状态"><details v-if="task.error_message" class="task-error-detail"><summary>{{ task.error_message }}</summary><p>{{ task.error_message }}</p><button class="text-button" @click="copyLog(task)">复制任务日志</button></details><span v-else>{{ task.last_log_line || (task.status === 'completed' ? '文件已保存' : '等待更新') }}</span></td>
        <td data-label="操作"><div class="row-actions"><button v-if="task.preview_count" class="icon-btn" :aria-label="`预览任务 ${task.id}`" title="预览" @click="preview(task)"><Eye :size="17" /></button><button class="icon-btn" :aria-label="`复制任务 ${task.id} 日志`" title="复制日志" @click="copyLog(task)"><FileText :size="17" /></button><button v-if="['failed','cancelled'].includes(task.status)" class="icon-btn" :aria-label="`重试任务 ${task.id}`" title="重试" @click="action(task, 'retry')"><RotateCcw :size="17" /></button><button class="icon-btn danger" :aria-label="`删除任务 ${task.id}`" title="删除" @click="remove(task)"><Trash2 :size="17" /></button></div></td>
      </tr>
    </tbody></table><div v-if="!tasks.length && !loadError" class="empty-state"><Download /><strong>暂无 X 下载任务</strong></div></div>
    <Pager v-if="!loadError || tasks.length" :page="page" :pages="pages" :total="total" @change="page = $event" />
  </section>
</template>
