<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowUpRight, Download, Image, Play, Plus, Search } from '@lucide/vue'
import { api } from '../api'
import { isVideoMedia, mediaPreviewType, openMedia } from '../media'
import { bytes, dateTime, platformNames, useContextActions } from '../workspace'
import type { LibraryMedia, MediaItem, PageData } from '../types'
import PageHeader from '../components/PageHeader.vue'
import FilterToolbar from '../components/FilterToolbar.vue'
import MediaThumbnail from '../components/MediaThumbnail.vue'
import StateView from '../components/StateView.vue'
import Pager from '../components/Pager.vue'
import InspectorPanel from '../components/InspectorPanel.vue'
import IssueDetail from '../components/IssueDetail.vue'

const route = useRoute(), router = useRouter()
const text = (value: unknown) => typeof value === 'string' ? value : ''
const queryPage = () => Math.max(1, Math.floor(Number(route.query.page) || 1))
const items = ref<LibraryMedia[]>([]), total = ref(0), pages = ref(1), page = ref(queryPage())
const platform = ref(text(route.query.platform)), type = ref(text(route.query.type)), search = ref(text(route.query.q)), author = ref(text(route.query.author))
const sort = ref(text(route.query.sort) === 'created_asc' ? 'created_asc' : 'created_desc')
const loading = ref(true), loaded = ref(false), error = ref(''), updatedAt = ref('')
const inspected = computed(() => items.value.find(item => item.key === text(route.query.media)))
const detailOpen = computed(() => Boolean(route.query.media))
let sequence = 0, controller: AbortController | undefined, searchTimer: number | undefined
const endpoint = (value?: string) => value ? value.startsWith('/api/') ? value : `/api${value}` : ''
const previewItems = computed<MediaItem[]>(() => items.value.filter(item => item.available !== false).map(item => ({ url: endpoint(item.preview_endpoint), type: mediaPreviewType(item.media_type), title: item.title || item.filename })))
function syncQuery() {
  void router.replace({ query: { platform: platform.value || undefined, type: type.value || undefined, q: search.value.trim() || undefined, author: author.value.trim() || undefined, sort: sort.value === 'created_asc' ? sort.value : undefined, page: page.value > 1 ? String(page.value) : undefined } })
}
async function load() {
  const current = ++sequence
  controller?.abort(); controller = new AbortController()
  loading.value = true
  const params = new URLSearchParams({page:String(page.value), page_size:'30', sort_by:sort.value})
  if (platform.value) params.set('platform', platform.value)
  if (type.value) params.set('media_type', type.value)
  if (search.value.trim()) params.set('q', search.value.trim())
  if (author.value.trim()) params.set('author', author.value.trim())
  try {
    const data = await api<PageData<LibraryMedia>>(`/operations/media?${params}`, { signal: controller.signal })
    if (current !== sequence) return
    if (page.value > Math.max(1, data.pages)) { page.value = Math.max(1, data.pages); syncQuery(); void load(); return }
    items.value = data.items; total.value = data.total; pages.value = data.pages
    loaded.value = true; error.value = ''; updatedAt.value = new Date().toISOString()
  } catch (reason: any) { if (current === sequence && reason.name !== 'AbortError') error.value = reason.message || '媒体库读取失败' }
  finally { if (current === sequence) loading.value = false }
}
function filters() { page.value = 1; syncQuery(); void load() }
function queueSearch() { clearTimeout(searchTimer); searchTimer = window.setTimeout(filters, 350) }
function changePage(value: number) { page.value = value; syncQuery(); void load() }
function inspect(item: LibraryMedia) { void router.replace({ query: {...route.query, media:item.key} }) }
function closeDetail() { void router.replace({ query:{...route.query, media:undefined} }) }
function preview(item: LibraryMedia) {
  const index = previewItems.value.findIndex(entry => entry.url === endpoint(item.preview_endpoint))
  if (index >= 0) openMedia(previewItems.value, index)
}
function newDownload() { window.dispatchEvent(new CustomEvent('app:new-download')) }
function duration(item: LibraryMedia) {
  if (!item.duration_ms) return ''
  const seconds = Math.round(item.duration_ms / 1000)
  return `${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`
}
useContextActions(() => [{id:'refresh-library',label:'刷新媒体库',run:load},{id:'new-media',label:'新建下载',run:newDownload}])
watch(() => route.query, () => {
  const next = [text(route.query.platform),text(route.query.type),text(route.query.q),text(route.query.author),text(route.query.sort) === 'created_asc' ? 'created_asc' : 'created_desc',queryPage()]
  if (next.join('|') === [platform.value,type.value,search.value.trim(),author.value.trim(),sort.value,page.value].join('|')) return
  platform.value = next[0] as string; type.value = next[1] as string; search.value = next[2] as string; author.value = next[3] as string; sort.value = next[4] as string; page.value = next[5] as number
  void load()
})
onMounted(load)
onBeforeUnmount(() => { sequence++; controller?.abort(); clearTimeout(searchTimer) })
</script>

