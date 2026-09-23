<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Download, ExternalLink, Plus, RefreshCw, Search, Trash2, UserRound, Users } from '@lucide/vue'
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
let suppressAutoLoad = false

async function load() {
  loading.value = true
  try {
    const params = new URLSearchParams({ page: String(page.value), page_size: '20', account_status: account.value })
    if (subscribed.value) params.set('is_subscribed', subscribed.value)
    if (search.value.trim()) params.set('q', search.value.trim())
    const data = await api<PageData<Author>>(`/authors/?${params}`)
    authors.value = data.items; pages.value = data.pages; total.value = data.total; loadError.value = ''
  } catch (error: any) { loadError.value = error.message || '加载作者失败'; store.notify(loadError.value, 'error') }
  finally { loading.value = false }
}
async function add() {
  if (!input.value.trim()) return store.notify('请输入作者分享链接', 'error')
  if (store.risk.active) return store.notify('抖音接口正在冷却', 'error')
  try {
    const result = await api<any>('/authors/', { method: 'POST', ...jsonBody({ share_url: input.value.trim(), is_subscribed: false, check_interval: 21600 }) })
    input.value = ''
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
  if (searchTimer.value != null) window.clearTimeout(searchTimer.value)
  if (highlightTimer.value != null) window.clearTimeout(highlightTimer.value)
})
</script>

<template>
  <section class="workspace-card">
    <header class="workspace-header">
      <div><h2>作者与作品</h2><span>抖音作者 · 订阅状态、检查结果和已保存作品</span></div>
      <div class="header-actions"><button class="btn ghost" :disabled="store.risk.active" @click="checkAll"><RefreshCw :size="16" />检查更新</button><button class="btn ghost" @click="load"><RefreshCw :size="16" />刷新</button></div>
    </header>
    <div v-if="loadError" class="load-error-banner" role="alert">作者状态暂不可用：{{ loadError }}{{ authors.length ? '；下方为上次读取的结果。' : '' }}<button class="text-button" @click="load">重试</button></div>
    <form class="command-bar" @submit.prevent="add"><Plus :size="18" /><input v-model="input" aria-label="抖音作者主页链接" placeholder="粘贴作者主页链接…" /><button class="btn primary" :disabled="store.risk.active">添加作者</button></form>
    <div class="filter-row">
      <label class="search"><Search :size="16" /><input v-model="search" aria-label="搜索全部作者" placeholder="搜索全部作者" @input="queueSearch" /></label>
      <div class="selects"><select v-model="subscribed" aria-label="筛选订阅状态"><option value="">全部订阅状态</option><option value="true">已订阅</option><option value="false">未订阅</option></select><select v-model="account" aria-label="筛选账号状态"><option value="all">全部账号状态</option><option value="normal">正常账号</option><option value="abnormal">异常账号</option><option value="banned">封禁/禁言</option><option value="deleted">已销号</option><option value="restricted">不可访问</option></select></div>
    </div>
    <div class="table-shell" :class="{ loading }">
      <table class="data-table author-table"><thead><tr><th>作者</th><th>媒体库</th><th>自动更新</th><th>订阅</th><th class="actions-col">操作</th></tr></thead><tbody>
        <tr v-for="author in authors" :id="`author-row-${author.id}`" :key="author.id" tabindex="-1" :class="{ 'author-highlight': highlightedAuthorId === author.id }">
          <td data-label="作者"><div class="author-cell"><span class="avatar"><img v-if="author.avatar_url" :src="`/api/authors/${author.id}/avatar`" alt="" loading="lazy" /><UserRound v-else /></span><div><strong>{{ author.nickname || '未知作者' }}</strong><a v-if="author.share_url" :href="author.share_url" target="_blank" rel="noopener noreferrer">查看主页 <ExternalLink :size="12" /></a><span v-else>{{ author.sec_uid }}</span></div></div></td>
          <td data-label="媒体库"><strong>{{ author.total_works.toLocaleString() }} 个作品</strong><span>{{ author.downloaded_works.toLocaleString() }} 个已下载</span></td>
          <td data-label="自动更新"><span class="status subtle" :data-tone="author.auto_update_status">{{ author.auto_update_message || (author.is_subscribed ? '等待检查' : '未订阅') }}</span><details v-if="author.last_error" class="task-error-detail"><summary>查看最近错误</summary><p>{{ author.last_error }}</p></details></td>
          <td data-label="订阅"><button class="switch" :class="{ on: author.is_subscribed }" role="switch" :aria-checked="author.is_subscribed" :aria-label="`${author.is_subscribed ? '取消订阅' : '订阅'} ${author.nickname || '作者 ' + author.id}`" @click="toggle(author)"><i /></button></td>
          <td data-label="操作"><div class="row-actions"><button class="btn ghost compact" @click="router.push(`/douyin/authors/${author.id}/works`)"><Users :size="15" />作品管理</button><button class="icon-btn" title="下载" :aria-label="`下载 ${author.nickname || '作者 ' + author.id} 的作品`" :disabled="store.risk.active" @click="download(author)"><Download :size="17" /></button><button class="icon-btn danger" title="删除" :aria-label="`删除 ${author.nickname || '作者 ' + author.id}`" @click="remove(author)"><Trash2 :size="17" /></button></div></td>
        </tr>
      </tbody></table>
      <div v-if="!loading && !authors.length && !loadError" class="empty-state"><Users /><strong>暂无作者</strong><span>添加作者后即可管理订阅与作品</span></div>
    </div>
    <Pager v-if="!loadError || authors.length" :page="page" :pages="pages" :total="total" @change="page = $event" />
  </section>
</template>
