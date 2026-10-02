<script setup lang="ts">
import IssueDetail from '../components/IssueDetail.vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Download, ExternalLink, MoreHorizontal, Plus, RefreshCw, Search, SlidersHorizontal, Trash2, UserRound, Users } from '@lucide/vue'
import { useRoute, useRouter } from 'vue-router'
import { api, jsonBody } from '../api'
import { useAppStore } from '../stores/app'
import type { Author, PageData } from '../types'
import Pager from '../components/Pager.vue'

const store = useAppStore(), router = useRouter(), route = useRoute()
const authors = ref<Author[]>([]), page = ref(1), pages = ref(1), total = ref(0), loading = ref(false)
const loadError = ref('')
const input = ref(''), search = ref(''), subscribed = ref(''), account = ref('all')
const searchTimer = ref<number>()
const highlightTimer = ref<number>()
const highlightedAuthorId = ref<number>()
const addOpen = ref(false), filtersOpen = ref(false), expandedAuthorId = ref<number>()
const activeFilterCount = computed(() => Number(!!subscribed.value) + Number(account.value !== 'all'))
const authorInput = ref<HTMLInputElement | null>(null)
let loadSequence = 0
let suppressAutoLoad = false

async function load() {
  const sequence = ++loadSequence
  loading.value = true
  try {
    const params = new URLSearchParams({ page: String(page.value), page_size: '20', account_status: account.value })
    if (subscribed.value) params.set('is_subscribed', subscribed.value)
    if (search.value.trim()) params.set('q', search.value.trim())
    const data = await api<PageData<Author>>(`/authors/?${params}`)
    if (sequence !== loadSequence) return
    authors.value = data.items; pages.value = data.pages; total.value = data.total; loadError.value = ''
  } catch (error: any) { if (sequence === loadSequence) { loadError.value = error.message || '加载作者失败'; store.notify(loadError.value, 'error') } }
  finally { if (sequence === loadSequence) loading.value = false }
}
async function toggleAdd() {
  addOpen.value = !addOpen.value
  if (addOpen.value) { await nextTick(); authorInput.value?.focus() }
}
async function add() {
  if (!input.value.trim()) return store.notify('请输入作者分享链接', 'error')
  if (store.risk.active) return store.notify('抖音接口正在冷却', 'error')
  try {
    const result = await api<any>('/authors/', { method: 'POST', ...jsonBody({ share_url: input.value.trim(), is_subscribed: false, check_interval: 21600 }) })
    input.value = ''
    addOpen.value = false
    if (result.already_exists) {
      store.notify('作者已存在，已定位到对应记录', 'info')
      await locateAuthor(result.id, result.position)
    } else {
      store.notify('作者已添加')
      await load()
    }
    await store.refreshStatus()
  } catch (error: any) { store.notify(error.message || '添加作者失败', 'error') }
}
async function locateAuthor(authorId: number, position = 0) {
  suppressAutoLoad = true
  search.value = ''; subscribed.value = ''; account.value = 'all'
  page.value = Math.floor(Math.max(0, Number(position) || 0) / 20) + 1
  await load()
  suppressAutoLoad = false
  highlightedAuthorId.value = Number(authorId)
  await nextTick()
  const row = document.getElementById(`author-row-${authorId}`)
  row?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  row?.focus({ preventScroll: true })
  if (highlightTimer.value != null) window.clearTimeout(highlightTimer.value)
  highlightTimer.value = window.setTimeout(() => { highlightedAuthorId.value = undefined }, 4200)
  if (route.query.focus) {
    await router.replace({ query: { ...route.query, focus: undefined, position: undefined } })
  }
}
async function toggle(author: Author) {
  const endpoint = author.is_subscribed ? 'unsubscribe' : 'subscribe'
  try { await api(`/authors/${author.id}/${endpoint}`, { method: 'POST' }); author.is_subscribed = !author.is_subscribed; store.notify(author.is_subscribed ? '已订阅' : '已取消订阅') }
  catch (error: any) { store.notify(error.message || '操作失败', 'error') }
}
async function download(author: Author) {
  if (store.risk.active) return store.notify('抖音接口正在冷却', 'error')
  try { const result = await api<any>(`/authors/${author.id}/download`, { method: 'POST' }); store.notify(result.message) }
  catch (error: any) { store.notify(error.message || '提交失败', 'error') }
}
async function remove(author: Author) {
  if (!confirm(`确定删除作者“${author.nickname || author.id}”及其任务和已下载文件？`)) return
  try { await api(`/authors/${author.id}`, { method: 'DELETE' }); store.notify('作者已删除'); await load(); await store.refreshStatus() }
  catch (error: any) { store.notify(error.message || '删除失败', 'error') }
}
async function checkAll() {
  if (store.risk.active) return store.notify('抖音接口正在冷却', 'error')
  try { const result = await api<any>('/authors/check-all', { method: 'POST' }); store.notify(result.message) }
  catch (error: any) { store.notify(error.message || '提交检查失败', 'error') }
}
function resetAndLoad() { if (page.value === 1) load(); else page.value = 1 }
function queueSearch() {
  if (searchTimer.value != null) window.clearTimeout(searchTimer.value)
  searchTimer.value = window.setTimeout(resetAndLoad, 350)
}
watch(page, () => { if (!suppressAutoLoad) load() })
watch([subscribed, account], () => { if (!suppressAutoLoad) resetAndLoad() })
onMounted(async () => {
  const focusId = Number(route.query.focus)
  if (Number.isInteger(focusId) && focusId > 0) await locateAuthor(focusId, Number(route.query.position) || 0)
  else await load()
})
onBeforeUnmount(() => {
  loadSequence++
  if (searchTimer.value != null) window.clearTimeout(searchTimer.value)
  if (highlightTimer.value != null) window.clearTimeout(highlightTimer.value)
})
</script>