<template>
  <section class="workspace-page library-workspace">
    <PageHeader title="媒体库" description="保存下来的图片、视频与实况。" :busy="loading" :updated-at="updatedAt" refreshable @refresh="load"><button class="btn primary" @click="newDownload"><Plus :size="17" />新建下载</button></PageHeader>
    <nav class="view-tabs" aria-label="媒体类型">
      <button v-for="option in [['','全部媒体'],['video','视频'],['image','图片'],['live','实况']]" :key="option[0]" :class="{active:type === option[0]}" :aria-pressed="type === option[0]" @click="type = option[0]!; filters()">{{ option[1] }}</button>
    </nav>
    <FilterToolbar :active="[platform && platformNames[platform],author && '作者：'+author].filter(Boolean).join(' · ')" @clear="platform=''; author=''; type=''; search=''; filters()">
      <template #search><label class="search"><Search :size="17" /><input v-model="search" aria-label="搜索已保存媒体" placeholder="搜索标题、作者或文件名" @input="queueSearch" /></label></template>
      <select v-model="platform" aria-label="筛选媒体平台" @change="filters"><option value="">全部平台</option><option v-for="(label,key) in platformNames" :key="key" :value="key">{{label}}</option></select>
      <input v-model="author" class="author-filter" aria-label="筛选作者名称" placeholder="作者名称" @input="queueSearch" />
      <select v-model="sort" aria-label="媒体排序" @change="filters"><option value="created_desc">最近入库</option><option value="created_asc">最早入库</option></select>
    </FilterToolbar>
    <div class="library-context"><h2>{{ type === 'video' ? '视频' : type === 'image' ? '图片' : type === 'live' ? '实况' : '全部媒体' }}<span>{{ loaded ? total.toLocaleString() : '-' }}</span></h2><span>{{ sort === 'created_asc' ? '按最早入库排序' : '按最近入库排序' }}<span class="library-preview-hint">，可连续预览当前页</span></span></div>
    <div v-if="error && items.length" class="load-error-banner" role="alert"><IssueDetail :message="error" impact="显示上次读取的媒体。" /><button class="text-button" @click="load">重试</button></div>
    <StateView v-if="!items.length" :loading="loading" :error="error" :title="search || platform || type || author ? '没有找到匹配的媒体' : '媒体库等待第一份内容'" description="下载完成的媒体会出现在这里。也可以调整筛选后重新查找。" @retry="load"><button class="btn primary" @click="newDownload">添加下载</button></StateView>
    <div v-else class="library-grid" :aria-busy="loading">
      <article v-for="item in items" :key="item.key" class="library-item" :class="{selected:inspected?.key === item.key}">
        <button class="library-cover" :aria-label="'预览 '+(item.title || item.filename)" :disabled="item.available === false" @click="preview(item)">
          <MediaThumbnail :src="endpoint(item.thumbnail_endpoint) || (item.media_type === 'image' ? endpoint(item.preview_endpoint) : '')" :type="item.media_type" natural />
          <span v-if="item.available === false" class="media-unavailable">文件不可用</span>
        </button>
        <div class="library-caption"><div class="library-file-meta"><span class="library-media-kind"><Play v-if="isVideoMedia(item.media_type)" :size="13" aria-hidden="true" /><Image v-else :size="13" aria-hidden="true" />{{ ['live','live_photo'].includes(item.media_type) ? '实况' : item.media_type === 'video' ? duration(item) || '视频' : '图片' }}</span><small>{{platformNames[item.platform]}}</small></div><button class="record-link" :title="item.title || item.filename" @click="inspect(item)">{{item.title || item.filename}}</button><div class="library-author"><span>{{item.author_name || '未提供作者'}}</span><button class="icon-btn" :aria-label="'查看 '+(item.title || item.filename)+' 的详情'" @click="inspect(item)"><ArrowUpRight :size="17" /></button></div><small>{{bytes(item.file_size)}}</small></div>
      </article>
    </div>
    <Pager v-if="loaded" :page="page" :pages="pages" :total="total" @change="changePage" />
    <InspectorPanel :open="detailOpen" :title="inspected?.title || inspected?.filename || '媒体详情'" :subtitle="inspected ? platformNames[inspected.platform] : ''" @close="closeDetail">
      <template v-if="inspected"><button class="inspector-media-preview" :disabled="inspected.available === false" aria-label="预览当前媒体" @click="preview(inspected)"><MediaThumbnail :src="endpoint(inspected.thumbnail_endpoint) || (inspected.media_type === 'image' ? endpoint(inspected.preview_endpoint) : '')" :type="inspected.media_type" /></button>
        <dl class="detail-fields"><div><dt>作者</dt><dd>{{inspected.author_name || '未提供'}}</dd></div><div><dt>文件</dt><dd>{{inspected.filename}}</dd></div><div><dt>大小</dt><dd>{{bytes(inspected.file_size)}}</dd></div><div><dt>入库时间</dt><dd>{{dateTime(inspected.created_at)}}</dd></div><div v-if="inspected.published_at"><dt>发布时间</dt><dd>{{dateTime(inspected.published_at)}}</dd></div><div><dt>可用状态</dt><dd>{{inspected.available === false ? '文件不可用，请核对关联任务' : '已入库'}}</dd></div></dl>
        <div class="detail-links"><RouterLink class="btn ghost" :to="{path:'/operations/tasks',query:{task_key:inspected.task_key}}">查看关联任务</RouterLink><RouterLink v-if="inspected.author_id && ['douyin','x'].includes(inspected.platform)" class="btn ghost" :to="{path:'/authors/'+inspected.platform,query:{author:String(inspected.author_id)}}">查看作者订阅</RouterLink><RouterLink v-if="inspected.work_id && inspected.author_id && inspected.platform === 'douyin'" class="btn ghost" :to="{path:'/authors/douyin/'+inspected.author_id+'/works',query:{work:String(inspected.work_id)}}">管理作品与文件</RouterLink><button v-if="inspected.author_name" class="text-button" @click="author=inspected.author_name; filters()">查看该作者的媒体</button></div>
      </template><StateView v-else :loading="loading" :error="error" title="当前页面没有这份媒体" description="关闭详情后调整筛选或返回原来的分页。" @retry="load" />
      <template v-if="inspected" #footer><button class="btn primary" :disabled="inspected.available === false" @click="preview(inspected)"><Play :size="16" />预览</button><a v-if="inspected.download_endpoint && inspected.available !== false" class="btn" :href="endpoint(inspected.download_endpoint)" download><Download :size="16" />下载文件</a></template>
    </InspectorPanel>
  </section>
</template>
