<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ArrowLeft, CheckSquare, Download, Eye, Image, RefreshCw, Search, Trash2 } from '@lucide/vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import { openMedia } from '../media'
import { useAppStore } from '../stores/app'
import type { Author, MediaItem, Work } from '../types'

const route = useRoute(), router = useRouter(), store = useAppStore()
type WorkSort = 'published_desc' | 'published_asc' | 'discovered_desc' | 'discovered_asc'
const author = ref<Author>(), works = ref<Work[]>([]), loading = ref(false), filter = ref('all'), search = ref(''), selected = ref<number[]>([])
const sort = ref<WorkSort>('published_desc')
const id = Number(route.params.id)
const shown = computed(() => works.value.filter(work => {
  const state = filter.value === 'all' || (filter.value === 'done' ? work.is_downloaded : !work.is_downloaded)
  return state && (!search.value || `${work.title} ${work.aweme_id}`.toLowerCase().includes(search.value.toLowerCase()))
}))
async function load() {
  loading.value = true
  try { [author.value, works.value] = await Promise.all([api<Author>(`/authors/${id}`), api<Work[]>(`/authors/${id}/works?page=1&page_size=100&sort_by=${sort.value}`)]) }
  catch (error: any) { store.notify(error.message || '加载作品失败', 'error') }
  finally { loading.value = false }
}
function formatWorkTime(value?: string) {
  if (!value) return '未知'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '未知'
  return date.toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  })
}
function media(work: Work): MediaItem[] {
  if (work.work_type === 'video') {
    const file = work.files.find(item => item.local_available && item.preview_url)
    return file?.preview_url ? [{ url: file.preview_url, type: 'video', title: work.title }] : []
  }
  const local = work.files.filter(file => file.preview_url).map(file => ({ url: file.preview_url!, type: file.media_type === 'video' ? 'video' : 'image', title: work.title } as MediaItem))
  if (local.length) return local
  return work.image_urls.map(url => ({ url, type: 'image', title: work.title }))
}
function preview(work: Work) { const items = media(work); if (items.length) openMedia(items); else store.notify('当前作品暂无可用预览', 'info') }
async function workAction(work: Work, endpoint: string, method = 'POST') {
  try { const result = await api<any>(`/works/${work.id}/${endpoint}`, { method }); store.notify(result.message || '操作成功'); await load() }
  catch (error: any) { store.notify(error.message || '操作失败', 'error') }
}
async function remove(work: Work) {
  if (!confirm('确定删除该作品记录及已下载文件？')) return
  try { await api(`/works/${work.id}`, { method: 'DELETE' }); store.notify('作品已删除'); await load() }
  catch (error: any) { store.notify(error.message || '删除失败', 'error') }
}
async function batchDelete() {
  if (!selected.value.length || !confirm(`确定删除选中的 ${selected.value.length} 个作品？`)) return
  try { const result = await api<any>('/works/batch-delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ work_ids: selected.value }) }); store.notify(result.message || '批量删除完成'); selected.value = []; await load() }
  catch (error: any) { store.notify(error.message || '批量删除失败', 'error') }
}
onMounted(load)
</script>

<template>
  <section class="works-workspace">
    <header class="works-hero"><button class="icon-btn" @click="router.push('/douyin/authors')"><ArrowLeft /></button><span class="avatar large"><img v-if="author?.avatar_url" :src="`/api/authors/${id}/avatar`" alt="" /></span><div><p class="eyebrow">CREATOR WORKSPACE</p><h2>{{ author?.nickname || '作者作品' }}</h2><span>{{ works.length }} 个作品 · {{ works.filter(w => w.is_downloaded).length }} 个已完成</span></div><div class="header-actions"><button v-if="selected.length" class="btn danger" @click="batchDelete"><Trash2 :size="16" />删除选中 ({{ selected.length }})</button><button class="btn ghost" @click="load"><RefreshCw :size="16" />刷新</button></div></header>
    <div class="works-toolbar"><nav class="segmented"><button :class="{ active: filter === 'all' }" @click="filter = 'all'">全部</button><button :class="{ active: filter === 'done' }" @click="filter = 'done'">已下载</button><button :class="{ active: filter === 'pending' }" @click="filter = 'pending'">未完成</button></nav><label class="work-sort"><span>排序</span><select v-model="sort" aria-label="作品排序方式" @change="load"><option value="published_desc">作品时间：最新</option><option value="published_asc">作品时间：最早</option><option value="discovered_desc">收录时间：最新</option><option value="discovered_asc">收录时间：最早</option></select></label><label class="search"><Search :size="16" /><input v-model="search" placeholder="搜索作品" /></label></div>
    <main class="work-grid" :class="{ loading }">
      <article v-for="work in shown" :key="work.id" class="work-card">
        <div class="work-cover" @click="preview(work)"><img v-if="work.primary_preview_url" :src="work.primary_preview_url" alt="" loading="lazy" referrerpolicy="no-referrer" /><Image v-else :size="36" /><span>{{ work.work_type === 'images' ? `${work.image_count} 张` : '视频' }}</span><button class="select-box" :class="{ active: selected.includes(work.id) }" @click.stop="selected = selected.includes(work.id) ? selected.filter(v => v !== work.id) : [...selected, work.id]"><CheckSquare :size="18" /></button></div>
        <div class="work-copy"><strong :title="work.title">{{ work.title || `作品 ${work.aweme_id}` }}</strong><time v-if="work.published_at" :datetime="work.published_at">作品时间：{{ formatWorkTime(work.published_at) }}</time><span v-else>作品时间：未知</span><span>{{ work.completed_task_count }}/{{ work.total_task_count }} 个文件 · {{ work.is_downloaded ? '已完成' : '未完成' }}</span></div>
        <footer><button class="btn ghost compact" @click="preview(work)"><Eye :size="14" />预览</button><button class="btn ghost compact" @click="workAction(work, 'redownload')"><Download :size="14" />重新下载</button><button v-if="work.download_status === 'failed'" class="btn ghost compact" @click="workAction(work, 'retry-failed')"><RefreshCw :size="14" />重试</button><button class="icon-btn danger" @click="remove(work)"><Trash2 :size="16" /></button></footer>
      </article>
      <div v-if="!loading && !shown.length" class="empty-state wide"><Image /><strong>没有符合条件的作品</strong></div>
    </main>
  </section>
</template>