<template>
  <section class="workspace-card authors-workspace">
    <header class="workspace-header">
      <div><h2>作者与作品</h2><span>抖音作者 · 订阅状态、检查结果和已保存作品</span></div>
      <div class="header-actions"><button class="btn ghost" :disabled="store.risk.active" @click="checkAll"><RefreshCw :size="16" />检查更新</button><button class="btn ghost" @click="load"><RefreshCw :size="16" />刷新</button></div>
    </header>
    <div v-if="loadError" class="load-error-banner" role="alert"><IssueDetail :message="loadError" impact="当前数据读取失败；下方如有列表，为上次读取的结果。" /><button class="text-button" @click="load">重试</button></div>
    <form id="author-add-form" class="command-bar author-add-form" :class="{ 'mobile-open': addOpen }" @submit.prevent="add"><Plus :size="18" /><input ref="authorInput" v-model="input" aria-label="抖音作者主页链接" placeholder="粘贴作者主页链接…" /><button class="btn primary" :disabled="store.risk.active">添加作者</button></form>
    <div class="filter-row author-toolbar">
      <label class="search"><Search :size="16" /><input v-model="search" aria-label="搜索全部作者" placeholder="搜索全部作者" @input="queueSearch" /></label>
      <button class="icon-btn author-mobile-control" type="button" aria-label="添加作者" aria-controls="author-add-form" :aria-expanded="addOpen" @click="toggleAdd"><Plus :size="20" /></button>
      <button class="icon-btn author-mobile-control" :class="{ 'filter-active': activeFilterCount > 0 }" type="button" :aria-label="`筛选作者${activeFilterCount ? '，已启用 ' + activeFilterCount + ' 项' : ''}`" aria-controls="author-filter-options" :aria-expanded="filtersOpen" @click="filtersOpen = !filtersOpen"><SlidersHorizontal :size="19" /><span v-if="activeFilterCount" class="filter-count">{{ activeFilterCount }}</span></button>
      <div id="author-filter-options" class="selects author-filter-options" :class="{ 'mobile-open': filtersOpen }"><select v-model="subscribed" aria-label="筛选订阅状态"><option value="">全部订阅状态</option><option value="true">已订阅</option><option value="false">未订阅</option></select><select v-model="account" aria-label="筛选账号状态"><option value="all">全部账号状态</option><option value="normal">正常账号</option><option value="abnormal">异常账号</option><option value="banned">封禁/禁言</option><option value="deleted">已销号</option><option value="restricted">不可访问</option></select></div>
    </div>
    <div class="table-shell" :class="{ loading }">
      <table class="data-table author-table"><thead><tr><th>作者</th><th>媒体库</th><th>自动更新</th><th>订阅</th><th class="actions-col">操作</th></tr></thead><tbody>
        <tr v-for="author in authors" :id="`author-row-${author.id}`" :key="author.id" tabindex="-1" :class="{ 'author-highlight': highlightedAuthorId === author.id }">
          <td data-label="作者" class="author-identity"><div class="author-cell"><span class="avatar"><img v-if="author.avatar_url" :src="`/api/authors/${author.id}/avatar`" alt="" loading="lazy" /><UserRound v-else /></span><div><RouterLink class="author-name" :to="`/douyin/authors/${author.id}/works`"><strong>{{ author.nickname || '未知作者' }}</strong></RouterLink><a v-if="author.share_url" class="author-desktop-home" :href="author.share_url" target="_blank" rel="noopener noreferrer">查看主页 <ExternalLink :size="12" /></a><span v-else class="author-desktop-home">{{ author.sec_uid }}</span><span class="author-mobile-meta">{{ author.downloaded_works.toLocaleString() }}/{{ author.total_works.toLocaleString() }} 已下载<span class="author-mobile-state" :data-tone="author.auto_update_status">{{ author.auto_update_message || (author.is_subscribed ? '等待检查' : '未订阅') }}</span></span></div></div></td>
          <td data-label="媒体库"><strong>{{ author.total_works.toLocaleString() }} 个作品</strong><span>{{ author.downloaded_works.toLocaleString() }} 个已下载</span></td>
          <td data-label="自动更新"><span class="status subtle" :data-tone="author.auto_update_status">{{ author.auto_update_message || (author.is_subscribed ? '等待检查' : '未订阅') }}</span><IssueDetail v-if="author.last_error" :message="author.last_error" /></td>
          <td data-label="订阅"><button class="switch" :class="{ on: author.is_subscribed }" role="switch" :aria-checked="author.is_subscribed" :aria-label="`${author.is_subscribed ? '取消订阅' : '订阅'} ${author.nickname || '作者 ' + author.id}`" @click="toggle(author)"><i /></button></td>
          <td data-label="操作" class="author-actions"><div class="row-actions author-desktop-actions"><button class="btn ghost compact" @click="router.push(`/douyin/authors/${author.id}/works`)"><Users :size="15" />作品管理</button><button class="icon-btn" title="下载" :aria-label="`下载 ${author.nickname || '作者 ' + author.id} 的作品`" :disabled="store.risk.active" @click="download(author)"><Download :size="17" /></button><button class="icon-btn danger" title="删除" :aria-label="`删除 ${author.nickname || '作者 ' + author.id}`" @click="remove(author)"><Trash2 :size="17" /></button></div><button class="icon-btn author-mobile-control" :aria-label="`更多操作：${author.nickname || '作者 ' + author.id}`" :aria-expanded="expandedAuthorId === author.id" :aria-controls="`author-options-${author.id}`" @click="expandedAuthorId = expandedAuthorId === author.id ? undefined : author.id"><MoreHorizontal :size="20" /></button></td>
          <td v-if="expandedAuthorId === author.id" :id="`author-options-${author.id}`" class="author-mobile-details" colspan="5">
            <div class="author-detail-actions"><a v-if="author.share_url" class="btn ghost compact" :href="author.share_url" target="_blank" rel="noopener noreferrer"><ExternalLink :size="15" />主页</a><button class="btn ghost compact" :disabled="store.risk.active" @click="download(author)"><Download :size="15" />下载作品</button><button class="btn ghost compact danger" @click="remove(author)"><Trash2 :size="15" />删除</button></div>
            <p>自动更新：{{ author.auto_update_message || (author.is_subscribed ? '等待检查' : '未订阅') }}</p><IssueDetail v-if="author.last_error" :message="author.last_error" />
          </td>
        </tr>
      </tbody></table>
      <div v-if="!loading && !authors.length && !loadError" class="empty-state"><Users /><strong>暂无作者</strong><span>添加作者后即可管理订阅与作品</span></div>
    </div>
    <Pager v-if="!loadError || authors.length" :page="page" :pages="pages" :total="total" @change="page = $event" />
  </section>
