<script setup lang="ts">
import IssueDetail from "./components/IssueDetail.vue";
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  Activity,
  BookOpen,
  ChevronLeft,
  ChevronRight,
  Download,
  HardDrive,
  LayoutDashboard,
  Layers3,
  Menu,
  MoonStar,
  Settings,
  Sun,
  UserRound,
  Users,
  X,
  Search,
  Plus,
  ArrowLeft,
  Images,
} from "@lucide/vue";
import { api, saveToken } from "./api";
import MediaLightbox from "./components/MediaLightbox.vue";
import NewDownloadPanel from "./components/NewDownloadPanel.vue";
import CommandPalette from "./components/CommandPalette.vue";
import ConfirmDialog from "./components/ConfirmDialog.vue";
import { modalDepth, inspectorDepth } from "./workspace";
import {
  settingsNavigation,
  maintenanceNavigation,
  sectionPath,
} from "./settings-registry";
import RiskBanner from "./components/RiskBanner.vue";
import {
  captureFocusOrigin,
  focusFirst,
  restoreFocus,
  trapFocus,
} from "./focus";
import { useAppStore } from "./stores/app";
import type { MediaItem, MediaPlatform } from "./types";
import BootstrapView from "./views/BootstrapView.vue";

const store = useAppStore(),
  route = useRoute(),
  router = useRouter();
const initialized = ref(false),
  bootstrap = ref<any>({ ready: true });
const commandOpen = ref(false);
const authOpen = ref(false),
  token = ref("");
const previewOpen = ref(false),
  previewItems = ref<MediaItem[]>([]),
  previewStart = ref(0);
const newDownloadOpen = ref(false),
  newDownloadButton = ref<HTMLButtonElement | null>(null);
const captureIntent = ref<"download" | "subscribe">("download"),
  capturePlatform = ref("");
const captureOrigin = ref<HTMLElement | null>(null);
const sidebarElement = ref<HTMLElement | null>(null),
  mobileMenuButton = ref<HTMLButtonElement | null>(null);
const authDialog = ref<HTMLElement | null>(null),
  authInput = ref<HTMLInputElement | null>(null);
const mobileViewport = ref(false);
const readiness = ref<"ready" | "not_ready" | "unknown">("unknown");
let statusTimer: number | null = null;
let authReturnFocus: HTMLElement | null = null;
let mobileQuery: MediaQueryList | null = null;
let themeQuery: MediaQueryList | null = null;
const blocking = computed(
  () =>
    authOpen.value ||
    newDownloadOpen.value ||
    previewOpen.value ||
    modalDepth.value > 0,
);
watch(authOpen, (value) => {
  modalDepth.value += value ? 1 : -1;
});
watch(blocking, (value) => {
  document.body.classList.toggle("modal-open", value);
});
function themeChanged() {
  if (store.theme === "auto") store.applyTheme();
}

const directoryMode = computed(() =>
  route.path.startsWith("/settings")
    ? "settings"
    : route.path.startsWith("/operations/maintenance")
      ? "maintenance"
      : null,
);
const directory = computed(() =>
  directoryMode.value === "settings"
    ? settingsNavigation
    : maintenanceNavigation,
);
const pageTitle = computed(() =>
  directoryMode.value === "settings"
    ? "设置"
    : directoryMode.value === "maintenance"
      ? "系统"
      : route.path === "/library"
        ? "媒体库"
      : route.path.includes("/works")
        ? "订阅 / 作品"
        : route.path.startsWith("/authors")
          ? "订阅"
          : route.path === "/automation"
            ? "订阅 / 运行记录"
            : route.path === "/operations/tasks"
              ? "下载任务"
              : "总览",
);
const themeIcon = computed(() => (store.theme === "light" ? Sun : MoonStar));
const mobileNavigationOpen = computed(
  () => mobileViewport.value && store.sidebarOpen,
);

