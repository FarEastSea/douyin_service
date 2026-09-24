<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Activity, BookOpen, ChevronLeft, ChevronRight, Download, HardDrive, LayoutDashboard, Layers3, Menu, MoonStar, Settings, Sun, UserRound, Users, X } from '@lucide/vue'
import { api, saveToken } from './api'
import MediaLightbox from './components/MediaLightbox.vue'
import NewDownloadPanel from './components/NewDownloadPanel.vue'
import RiskBanner from './components/RiskBanner.vue'
import { focusFirst, restoreFocus, trapFocus } from './focus'
import { useAppStore } from './stores/app'
import type { MediaItem, MediaPlatform } from './types'
import BootstrapView from './views/BootstrapView.vue'

const store = useAppStore(), route = useRoute(), router = useRouter()
const initialized = ref(false), bootstrap = ref<any>({ ready: true })
const collapsed = ref(localStorage.getItem('sidebar-collapsed') === 'true')
const platformExpanded = ref(false)
const authOpen = ref(false), token = ref('')
const previewOpen = ref(false), previewItems = ref<MediaItem[]>([]), previewStart = ref(0)
const newDownloadOpen = ref(false), newDownloadButton = ref<HTMLButtonElement | null>(null)
const sidebarElement = ref<HTMLElement | null>(null), mobileMenuButton = ref<HTMLButtonElement | null>(null)
const authDialog = ref<HTMLElement | null>(null), authInput = ref<HTMLInputElement | null>(null)
const mobileViewport = ref(false)
const readiness = ref<'ready' | 'not_ready' | 'unknown'>('unknown')
let statusTimer: number | null = null
let authReturnFocus: HTMLElement | null = null
let mobileQuery: MediaQueryList | null = null

const fallbackPlatforms: MediaPlatform[] = [
  { id: 'douyin', name: '抖音', short_name: '抖音', route_prefix: '/douyin', icon_text: '抖', domains: [], capabilities: { tasks: true, authors: true, works: true, subscriptions: true, subscription_reports: true, settings: true, profile_download: false, work_download: true } },
  { id: 'x', name: 'X/Twitter', short_name: 'X', route_prefix: '/x', icon_text: '@', domains: [], capabilities: { tasks: true, authors: true, works: false, subscriptions: true, subscription_reports: false, settings: true, profile_download: true, work_download: true } },
  { id: 'tiktok', name: 'TikTok', short_name: 'TikTok', route_prefix: '/tiktok', icon_text: 'T', domains: [], capabilities: { tasks: true, authors: false, works: false, subscriptions: false, subscription_reports: false, settings: true, profile_download: true, work_download: true } },
  { id: 'weibo', name: '微博', short_name: '微博', route_prefix: '/weibo', icon_text: '微', domains: [], capabilities: { tasks: true, authors: false, works: false, subscriptions: false, subscription_reports: false, settings: true, profile_download: true, work_download: true } },
  { id: 'bilibili', name: '哔哩哔哩', short_name: 'B站', route_prefix: '/bilibili', icon_text: '哔', domains: [], capabilities: { tasks: true, authors: false, works: false, subscriptions: false, subscription_reports: false, settings: true, profile_download: true, work_download: true } },
  { id: 'xhs', name: '小红书', short_name: '小红书', route_prefix: '/xhs', icon_text: '红', domains: [], capabilities: { tasks: true, authors: false, works: false, subscriptions: false, subscription_reports: false, settings: true, profile_download: false, work_download: true } },
]
const platforms = computed(() => store.platforms.length ? store.platforms : fallbackPlatforms)
const pageTitle = computed(() => {
  if (route.path === '/dashboard') return '工作台'
  if (route.path === '/operations/tasks') return '全部任务'
  if (route.path.startsWith('/operations/maintenance')) return '运行维护'
  if (route.path.startsWith('/settings')) return '设置'
  if (route.path.includes('/works')) return '作者作品'
  if (route.path === '/douyin/authors') return '作者与作品'
  if (route.path === '/douyin/updates') return '自动更新'
  if (route.path === '/x/authors') return 'X 用户管理'
  return `${platforms.value.find(item => route.path.startsWith(`${item.route_prefix}/`))?.name || '平台'}专项任务`
})
const themeIcon = computed(() => store.theme === 'light' ? Sun : MoonStar)
const mobileNavigationOpen = computed(() => mobileViewport.value && store.sidebarOpen)

