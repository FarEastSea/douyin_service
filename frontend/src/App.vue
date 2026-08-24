<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Activity, AtSign, BookOpen, ChevronLeft, Download, LayoutDashboard, Menu, MoonStar, Settings, Sun, UserRound, Users, X } from '@lucide/vue'
import { api, saveToken } from './api'
import MediaLightbox from './components/MediaLightbox.vue'
import RiskBanner from './components/RiskBanner.vue'
import { useAppStore } from './stores/app'
import type { MediaItem } from './types'
import BootstrapView from './views/BootstrapView.vue'

const store = useAppStore(), route = useRoute(), router = useRouter()
const initialized = ref(false), bootstrap = ref<any>({ ready: true })
const collapsed = ref(localStorage.getItem('sidebar-collapsed') === 'true')
const authOpen = ref(false), token = ref('')
const previewOpen = ref(false), previewItems = ref<MediaItem[]>([]), previewStart = ref(0)
const platform = computed(() => route.path.startsWith('/x/') ? 'x' : 'douyin')
const nav = computed(() => platform.value === 'x' ? [
  { path: '/x/tasks', label: '下载任务', icon: Download },
  { path: '/x/authors', label: '用户管理', icon: Users },
  { path: '/x/settings', label: '设置', icon: Settings },
] : [
  { path: '/douyin/tasks', label: '下载任务', icon: Download },
  { path: '/douyin/authors', label: '作者管理', icon: Users },
  { path: '/douyin/updates', label: '自动更新', icon: Activity },
  { path: '/douyin/settings', label: '设置', icon: Settings },
])
const themeIcon = computed(() => store.theme === 'light' ? Sun : MoonStar)

function toggleSidebar() { collapsed.value = !collapsed.value; localStorage.setItem('sidebar-collapsed', String(collapsed.value)) }
function switchPlatform(next: 'douyin' | 'x') { router.push(next === 'douyin' ? '/douyin/tasks' : '/x/tasks'); store.sidebarOpen = false }
function login() { if (!token.value.trim()) return; saveToken(token.value); authOpen.value = false; token.value = ''; store.refreshStatus(); router.go(0) }
function preview(event: Event) { const detail = (event as CustomEvent).detail; previewItems.value = detail.items; previewStart.value = detail.start || 0; previewOpen.value = true }
async function init() {
  store.applyTheme()
  try { bootstrap.value = await api('/bootstrap/status') } catch { bootstrap.value = { ready: true } }
  initialized.value = true
  if (bootstrap.value.ready) { await store.refreshStatus(); store.startRiskClock() }
}
onMounted(() => { init(); window.addEventListener('app:auth-required', () => authOpen.value = true); window.addEventListener('app:preview', preview) })
onBeforeUnmount(() => { window.removeEventListener('app:preview', preview); store.stopStatusClock() })
</script>

<template>
  <div v-if="!initialized" class="app-loading"><div class="brand-mark"><LayoutDashboard /></div><span>正在载入控制台…</span></div>
  <BootstrapView v-else-if="!bootstrap.ready" :status="bootstrap" />
  <div v-else class="app-shell" :class="{ collapsed, 'mobile-open': store.sidebarOpen }">
    <aside class="sidebar">
      <header class="brand"><div class="brand-mark"><Download /></div><div><small>MEDIA OPS</small><strong>媒体控制台</strong></div><button class="collapse-btn" @click="toggleSidebar"><ChevronLeft /></button></header>
      <div class="platform-switch"><button :class="{ active: platform === 'douyin' }" @click="switchPlatform('douyin')"><span>抖</span><b>抖音</b></button><button :class="{ active: platform === 'x' }" @click="switchPlatform('x')"><AtSign /><b>X</b></button></div>
      <nav class="main-nav"><span class="nav-label">工作区</span><RouterLink v-for="item in nav" :key="item.path" :to="item.path" @click="store.sidebarOpen = false"><component :is="item.icon" /><span>{{ item.label }}</span></RouterLink></nav>
      <footer><div class="service-state"><i /><span>服务在线</span></div><button class="icon-btn" :title="`主题：${store.theme}`" @click="store.cycleTheme"><component :is="themeIcon" /></button></footer>
    </aside>
    <button class="mobile-backdrop" aria-label="关闭导航" @click="store.sidebarOpen = false" />

    <main class="main-area">
      <header class="topbar"><button class="icon-btn mobile-menu" @click="store.sidebarOpen = true"><Menu /></button><div><span>{{ platform === 'douyin' ? '抖音媒体运营' : 'X 媒体运营' }}</span><strong>媒体下载管理系统</strong></div><div class="topbar-actions"><a class="icon-btn" href="/docs" target="_blank" title="API 文档"><BookOpen /></a><button class="profile-button" title="管理凭据" @click="authOpen = true"><UserRound /></button></div></header>
      <RiskBanner />
      <section v-if="!route.path.includes('/works')" class="summary-grid">
        <article><span>作者总数</span><strong>{{ store.stats.total_authors.toLocaleString() }}</strong><i data-tone="violet" /></article>
        <article><span>已订阅</span><strong>{{ store.stats.subscribed_authors.toLocaleString() }}</strong><i data-tone="green" /></article>
        <article><span>待处理</span><strong>{{ store.stats.pending_tasks.toLocaleString() }}</strong><i data-tone="amber" /></article>
        <article><span>下载中</span><strong>{{ store.stats.downloading_tasks.toLocaleString() }}</strong><i data-tone="blue" /></article>
        <article><span>累计下载</span><strong>{{ store.stats.total_downloads.toLocaleString() }}</strong><i data-tone="accent" /></article>
      </section>
      <RouterView />
    </main>

    <Transition name="toast"><div v-if="store.toast" class="toast" :data-tone="store.toast.tone">{{ store.toast.message }}</div></Transition>
    <MediaLightbox :open="previewOpen" :items="previewItems" :start="previewStart" @close="previewOpen = false; previewItems = []" />
    <Teleport to="body"><div v-if="authOpen" class="auth-overlay" @click.self="authOpen = false"><form class="auth-dialog" @submit.prevent="login"><button type="button" class="icon-btn close" @click="authOpen = false"><X /></button><div class="brand-mark"><UserRound /></div><p class="eyebrow">PROTECTED CONSOLE</p><h2>输入管理 Token</h2><p>Token 只保存在当前浏览器，并通过 Authorization 请求头发送。</p><input v-model="token" type="password" autofocus placeholder="Bearer Token" /><button class="btn primary">登录并继续</button></form></div></Teleport>
  </div>
</template>