function toggleSidebar() {
  dismissSidebar();
}
function closeSidebar() {
  store.sidebarOpen = false;
}
function openSidebar() {
  store.sidebarOpen = true;
  void nextTick(() => focusFirst(sidebarElement.value));
}
function dismissSidebar() {
  store.sidebarOpen = false;
  void nextTick(() => restoreFocus(mobileMenuButton.value));
}
function preview(event: Event) {
  const detail = (event as CustomEvent).detail;
  previewItems.value = detail.items;
  previewStart.value = detail.start || 0;
  previewOpen.value = true;
}
function openNewDownload(event?: Event) {
  captureOrigin.value =
    event?.currentTarget instanceof HTMLElement
      ? event.currentTarget
      : captureFocusOrigin();
  const detail = (event as CustomEvent | undefined)?.detail;
  captureIntent.value =
    detail?.intent === "subscribe" ? "subscribe" : "download";
  capturePlatform.value =
    typeof detail?.platform === "string" ? detail.platform : "";
  newDownloadOpen.value = true;
  closeSidebar();
}
function openSubscription() {
  openNewDownload(
    new CustomEvent("app:new-download", { detail: { intent: "subscribe" } }),
  );
}
function closeNewDownload() {
  newDownloadOpen.value = false;
}
function handleVisibilityChange() {
  if (!document.hidden) void refreshHealth();
}
async function refreshHealth() {
  await store.refreshRisk();
  try {
    const data = await api<{ ready: boolean }>("/status/readiness");
    readiness.value = data.ready ? "ready" : "not_ready";
  } catch {
    readiness.value = "unknown";
  }
}
async function init() {
  store.applyTheme();
  try {
    bootstrap.value = await api("/bootstrap/status");
  } catch {
    bootstrap.value = { ready: true };
  }
  initialized.value = true;
  if (bootstrap.value.ready) {
    store.startRiskClock();
    void store.refreshStatus();
    void refreshHealth();
    statusTimer = window.setInterval(() => {
      if (!document.hidden) void refreshHealth();
    }, 30_000);
  }
}
function showAuth(returnFocus?: HTMLElement | null) {
  authReturnFocus =
    returnFocus ||
    (document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null);
  authOpen.value = true;
  void nextTick(() => focusFirst(authDialog.value, authInput.value));
}
function openAuth(event?: Event) {
  showAuth(
    event?.currentTarget instanceof HTMLElement ? event.currentTarget : null,
  );
}
function closeAuth() {
  authOpen.value = false;
  const target = authReturnFocus;
  authReturnFocus = null;
  void nextTick(() => restoreFocus(target));
}
function requireAuth() {
  newDownloadOpen.value = false;
  void nextTick(() => showAuth(captureOrigin.value || newDownloadButton.value));
}
function onGlobalKeydown(event: KeyboardEvent) {
  if (
    (event.ctrlKey || event.metaKey) &&
    event.key.toLowerCase() === "k" &&
    (modalDepth.value === inspectorDepth.value || commandOpen.value)
  ) {
    event.preventDefault();
    closeSidebar();
    commandOpen.value = !commandOpen.value;
    return;
  }
  if (authOpen.value) {
    if (event.key === "Escape") {
      event.preventDefault();
      closeAuth();
    } else trapFocus(event, authDialog.value);
    return;
  }
  if (mobileNavigationOpen.value) {
    if (event.key === "Escape") {
      event.preventDefault();
      dismissSidebar();
    } else trapFocus(event, sidebarElement.value);
  }
}
function login() {
  if (!token.value.trim()) return;
  saveToken(token.value);
  authOpen.value = false;
  token.value = "";
  void store.refreshStatus();
  void refreshHealth();
  router.go(0);
}
function updateMobileViewport(event: MediaQueryListEvent | MediaQueryList) {
  mobileViewport.value = event.matches;
  if (!event.matches) store.sidebarOpen = false;
}
onMounted(() => {
  mobileQuery = window.matchMedia("(max-width: 899px)");
  updateMobileViewport(mobileQuery);
  mobileQuery.addEventListener("change", updateMobileViewport);
  themeQuery = matchMedia("(prefers-color-scheme: light)");
  themeQuery.addEventListener("change", themeChanged);
  void init();
  window.addEventListener("app:auth-required", requireAuth);
  window.addEventListener("app:preview", preview);
  window.addEventListener("app:new-download", openNewDownload);
  document.addEventListener("keydown", onGlobalKeydown);
  document.addEventListener("visibilitychange", handleVisibilityChange);
});
onBeforeUnmount(() => {
  window.removeEventListener("app:auth-required", requireAuth);
  window.removeEventListener("app:preview", preview);
  window.removeEventListener("app:new-download", openNewDownload);
  document.removeEventListener("keydown", onGlobalKeydown);
  document.removeEventListener("visibilitychange", handleVisibilityChange);
  themeQuery?.removeEventListener("change", themeChanged);
  mobileQuery?.removeEventListener("change", updateMobileViewport);
  if (statusTimer !== null) window.clearInterval(statusTimer);
  store.stopRiskClock();
});
</script>

