<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { Download, Eye, Plus, RefreshCw, Search, UserRound, Users } from '@lucide/vue'
import { useRouter } from 'vue-router'
import { api, jsonBody } from '../api'
import Pager from '../components/Pager.vue'
import { useAppStore } from '../stores/app'
import type { PageData, PlatformAuthor, PlatformTask } from '../types'

const store = useAppStore(), router = useRouter()
const authors = ref<PlatformAuthor[]>([]), page = ref(1), pages = ref(1), total = ref(0)
const input = ref(''), search = ref(''), busy = ref(false)
let searchTimer: number | undefined

async function load() {
  try {
    const params = new URLSearchParams({ page: String(page.value), page_size: '20' })
    if (search.value.trim()) params.set('q', search.value.trim())
    const data = await api<PageData<PlatformAuthor>>(`/platform-downloads/xhs/authors?${params}`)
    authors.value = data.items; pages.value = data.pages; total.value = data.total
  } catch (error: any) { store.notify(error.message || '加载小红书作者失败', 'error') }
}
async function add() {
  if (!input.value.trim()) return store.notify('请粘贴小红书作者主页链接', 'error')
  busy.value = true
  try {
    const task = await api<PlatformTask>('/platform-downloads/xhs/download', { method: 'POST', ...jsonBody({ source: input.value.trim() }) })
    input.value = ''; store.notify(`作者采集任务 #${task.id} 已提交`)
    router.push('/xhs/tasks')
  } catch (error: any) { store.notify(error.message || '提交作者采集失败', 'error') }
  finally { busy.value = false }
}
async function subscription(author: PlatformAuthor) {
  try {
    const updated = await api<PlatformAuthor>(`/platform-downloads/xhs/authors/${author.id}/subscription`, {
      method: 'POST', ...jsonBody({ is_subscribed: !author.is_subscribed }),
    })
    Object.assign(author, updated); store.notify(updated.is_subscribed ? '已开启自动检查' : '已取消自动检查')
  } catch (error: any) { store.notify(error.message || '更新订阅失败', 'error') }
}
async function scan(author: PlatformAuthor) {
  try {
    const task = await api<PlatformTask>(`/platform-downloads/xhs/authors/${author.id}/scan`, { method: 'POST' })
    store.notify(`检查任务 #${task.id} 已提交`); router.push('/xhs/tasks')
  } catch (error: any) { store.notify(error.message || '提交检查失败', 'error') }
}
watch(page, load)
watch(search, () => { page.value = 1; if (searchTimer) clearTimeout(searchTimer); searchTimer = window.setTimeout(load, 250) })
onMounted(load)
</script>

<template>
  <section class="workspace-card">
    <header class="workspace-header"><div><p class="eyebrow">XHS CREATOR LIBRARY</p><h2>小红书作者管理</h2><span>作者主页由受管浏览器低频扫描，新增笔记复用现有下载链路</span></div><button class="btn ghost" @click="load"><RefreshCw :size="16" />刷新</button></header>
    <form class="command-bar" @submit.prevent="add"><span class="media-icon">红</span><input v-model="input" placeholder="粘贴 https://www.xiaohongshu.com/user/profile/... 作者主页" /><button class="btn primary" :disabled="busy"><Plus :size="16" />采集作者</button></form>
    <div class="filter-row"><label class="search"><Search :size="15" /><input v-model="search" placeholder="搜索昵称、小红书号或作者 ID" /></label></div>
    <div class="table-shell"><table class="data-table"><thead><tr><th>作者</th><th>状态</th><th>作品</th><th>自动检查</th><th class="actions-col">操作</th></tr></thead><tbody><tr v-for="author in authors" :key="author.id"><td><div class="author-cell"><span class="avatar"><img v-if="author.avatar_url" :src="author.avatar_url" alt="" /><UserRound v-else /></span><div><strong>{{ author.nickname || author.red_id || author.external_user_id }}</strong><span>{{ author.red_id ? `小红书号 ${author.red_id}` : author.external_user_id }}</span></div></div></td><td><span class="status subtle">{{ author.account_status === 'healthy' ? '正常' : author.account_status === 'auth_required' ? '需登录' : '待确认' }}</span><small v-if="author.last_error" class="inline-error">{{ author.last_error }}</small><small v-else-if="author.last_success_at">最近成功 {{ new Date(author.last_success_at).toLocaleString() }}</small></td><td><strong>{{ author.total_works }}</strong><span>条已发现笔记</span></td><td><button class="switch" :class="{ on: author.is_subscribed }" :aria-label="author.is_subscribed ? '取消自动检查' : '开启自动检查'" @click="subscription(author)"><i /></button></td><td><div class="row-actions"><button class="btn ghost compact" @click="router.push(`/xhs/authors/${author.id}/works`)"><Eye :size="15" />作品</button><button class="btn ghost compact" @click="scan(author)"><Download :size="15" />立即检查</button></div></td></tr></tbody></table><div v-if="!authors.length" class="empty-state"><Users /><strong>暂无小红书作者</strong><span>首次采集成功后会在这里建立作者档案</span></div></div>
    <Pager :page="page" :pages="pages" :total="total" @change="page = $event" />
  </section>
</template>