</template>

<style scoped>
.author-mobile-control, .author-mobile-details, .author-cell .author-mobile-meta { display: none; }
.author-cell .author-name { display: block; max-width: 100%; overflow: hidden; color: var(--text); font-size: inherit; }
.author-name strong { display: block; }
.author-table td .status { max-width: 100%; overflow: hidden; text-overflow: ellipsis; }
.author-name:hover { color: var(--accent); }
@media (max-width: 899px) {
  .authors-workspace { min-height: 0; }
  .authors-workspace .workspace-header { min-height: 0; padding: 8px 12px; flex-direction: row; align-items: center; border: 0; }
  .workspace-header > div:first-child { display: block; }
  .authors-workspace .header-actions { width: auto; margin-left: auto; gap: 6px; }
  .header-actions .btn { min-height: 44px; padding: 8px 10px; }
  .authors-workspace .author-toolbar { display: grid; grid-template-columns: minmax(0, 1fr) 44px 44px; min-height: 0; padding: 4px 12px 12px; gap: 8px; }
  .author-toolbar .search { width: auto; min-width: 0; min-height: 44px; }
  .author-toolbar input, .author-add-form input, .author-filter-options select { font-size: 16px; }
  .author-mobile-control { display: inline-flex; width: 44px; height: 44px; flex: 0 0 44px; position: relative; }
  .filter-active { color: var(--accent); border-color: var(--accent); }
  .filter-count { position: absolute; top: 0; right: 2px; color: var(--accent); font-size: 12px; font-weight: 700; }
  .authors-workspace .author-add-form { display: none; margin: 0 12px 8px; min-height: 52px; }
  .authors-workspace .author-add-form.mobile-open { display: flex; }
  .author-add-form > svg { display: none; }
  .author-add-form .btn { min-height: 44px; padding: 8px; }
  .author-toolbar .author-filter-options { display: none; grid-column: 1 / -1; width: 100%; margin: 0; gap: 8px; }
  .author-toolbar .author-filter-options.mobile-open { display: flex; }
  .author-filter-options select { width: 0; flex: 1; min-height: 44px; padding-left: 8px; }
  .authors-workspace .table-shell { padding: 0; min-height: 0; }
  .authors-workspace .author-table tbody { display: block; }
  .authors-workspace .author-table tbody tr { display: grid; grid-template-columns: minmax(0, 1fr) 44px 44px; align-items: center; padding: 9px 12px; gap: 0 4px; border: 0; border-bottom: 1px solid var(--line); border-radius: 0; background: transparent; }
  .authors-workspace .author-table tbody tr:last-child { border-bottom: 0; }
  .authors-workspace .author-table tbody tr.author-highlight { box-shadow: inset 0 0 0 1px var(--accent); }
  .authors-workspace .author-table tbody tr.author-highlight td { box-shadow: none; }
  .authors-workspace .author-table td { width: auto !important; padding: 0; height: auto; border: 0; min-width: 0; }
  .authors-workspace .author-table td[data-label]::before { content: none; display: none; }
  .authors-workspace .author-table td:nth-child(2), .authors-workspace .author-table td:nth-child(3) { display: none; }
  .authors-workspace .author-table td:nth-child(4) { position: static; margin: 0; border: 0; }
  .author-cell { gap: 9px; }
  .author-cell .avatar { width: 36px; height: 36px; min-width: 36px; }
  .author-cell > div { flex: 1; overflow: hidden; gap: 3px; }
  .author-cell .author-name { display: block; font-size: 15px; line-height: 1.4; }
  .author-name strong { display: block; }
  .author-cell .author-desktop-home, .author-desktop-actions { display: none; }
  .author-cell .author-mobile-meta { display: flex; min-width: 0; gap: 7px; font-size: 12px; line-height: 1.5; white-space: nowrap; }
  .author-mobile-state { display: block; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); }
  .author-mobile-state[data-tone="new_works"] { color: var(--green); }
  .author-mobile-state[data-tone="error"], .author-mobile-state[data-tone="failed"] { color: var(--red); }
  .author-table .switch { position: relative; width: 44px; height: 44px; padding: 0; border: 0; background: transparent; box-shadow: none; }
  .author-table .switch::before { content: ''; position: absolute; width: 40px; height: 23px; left: 2px; top: 10px; border: 1px solid var(--line-strong); border-radius: 99px; background: var(--surface-3); }
  .author-table .switch.on::before { background: var(--accent); border-color: var(--accent); }
  .author-table .switch i { position: absolute; left: 5px; top: 13px; }
  .authors-workspace .author-table .author-mobile-details { display: block; grid-column: 1 / -1; padding: 10px 0 2px; }
  .author-detail-actions { display: flex; flex-wrap: wrap; gap: 8px; }
  .author-detail-actions .btn { min-height: 44px; }
  .author-mobile-details p { margin: 8px 0 0; color: var(--muted); font-size: 13px; line-height: 1.5; overflow-wrap: anywhere; }
}
</style>