<template>
  <div v-if="!initialized" class="app-loading">
    <div class="brand-mark"><LayoutDashboard /></div>
    <span>正在载入控制台…</span>
  </div>
  <BootstrapView v-else-if="!bootstrap.ready" :status="bootstrap" />
  <div
    v-else
    class="app-shell"
    :class="{
      'directory-mode': directoryMode,
      'mobile-open': store.sidebarOpen,
      'has-capture-dock': !directoryMode,
    }"
    :inert="blocking"
  >
    <a class="skip-link" href="#main-content">跳转到主要内容</a>
    <header class="workspace-masthead">
      <div class="masthead-inner">
        <button ref="mobileMenuButton" class="icon-btn mobile-menu" aria-label="打开导航" @click="openSidebar"><Menu :size="21" /></button>
        <RouterLink class="workspace-brand" to="/dashboard" aria-label="媒体工作台总览"><Download :size="24" /><strong>媒体工作台</strong></RouterLink>
        <nav class="workspace-navigation" aria-label="工作区">
          <RouterLink to="/dashboard">总览</RouterLink>
          <RouterLink to="/operations/tasks">任务</RouterLink>
          <RouterLink to="/library">媒体库</RouterLink>
          <RouterLink to="/authors/douyin" :class="{ 'router-link-active': route.path.startsWith('/authors') || route.path === '/automation' }">订阅</RouterLink>
        </nav>
        <div class="masthead-tools">
          <button class="icon-btn masthead-command" aria-label="打开命令面板" title="命令面板 Ctrl / ⌘ K" @click="commandOpen = true"><Search :size="18" /></button>
          <button class="icon-btn" :title="`主题：${store.theme}`" :aria-label="`切换主题，当前为${store.theme}`" @click="store.cycleTheme"><component :is="themeIcon" :size="18" /></button>
          <button ref="newDownloadButton" class="icon-btn masthead-add" aria-label="新建下载" title="新建下载" @click="openNewDownload"><Plus :size="18" /></button>
          <button class="icon-btn masthead-auth" aria-label="管理凭据" title="管理凭据" @click="openAuth"><UserRound :size="18" /></button>
        </div>
      </div>
    </header>
    <aside
      ref="sidebarElement"
      class="sidebar"
      :inert="mobileViewport && !store.sidebarOpen"
      :aria-hidden="mobileViewport && !store.sidebarOpen ? 'true' : undefined"
      aria-label="主导航"
      :role="mobileNavigationOpen ? 'dialog' : undefined"
      :aria-modal="mobileNavigationOpen ? 'true' : undefined"
      :tabindex="mobileNavigationOpen ? -1 : undefined"
    >
      <header class="brand">
        <div class="brand-mark"><Download /></div>
        <div><strong>媒体工作台</strong></div>
        <button
          class="collapse-btn"
          aria-label="关闭导航"
          @click="toggleSidebar"
        >
          <X />
        </button>
      </header>
      <template v-if="directoryMode">
        <RouterLink class="back-workspace" to="/dashboard" @click="closeSidebar"
          ><ArrowLeft :size="16" /><span>返回工作区</span></RouterLink
        >
        <nav class="main-nav directory-nav" :aria-label="pageTitle + '目录'">
          <div v-for="group in directory" :key="group.id" class="nav-group">
            <span class="nav-label">{{ group.label }}</span
            ><RouterLink
              v-for="item in group.items"
              :key="item.id"
              :to="sectionPath(directoryMode, item.id)"
              @click="closeSidebar"
              ><span>{{ item.label }}</span></RouterLink
            >
          </div>
        </nav>
      </template>
      <template v-else>
        <nav class="main-nav" aria-label="工作区">
          <RouterLink to="/dashboard" title="总览" @click="closeSidebar"
            ><LayoutDashboard /><span>总览</span></RouterLink
          >
          <RouterLink
            to="/operations/tasks"
            title="任务"
            @click="closeSidebar"
            ><Layers3 /><span>任务</span></RouterLink
          >
          <RouterLink to="/library" title="媒体库" @click="closeSidebar"><Images /><span>媒体库</span></RouterLink>
          <RouterLink
            to="/authors/douyin"
            title="订阅"
            :class="{ 'router-link-active': route.path.startsWith('/authors') || route.path === '/automation' }"
            @click="closeSidebar"
            ><Users /><span>订阅</span></RouterLink
          >
        </nav>
        <nav class="main-nav utility-nav" aria-label="系统">
          <RouterLink
            to="/operations/maintenance/services"
            title="系统"
            @click="closeSidebar"
            ><HardDrive /><span>系统</span></RouterLink
          >
          <RouterLink
            to="/settings/application"
            title="设置"
            @click="closeSidebar"
            ><Settings /><span>设置</span></RouterLink
          >
        </nav>
      </template>
      <footer>
        <RouterLink
          to="/operations/maintenance/services"
          class="service-state"
          :data-state="readiness"
          @click="closeSidebar"
          ><i /><span>{{
            readiness === "ready"
              ? "服务就绪"
              : readiness === "not_ready"
                ? "服务需检查"
                : "状态未确认"
          }}</span></RouterLink
        ><button
          class="icon-btn"
          :title="`主题：${store.theme}`"
          :aria-label="`切换主题，当前为${store.theme}`"
          @click="store.cycleTheme"
        >
          <component :is="themeIcon" />
        </button>
      </footer>
    </aside>
    <button
      class="mobile-backdrop"
      aria-label="关闭导航"
      @click="dismissSidebar"
    />

    <main
      id="main-content"
      class="main-area"
      tabindex="-1"
      :inert="mobileNavigationOpen"
      :aria-hidden="mobileNavigationOpen ? 'true' : undefined"
    >
      <RiskBanner v-if="route.path !== '/dashboard'" />
      <RouterView />
    </main>
    <footer class="workspace-utility-footer">
      <RouterLink to="/operations/maintenance/services" class="service-state" :data-state="readiness"><i /><span>{{ readiness === 'ready' ? '服务就绪' : readiness === 'not_ready' ? '服务需检查' : '状态未确认' }}</span></RouterLink>
      <nav aria-label="系统工具"><RouterLink to="/operations/maintenance/services"><HardDrive :size="16" />系统</RouterLink><RouterLink to="/settings/application"><Settings :size="16" />设置</RouterLink></nav>
    </footer>
    <nav
      v-if="!directoryMode"
      class="mobile-capture-dock"
      aria-label="快捷添加"
      :inert="mobileNavigationOpen"
    >
      <button class="btn ghost" @click="openSubscription">
        <Users :size="18" />订阅作者
      </button>
      <button class="btn primary" @click="openNewDownload">
        <Download :size="18" />下载作品
      </button>
    </nav>

    <Teleport to="body">
      <Transition name="toast"
        ><div
          v-if="store.toast"
          class="toast"
          :class="{ 'above-capture-dock': !directoryMode }"
          :data-tone="store.toast.tone"
          :role="store.toast.tone === 'error' ? 'alert' : 'status'"
        >
          <IssueDetail
            v-if="store.toast.tone === 'error'"
            :message="store.toast.message"
          /><span v-else>{{ store.toast.message }}</span
          ><button
            class="icon-btn"
            aria-label="关闭通知"
            @click="store.toast = null"
          >
            <X :size="16" />
          </button></div
      ></Transition>
    </Teleport>
    <MediaLightbox
      :open="previewOpen"
      :items="previewItems"
      :start="previewStart"
      @close="
        previewOpen = false;
        previewItems = [];
      "
    />
    <CommandPalette v-if="commandOpen" @close="commandOpen = false" />
    <ConfirmDialog />
    <Teleport to="body"
      ><NewDownloadPanel
        v-if="newDownloadOpen"
        :intent="captureIntent"
        :platform="capturePlatform"
        :return-focus="captureOrigin"
        @close="closeNewDownload"
    /></Teleport>
    <Teleport to="body"
      ><div v-if="authOpen" class="auth-overlay" @click.self="closeAuth">
        <form
          ref="authDialog"
          class="auth-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="auth-title"
          tabindex="-1"
          @submit.prevent="login"
        >
          <button
            type="button"
            class="icon-btn close"
            aria-label="关闭"
            @click="closeAuth"
          >
            <X />
          </button>
          <div class="brand-mark"><UserRound /></div>
          <h2 id="auth-title">输入管理 Token</h2>
          <p>Token 只保存在当前浏览器，并通过 Authorization 请求头发送。</p>
          <label for="admin-token">管理 Token</label
          ><input
            id="admin-token"
            ref="authInput"
            v-model="token"
            type="password"
            autocomplete="off"
            placeholder="Bearer Token"
          /><button class="btn primary">登录并继续</button>
        </form>
      </div></Teleport
    >
  </div>
</template>
