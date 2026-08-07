<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { Download, ExternalLink, Plus, RefreshCw, Search, Trash2, UserRound, Users } from '@lucide/vue'
import { useRouter } from 'vue-router'
import { api, jsonBody } from '../api'
import { useAppStore } from '../stores/app'
import type { Author, PageData } from '../types'
import Pager from '../components/Pager.vue'

const store = useAppStore(), router = useRouter()
const authors = ref<Author[]>([]), page = ref(1), pages = ref(1), total = ref(0), loading = ref(false)
const input = ref(''), search = ref(''), subscribed = ref(''), account = ref('all')

async function load() {
  loading.value = true
  try {
    const params = new URLSearchParams({ page: String(page.value), page_size: '20', account_status: account.value })
    if (subscribed.value) params.set('is_subscribed', subscribed.value)
    const data = await api<PageData<Author>>(`/authors/?${params}`)
    authors.value = data.items; pages.value = data.pages; total.value = data.total
  } catch (error: any) { store.notify(error.message || '加载作者失败', 'error') }
  finally { loading.value = false }
}
async function add() {
  if (!input.value.trim()) return store.notify('请输入作者分享链接', 'error')
  if (store.risk.active) return store.notify('抖音接口正在冷却', 'error')
  try {
    const result = await api<any>('/authors/', { method: 'POST', ...jsonBody({ share_url: input.value.trim(), is_subscribed: false, check_interval: 21600 }) })
    input.value = ''; store.notify(result.already_exists ? '作者已存在，资料已刷新' : '作者已添加'); await load(); await store.refreshStatus()
  } catch (error: any) { store.notify(error.message || '添加作者失败', 'error') }
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
watch([page, subscribed, account], () => load())
onMounted(load)
</script>

<template>
  <section class="workspace-card">
    <header class="workspace-header">
      <div><p class="eyebrow">CREATOR LIBRARY</p><h2>作者管理</h2><span>订阅、检查并管理作者媒体库</span></div>
      <div class="header-actions"><button class="btn ghost" :disabled="store.risk.active" @click="checkAll"><RefreshCw :size="16" />检查更新</button><button class="btn ghost" @click="load"><RefreshCw :size="16" />刷新</button></div>
    </header>
    <form class="command-bar" @submit.prevent="add"><Plus :size="18" /><input v-model="input" placeholder="粘贴作者主页链接…" /><button class="btn primary" :disabled="store.risk.active">添加作者</button></form>
    <div class="filter-row">
      <label class="search"><Search :size="16" /><input v-model="search" placeholder="筛选当前页作者" /></label>
      <div class="selects"><select v-model="subscribed"><option value="">全部订阅状态</option><option value="true">已订阅</option><option value="false">未订阅</option></select><select v-model="account"><option value="all">全部账号状态</option><option value="normal">正常账号</option><option value="abnormal">异常账号</option><option value="banned">封禁/禁言</option><option value="deleted">已销号</option><option value="restricted">不可访问</option></select></div>
    </div>
    <div class="table-shell" :class="{ loading }">
      <table class="data-table author-table"><thead><tr><th>作者</th><th>媒体库</th><th>自动更新</th><th>订阅</th><th class="actions-col">操作</th></tr></thead><tbody>
        <tr v-for="author in authors.filter(a => !search || `${a.nickname} ${a.sec_uid}`.toLowerCase().includes(search.toLowerCase()))" :key="author.id">
          <td><div class="author-cell"><span class="avatar"><img v-if="author.avatar_url" :src="`/api/authors/${author.id}/avatar`" alt="" loading="lazy" /><UserRound v-else /></span><div><strong>{{ author.nickname || '未知作者' }}</strong><a v-if="author.share_url" :href="author.share_url" target="_blank" rel="noopener">查看主页 <ExternalLink :size="12" /></a><span v-else>{{ author.sec_uid }}</span></div></div></td>
          <td><strong>{{ author.total_works.toLocaleString() }} 个作品</strong><span>{{ author.downloaded_works.toLocaleString() }} 个已下载</span></td>
          <td><span class="status subtle" :data-tone="author.auto_update_status">{{ author.auto_update_message || (author.is_subscribed ? '等待检查' : '未订阅') }}</span><small v-if="author.last_error" class="inline-error" :title="author.last_error">{{ author.last_error }}</small></td>
          <td><button class="switch" :class="{ on: author.is_subscribed }" role="switch" :aria-checked="author.is_subscribed" @click="toggle(author)"><i /></button></td>
          <td><div class="row-actions"><button class="btn ghost compact" @click="router.push(`/douyin/authors/${author.id}/works`)"><Users :size="15" />作品管理</button><button class="icon-btn" title="下载" :disabled="store.risk.active" @click="download(author)"><Download :size="17" /></button><button class="icon-btn danger" title="删除" @click="remove(author)"><Trash2 :size="17" /></button></div></td>
        </tr>
      </tbody></table>
      <div v-if="!loading && !authors.length" class="empty-state"><Users /><strong>暂无作者</strong><span>添加作者后即可管理订阅与作品</span></div>
    </div>
    <Pager :page="page" :pages="pages" :total="total" @change="page = $event" />
  </section>
</template>