function toggleSidebar() {
  if (mobileViewport.value) { dismissSidebar(); return }
  collapsed.value = !collapsed.value
  localStorage.setItem('sidebar-collapsed', String(collapsed.value))
}
function closeSidebar() { store.sidebarOpen = false }
function openSidebar() {
  store.sidebarOpen = true
  void nextTick(() => focusFirst(sidebarElement.value))
}
function dismissSidebar() {
  store.sidebarOpen = false
  void nextTick(() => restoreFocus(mobileMenuButton.value))
}
function preview(event: Event) { const detail = (event as CustomEvent).detail; previewItems.value = detail.items; previewStart.value = detail.start || 0; previewOpen.value = true }
function openNewDownload() { newDownloadOpen.value = true; closeSidebar() }
function closeNewDownload() { newDownloadOpen.value = false; void nextTick(() => newDownloadButton.value?.focus()) }
function handleVisibilityChange() { if (!document.hidden) void refreshHealth() }
async function refreshHealth() {
  await store.refreshRisk()
  try { const data = await api<{ ready: boolean }>('/status/readiness'); readiness.value = data.ready ? 'ready' : 'not_ready' }
  catch { readiness.value = 'unknown' }
}
async function init() {
  store.applyTheme()
  try { bootstrap.value = await api('/bootstrap/status') } catch { bootstrap.value = { ready: true } }
  initialized.value = true
  if (bootstrap.value.ready) {
    store.startRiskClock()
    void store.refreshStatus()
    void refreshHealth()
    statusTimer = window.setInterval(() => { if (!document.hidden) void refreshHealth() }, 30_000)
  }
}
function showAuth(returnFocus?: HTMLElement | null) {
  authReturnFocus = returnFocus || (document.activeElement instanceof HTMLElement ? document.activeElement : null)
  authOpen.value = true
  document.body.classList.add('modal-open')
  void nextTick(() => focusFirst(authDialog.value, authInput.value))
}
function openAuth(event?: Event) {
  showAuth(event?.currentTarget instanceof HTMLElement ? event.currentTarget : null)
}
function closeAuth() {
  authOpen.value = false
  document.body.classList.remove('modal-open')
  const target = authReturnFocus
  authReturnFocus = null
  void nextTick(() => restoreFocus(target))
}
function requireAuth() {
  newDownloadOpen.value = false
  void nextTick(() => showAuth(newDownloadButton.value))
}
function onGlobalKeydown(event: KeyboardEvent) {
  if (authOpen.value) {
    if (event.key === 'Escape') { event.preventDefault(); closeAuth() }
    else trapFocus(event, authDialog.value)
    return
  }
  if (mobileNavigationOpen.value) {
    if (event.key === 'Escape') { event.preventDefault(); dismissSidebar() }
    else trapFocus(event, sidebarElement.value)
  }
}
function login() {
  if (!token.value.trim()) return
  saveToken(token.value); authOpen.value = false; document.body.classList.remove('modal-open'); token.value = ''
  void store.refreshStatus(); void refreshHealth(); router.go(0)
}
function updateMobileViewport(event: MediaQueryListEvent | MediaQueryList) {
  mobileViewport.value = event.matches
  if (!event.matches) store.sidebarOpen = false
}
onMounted(() => {
  mobileQuery = window.matchMedia('(max-width: 900px)')
  updateMobileViewport(mobileQuery)
  mobileQuery.addEventListener('change', updateMobileViewport)
  void init()
  window.addEventListener('app:auth-required', requireAuth)
  window.addEventListener('app:preview', preview)
  window.addEventListener('app:new-download', openNewDownload)
  document.addEventListener('keydown', onGlobalKeydown)
  document.addEventListener('visibilitychange', handleVisibilityChange)
})
onBeforeUnmount(() => {
  window.removeEventListener('app:auth-required', requireAuth)
  window.removeEventListener('app:preview', preview)
  window.removeEventListener('app:new-download', openNewDownload)
  document.removeEventListener('keydown', onGlobalKeydown)
  document.removeEventListener('visibilitychange', handleVisibilityChange)
  mobileQuery?.removeEventListener('change', updateMobileViewport)
  if (statusTimer !== null) window.clearInterval(statusTimer)
  document.body.classList.remove('modal-open')
  store.stopRiskClock()
})
</script>

