<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { Download, Plus, RefreshCw, Trash2, UserRound, Users, X } from '@lucide/vue'
import { api, jsonBody } from '../api'
import { useAppStore } from '../stores/app'
import type { PageData, XAuthor } from '../types'
import Pager from '../components/Pager.vue'

const store = useAppStore(), authors = ref<XAuthor[]>([]), page = ref(1), pages = ref(1), total = ref(0), input = ref('')
const loadError = ref('')
async function load() { try { const data = await api<PageData<XAuthor>>(`/x/authors/?page=${page.value}&page_size=20`); authors.value = data.items; pages.value = data.pages; total.value = data.total; loadError.value = '' } catch (e: any) { loadError.value = e.message || '加载 X 用户失败'; store.notify(loadError.value, 'error') } }
async function add() { if (!input.value.trim()) return; try { await api('/x/authors/', { method: 'POST', ...jsonBody({ profile_url: input.value.trim(), is_subscribed: false, check_interval: 3600 }) }); input.value = ''; store.notify('X 用户已添加'); await load() } catch (e: any) { store.notify(e.message, 'error') } }
async function action(author: XAuthor, endpoint: string, method = 'POST') { const path = endpoint ? `/x/authors/${author.id}/${endpoint}` : `/x/authors/${author.id}`; try { const result = await api<any>(path, { method }); store.notify(result.message || '操作完成'); await load() } catch (e: any) { store.notify(e.message, 'error') } }
async function remove(author: XAuthor) { if (confirm(`确定删除 @${author.username}？`)) await action(author, '', 'DELETE') }
async function checkAll() { try { const result = await api<any>('/x/authors/check-all', { method: 'POST' }); store.notify(result.message) } catch (e: any) { store.notify(e.message, 'error') } }
watch(page, load); onMounted(load)
</script>

<template>
  <section class="workspace-card">
    <header class="workspace-header"><div><h2>X 用户管理</h2><span>管理订阅用户与增量下载</span></div><div class="header-actions"><button class="btn ghost" @click="checkAll"><RefreshCw :size="16" />检查订阅</button><button class="btn ghost" @click="load"><RefreshCw :size="16" />刷新</button></div></header>
    <div v-if="loadError" class="load-error-banner" role="alert">用户状态暂不可用：{{ loadError }}{{ authors.length ? '；下方为上次读取的结果。' : '' }}<button class="text-button" @click="load">重试</button></div>
    <form class="command-bar" @submit.prevent="add"><X :size="18" /><input v-model="input" aria-label="X 用户主页或用户名" placeholder="输入 X 用户主页 URL 或 @用户名…" /><button class="btn primary"><Plus :size="16" />添加用户</button></form>
    <div class="table-shell"><table class="data-table"><thead><tr><th>用户</th><th>状态</th><th>下载量</th><th>订阅</th><th class="actions-col">操作</th></tr></thead><tbody>
      <tr v-for="author in authors" :key="author.id">
        <td data-label="用户"><div class="author-cell"><span class="avatar"><img v-if="author.avatar_url" :src="author.avatar_url" alt="" /><UserRound v-else /></span><div><strong>{{ author.display_name || `@${author.username}` }}</strong><span>@{{ author.username }}</span></div></div></td>
        <td data-label="状态"><span class="status subtle">{{ author.account_status_label || '正常' }}</span><details v-if="author.last_error" class="task-error-detail"><summary>{{ author.last_error }}</summary><p>{{ author.last_error }}</p></details></td>
        <td data-label="下载量"><strong>{{ author.total_downloads || 0 }}</strong><span>个媒体文件</span></td>
        <td data-label="订阅"><button class="switch" :class="{ on: author.is_subscribed }" role="switch" :aria-checked="author.is_subscribed" :aria-label="`${author.is_subscribed ? '取消订阅' : '订阅'} @${author.username}`" @click="action(author, author.is_subscribed ? 'unsubscribe' : 'subscribe')"><i /></button></td>
        <td data-label="操作"><div class="row-actions"><button class="btn ghost compact" @click="action(author, 'download')"><Download :size="15" />下载</button><button class="icon-btn danger" :aria-label="`删除 @${author.username}`" @click="remove(author)"><Trash2 :size="17" /></button></div></td>
      </tr>
    </tbody></table><div v-if="!authors.length && !loadError" class="empty-state"><Users /><strong>暂无 X 用户</strong></div></div>
    <Pager v-if="!loadError || authors.length" :page="page" :pages="pages" :total="total" @change="page = $event" />
  </section>
</template>
