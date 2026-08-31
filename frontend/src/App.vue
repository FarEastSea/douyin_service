<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Activity, BookOpen, ChevronLeft, Download, LayoutDashboard, Menu, MoonStar, Settings, Sun, UserRound, Users, X } from '@lucide/vue'
import { api, saveToken } from './api'
import MediaLightbox from './components/MediaLightbox.vue'
import RiskBanner from './components/RiskBanner.vue'
import { useAppStore } from './stores/app'
import type { MediaItem } from './types'
import type { MediaPlatform } from './types'
import BootstrapView from './views/BootstrapView.vue'

const store = useAppStore(), route = useRoute(), router = useRouter()
const initialized = ref(false), bootstrap = ref<any>({ ready: true })
const collapsed = ref(localStorage.getItem('sidebar-collapsed') === 'true')
const authOpen = ref(false), token = ref('')
const previewOpen = ref(false), previewItems = ref<MediaItem[]>([]), previewStart = ref(0)
let statsTimer: number | null = null
const fallbackPlatforms: MediaPlatform[] = [
  { id: 'douyin', name: '抖音', short_name: '抖音', route_prefix: '/douyin', icon_text: '抖', domains: [], capabilities: { tasks: true, authors: true, works: true, subscriptions: true, subscription_reports: true, settings: true, profile_download: false, work_download: true } },
  { id: 'x', name: 'X/Twitter', short_name: 'X', route_prefix: '/x', icon_text: '@', domains: [], capabilities: { tasks: true, authors: true, works: false, subscriptions: true, subscription_reports: false, settings: true, profile_download: true, work_download: false } },
]
const platforms = computed(() => store.platforms.length ? store.platforms : fallbackPlatforms)
const platform = computed(() => platforms.value.find(item => route.path === item.route_prefix || route.path.startsWith(`${item.route_prefix}/`)) || platforms.value[0])
const nav = computed(() => {
  const current = platform.value, prefix = current.route_prefix, items = []
  if (current.capabilities.tasks) items.push({ path: `${prefix}/tasks`, label: '下载任务', icon: Download })
  if (current.capabilities.authors) items.push({ path: `${prefix}/authors`, label: current.id === 'x' ? '用户管理' : '作者管理', icon: Users })
  if (current.capabilities.subscription_reports) items.push({ path: `${prefix}/updates`, label: '自动更新', icon: Activity })
  if (current.capabilities.settings) items.push({ path: `${prefix}/settings`, label: '设置', icon: Settings })
  return items
})
const themeIcon = computed(() => store.theme === 'light' ? Sun : MoonStar)

function toggleSidebar() { collapsed.value = !collapsed.value; localStorage.setItem('sidebar-collapsed', String(collapsed.value)) }
function switchPlatform(next: MediaPlatform) { router.push(`${next.route_prefix}/tasks`); store.sidebarOpen = false }
function login() { if (!token.value.trim()) return; saveToken(token.value); authOpen.value = false; token.value = ''; store.refreshStatus(); router.go(0) }
function preview(event: Event) { const detail = (event as CustomEvent).detail; previewItems.value = detail.items; previewStart.value = detail.start || 0; previewOpen.value = true }
function refreshVisibleStats() { if (!document.hidden) void store.refreshStats() }
function handleVisibilityChange() { if (!document.hidden) void store.refreshStatus() }
async function init() {
  store.applyTheme()
  try { bootstrap.value = await api('/bootstrap/status') } catch { bootstrap.value = { ready: true } }
  initialized.value = true
  if (bootstrap.value.ready) {
    store.startRiskClock()
    statsTimer = window.setInterval(refreshVisibleStats, 5000)
    void store.refreshStatus()
  }
}
function requireAuth() { authOpen.value = true }
onMounted(() => { init(); window.addEventListener('app:auth-required', requireAuth); window.addEventListener('app:preview', preview); document.addEventListener('visibilitychange', handleVisibilityChange) })
onBeforeUnmount(() => {
  window.removeEventListener('app:auth-required', requireAuth)
  window.removeEventListener('app:preview', preview)
  document.removeEventListener('visibilitychange', handleVisibilityChange)
  if (statsTimer != null) window.clearInterval(statsTimer)
  store.stopRiskClock()
})
</script>

<template>
  <div v-if="!initialized" class="app-loading"><div class="brand-mark"><LayoutDashboard /></div><span>正在载入控制台…</span></div>
  <BootstrapView v-else-if="!bootstrap.ready" :status="bootstrap" />
  <div v-else class="app-shell" :class="{ collapsed, 'mobile-open': store.sidebarOpen }">
    <aside class="sidebar">
      <header class="brand"><div class="brand-mark"><Download /></div><div><small>MEDIA OPS</small><strong>媒体控制台</strong></div><button class="collapse-btn" @click="toggleSidebar"><ChevronLeft /></button></header>
      <div class="platform-switch" :style="{ gridTemplateColumns: `repeat(${platforms.length}, minmax(0, 1fr))` }"><button v-for="item in platforms" :key="item.id" :class="{ active: platform.id === item.id }" @click="switchPlatform(item)"><span>{{ item.icon_text }}</span><b>{{ item.short_name }}</b></button></div>
      <nav class="main-nav"><span class="nav-label">工作区</span><RouterLink v-for="item in nav" :key="item.path" :to="item.path" @click="store.sidebarOpen = false"><component :is="item.icon" /><span>{{ item.label }}</span></RouterLink></nav>
      <footer><div class="service-state"><i /><span>服务在线</span></div><button class="icon-btn" :title="`主题：${store.theme}`" @click="store.cycleTheme"><component :is="themeIcon" /></button></footer>
    </aside>
    <button class="mobile-backdrop" aria-label="关闭导航" @click="store.sidebarOpen = false" />

    <main class="main-area">
      <header class="topbar"><button class="icon-btn mobile-menu" @click="store.sidebarOpen = true"><Menu /></button><div><span>{{ platform.name }}媒体运营</span><strong>媒体下载管理系统</strong></div><div class="topbar-actions"><a class="icon-btn" href="/docs" target="_blank" title="API 文档"><BookOpen /></a><button class="profile-button" title="管理凭据" @click="authOpen = true"><UserRound /></button></div></header>
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