<template>
  <div v-if="!initialized" class="app-loading"><div class="brand-mark"><LayoutDashboard /></div><span>正在载入控制台…</span></div>
  <BootstrapView v-else-if="!bootstrap.ready" :status="bootstrap" />
  <div v-else class="app-shell" :class="{ collapsed, 'mobile-open': store.sidebarOpen }" :inert="authOpen || newDownloadOpen || previewOpen">
    <a class="skip-link" href="#main-content">跳转到主要内容</a>
    <aside ref="sidebarElement" class="sidebar" aria-label="主导航" :role="mobileNavigationOpen ? 'dialog' : undefined" :aria-modal="mobileNavigationOpen ? 'true' : undefined" :tabindex="mobileNavigationOpen ? -1 : undefined">
      <header class="brand"><div class="brand-mark"><Download /></div><div><small>MEDIA OPS</small><strong>媒体控制台</strong></div><button class="collapse-btn" :aria-label="mobileViewport ? '关闭导航' : collapsed ? '展开侧栏' : '收起侧栏'" @click="toggleSidebar"><ChevronLeft v-if="mobileViewport || !collapsed" /><ChevronRight v-else /></button></header>
      <nav class="main-nav" aria-label="工作区">
        <span class="nav-label">工作区</span>
        <RouterLink to="/dashboard" title="工作台" @click="closeSidebar"><LayoutDashboard /><span>工作台</span></RouterLink>
        <RouterLink to="/operations/tasks" title="全部任务" @click="closeSidebar"><Layers3 /><span>全部任务</span></RouterLink>
        <RouterLink to="/douyin/authors" title="作者与作品" @click="closeSidebar"><Users /><span>作者与作品</span></RouterLink>
        <RouterLink to="/douyin/updates" title="自动更新" @click="closeSidebar"><Activity /><span>自动更新</span></RouterLink>
        <span class="nav-label nav-label-secondary">更多能力</span>
        <button class="nav-expander" :aria-expanded="platformExpanded" aria-controls="platform-nav" title="平台专项" @click="platformExpanded = !platformExpanded"><Download /><span>平台专项</span><ChevronRight class="expander-chevron" :class="{ expanded: platformExpanded }" /></button>
        <div v-if="platformExpanded" id="platform-nav" class="platform-nav">
          <RouterLink v-for="item in platforms.filter(value => value.capabilities.tasks)" :key="item.id" :to="`${item.route_prefix}/tasks`" :title="`${item.name}任务`" @click="closeSidebar"><b>{{ item.icon_text }}</b><span>{{ item.name }}任务</span></RouterLink>
          <RouterLink v-if="platforms.some(item => item.id === 'x' && item.capabilities.authors)" to="/x/authors" title="X 用户管理" @click="closeSidebar"><b>@</b><span>X 用户管理</span></RouterLink>
        </div>
      </nav>
      <nav class="main-nav utility-nav" aria-label="系统">
        <RouterLink to="/operations/maintenance/services" title="运行维护" @click="closeSidebar"><HardDrive /><span>运行维护</span></RouterLink>
        <RouterLink to="/settings/application" title="设置" @click="closeSidebar"><Settings /><span>设置</span></RouterLink>
      </nav>
      <footer><RouterLink to="/operations/maintenance/services" class="service-state" :data-state="readiness" @click="closeSidebar"><i /><span>{{ readiness === 'ready' ? '服务就绪' : readiness === 'not_ready' ? '服务需检查' : '状态未确认' }}</span></RouterLink><button class="icon-btn" :title="`主题：${store.theme}`" :aria-label="`切换主题，当前为${store.theme}`" @click="store.cycleTheme"><component :is="themeIcon" /></button></footer>
    </aside>
    <button class="mobile-backdrop" aria-label="关闭导航" @click="dismissSidebar" />

    <main id="main-content" class="main-area" tabindex="-1" :inert="mobileNavigationOpen" :aria-hidden="mobileNavigationOpen ? 'true' : undefined">
      <header class="topbar"><button ref="mobileMenuButton" class="icon-btn mobile-menu" aria-label="打开导航" @click="openSidebar"><Menu /></button><div class="topbar-title"><span>媒体下载管理系统</span><strong>{{ pageTitle }}</strong></div><div class="topbar-actions"><button ref="newDownloadButton" class="btn primary topbar-create" @click="openNewDownload"><Download :size="17" /><span class="create-label-full">新建下载</span><span class="create-label-short">新建</span></button><a class="icon-btn" href="/docs" target="_blank" rel="noopener noreferrer" title="API 文档" aria-label="打开 API 文档"><BookOpen /></a><button class="profile-button" title="管理凭据" aria-label="管理凭据" @click="openAuth"><UserRound /></button></div></header>
      <RiskBanner v-if="route.path !== '/dashboard'" />
      <RouterView />
    </main>

    <Transition name="toast"><div v-if="store.toast" class="toast" :data-tone="store.toast.tone" role="status">{{ store.toast.message }}</div></Transition>
    <MediaLightbox :open="previewOpen" :items="previewItems" :start="previewStart" @close="previewOpen = false; previewItems = []" />
    <Teleport to="body"><NewDownloadPanel v-if="newDownloadOpen" @close="closeNewDownload" /></Teleport>
    <Teleport to="body"><div v-if="authOpen" class="auth-overlay" @click.self="closeAuth"><form ref="authDialog" class="auth-dialog" role="dialog" aria-modal="true" aria-labelledby="auth-title" tabindex="-1" @submit.prevent="login"><button type="button" class="icon-btn close" aria-label="关闭" @click="closeAuth"><X /></button><div class="brand-mark"><UserRound /></div><h2 id="auth-title">输入管理 Token</h2><p>Token 只保存在当前浏览器，并通过 Authorization 请求头发送。</p><label for="admin-token">管理 Token</label><input id="admin-token" ref="authInput" v-model="token" type="password" autocomplete="off" placeholder="Bearer Token" /><button class="btn primary">登录并继续</button></form></div></Teleport>
  </div>
</template>
