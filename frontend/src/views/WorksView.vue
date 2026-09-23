<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { ArrowLeft, CheckSquare, Download, Eye, Image, RefreshCw, Search, TrendingUp, Trash2, X } from '@lucide/vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import { openMedia } from '../media'
import { focusFirst, restoreFocus, trapFocus } from '../focus'
import { useAppStore } from '../stores/app'
import Pager from '../components/Pager.vue'
import type { Author, MediaItem, PageData, Work } from '../types'

const route = useRoute(), router = useRouter(), store = useAppStore()
type WorkSort = 'published_desc' | 'published_asc' | 'discovered_desc' | 'discovered_asc'
type DownloadFilter = 'all' | 'completed' | 'incomplete' | 'active' | 'failed' | 'not_started'
type WorkTypeFilter = 'all' | 'video' | 'images'
const author = ref<Author>(), works = ref<Work[]>([]), loading = ref(false), filter = ref<DownloadFilter>('all'), search = ref(''), selected = ref<number[]>([])
const loadError = ref('')
const failedCovers = ref<Set<number>>(new Set())
const failedVideoPreviews = ref<Set<number>>(new Set())
const workType = ref<WorkTypeFilter>('all'), publishedFrom = ref(''), publishedTo = ref('')
const sort = ref<WorkSort>('published_desc')
const page = ref(1), pages = ref(1), total = ref(0), pageSize = 30
const searchTimer = ref<number>()
const trendWork = ref<Work>(), trendSnapshots = ref<any[]>([]), trendMetric = ref('digg_count'), trendLoading = ref(false)
const trendDialog = ref<HTMLElement | null>(null)
let trendReturnFocus: HTMLElement | null = null
const trendMetrics = [
  ['digg_count', '点赞'], ['comment_count', '评论'], ['collect_count', '收藏'],
  ['share_count', '分享'], ['play_count', '播放'],
]
const trendSeries = computed(() => [...trendSnapshots.value].reverse().filter(item => item[trendMetric.value] != null))
const trendSummary = computed(() => {
  const values = trendSeries.value.map(item => Number(item[trendMetric.value] || 0))
  const deltas = values.slice(1).map((value, index) => value - values[index])
  const positives = deltas.filter(value => value > 0).sort((a, b) => a - b)
  const median = positives.length ? positives[Math.floor(positives.length / 2)] : 0
  const latestDelta = deltas.at(-1) || 0
  return { first: values[0] || 0, latest: values.at(-1) || 0, delta: (values.at(-1) || 0) - (values[0] || 0), latestDelta, unusual: positives.length >= 3 && latestDelta > Math.max(100, median * 3) }
})
const trendPoints = computed(() => {
  const values = trendSeries.value.map(item => Number(item[trendMetric.value] || 0))
  if (!values.length) return ''
  const min = Math.min(...values), max = Math.max(...values), span = Math.max(1, max - min)
  return values.map((value, index) => `${values.length === 1 ? 50 : index / (values.length - 1) * 100},${90 - (value - min) / span * 80}`).join(' ')
})
const id = Number(route.params.id)
async function load() {
  loading.value = true
  const params = new URLSearchParams({ paginated: 'true', page: String(page.value), page_size: String(pageSize), sort_by: sort.value })
  if (filter.value !== 'all') params.set('download_status', filter.value)
  if (workType.value !== 'all') params.set('work_type', workType.value)
  if (publishedFrom.value) params.set('published_from', publishedFrom.value)
  if (publishedTo.value) params.set('published_to', publishedTo.value)
  if (search.value.trim()) params.set('q', search.value.trim())
  try {
    const [authorData, data] = await Promise.all([api<Author>(`/authors/${id}`), api<PageData<Work>>(`/authors/${id}/works?${params}`)])
    author.value = authorData; works.value = data.items; total.value = data.total; pages.value = data.pages; selected.value = []; failedCovers.value = new Set(); failedVideoPreviews.value = new Set(); loadError.value = ''
  }
  catch (error: any) { loadError.value = error.message || '加载作品失败'; store.notify(loadError.value, 'error') }
  finally { loading.value = false }
}
function changeFilters() { page.value = 1; load() }
function changeSort() { page.value = 1; load() }
function changePage(value: number) { page.value = value; load() }
function queueSearch() {
  if (searchTimer.value != null) window.clearTimeout(searchTimer.value)
  searchTimer.value = window.setTimeout(() => { page.value = 1; load() }, 350)
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
function formatCount(value?: number) {
  if (value == null) return '—'
  if (value >= 100000000) return `${(value / 100000000).toFixed(value >= 1000000000 ? 0 : 1)}亿`
  if (value >= 10000) return `${(value / 10000).toFixed(value >= 100000 ? 0 : 1)}万`
  return String(value)
}
function formatDuration(value?: number) {
  if (value == null) return ''
  const seconds = Math.max(0, Math.round(value / 1000))
  const minutes = Math.floor(seconds / 60)
  return `${minutes ? `${minutes}:` : ''}${String(seconds % 60).padStart(minutes ? 2 : 1, '0')} 秒`
}
function workSpecs(work: Work) {
  return [work.width && work.height ? `${work.width}×${work.height}` : '', formatDuration(work.duration_ms)].filter(Boolean).join(' · ')
}
function hasStats(work: Work) {
  return [work.digg_count, work.comment_count, work.collect_count, work.share_count, work.play_count].some(value => value != null)
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
function markCoverFailed(workId: number) { failedCovers.value.add(workId) }
function markVideoPreviewFailed(workId: number) { failedVideoPreviews.value.add(workId) }
function primeVideoPreview(event: Event) {
  const video = event.currentTarget as HTMLVideoElement
  if (Number.isFinite(video.duration) && video.duration > 0) video.currentTime = Math.min(0.1, video.duration / 2)
}
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
async function showTrend(work: Work) {
  trendReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
  trendWork.value = work; trendLoading.value = true
  document.body.classList.add('modal-open')
  void nextTick(() => focusFirst(trendDialog.value))
  try { trendSnapshots.value = await api<any[]>(`/works/${work.id}/stats`) }
  catch (error: any) { trendSnapshots.value = []; store.notify(error.message || '加载统计趋势失败', 'error') }
  finally { trendLoading.value = false }
}
function closeTrend() {
  trendWork.value = undefined
  document.body.classList.remove('modal-open')
  const target = trendReturnFocus
  trendReturnFocus = null
  void nextTick(() => restoreFocus(target))
}
function trendKeydown(event: KeyboardEvent) {
  if (!trendWork.value) return
  if (event.key === 'Escape') { event.preventDefault(); closeTrend() }
  else trapFocus(event, trendDialog.value)
}
onMounted(() => { void load(); document.addEventListener('keydown', trendKeydown) })
onBeforeUnmount(() => { if (searchTimer.value != null) window.clearTimeout(searchTimer.value); document.removeEventListener('keydown', trendKeydown); document.body.classList.remove('modal-open') })
</script>

<template>
  <section class="works-workspace">
    <header class="works-hero"><button class="icon-btn" aria-label="返回作者列表" @click="router.push('/douyin/authors')"><ArrowLeft /></button><span class="avatar large"><img v-if="author?.avatar_url" :src="`/api/authors/${id}/avatar`" alt="" /></span><div><h2>{{ author?.nickname || '作者作品' }}</h2><span>{{ loadError && !author ? '—' : total }} 个作品 · 当前页 {{ works.filter(w => w.is_downloaded).length }} 个已完成</span></div><div class="header-actions"><button v-if="selected.length" class="btn danger" @click="batchDelete"><Trash2 :size="16" />删除选中 ({{ selected.length }})</button><button class="btn ghost" @click="load"><RefreshCw :size="16" />刷新</button></div></header>
    <div v-if="loadError" class="load-error-banner" role="alert">作品状态暂不可用：{{ loadError }}{{ works.length ? '；下方为上次读取的结果。' : '' }}<button class="text-button" @click="load">重试</button></div>
    <div class="works-toolbar"><label class="work-sort"><span>状态</span><select v-model="filter" aria-label="下载状态" @change="changeFilters"><option value="all">全部状态</option><option value="completed">已下载</option><option value="incomplete">未完成</option><option value="active">处理中</option><option value="failed">失败/取消</option><option value="not_started">未创建任务</option></select></label><label class="work-sort"><span>类型</span><select v-model="workType" aria-label="作品类型" @change="changeFilters"><option value="all">全部类型</option><option value="video">视频</option><option value="images">图集</option></select></label><label class="work-sort work-date"><span>从</span><input v-model="publishedFrom" type="date" aria-label="发布日期起始" @change="changeFilters" /></label><label class="work-sort work-date"><span>至</span><input v-model="publishedTo" type="date" aria-label="发布日期结束" @change="changeFilters" /></label><label class="work-sort"><span>排序</span><select v-model="sort" aria-label="作品排序方式" @change="changeSort"><option value="published_desc">作品时间：最新</option><option value="published_asc">作品时间：最早</option><option value="discovered_desc">收录时间：最新</option><option value="discovered_asc">收录时间：最早</option></select></label><label class="search"><Search :size="16" /><input v-model="search" placeholder="搜索全部作品" @input="queueSearch" /></label></div>
    <div class="work-grid" :class="{ loading }">
      <article v-for="work in works" :key="work.id" class="work-card">
        <div class="work-cover"><button class="cover-preview" :aria-label="`预览 ${work.title || `作品 ${work.aweme_id}`}`" @click="preview(work)"><img v-if="work.primary_preview_url && !failedCovers.has(work.id)" :src="work.primary_preview_url" alt="" loading="lazy" @error="markCoverFailed(work.id)" /><video v-else-if="work.work_type === 'video' && work.video_url && !failedVideoPreviews.has(work.id)" :src="work.video_url" muted playsinline preload="metadata" aria-hidden="true" @loadedmetadata="primeVideoPreview" @error="markVideoPreviewFailed(work.id)" /><div v-else class="cover-placeholder"><Image :size="34" /><small>封面暂不可用</small></div></button><span>{{ work.work_type === 'images' ? `${work.image_count} 张` : '视频' }}</span><button class="select-box" :class="{ active: selected.includes(work.id) }" :aria-label="`${selected.includes(work.id) ? '取消选择' : '选择'} ${work.title || `作品 ${work.aweme_id}`}`" @click="selected = selected.includes(work.id) ? selected.filter(v => v !== work.id) : [...selected, work.id]"><CheckSquare :size="18" /></button></div>
        <div class="work-copy">
          <strong :title="work.title">{{ work.title || `作品 ${work.aweme_id}` }}</strong>
          <time v-if="work.published_at" :datetime="work.published_at">作品时间：{{ formatWorkTime(work.published_at) }}</time><span v-else>作品时间：未知</span>
          <span v-if="workSpecs(work)">{{ workSpecs(work) }}</span>
          <span>{{ work.completed_task_count }}/{{ work.total_task_count }} 个文件 · {{ work.is_downloaded ? '已完成' : '未完成' }}</span>
          <div v-if="work.hashtags?.length" class="work-tags"><span v-for="tag in work.hashtags.slice(0, 4)" :key="tag">#{{ tag }}</span></div>
          <span v-if="work.music_title" class="work-music">音乐：{{ work.music_title }}<template v-if="work.music_author"> · {{ work.music_author }}</template></span>
          <div v-if="hasStats(work)" class="work-stats"><span>赞 {{ formatCount(work.digg_count) }}</span><span>评 {{ formatCount(work.comment_count) }}</span><span>藏 {{ formatCount(work.collect_count) }}</span><span>转 {{ formatCount(work.share_count) }}</span><span v-if="work.play_count != null">播 {{ formatCount(work.play_count) }}</span></div>
        </div>
        <footer><button class="btn ghost compact" @click="preview(work)"><Eye :size="14" />预览</button><button v-if="hasStats(work)" class="btn ghost compact" @click="showTrend(work)"><TrendingUp :size="14" />趋势</button><button class="btn ghost compact" @click="workAction(work, 'redownload')"><Download :size="14" />重新下载</button><button v-if="work.download_status === 'failed'" class="btn ghost compact" @click="workAction(work, 'retry-failed')"><RefreshCw :size="14" />重试</button><button class="icon-btn danger" :aria-label="`删除 ${work.title || `作品 ${work.aweme_id}`}`" @click="remove(work)"><Trash2 :size="16" /></button></footer>
      </article>
      <div v-if="!loading && !works.length && !loadError" class="empty-state wide"><Image /><strong>没有符合条件的作品</strong></div>
    </div>
    <Pager v-if="!loadError || works.length" :page="page" :pages="pages" :total="total" @change="changePage" />
    <Teleport to="body"><div v-if="trendWork" class="trend-overlay" @click.self="closeTrend"><section ref="trendDialog" class="trend-dialog" role="dialog" aria-modal="true" aria-labelledby="work-trend-title" tabindex="-1"><header><div><p class="eyebrow">GROWTH ANALYTICS</p><h3 id="work-trend-title">{{ trendWork.title || `作品 ${trendWork.aweme_id}` }}</h3><span>{{ trendSnapshots.length }} 个统计快照</span></div><button class="icon-btn" aria-label="关闭作品趋势" @click="closeTrend"><X /></button></header><nav aria-label="趋势指标"><button v-for="metric in trendMetrics" :key="metric[0]" :class="{ active: trendMetric === metric[0] }" :aria-pressed="trendMetric === metric[0]" @click="trendMetric = metric[0]">{{ metric[1] }}</button></nav><div v-if="trendLoading" class="empty-state">正在读取趋势…</div><template v-else-if="trendSeries.length"><div class="trend-kpis"><article><strong>{{ formatCount(trendSummary.latest) }}</strong><span>当前值</span></article><article><strong>+{{ formatCount(trendSummary.delta) }}</strong><span>区间增长</span></article><article :data-alert="trendSummary.unusual"><strong>+{{ formatCount(trendSummary.latestDelta) }}</strong><span>最近增量{{ trendSummary.unusual ? ' · 异常增长' : '' }}</span></article></div><svg class="trend-chart" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="互动数据变化曲线"><line x1="0" y1="90" x2="100" y2="90" /><line x1="0" y1="50" x2="100" y2="50" /><line x1="0" y1="10" x2="100" y2="10" /><polyline :points="trendPoints" /></svg><div class="trend-range"><span>{{ new Date(trendSeries[0].observed_at).toLocaleString() }}</span><span>{{ new Date(trendSeries[trendSeries.length - 1].observed_at).toLocaleString() }}</span></div><div class="trend-table"><article v-for="snapshot in [...trendSeries].reverse().slice(0, 20)" :key="snapshot.id"><time>{{ new Date(snapshot.observed_at).toLocaleString() }}</time><strong>{{ formatCount(snapshot[trendMetric]) }}</strong><span>{{ snapshot.source }}</span></article></div></template><div v-else class="empty-state"><TrendingUp /><strong>暂无可绘制的统计历史</strong><span>后续采集到变化后会自动追加快照</span></div></section></div></Teleport>
  </section>
</template>

<style scoped>
.trend-overlay { position:fixed; inset:0; z-index:80; display:grid; place-items:center; padding:20px; background:rgba(5,9,18,.72); backdrop-filter:blur(8px); }
.trend-dialog { width:min(760px,100%); max-height:88vh; overflow:auto; padding:20px; border:1px solid var(--line); border-radius:16px; background:var(--surface); box-shadow:0 24px 80px rgba(0,0,0,.28); }
.trend-dialog>header { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; }.trend-dialog h3{margin:3px 0}.trend-dialog header span{color:var(--muted);font-size:12px}
.trend-dialog nav { margin:16px 0; display:flex; gap:6px; flex-wrap:wrap; }.trend-dialog nav button{padding:7px 11px;border:1px solid var(--line);border-radius:8px;background:var(--surface-2);color:var(--muted);cursor:pointer}.trend-dialog nav button.active{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
.trend-kpis { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; }.trend-kpis article{padding:12px;border:1px solid var(--line);border-radius:10px;background:var(--surface-2);display:grid;gap:3px}.trend-kpis article[data-alert="true"]{border-color:var(--amber)}.trend-kpis strong{font-size:20px}.trend-kpis span{color:var(--muted);font-size:12px}
.trend-chart { width:100%; height:230px; margin-top:16px; overflow:visible; }.trend-chart line{stroke:var(--line);stroke-width:.5}.trend-chart polyline{fill:none;stroke:var(--accent);stroke-width:2;vector-effect:non-scaling-stroke;stroke-linecap:round;stroke-linejoin:round}
.trend-range{display:flex;justify-content:space-between;color:var(--muted);font-size:12px}.trend-table{margin-top:14px;border-top:1px solid var(--line)}.trend-table article{display:grid;grid-template-columns:1fr auto 90px;gap:12px;padding:8px 0;border-bottom:1px solid var(--line);font-size:12px}.trend-table span,.trend-table time{color:var(--muted)}
@media(max-width:600px){.trend-kpis{grid-template-columns:1fr}.trend-chart{height:170px}}
</style>
