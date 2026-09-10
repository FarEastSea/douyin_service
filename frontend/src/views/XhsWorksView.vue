<script setup lang="ts">
import { ArrowLeft, ExternalLink, RefreshCw } from '@lucide/vue'
import { onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import Pager from '../components/Pager.vue'
import { useAppStore } from '../stores/app'
import type { PageData, PlatformWork } from '../types'

const store = useAppStore(), route = useRoute(), router = useRouter()
const works = ref<PlatformWork[]>([]), page = ref(1), pages = ref(1), total = ref(0)
const authorId = Number(route.params.id)
async function load() {
  try {
    const data = await api<PageData<PlatformWork>>(`/platform-downloads/xhs/authors/${authorId}/works?page=${page.value}&page_size=30`)
    works.value = data.items; pages.value = data.pages; total.value = data.total
  } catch (error: any) { store.notify(error.message || '加载作者作品失败', 'error') }
}
watch(page, load); onMounted(load)
</script>

<template><section class="workspace-card"><header class="workspace-header"><div><p class="eyebrow">XHS CREATOR WORKS</p><h2>作者作品</h2><span>已验证并进入本地任务体系的作品索引</span></div><div class="header-actions"><button class="btn ghost" @click="router.push('/xhs/authors')"><ArrowLeft :size="16" />返回作者</button><button class="btn ghost" @click="load"><RefreshCw :size="16" />刷新</button></div></header><div class="table-shell"><table class="data-table"><thead><tr><th>作品</th><th>类型</th><th>发布时间</th><th>下载任务</th><th class="actions-col">来源</th></tr></thead><tbody><tr v-for="work in works" :key="work.id"><td><div class="media-cell"><span class="avatar"><img v-if="work.cover_url" :src="work.cover_url" alt="" /><span v-else class="media-icon">红</span></span><div><strong>{{ work.title || work.external_work_id }}</strong><span>{{ work.external_work_id }}</span></div></div></td><td><span class="status subtle">{{ work.work_type === 'video' ? '视频' : work.work_type === 'image' ? '图文' : '未知' }}</span></td><td><span>{{ work.published_at ? new Date(work.published_at).toLocaleString() : '未提供' }}</span></td><td><span>{{ work.download_task_id ? `#${work.download_task_id}` : '尚未完成' }}</span></td><td><a class="icon-btn" :href="work.source_url" target="_blank" rel="noreferrer" title="打开原作品"><ExternalLink :size="17" /></a></td></tr></tbody></table><div v-if="!works.length" class="empty-state"><strong>尚未发现作品</strong></div></div><Pager :page="page" :pages="pages" :total="total" @change="page = $event" /></section></template>
